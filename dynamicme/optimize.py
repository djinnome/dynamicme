#============================================================
# File optimize.py
#
# class  
#
# MI(N)LP methods 
#
# Laurence Yang, SBRG, UCSD
#
# 02 Feb 2018:  first version
#============================================================

from six import iteritems
from cobra.core.Solution import Solution
from cobra import Reaction, Metabolite, Model
from cobra import DictList
from generate import copy_model
from scipy.sparse import dok_matrix

import numpy as np
import cobra
import copy
import sys

solver_apis = ['gurobipy', 'cplex']

for api in solver_apis:
    try:
        lib = __import__(api)
    except:
        print sys.exc_info()
    else:
        globals()[api] = lib

class Variable(Reaction):
    def __init__(self, *args, **kwargs):
        self.quadratic_coeff = None
        super(Variable, self).__init__(*args, **kwargs)

class Constraint(Metabolite):
    pass

class EnzymeConc(Variable):
    def __init__(self, *args, **kwargs):
        self.kDa = None
        super(EnzymeConc, self).__init__(*args, **kwargs)

    @property
    def mass(self):
        return self.kDa

class Optimizer(object):
    """
    Methods for bilevel opt and others
    """
    def __init__(self, mdl, objective_sense='maximize'):
        self.mdl = mdl
        self.objective_sense = objective_sense

    def add_duality_gap_constraint(self, primal_sense='max', clear_obj=False, index=None,
            INF=1e3, inplace=True):
        """
        Add duality gap as a constraint to current model
        Inputs:
        primal_sense : 'max' or 'min
        clear_obj : clear the new model's objective function
        """
        #   max     c'x
        #   s.t.    c'x - wa*b - wu*u + wl*l = 0
        #           Ax [<=>] b
        #           wa*A + wu - wl = c'
        #           l <= x <= u
        #           wl, wu >= 0
        #           wa >= 0 if ai*x <= bi
        #           wa <= 0 if ai*x >= bi
        #           wa \in R if ai*x == bi

        primal  = self.mdl
        dual = self.make_dual(primal, LB=-INF, UB=INF, primal_sense=primal_sense)
        if inplace:
            mdl = self.mdl
        else:
            mdl = Model('duality_gap')

        #----------------------------------------------------
        # Add Primal variables and constraints
        if index is None:
            cons_gap = Constraint('duality_gap')
        else:
            cons_gap = Constraint('duality_gap_%s'%index)
        cons_gap._constraint_sense = 'E'
        cons_gap._bound = 0.
        mdl.add_metabolites(cons_gap)

        if not inplace:
            primal_reactions = primal.reactions.copy()
            for rxn in primal.reactions:
                var = Variable(rxn.id)
                mdl.add_reaction(var)
                clone_attributes(rxn, var)
                for met,s in iteritems(rxn.metabolites):
                    cons = Constraint(met.id)
                    clone_attributes(met, cons)
                    var.add_metabolites({cons:s})
            # Add duality gap, dual variables, and dual constraints
            if rxn.objective_coefficient != 0:
                var.add_metabolites({cons_gap:rxn.objective_coefficient})

        else:
            for rxn in mdl.reactions:
                # Add duality gap, dual variables, and dual constraints
                if rxn.objective_coefficient != 0:
                    rxn.add_metabolites({cons_gap:rxn.objective_coefficient})

        for rxn in dual.reactions:
            dvar = Variable(rxn.id)
            mdl.add_reaction(dvar)
            clone_attributes(rxn, dvar)
            dvar.add_metabolites({cons_gap:-rxn.objective_coefficient})

        #----------------------------------------------------
        # Add dual constraints
        for met in dual.metabolites:
            cons_dual = Constraint(met.id)
            cons_dual._constraint_sense = met._constraint_sense
            cons_dual._bound = met._bound
            mdl.add_metabolites(cons_dual)
            for rxn in met.reactions:
                dvar = mdl.reactions.get_by_id(rxn.id)
                dvar.add_metabolites({cons_dual:rxn.metabolites[met]})

        return mdl

    def make_dual(self, mdl, LB=-1000, UB=1000, primal_sense='max'):
        """
        Return dual of current model
        """
        objective_sense=self.objective_sense
        dual = Model('dual')
        # Primal:
        #   max     c'x
        #   s.t.    Ax [<=>] b
        #           l <= x <= u
        #
        # Dual:
        #   min     wa*b + wu*u - wl*l
        #   s.t.    wa*A + wu - wl = c'
        #           wl, wu >= 0

        wa_dict = {}

        for met in mdl.metabolites:
            wa = Variable('wa_'+met.id)
            wa_dict[met.id] = wa
            wa.objective_coefficient = met._bound
            if primal_sense == 'max':
                if met._constraint_sense == 'E':
                    wa.lower_bound = LB
                    wa.upper_bound = UB
                elif met._constraint_sense == 'L':
                    wa.lower_bound = 0.
                    wa.upper_bound = UB
                elif met._constraint_sense == 'G':
                    wa.lower_bound = LB
                    wa.upper_bound = 0.
            elif primal_sense == 'min':
                if met._constraint_sense == 'E':
                    wa.lower_bound = LB
                    wa.upper_bound = UB
                elif met._constraint_sense == 'G':
                    wa.lower_bound = 0.
                    wa.upper_bound = UB
                elif met._constraint_sense == 'L':
                    wa.lower_bound = LB
                    wa.upper_bound = 0.
        dual.add_reactions(wa_dict.values())

        wl_dict = {}
        wu_dict = {}
        for rxn in mdl.reactions:
            wl = Variable('wl_'+rxn.id)
            wu = Variable('wu_'+rxn.id)
            wl.lower_bound = 0.
            wu.lower_bound = 0.
            wl_dict[rxn.id] = wl
            wu_dict[rxn.id] = wu
            if primal_sense=='max':
                wl.objective_coefficient = -rxn.lower_bound
                wu.objective_coefficient = rxn.upper_bound
            elif primal_sense=='min':
                wl.objective_coefficient = rxn.lower_bound
                wu.objective_coefficient = -rxn.upper_bound

            cons = Constraint(rxn.id)
            if primal_sense=='max':
                if rxn.lower_bound<0 and rxn.upper_bound>0:
                    cons._constraint_sense = 'E'
                elif rxn.lower_bound==0 and rxn.upper_bound>0:
                    cons._constraint_sense = 'G'
                elif rxn.lower_bound<0 and rxn.upper_bound<=0:
                    cons._constraint_sense = 'L'
            elif primal_sense=='min':
                if rxn.lower_bound<0 and rxn.upper_bound>0:
                    cons._constraint_sense = 'E'
                elif rxn.lower_bound==0 and rxn.upper_bound>0:
                    cons._constraint_sense = 'L'
                elif rxn.lower_bound<0 and rxn.upper_bound<=0:
                    cons._constraint_sense = 'G'

            cons._bound = rxn.objective_coefficient

            if primal_sense=='max':
                wl.add_metabolites({cons:-1.})
                wu.add_metabolites({cons:1.})
            elif primal_sense=='min':
                wl.add_metabolites({cons:1.})
                wu.add_metabolites({cons:-1.})

            for met,s in iteritems(rxn.metabolites):
                wa = wa_dict[met.id]
                wa.add_metabolites({cons:s})

        # Remember to minimize the problem
        dual.add_reactions(wl_dict.values())
        dual.add_reactions(wu_dict.values())

        return dual

    def to_radix(self, mdl, var_cons_dict, radix, powers,
            num_digits_per_power=None, digits=None,
            radix_multiplier=1., M=1e3, prevent_zero=False):
        """
        Given sum_j ai*xj = di,
        discretize ai into radix form, adding necessary constraints.

        Inputs
        var_cons_dict : dict of group_id: (var, cons, coeff0) tuples that share the same coefficient
        prevent_zero : prevent radix-based value from becoming zero

        """
        pwr_min = min(powers)
        pwr_max = max(powers)
        if digits is None:
            # Don't need to start with k=0 since you get it with y_kl=0
            digits  = np.linspace(1, radix-1, num_digits_per_power)

        # Add new rows and columns at once at the end to save time
        # new_mets = set()
        # new_rxns = set()

        # All var, cons pairs in var_cons_pairs list share the same binary variables
        for group_id, var_cons_coeff in iteritems(var_cons_dict):
            for l,pwr in enumerate(powers):
                for k,digit in enumerate(digits):
                    yid = 'binary_%s%s%s'%(group_id,k,l)
                    if mdl.reactions.has_id(yid):
                        y_klj = mdl.reactions.get_by_id(yid)
                    elif hasattr(mdl, 'observer') and mdl.observer.reactions.has_id(yid):
                        ### TODO: make this observer checking more inherent?
                        y_klj = mdl.observer.reactions.get_by_id(yid)
                    else:
                        y_klj = Variable(yid)
                        y_klj.variable_kind = 'integer'
                        y_klj.lower_bound = 0.
                        y_klj.upper_bound = 1.
                        mdl.add_reaction(y_klj)
                        #new_rxns.add(y_klj)

                    for rxn, cons, a0 in var_cons_coeff:
                        # Remove the old column in this constraint
                        if rxn.metabolites.has_key(cons):   # slow 1
                            #rxn.subtract_metabolites({cons:rxn.metabolites[cons]})
                            rxn._metabolites.pop(cons)
                            cons._reaction.remove(rxn)

                        rid = rxn.id
                        cid = cons.id
                        # z_klj = xj if y_klj=1
                        z_klj = Variable('z_%s_%s%s%s'%(rid,cid,k,l))
                        z_klj.lower_bound = rxn.lower_bound
                        z_klj.upper_bound = rxn.upper_bound
                        mdl.add_reaction(z_klj) # slow 3
                        #new_rxns.add(z_klj)

                        coeff = radix**pwr * digit * a0
                        # z_klj.add_metabolites({cons:coeff})
                        z_klj._metabolites[cons] = coeff
                        cons._reaction.add(z_klj)

                        cons_zdiff_L = Constraint('zdiff_L_%s_%s%s%s'%(rid,cid,k,l))
                        cons_zdiff_L._constraint_sense = 'L'
                        cons_zdiff_L._bound = M
                        cons_zdiff_U = Constraint('zdiff_U_%s_%s%s%s'%(rid,cid,k,l))
                        cons_zdiff_U._constraint_sense = 'L'
                        cons_zdiff_U._bound = M
                        mdl.add_metabolites([cons_zdiff_L, cons_zdiff_U])   # slow 2
                        #new_mets.add(cons_zdiff_L)
                        #new_mets.add(cons_zdiff_U)

                        #z_klj.add_metabolites({cons_zdiff_L:-1.})
                        z_klj._metabolites[cons_zdiff_L] = -1.
                        cons_zdiff_L._reaction.add(z_klj)
                        #y_klj.add_metabolites({cons_zdiff_L:M})
                        y_klj._metabolites[cons_zdiff_L] = M
                        cons_zdiff_L._reaction.add(y_klj)
                        #rxn.add_metabolites({cons_zdiff_L:1.}) # too slow
                        rxn._metabolites[cons_zdiff_L] = 1.
                        cons_zdiff_L._reaction.add(rxn)

                        #z_klj.add_metabolites({cons_zdiff_U:1.})
                        z_klj._metabolites[cons_zdiff_U] = 1.
                        cons_zdiff_U._reaction.add(z_klj)
                        #y_klj.add_metabolites({cons_zdiff_U:M})
                        y_klj._metabolites[cons_zdiff_U] = M
                        cons_zdiff_U._reaction.add(y_klj)
                        # rxn.add_metabolites({cons_zdiff_U:-1.}) # too slow
                        rxn._metabolites[cons_zdiff_U] = -1.
                        cons_zdiff_U._reaction.add(rxn)

                        cons_z_L = Constraint('z_L_%s_%s%s%s'%(rid,cid,k,l))
                        cons_z_L._constraint_sense = 'L'
                        cons_z_L._bound = 0.
                        cons_z_U = Constraint('z_U_%s_%s%s%s'%(rid,cid,k,l))
                        cons_z_U._constraint_sense = 'L'
                        cons_z_U._bound = 0.
                        mdl.add_metabolites([cons_z_L, cons_z_U])   # slow 2
                        # new_mets.add(cons_z_L)
                        # new_mets.add(cons_z_U)

                        #z_klj.add_metabolites({cons_z_L:-1.})
                        z_klj._metabolites[cons_z_L]=-1.
                        cons_z_L._reaction.add(z_klj)
                        #y_klj.add_metabolites({cons_z_L:rxn.lower_bound})
                        y_klj._metabolites[cons_z_L]=rxn.lower_bound
                        cons_z_L._reaction.add(y_klj)
                        #z_klj.add_metabolites({cons_z_U:1.})
                        z_klj._metabolites[cons_z_U]=1.
                        cons_z_U._reaction.add(z_klj)
                        #y_klj.add_metabolites({cons_z_U:-rxn.upper_bound})
                        y_klj._metabolites[cons_z_U]=-rxn.upper_bound
                        cons_z_U._reaction.add(y_klj)

        # Only one digit active at a time for each power
        # Note this only involves the binaries
        cons_digs = []
        for group_id, var_cons_coeff in iteritems(var_cons_dict):
            for l,pwr in enumerate(powers):
                cons_one_digit = Constraint('cons_digit_%s%s'%(group_id,l))
                cons_one_digit._constraint_sense = 'L'
                cons_one_digit._bound = 1.
                cons_digs.append(cons_one_digit)
                #mdl.add_metabolites(cons_one_digit)

                for k,digit in enumerate(digits):
                    yid = 'binary_%s%s%s'%(group_id,k,l)
                    if mdl.reactions.has_id(yid):
                        y_klj = mdl.reactions.get_by_id(yid)
                    elif hasattr(mdl, 'observer') and mdl.observer.reactions.has_id(yid):
                        ### TODO: make this observer checking more inherent?
                        y_klj = mdl.observer.reactions.get_by_id(yid)
                    else:
                        raise KeyError('%s not in model or observer'%yid)

                    y_klj.add_metabolites({cons_one_digit:1.}, combine=False)

        mdl.add_metabolites(cons_digs)

        if prevent_zero:
            # sum_kl y_groupi >= 1
            for group_id, var_cons_coeff in iteritems(var_cons_dict):
                cons = Constraint('force_nonzero_%s'%group_id)
                cons._bound = 0.9 # 1
                cons._constraint_sense = 'G'
                mdl.add_metabolites(cons)

                for l,pwr in enumerate(powers):
                    for k,digit in enumerate(digits):
                        yid = 'binary_%s%s%s'%(group_id,k,l)
                        if mdl.reactions.has_id(yid):
                            y_klj = mdl.reactions.get_by_id(yid)
                        elif hasattr(mdl, 'observer') and mdl.observer.reactions.has_id(yid):
                            ### TODO: make this observer checking more inherent?
                            y_klj = mdl.observer.reactions.get_by_id(yid)
                        else:
                            raise KeyError('%s not in model or observer'%yid)

                        y_klj._metabolites[cons] = 1.
                        cons._reaction.add(y_klj)

        # mdl.add_reactions(new_rxns)
        # mdl.add_metabolites(new_mets)

        return digits


    def make_disjunctive_primal_dual(self, mdl, a12_dict, M=1e4):
        """
        Make some constraints disjunctive for both primal and dual
        Inputs:
        a12_dict : {(met,rxn):(a1, a2)}
        """
        # Primal:
        #   max     c'x
        #   s.t.    sum_j (a1ij*(1-yij)*xj + a2ij*yij*xj) [<=>] bi
        #           l <= x <= u
        #           yij \in {0,1}
        #
        # Dual:
        #   min     wa*b + wu*u - wl*l
        #   s.t.    sum_i (wai*a1ij*(1-yij) + wai*a2ij*yij) + wuj - wlj = cj
        #           wl, wu >= 0
        #           yij \in {0,1}
        #
        #   max     c'x
        #   s.t.    c'x - wa*b - wu*u + wl*l = 0
        #           sum_j (a1ij*(1-yij)*xj + a2ij*yij*xj) [<=>] bi
        #           sum_i (wai*a1ij*(1-yij) + wai*a2ij*yij) + wuj - wlj = cj
        #           l <= x <= u
        #           wl, wu >= 0
        #           yij \in {0,1}
        #
        #   max     c'x
        #   s.t.    c'x - wa*b - wu*u + wl*l = 0
        #           sum_j a1ij*xj - a1ij*zij + a2ij*zij [<=>] bi
        #           l*yij <= zij <= u*yij
        #           -M*(1-yij) <= zij - xj <= M*(1-yij)
        #           sum_i a1ij*wai - a1ij*zaij + a2ij*zaij + wu - wl = cj
        #           wal*yij <= zaij <= wau*yij
        #           -M*(1-yij) <= zaij - wai <= M*(1-yij)
        #           l <= x <= u
        #           wl, wu >= 0
        #           yij \in {0,1}

        for met_rxn, a12 in iteritems(a12_dict):
            met = met_rxn[0]
            rxn = met_rxn[1]
            a1  = a12[0]
            a2  = a12[1]

            yij = Variable('binary_%s_%s'%(met.id,rxn.id))
            yij.variable_kind = 'integer'
            yij.lower_bound = 0.
            yij.upper_bound = 1.
            try:
                mdl.add_reaction(yij)
            except ValueError:
                yij = mdl.reactions.get_by_id(yij.id)
            zij = Variable('z_%s_%s'%(met.id,rxn.id))
            zij.lower_bound = rxn.lower_bound
            zij.upper_bound = rxn.upper_bound
            try:
                mdl.add_reaction(zij)
            except:
                zij = mdl.reactions.get_by_id(zij.id)
            # Used to be:
            #   sum_j aij*xj [<=>] bi
            # Change to:
            #   sum_j a1ij*xj - a1ij*zij + a2ij*zij [<=>] bi
            rxn._metabolites[met] = a1
            zij.add_metabolites({met:-a1+a2}, combine=False)
            # Add: l*yij <= zij <= u*yij
            cons_zl = Constraint('z_l_%s_%s'%(met.id,rxn.id))
            cons_zl._constraint_sense = 'L'
            cons_zl._bound = 0.
            yij.add_metabolites({cons_zl:rxn.lower_bound}, combine=False)
            zij.add_metabolites({cons_zl:-1.}, combine=False)
            cons_zu = Constraint('z_u_%s_%s'%(met.id,rxn.id))
            cons_zu._constraint_sense = 'L'
            cons_zu._bound = 0.
            yij.add_metabolites({cons_zu:-rxn.upper_bound}, combine=False)
            zij.add_metabolites({cons_zu:1.}, combine=False)
            # Add: -M*(1-yij) <= zij - xj <= M*(1-yij)
            cons_zl = Constraint('z_M_l_%s_%s'%(met.id,rxn.id))
            cons_zl._constraint_sense = 'L'
            cons_zl._bound = M
            rxn.add_metabolites({cons_zl:1.}, combine=False)
            zij.add_metabolites({cons_zl:-1.}, combine=False)
            yij.add_metabolites({cons_zl:M}, combine=False)
            cons_zu = Constraint('z_M_u_%s_%s'%(met.id,rxn.id))
            cons_zu._constraint_sense = 'L'
            cons_zu._bound = M
            rxn.add_metabolites({cons_zu:-1.}, combine=False)
            zij.add_metabolites({cons_zu:1.}, combine=False)
            yij.add_metabolites({cons_zu:M}, combine=False)
            # Used to be:
            # wa*A + wu - wl = c'
            # Change to:
            # sum_i a1ij*wai - a1ij*zaij + a2ij*zaij + wu - wl = cj
            cons = mdl.metabolites.get_by_id(rxn.id)
            wa = mdl.reactions.get_by_id('wa_'+met.id)
            wl = mdl.reactions.get_by_id('wl_'+rxn.id)
            wu = mdl.reactions.get_by_id('wu_'+rxn.id)
            zaij = Variable('za_%s_%s'%(met.id,rxn.id))
            zaij.lower_bound = wa.lower_bound
            zaij.upper_bound = wa.upper_bound
            try:
                mdl.add_reaction(zaij)
            except ValueError:
                zaij = mdl.reactions.get_by_id(zaij.id)
            wa._metabolites[cons] = a1
            zaij.add_metabolites({cons:-a1+a2}, combine=False)
            # wal*yij <= zaij <= wau*yij
            cons_zl = Constraint('za_l_%s_%s'%(met.id,rxn.id))
            cons_zl._constraint_sense = 'L'
            cons_zl._bound = 0.
            yij.add_metabolites({cons_zl:wa.lower_bound}, combine=False)
            zaij.add_metabolites({cons_zl:-1.}, combine=False)
            cons_zu = Constraint('za_u_%s_%s'%(met.id,rxn.id))
            cons_zu._constraint_sense = 'L'
            cons_zu._bound = 0.
            yij.add_metabolites({cons_zu:-wa.upper_bound}, combine=False)
            zaij.add_metabolites({cons_zu:1.}, combine=False)
            # -M*(1-yij) <= zaij - wai <= M*(1-yij)
            cons_zl = Constraint('za_M_l_%s_%s'%(met.id,rxn.id))
            cons_zl._constraint_sense = 'L'
            cons_zl._bound =  M
            wa.add_metabolites({cons_zl:1.}, combine=False)
            yij.add_metabolites({cons_zl:M}, combine=False)
            zaij.add_metabolites({cons_zl:-1.}, combine=False)
            cons_zu = Constraint('za_M_u_%s_%s'%(met.id,rxn.id))
            cons_zu._constraint_sense = 'L'
            cons_zu._bound = M
            wa.add_metabolites({cons_zu:-1.}, combine=False)
            yij.add_metabolites({cons_zu:M}, combine=False)
            zaij.add_metabolites({cons_zu:1.}, combine=False)

        return mdl

    def stack_disjunctive(self, mdl, a12_dict, cond_ids, M=1e4):
        """
        Stack models in mdls into disjunctive program.
        keff params are coupled by default
        Binary variables shared across conditions
        """
        #   max     c'yi
        #   xk,wk,zk,yi
        #   s.t.    c'xk - wak*b - wuk*u + wlk*l = 0
        #           sum_j a1ij*xkj - a1ij*zij + a2ij*zij [<=>] bi
        #           l*yij <= zij <= u*yij
        #           -M*(1-yij) <= zij - xj <= M*(1-yij)
        #           sum_i a1ij*wai - a1ij*zaij + a2ij*zaij + wu - wl = cj
        #           wal*yij <= zaij <= wau*yij
        #           -M*(1-yij) <= zaij - wai <= M*(1-yij)
        #           l <= x <= u
        #           wl, wu >= 0
        #           yij \in {0,1}

        for mid_rid, a12 in iteritems(a12_dict):
            mid = mid_rid[0]
            rid = mid_rid[1]
            a1  = a12[0]
            a2  = a12[1]

            # Binary variables shared
            yij = Variable('binary_%s_%s'%(mid,rid))
            yij.variable_kind = 'integer'
            yij.lower_bound = 0.
            yij.upper_bound = 1.
            try:
                mdl.add_reaction(yij)
            except ValueError:
                yij = mdl.reactions.get_by_id(yij.id)

            # Z variables are condition-specific
            for cid in cond_ids:
                met = mdl.metabolites.get_by_id(mid+'_%s'%cid)
                rxn = mdl.reactions.get_by_id(rid+'_%s'%cid)
                zij = Variable('z_%s_%s'%(met.id,rxn.id))
                zij.lower_bound = rxn.lower_bound
                zij.upper_bound = rxn.upper_bound
                try:
                    mdl.add_reaction(zij)
                except:
                    zij = mdl.reactions.get_by_id(zij.id)
                # Used to be:
                #   sum_j aij*xj [<=>] bi
                # Change to:
                #   sum_j a1ij*xj - a1ij*zij + a2ij*zij [<=>] bi
                rxn._metabolites[met] = a1
                zij.add_metabolites({met:-a1+a2}, combine=False)
                # Add: l*yij <= zij <= u*yij
                cons_zl = Constraint('z_l_%s_%s'%(met.id,rxn.id))
                cons_zl._constraint_sense = 'L'
                cons_zl._bound = 0.
                yij.add_metabolites({cons_zl:rxn.lower_bound}, combine=True)
                zij.add_metabolites({cons_zl:-1.}, combine=False)
                cons_zu = Constraint('z_u_%s_%s'%(met.id,rxn.id))
                cons_zu._constraint_sense = 'L'
                cons_zu._bound = 0.
                yij.add_metabolites({cons_zu:-rxn.upper_bound}, combine=True)
                zij.add_metabolites({cons_zu:1.}, combine=False)
                # Add: -M*(1-yij) <= zij - xj <= M*(1-yij)
                cons_zl = Constraint('z_M_l_%s_%s'%(met.id,rxn.id))
                cons_zl._constraint_sense = 'L'
                cons_zl._bound = M
                rxn.add_metabolites({cons_zl:1.}, combine=False)
                zij.add_metabolites({cons_zl:-1.}, combine=False)
                yij.add_metabolites({cons_zl:M}, combine=True)
                cons_zu = Constraint('z_M_u_%s_%s'%(met.id,rxn.id))
                cons_zu._constraint_sense = 'L'
                cons_zu._bound = M
                rxn.add_metabolites({cons_zu:-1.}, combine=False)
                zij.add_metabolites({cons_zu:1.}, combine=False)
                yij.add_metabolites({cons_zu:M}, combine=True)
                # Used to be:
                # wa*A + wu - wl = c'
                # Change to:
                # sum_i a1ij*wai - a1ij*zaij + a2ij*zaij + wu - wl = cj
                cons = mdl.metabolites.get_by_id(rxn.id)
                wa = mdl.reactions.get_by_id('wa_'+met.id)
                wl = mdl.reactions.get_by_id('wl_'+rxn.id)
                wu = mdl.reactions.get_by_id('wu_'+rxn.id)
                zaij = Variable('za_%s_%s'%(met.id,rxn.id))
                zaij.lower_bound = wa.lower_bound
                zaij.upper_bound = wa.upper_bound
                try:
                    mdl.add_reaction(zaij)
                except ValueError:
                    zaij = mdl.reactions.get_by_id(zaij.id)
                wa._metabolites[cons] = a1
                zaij.add_metabolites({cons:-a1+a2}, combine=False)
                # wal*yij <= zaij <= wau*yij
                cons_zl = Constraint('za_l_%s_%s'%(met.id,rxn.id))
                cons_zl._constraint_sense = 'L'
                cons_zl._bound = 0.
                yij.add_metabolites({cons_zl:wa.lower_bound}, combine=True)
                zaij.add_metabolites({cons_zl:-1.}, combine=False)
                cons_zu = Constraint('za_u_%s_%s'%(met.id,rxn.id))
                cons_zu._constraint_sense = 'L'
                cons_zu._bound = 0.
                yij.add_metabolites({cons_zu:-wa.upper_bound}, combine=True)
                zaij.add_metabolites({cons_zu:1.}, combine=False)
                # -M*(1-yij) <= zaij - wai <= M*(1-yij)
                cons_zl = Constraint('za_M_l_%s_%s'%(met.id,rxn.id))
                cons_zl._constraint_sense = 'L'
                cons_zl._bound =  M
                wa.add_metabolites({cons_zl:1.}, combine=False)
                yij.add_metabolites({cons_zl:M}, combine=True)
                zaij.add_metabolites({cons_zl:-1.}, combine=False)
                cons_zu = Constraint('za_M_u_%s_%s'%(met.id,rxn.id))
                cons_zu._constraint_sense = 'L'
                cons_zu._bound = M
                wa.add_metabolites({cons_zu:-1.}, combine=False)
                yij.add_metabolites({cons_zu:M}, combine=True)
                zaij.add_metabolites({cons_zu:1.}, combine=False)

        return mdl


class ObservedDictList(DictList):
    def __init__(self, observer, *args, **kwargs):
        self.observer = observer
        super(ObservedDictList, self).__init__(*args, **kwargs)

    def has_id(self, _id):
        return _id in self.observer or super(ObservedDictList, self).has_id(_id)

    def get_by_id(self, _id):
        try:
            return super(ObservedDictList, self).get_by_id(_id)
        except KeyError:
            return self.observer.get_by_id(_id)


class ObservedModel(cobra.core.Model):
    def __init__(self, observer, *args, **kwargs):
        self.observer = observer
        super(ObservedModel, self).__init__(*args, **kwargs)
        # Make reactions ObservedDictList
        # self.reactions = ObservedDictList(observer.reactions, self.reactions)
        # # Make metabolites ObservedDictList
        # self.metabolites = ObservedDictList(observer.metabolites, self.metabolites)

    def add_reactions(self, *args, **kwargs):
        self.observer.add_reactions(*args, **kwargs)
        super(ObservedModel, self).add_reactions(*args, **kwargs)

    def add_metabolites(self, *args, **kwargs):
        self.observer.add_metabolites(*args, **kwargs)
        super(ObservedModel, self).add_metabolites(*args, **kwargs)

    def remove_reactions(self, *args, **kwargs):
        self.observer.remove_reactions(*args, **kwargs)
        super(ObservedModel, self).remove_reactions(*args, **kwargs)


class StackOptimizer(object):
    def __init__(self):
        self.model_dict = {}
        self.model = None
        self.Q = None

    def update(self):
        """
        Update stacked model with updates to individual models
        """
        for k,mdl in iteritems(self.model_dict):
            pass

        self.update_quadratic_objective()


    def stack_models(self, mdl_ref, df_conds, col_ind='cond'):
        """
        Stack models according to data frame
        Also keep some reference to each condition-specific model

        Inputs:
        mdl_ref : reference model
        df_conds : dataframe of conditions with columns:
            cond rxn lb ub obj
        """
        stacked_model = Model('stacked')
        conds = df_conds[col_ind].unique()

        for cind,cond in enumerate(conds):
            dfi = df_conds[ df_conds[col_ind]==cond]
            # Create clone of reference model
            suffix = '_%s'%cond
            mdli = clone_model(mdl_ref, stacked_model, suffix=suffix)
            # Modify its lb, ub
            for i,row in dfi.iterrows():
                rxn = mdli.reactions.get_by_id(row['rxn']+suffix)
                rxn.lower_bound = row['lb']
                rxn.upper_bound = row['ub']
                rxn.objective_coefficient = row['obj']

            self.model_dict[cond] = mdli

        self.model = stacked_model

    def update_quadratic_objective(self):
        # Create/update quadratic component
        model = self.model
        Q_inds = []
        for rxn in model.reactions:
            if hasattr(rxn, 'quadratic_coeff'):
                if rxn.quadratic_coeff is not None:
                    ind = model.reactions.index(rxn)
                    Q_inds.append((ind,rxn.quadratic_coeff))

        if len(Q_inds)>0:
            N = len(model.reactions)
            Q = dok_matrix((N,N))
            for ind_coeff in Q_inds:
                ind = ind_coeff[0]
                coeff = ind_coeff[1]
                Q[ind,ind] = coeff
        else:
            Q = None

        self.Q = Q


class SplitOptimizer(object):
    """
    Split data across submodels.
    """
    def __init__(self, *args, **kwargs):
        self.model_dict = {}
        super(SplitOptimizer, self).__init__(*args, **kwargs)

    def yield_models(self, mdl_ref, df_X, col_ind='cond'):
        """
        Generate models but store mapping from condition to model.
        """
        conds = df_X[col_ind].unique()
        for cind,cond in enumerate(conds):
            dfi = df_X[df_X[col_ind]==cond]
            suffix = '_%s'%cond
            mdli = copy_model(mdl_ref, suffix=suffix)
            for i,row in dfi.iterrows():
                rxn = mdli.reactions.get_by_id(row['rxn']+suffix)
                rxn.lower_bound = row['lb']
                rxn.upper_bound = row['ub']
                rxn.objective_coefficient = row['obj']

            yield (cond, mdli)


    def store_models(self, mdl_ref, df_conds, col_ind='cond'):
        """
        Store models instead of generating on the fly
        Also keep some reference to each condition-specific model.

        Inputs:
        mdl_ref : reference model
        df_conds : dataframe of conditions with columns:
            cond rxn lb ub obj
        """
        conds = df_X[col_ind].unique()
        for cind,cond in enumerate(conds):
            dfi = df_X[df_X[col_ind]==cond]
            suffix = '_%s'%cond
            mdli = copy_model(mdl_ref, suffix=suffix)
            for i,row in dfi.iterrows():
                rxn = mdli.reactions.get_by_id(row['rxn']+suffix)
                rxn.lower_bound = row['lb']
                rxn.upper_bound = row['ub']
                rxn.objective_coefficient = row['obj']

            self.model_dict[cond] = mdli


def clone_attributes(orig, clone):
    unique_rxn_attrs = ['_model','id','_metabolites','_genes']
    unique_met_attrs = ['_model','id','_reaction']

    if isinstance(orig,Reaction):
        for k,v in iteritems(orig.__dict__):
            if k not in unique_rxn_attrs:
                clone.__dict__[k] = v
    elif isinstance(orig,Metabolite):
        for k,v in iteritems(orig.__dict__):
            if k not in unique_met_attrs:
                clone.__dict__[k] = v
    else:
        raise ValueError("Type of original must be Reaction or Metabolite")


def clone_model(model, observer, suffix=''):
    clone  = ObservedModel(observer, model.id)
    rxns = [Reaction(rxn.id+suffix, rxn.name, rxn.subsystem,
        rxn.lower_bound, rxn.upper_bound, rxn.objective_coefficient) for
        rxn in model.reactions]
    mets = [Metabolite(met.id+suffix, met.formula, met.name, met.charge, met.compartment) for
            met in model.metabolites]
    clone.add_reactions(rxns)
    clone.add_metabolites(mets)

    unique_rxn_attrs = ['_model','id','_metabolites','_genes']
    unique_met_attrs = ['_model','id','_reaction']

    # Add properties and stoich
    for rxn0 in model.reactions:
        rxn = clone.reactions.get_by_id(rxn0.id+suffix)
        for k,v in iteritems(rxn0.__dict__):
            if k not in unique_rxn_attrs:
                rxn.__dict__[k] = v

        stoich = {m.id+suffix:s for m,s in iteritems(rxn0.metabolites)}
        rxn.add_metabolites(stoich)

    for met0 in model.metabolites:
        met = clone.metabolites.get_by_id(met0.id+suffix)
        for k,v in iteritems(met0.__dict__):
            if k not in unique_met_attrs:
                met.__dict__[k] = v

    return clone
