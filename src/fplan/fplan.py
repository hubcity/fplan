#!/usr/bin/env python3

import argparse
import re
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
import scipy.optimize


# Required Minimal Distributions from IRA starting with age 73
# last updated for 2024
RMD = [27.4, 26.5, 25.5, 24.6, 23.7, 22.9, 22.0, 21.1, 20.2, 19.4,  # age 72-81
       18.5, 17.7, 16.8, 16.0, 15.3, 14.5, 13.7, 12.9, 12.2, 11.5,  # age 82-91
       10.8, 10.1,  9.5,  8.9,  8.4,  7.8,  7.3,  6.8,  6.4,  6.0,  # age 92-101
        5.6,  5.2,  4.9,  4.6,  4.3,  4.1,  3.9,  3.7,  3.5,  3.4,  # age 102+
        3.3,  3.1,  3.0,  2.9,  2.8,  2.7,  2.5,  2.3,  2.0,  2.0]

def agelist(str):
    for x in str.split(','):
        m = re.match(r'^(\d+)(-(\d+)?)?$', x)
        if m:
            s = int(m.group(1))
            e = s
            if m.group(2):
                e = m.group(3)
                if e:
                    e = int(e)
                else:
                    e = 120
            for a in range(s,e+1):
                yield a
        else:
            raise Exception("Bad age " + str)

class Data:
    def load_file(self, file):
        global vper
        with open(file) as conffile:
            d = tomllib.loads(conffile.read())
        self.i_rate = 1 + d.get('inflation', 0) / 100       # inflation rate: 2.5 -> 1.025
        self.r_rate = 1 + d.get('returns', 6) / 100         # invest rate: 6 -> 1.06

        self.startage = d['startage']
        self.endage = d.get('endage', max(96, self.startage+5))

        # 2023 tax table (could predict it moves with inflation?)
        # married joint at the moment, can override in config file
        default_taxrates = [[0,      10], 
                            [22000,  12],
                            [89450 , 22],
                            [190750, 24],
                            [364200, 32],
                            [462500, 35],
                            [693750, 37]]
        default_stded = 27700
        default_state_taxrates = [[0, 0]]
        default_cg_taxrates = [[0,        0],
                               [89250,   15],
                               [553850,  20]]

        tmp_taxrates = default_taxrates
        tmp_state_taxrates = default_state_taxrates
        tmp_cg_taxrates = default_cg_taxrates

        if 'taxes' in d:
            tmp_taxrates = d['taxes'].get('taxrates', default_taxrates)
            tmp_state_taxrates = d['taxes'].get('state_rate', default_state_taxrates)
            tmp_cg_taxrates = d['taxes'].get('cg_taxrates', default_cg_taxrates)
            if (type(tmp_state_taxrates) is not list):
                tmp_state_taxrates = [[0, tmp_state_taxrates]]
            self.stded = d['taxes'].get('stded', default_stded)
            self.state_stded = d['taxes'].get('state_stded', self.stded)
            self.nii = d['taxes'].get('nii', 250000)
        else:
            self.stded = default_stded
            self.state_stded = default_stded
            self.nii = 250000
        self.taxrates = [[x,y/100.0] for (x,y) in tmp_taxrates]
        cutoffs = [x[0] for x in self.taxrates][1:] + [float('inf')]
        self.taxtable = list(map(lambda x, y: [x[1], x[0], y], self.taxrates, cutoffs))
        self.state_taxrates = [[x,y/100.0] for (x,y) in tmp_state_taxrates]
        cutoffs = [x[0] for x in self.state_taxrates][1:] + [float('inf')]
        self.state_taxtable = list(map(lambda x, y: [x[1], x[0], y], self.state_taxrates, cutoffs))
        self.cg_taxrates = [[x,y/100.0] for (x,y) in tmp_cg_taxrates]
        cutoffs = [x[0] for x in self.cg_taxrates][1:] + [float('inf')]
        self.cg_taxtable = list(map(lambda x, y: [x[1], x[0], y], self.cg_taxrates, cutoffs))


        # add columns for the standard deduction, tax brackets, 
        # state std deduction, state bracket, cg brackets(xN),
        # running account totals, state tax, fed tax, total taxes, 
        # yearly cap gains dist. and nii taxes
        global cg_vper 
        cg_vper = 9
        vper += 1 + len(self.taxtable) + \
            1 + len(self.state_taxtable) + cg_vper*len(self.cg_taxtable) + \
                3 + 1 + 1 + 1 + \
                    1 + cg_vper*2

        if 'prep' in d:
            self.workyr = d['prep']['workyears']
            self.maxsave = d['prep']['maxsave']
            self.maxsave_inflation = d['prep'].get('inflation', True)
            self.worktax = 1 + d['prep'].get('tax_rate', 25)/100
        else:
            self.workyr = 0
        self.retireage = self.startage + self.workyr
        self.numyr = self.endage - self.retireage

        self.aftertax = d.get('aftertax', {'bal': 0})
        if 'basis' not in self.aftertax:
            self.aftertax['basis'] = 0
        if 'distributions' not in self.aftertax:
            self.aftertax['distributions'] = 0.0
        self.aftertax['distributions'] *= 0.01

        self.IRA = d.get('IRA', {'bal': 0})
        if 'maxcontrib' not in self.IRA:
            self.IRA['maxcontrib'] = 19500 + 7000*2

        self.roth = d.get('roth', {'bal': 0})
        if 'maxcontrib' not in self.roth:
            self.roth['maxcontrib'] = 7000*2
        if 'contributions' not in self.roth:
            self.roth['contributions'] = []

        self.parse_expenses(d)
        self.sepp_end = max(5, 59-self.retireage)  # first year you can spend IRA reserved for SEPP
        self.sepp_ratio = 25                       # money per-year from SEPP  (bal/ratio)

    def parse_expenses(self, S):
        """ Return array of income/expense per year """
        INC = [0] * self.numyr
        EXP = [0] * self.numyr
        TAX = [0] * self.numyr
        STATE_TAX = [0] * self.numyr
        CEILING = [5000000] * self.numyr

        for k,v in S.get('expense', {}).items():
            for age in agelist(v['age']):
                year = age - self.retireage
                if year < 0:
                    continue
                elif year >= self.numyr:
                    break
                else:
                    amount = v['amount']
                    if v.get('inflation'):
                        amount *= self.i_rate ** (year + self.workyr)
                    EXP[year] += amount

        for k,v in S.get('income', {}).items():
            for age in agelist(v['age']):
                year = age - self.retireage
                if year < 0:
                    continue
                elif year >= self.numyr:
                    break
                else:
                    ceil = v.get('ceiling', 5000000)
                    if v.get('inflation'):
                        ceil *= self.i_rate ** (year + self.workyr)
                    if (ceil < CEILING[year]):
                        CEILING[year] = ceil
                    amount = v['amount']
                    if v.get('inflation'):
                        amount *= self.i_rate ** (year + self.workyr)
                    INC[year] += amount
                    if v.get('tax'):
                        TAX[year] += amount
                        if (v.get('state_tax') is None) or (v.get('state_tax')):
                            STATE_TAX[year] += amount
                    else:
                        if v.get('state_tax'):
                            STATE_TAX[year] += amount
        self.income = INC
        self.expenses = EXP
        self.taxed = TAX
        self.state_taxed = STATE_TAX
        self.ceiling = CEILING

# Minimize: c^T * x
# Subject to: A_ub * x <= b_ub
def solve(args):
    nvars = n1 + vper * (S.numyr + S.workyr)
    global fsave_offset, fira_offset, froth_offset, ira2roth_offset
    global save_offset, ira_offset, roth_offset, taxes_offset, cgd_offset
    global fedtax_offset, statetax_offset
    fsave_offset = 0
    fira_offset = fsave_offset + 1
    froth_offset = fira_offset + 1
    ira2roth_offset = froth_offset + 1
    stded_offset = ira2roth_offset + 1
    taxtable_offset = stded_offset + 1
    state_stded_offset = taxtable_offset + len(S.taxtable)
    state_taxtable_offset = state_stded_offset + 1
    cg_taxtable_offset = state_taxtable_offset + len(S.state_taxtable)
    save_offset = cg_taxtable_offset + len(S.cg_taxtable)*cg_vper
    ira_offset = save_offset + 1
    roth_offset = ira_offset + 1
    fedtax_offset = roth_offset + 1
    statetax_offset = fedtax_offset + 1
    taxes_offset = statetax_offset + 1
    cgd_offset = taxes_offset + 1
    nii_offset = cgd_offset + 1

    M = 5_000_000
    integrality = [0] * nvars
    bounds = [(0, None)] * nvars

    # put the <= constrtaints here
    A = []
    b = []

    # put the equality constrtaints here
    AE = []
    be = []

    # https://stackoverflow.com/questions/56050131/how-i-can-seperate-negative-and-positive-variables
    def split_value(actual_col, ind_col, pos_col, neg_col):
        nonlocal A, b, AE, be, bounds, integrality

        bounds[actual_col] = (None, None)
        bounds[ind_col] = (0, 1)
        integrality[ind_col] = 1

        row = [0] * nvars 
        row[actual_col] = 1
        row[pos_col] = -1
        row[neg_col] = 1
        AE += [row]
        be += [0]

        row = [0] * nvars 
        row[actual_col] = 1
        A += [row]
        b += [M]

        row = [0] * nvars 
        row[actual_col] = -1
        A += [row]
        b += [M]

        row = [0] * nvars
        row[pos_col] = 1
        row[ind_col] = -M
        A += [row]
        b += [0]

        row = [0] * nvars
        row[neg_col] = 1
        row[ind_col] = M
        A += [row] 
        b += [M]


    # https://www.fico.com/fico-xpress-optimization/docs/latest/mipform/dhtml/chap2s1.html?scroll=ssecminval
    def find_min(result_col, a_col, b_col, a_ind_col, b_ind_col):
        nonlocal A, b, AE, be, bounds, integrality

        bounds[a_ind_col] = (0, 1)
        bounds[b_ind_col] = (0, 1)
        integrality[a_ind_col] = 1
        integrality[b_ind_col] = 1

        row = [0] * nvars
        row[result_col] = 1
        row[a_col] = -1
        A += [row]
        b += [0]

        row = [0] * nvars
        row[result_col] = 1
        row[b_col] = -1
        A += [row]
        b += [0]

        row = [0] * nvars
        row[a_ind_col] = 1
        row[b_ind_col] = 1
        AE += [row]
        be += [1]

        row = [0] * nvars
        row[result_col] = -1
        row[a_col] = 1
        row[a_ind_col] = M + M
        A += [row]
        b += [M + M]

        row = [0] * nvars
        row[result_col] = -1
        row[b_col] = 1
        row[b_ind_col] = M + M
        A += [row]
        b += [M + M]


    # optimize this poly
    c = [0] * nvars
    if (args.spend is None):
        # maximize spending
        c[0] = -1
    else:
        # set spending to a fixed value
        # we'll minimize taxes later on
        row = [0] * nvars 
        row[0] = -1
        A += [row]
        b += [-1 * float(args.spend)]
    
    if (args.roth is not None):
        # set the final roth value
        i_mul = S.i_rate ** S.numyr
        row = [0] * nvars
        row[n0+(S.numyr-1)*vper+roth_offset] = -S.r_rate
        row[n0+(S.numyr-1)*vper+ira2roth_offset] = -S.r_rate
        row[n0+(S.numyr-1)*vper+froth_offset] = S.r_rate
        A += [row]
        b += [-i_mul * float(args.roth)]

#    if not args.sepp:    # assume sepp is off
    # force SEPP to zero
    row = [0] * nvars
    row[1] = 1
    A += [row]
    b += [0]

    # Work contributions don't exceed limits
    for year in range(S.workyr):
        # can't exceed maxsave per year
        year_offset = n1+year*vper
        n_fsave = year_offset + fsave_offset 
        n_fira = year_offset + fira_offset 
        n_froth = year_offset + froth_offset
        n_save = year_offset + save_offset 
        n_ira = year_offset + ira_offset 
        n_roth = year_offset + roth_offset
        n_cgd = year_offset + cgd_offset
        
        row = [0] * nvars
        row[n_fsave] = S.worktax
        row[n_fira] = 1
        row[n_froth] = S.worktax
        A += [row]
        if S.maxsave_inflation:
            b += [S.maxsave * S.i_rate ** year]

        else:
            b += [S.maxsave]


        # set year-end cap-gains distribution
        row = [0] * nvars
        row[n_cgd] = 1
        row[n_save] = -1 * S.r_rate * S.aftertax['distributions']
        row[n_fsave] = -1 * S.r_rate * S.aftertax['distributions']
        AE += [row]
        be += [0]

        # max IRA per year
        row = [0] * nvars
        row[n_fira] = 1
        A += [row]
        b += [S.IRA['maxcontrib'] * S.i_rate ** year]

        # max Roth per year
        row = [0] * nvars
        row[n_froth] = 1
        A += [row]
        b += [S.roth['maxcontrib'] * S.i_rate ** year]

         # calc running total beginning-of-year saving balance
        row = [0] * nvars
        row[n_save] = 1
        for y in range(year):
            row[n1+vper*y+fsave_offset] = -(S.r_rate ** (year - y))
        AE += [row]
        be += [S.aftertax['bal'] * S.r_rate ** (year)]

        # calc running total beginning-of-year ira balance
        row = [0] * nvars
        row[n_ira] = 1
        for y in range(year):
            row[n1+vper*y+fira_offset] = -(S.r_rate ** (year - y))
            # current version never does working year ira2roth conversions
            # row[n1+vper*y+ira2roth_offset] = (S.r_rate ** (year - y))
        AE += [row]
        be += [S.IRA['bal'] * S.r_rate ** (year)]

        # calc running total beginning-of-year roth balance
        row = [0] * nvars
        row[n_roth] = 1
        for y in range(year):
            row[n1+vper*y+froth_offset] = -(S.r_rate ** (year - y))
            # current version never does working year ira2roth conversions            
            # row[n1+vper*y+ira2roth_offset] = -(S.r_rate ** (year - y))
        AE += [row]
        be += [S.roth['bal'] * S.r_rate ** (year)]
       

    # For a study that influenced this strategy see
    # https://www.academyfinancial.org/resources/Documents/Proceedings/2009/3B-Coopersmith-Sumutka-Arvesen.pdf
    for year in range(S.numyr):
        i_mul = S.i_rate ** (year + S.workyr)
        year_offset = n0+vper*year
        n_fsave = year_offset + fsave_offset
        n_fira = year_offset + fira_offset
        n_froth = year_offset + froth_offset
        n_ira2roth = year_offset + ira2roth_offset
        n_stded = year_offset + stded_offset
        n_taxtable = year_offset + taxtable_offset
        n_state_stded = year_offset + state_stded_offset
        n_state_taxtable = year_offset + state_taxtable_offset
        n_cg_taxtable = year_offset + cg_taxtable_offset
        n_save = year_offset + save_offset
        n_ira = year_offset + ira_offset
        n_roth = year_offset + roth_offset
        n_fedtax = year_offset + fedtax_offset
        n_statetax = year_offset + statetax_offset
        n_taxes = year_offset + taxes_offset
        n_cgd = year_offset + cgd_offset
        n_nii = year_offset + nii_offset

        if (args.spend is not None):
            # spending is set so we'll minimize lifetime taxes in today's dollars
            c[n_taxes] = 1 / i_mul 
 
        # aftertax basis
        # XXX fix work contributions
        if S.aftertax['basis'] > 0:
            basis = 1 - (S.aftertax['basis'] /
                         (S.aftertax['bal'] *
                          (S.r_rate-S.aftertax['distributions'])**(year + S.workyr)))
            if basis < 0:
                basis = 0
        else:
            basis = 1
#        print("basis", basis)

        # Set capital gains distributions for the year
        # This assumes that the taxable distribution is based on the year-end value
        row = [0] * nvars
        row[n_cgd] = 1
        row[n_save] = -1 * S.r_rate * S.aftertax['distributions']
        row[n_fsave] = 1 * S.r_rate * S.aftertax['distributions']
        AE += [row]
        be += [0]


        if (S.ceiling[year] < 5000000):
            row = [0] * nvars
            row[n_cgd] = 1
            row[n_fsave] = basis
            row[n_fira] = 1
            row[n_ira2roth] = 1
            A += [row]
            b += [S.ceiling[year] - S.taxed[year]]
#            print("ceiling", S.ceiling[year])

        # limit how much can be considered part of the standard deduction
        row = [0] * nvars
        row[n_stded] = 1
        A += [row]
        b += [S.stded * i_mul]

        for idx, (rate, low, high) in enumerate(S.taxtable[0:-1]):
            # limit how much can be put in each tax bracket
            row = [0] * nvars
            row[n_taxtable+idx] = 1
            A += [row]
            b += [(high - low) * i_mul]

        # the sum of everything in the std deduction + tax brackets must 
        # be equal to fira + ira2roth + taxed_extra
        row = [0] * nvars
        row[n_fira] = 1
        row[n_ira2roth] = 1
        row[n_stded] = -1
        for idx in range(len(S.taxtable)):
            row[n_taxtable+idx] = -1
        AE += [row]
        be += [-S.taxed[year]]


        # limit how much can be considered part of the state standard deduction
        row = [0] * nvars
        row[n_state_stded] = 1
        A += [row]
        b += [S.state_stded * i_mul]

        for idx, (rate, low, high) in enumerate(S.state_taxtable[0:-1]):
            # limit how much can be put in each state tax bracket
            row = [0] * nvars
            row[n_state_taxtable+idx] = 1
            A += [row]
            b += [(high - low) * i_mul]

        # the sum of everything in the state std deduction + state tax brackets must 
        # be equal to fira + ira2roth + cgd + fsave*basis + state_taxed_extra
        # Note: capital gains are treated as income for state taxes
        row = [0] * nvars
        row[n_fira] = 1
        row[n_ira2roth] = 1
        row[n_fsave] = basis
        row[n_cgd] = 1
        row[n_state_stded] = -1
        for idx in range(len(S.state_taxtable)):
            row[n_state_taxtable+idx] = -1
        AE += [row]
        be += [-S.state_taxed[year]]


        for idx, (rate, low, high) in enumerate(S.cg_taxtable[0:-1]):
            # limit how much can be put in each cg tax bracket

            # how much does our taxable income take us over this bracket
            # store this value in (cg2 - cg3), see below
            row = [0] * nvars
            row[n_fira] = 1
            row[n_ira2roth] = 1
            row[n_stded] = -1
            row[n_cg_taxtable+idx*cg_vper+3] = 1
            row[n_cg_taxtable+idx*cg_vper+2] = -1
            AE += [row]
            be += [low*i_mul - S.taxed[year]]

            # the previous calc could have given a negative number
            # force max(0, previous_calc) into cg2
            split_value(n_cg_taxtable+idx*cg_vper+0, n_cg_taxtable+idx*cg_vper+1,
                        n_cg_taxtable+idx*cg_vper+2, n_cg_taxtable+idx*cg_vper+3)

            # put size of this bracket in cg4
            row = [0] * nvars
            row[n_cg_taxtable+idx*cg_vper+4] = 1
            AE += [row]
            be += [(high-low) * i_mul]

            # put the min(cg2, cg4) into cg7
            # cg5 and cg6 are used as temporary variables
            find_min(n_cg_taxtable+idx*cg_vper+7, n_cg_taxtable+idx*cg_vper+2,
                     n_cg_taxtable+idx*cg_vper+4, n_cg_taxtable+idx*cg_vper+5,
                     n_cg_taxtable+idx*cg_vper+6)

            # cg7 (part of bracket used by income) + cg8 (part of bracket used by cap gains)
            # must be less than the size of bracket
            row = [0] * nvars
            row[n_cg_taxtable+idx*cg_vper+7] = 1
            row[n_cg_taxtable+idx*cg_vper+8] = 1
            A += [row]
            b += [(high-low) * i_mul]

        # the sum of the used cg tax brackets must equal cgd + basis*fsave
        row = [0] * nvars
        row[n_fsave] = basis
        row[n_cgd] = 1
        for idx in range(len(S.cg_taxtable)):
            row[n_cg_taxtable+idx*cg_vper+8] = -1
        AE += [row]
        be += [0]


        # calc the nii tax
        # how much does our non-investment income take us over this bracket
        # store this value in (nii2 - nii3), see below
        row = [0] * nvars
        row[n_fira] = 1
        row[n_ira2roth] = 1
        row[n_nii+3] = 1
        row[n_nii+2] = -1
        AE += [row]
        be += [-S.taxed[year]]

        # the previous calc could have given a negative number
        # force max(0, previous_calc) into nii2
        split_value(n_nii+0, n_nii+1,
                    n_nii+2, n_nii+3)

        # put size of this bracket in nii4
        row = [0] * nvars
        row[n_nii+4] = 1
        AE += [row]
        be += [S.nii]       # no inflation for nii

        # put the min(nii2, nii4) into nii7
        # nii5 and nii6 are used as temporary variables
        find_min(n_nii+7, n_nii+2,
                 n_nii+4, n_nii+5,
                 n_nii+6)

        # nii7 (part of bracket used by income) + nii8 (part of bracket used by cap gains)
        # must be less than the size of bracket
        row = [0] * nvars
        row[n_nii+7] = 1
        row[n_nii+8] = 1
        A += [row]
        b += [S.nii]        # no inflation for nii

        # the sum of the used nii tax brackets must equal cgd + basis*fsave
        row = [0] * nvars
        row[n_fsave] = basis
        row[n_cgd] = 1
        for idx in range(2):
            row[n_nii+idx*cg_vper+8] = -1
        AE += [row]
        be += [0]



        # calc fed taxes
        row = [0] * nvars
        row[n_fedtax] = 1                       # this is where we will store fed taxes
        if year + S.retireage < 59:             # ira penalty
            row[n_fira] = -0.1
        row[n_froth] = -0
        for idx, (rate, low, high) in enumerate(S.taxtable):
            row[n_taxtable+idx] = -rate
            if args.bumptax and (year > float(args.bumpstart)):
                row[n_taxtable+idx] += -1 * float(args.bumptax)/100.0
        for idx, (rate, low, high) in enumerate(S.cg_taxtable):
            row[n_cg_taxtable+idx*cg_vper+8] = -rate
        row[n_nii+1*cg_vper+8] = -0.038
        AE += [row]
        be += [0]

        # calc state taxes
        row = [0] * nvars
        row[n_statetax] = 1                     # this is where we will store state taxes
        for idx, (rate, low, high) in enumerate(S.state_taxtable):
            row[n_state_taxtable+idx] = -rate
        AE += [row]
        be += [0]

        # calc total taxes
        row = [0] * nvars
        row[n_taxes] = 1
        row[n_fedtax] = -1
        row[n_statetax] = -1
        AE += [row]
        be += [0]

        # calc that everything withdrawn must equal spending money + total taxes
        row = [0] * nvars
        # spendable money
        row[n_fsave] = 1
        if (year+S.workyr > 0):
            row[n_cgd - vper] = 1           # spend last years cg distributions
        row[n_fira] = 1
        row[n_froth] = 1
        # spent money
        row[0] -= i_mul                     # spending floor
        row[n_taxes] = -1                   # taxes as computed earlier
        AE += [row]
        be += [-S.income[year] + S.expenses[year]]

        # calc running total beginning-of-year saving balance
        row = [0] * nvars
        row[n_save] = 1
        for y in range(S.workyr):
            row[n1+vper*y+fsave_offset] = -(S.r_rate ** (year + S.workyr - y))
        for y in range(year):
            row[n0+vper*y+fsave_offset] = S.r_rate ** (year - y)
            row[n0+vper*y+cgd_offset] = S.r_rate ** (year - y - 1)
        AE += [row]
        be += [S.aftertax['bal'] * S.r_rate ** (S.workyr + year)]

        # calc running total beginning-of-year ira balance
        row = [0] * nvars
        row[n_ira] = 1
        for y in range(S.workyr):
            row[n1+vper*y+fira_offset] = -(S.r_rate ** (year + S.workyr - y))
            # current version never does working year ira2roth conversions
            # row[n1+vper*y+ira2roth_offset] = (S.r_rate ** (year + S.workyr - y))
        for y in range(year):
            row[n0+vper*y+fira_offset] = S.r_rate ** (year - y)
            row[n0+vper*y+ira2roth_offset] = S.r_rate ** (year - y)
        AE += [row]
        be += [S.IRA['bal'] * S.r_rate ** (S.workyr + year)]

        # calc running total beginning-of-year roth balance
        row = [0] * nvars
        row[n_roth] = 1
        for y in range(S.workyr):
            row[n1+vper*y+froth_offset] = -(S.r_rate ** (year + S.workyr - y))
            # current version never does working year ira2roth conversions            
            # row[n1+vper*y+ira2roth_offset] = -(S.r_rate ** (year + S.workyr - y))
        for y in range(year):
            row[n0+vper*y+froth_offset] = S.r_rate ** (year - y)
            row[n0+vper*y+ira2roth_offset] = -(S.r_rate ** (year - y))
        AE += [row]
        be += [S.roth['bal'] * S.r_rate ** (S.workyr + year)]

      

    # final balance for savings needs to be positive
    row = [0] * nvars
    inc = 0
    for year in range(S.numyr):
        row[n0+vper*year+fsave_offset] = S.r_rate ** (S.numyr - year)
        row[n0+vper*year+cgd_offset] = S.r_rate ** (S.numyr - year - 1)
        #if S.income[year] > 0:
        #    inc += S.income[year] * S.r_rate ** (S.numyr - year)
    for year in range(S.workyr):
        row[n1+vper*year+fsave_offset] = -(S.r_rate ** (S.numyr + S.workyr - year))
    A += [row]
    b += [S.aftertax['bal'] * S.r_rate ** (S.workyr + S.numyr) + inc]


    # any years with income need to be positive in aftertax
    # for year in range(S.numyr):
    #     if S.income[year] == 0:
    #         continue
    #     row = [0] * nvars
    #     inc = 0
    #     for y in range(year):
    #         row[n0+vpy*y+0] = S.r_rate ** (year - y)
    #         inc += S.income[y] * S.r_rate ** (year - y)
    #     A += [row]
    #     b += [S.aftertax['bal'] * S.r_rate ** year + inc]

    # final balance for IRA needs to be positive
    row = [0] * nvars
    for year in range(S.numyr):
        row[n0+vper*year+fira_offset] = S.r_rate ** (S.numyr - year)
        row[n0+vper*year+ira2roth_offset] = S.r_rate ** (S.numyr - year)
        if year < S.sepp_end:
            row[1] += (1/S.sepp_ratio) * S.r_rate ** (S.numyr - year)
    for year in range(S.workyr):
        row[n1+vper*year+fira_offset] = -(S.r_rate ** (S.numyr + S.workyr - year))
    A += [row]
    b += [S.IRA['bal'] * S.r_rate ** (S.workyr + S.numyr)]


    # IRA balance at SEPP end needs to not touch SEPP money
#    row = [0] * nvars
#    for year in range(S.sepp_end):
#        row[n0+vper*year+fira_offset] = S.r_rate ** (S.sepp_end - year)
#        row[n0+vper*year+ira2roth_offset] = S.r_rate ** (S.sepp_end - year)
#    for year in range(S.workyr):
#        row[n1+vper*year+fira_offset] = -(S.r_rate ** (S.sepp_end + S.workyr - year))
#    row[1] = S.r_rate ** S.sepp_end
#    A += [row]
#    b += [S.IRA['bal'] * S.r_rate ** S.sepp_end]


    # before 59, Roth can only spend from contributions
    for year in range(min(S.numyr, 59-S.retireage)):
        row = [0] * nvars
        for y in range(0, year-4):
            row[n0+vper*y+ira2roth_offset] = -1
        for y in range(year+1):
            row[n0+vper*y+froth_offset] = 1

        # include contributions while working
        for y in range(min(S.workyr, S.workyr-4+year)):
            row[n1+vper*y+froth_offset] = -1

        A += [row]
        # only see initial balance after it has aged
        contrib = 0
        for (age, amount) in S.roth['contributions']:
            if age + 5 - S.retireage <= year:
                contrib += amount
        b += [contrib]


    # after 59 all of Roth can be spent, but contributions need to age
    # 5 years and the balance each year needs to be positive
    for year in range(max(0,59-S.retireage),S.numyr+1):
        row = [0] * nvars

        # remove previous withdrawls
        for y in range(year):
            row[n0+vper*y+froth_offset] = S.r_rate ** (year - y)

        # add previous conversions, but we can only see things
        # converted more than 5 years ago
        for y in range(year-5):
            row[n0+vper*y+ira2roth_offset] = -S.r_rate ** (year - y)

        # add contributions from work period
        for y in range(S.workyr):
            row[n1+vper*y+froth_offset] = -S.r_rate ** (S.workyr + year - y)

        A += [row]
        # initial balance
        b += [S.roth['bal'] * S.r_rate ** (S.workyr + year)]


    # starting with age 70 the user must take RMD payments
    for year in range(max(0,73-S.retireage),S.numyr):
        row = [0] * nvars
        age = year + S.retireage
        rmd = RMD[age - 72]

        # the gains from the initial balance minus any withdraws gives
        # the current balance.
        for y in range(year):
            row[n0+vper*y+fira_offset] = -(S.r_rate ** (year - y))
            row[n0+vper*y+ira2roth_offset] = -(S.r_rate ** (year - y))
            if year < S.sepp_end:
                row[1] -= (1/S.sepp_ratio) * S.r_rate ** (year - y)

        # include deposits during work years
        for y in range(S.workyr):
            row[n1+vper*y+fira_offset] = S.r_rate ** (S.workyr + year - y)

        # this year's withdraw times the RMD factor needs to be more than
        # the balance
        row[n0+vper*year+fira_offset] = -rmd

        A += [row]
        b += [-(S.IRA['bal'] * S.r_rate ** (S.workyr + year))]

    if args.verbose:
        print("Num vars: ", len(c))
        print("Num contraints A: ", len(A))
        print("Num contraints b: ", len(b))
        print("integrality: ", len(integrality))

    timelimit = 300
    if args.timelimit:
        timelimit = float(args.timelimit)

    res = scipy.optimize.linprog(c, A_ub=A, b_ub=b, A_eq=AE, b_eq=be,
                                 method="highs",
                                 bounds=bounds, 
                                 integrality=integrality, 
                                 options={'disp': args.verbose, 
                                          'time_limit': timelimit,
                                          'presolve': args.spend is None})
    if res.status > 1:
        print(res)
        exit(1)

#    for i in range(vper):
#        print("%i %f" % (i, res.x[n0+0*vper+i]))
    print(res.message)
    if (args.roth is not None):
        i_mul = S.i_rate ** S.numyr
        roth_value = res.x[n0+(S.numyr-1)*vper+roth_offset] 
        roth_value += res.x[n0+(S.numyr-1)*vper+ira2roth_offset]
        roth_value -= res.x[n0+(S.numyr-1)*vper+froth_offset]
        roth_value *= S.r_rate
        print("The ending value, including final year investment returns, of your Roth account will be %.0f" % roth_value)
        print("That is equivalent to %.0f in today's dollars" % (roth_value / i_mul))
    return res.x

def print_ascii(res):
    print("Yearly spending <= ", 100*int(res[0]/100))
    sepp = 100*int(res[1]/100)
    print("SEPP amount = ", sepp, sepp / S.sepp_ratio)
    print()
    if S.workyr > 0:
        print((" age" + " %5s" * 6) %
              ("save", "tSAVE", "IRA", "tIRA", "Roth", "tRoth"))
    for year in range(S.workyr):
        savings = res[n1+year*vper+save_offset]
        ira = res[n1+year*vper+ira_offset]
        roth = res[n1+year*vper+roth_offset]
        fsavings = res[n1+year*vper+fsave_offset]
        fira = res[n1+year*vper+fira_offset]
        froth = res[n1+year*vper+froth_offset]
        print((" %d:" + " %5.0f" * 6) %
              (year+S.startage,
               savings/1000, fsavings/1000,
               ira/1000, fira/1000,
               roth/1000, froth/1000))

    print((" age" + " %5s" * 13) %
          ("save", "fsave", "IRA", "fIRA", "SEPP", "Roth", "fRoth", "IRA2R",
           "rate", "tax", "spend", "extra", "cgd"))
    ttax = 0.0
    tspend = 0.0
    for year in range(S.numyr):
        i_mul = S.i_rate ** (year + S.workyr)
        fsavings = res[n0+year*vper+fsave_offset]
        fira = res[n0+year*vper+fira_offset]
        froth = res[n0+year*vper+froth_offset]
        ira2roth = res[n0+year*vper+ira2roth_offset]
        cgd = res[n0+year*vper+cgd_offset]
        if year < S.sepp_end:
            sepp_spend = sepp/S.sepp_ratio
        else:
            sepp_spend = 0
        savings = res[n0+year*vper+save_offset]
        ira = res[n0+year*vper+ira_offset]
        roth = res[n0+year*vper+roth_offset]

        inc = fira + ira2roth - S.stded*i_mul + S.taxed[year] + sepp_spend
        basis = 1
        if S.aftertax['basis'] > 0:
            basis = 1 - (S.aftertax['basis'] /
                         (S.aftertax['bal'] *
                          (S.r_rate-S.aftertax['distributions'])**(year + S.workyr)))
            if basis < 0:
                basis = 0
#        print("basis: ", basis)
        state_inc = fira + ira2roth - S.state_stded*i_mul + S.state_taxed[year] + basis*fsavings + cgd + sepp_spend
        tax = res[n0+year*vper+taxes_offset]

        fed_rate = 0
        if (inc > 0):
            fed_rate = next(r for (r, l, h) in S.taxtable if (inc <= h*i_mul))
        state_rate = 0
        if (state_inc > 0):
            state_rate = next(r for (r, l, h) in S.state_taxtable if (state_inc <= h*i_mul))
        rate = fed_rate + state_rate
        #if S.income[year]:
        #    savings += S.income[year]

        extra = S.expenses[year] - S.income[year]
        spend_cgd = 0
        if (year+S.workyr > 0):                 # spend last year's distributions
            spend_cgd = res[n0+year*vper+cgd_offset-vper]
        spending = fsavings + spend_cgd + fira + froth - tax - extra + sepp_spend

        ttax += tax / i_mul                     # totals in today's dollars
        tspend += (spending) / i_mul    # totals in today's dollars
        div_by = 1000
        print((" %d:" + " %5.0f" * 13) %
              (year+S.retireage,
               savings/div_by, fsavings/div_by,
               ira/div_by, fira/div_by, sepp_spend/div_by,
               roth/div_by, froth/div_by, ira2roth/div_by,
               rate * 100, tax/div_by, spending/div_by, 
               extra/div_by, cgd/div_by))


    print("\ntotal spending: %.0f" % tspend)
    print("total tax: %.0f (%.1f%%)" % (ttax, 100*ttax/(tspend+ttax)))


def print_csv(res):
    print("spend goal,%d" % res[0])
    print("savings,%d,%d" % (S.aftertax['bal'], S.aftertax['basis']))
    print("ira,%d" % S.IRA['bal'])
    print("roth,%d" % S.roth['bal'])

    print("age,save,fsave,IRA,fIRA,Roth,fRoth,IRA2R,income,expense,cgd,fed_tax,state_tax,spend")
    for year in range(S.numyr):
        savings = res[n0+year*vper+save_offset]
        fsavings = res[n0+year*vper+fsave_offset]
        ira = res[n0+year*vper+ira_offset]
        fira = res[n0+year*vper+fira_offset]
        roth = res[n0+year*vper+roth_offset]
        froth = res[n0+year*vper+froth_offset]
        ira2roth = res[n0+year*vper+ira2roth_offset]
        cgd = res[n0+year*vper+cgd_offset]
        fed_tax = res[n0+year*vper+fedtax_offset]
        state_tax = res[n0+year*vper+statetax_offset]
        print(("%d," * 13 + "%d") % (year+S.retireage,savings,fsavings,ira,
                                     fira,roth,froth,ira2roth,S.income[year],
                                     S.expenses[year],cgd,fed_tax,state_tax,
                                     res[0]*S.i_rate**(S.workyr+year)))

def main():
    # Instantiate the parser
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='store_true',
                        help="Extra output from solver")
    # The sepp code deserves a closer look before being re-enabled
    # I don't know if I broke it or not.
#    parser.add_argument('--sepp', action='store_true',
#                        help="Enable SEPP processing")
    parser.add_argument('--csv', action='store_true', help="Generate CSV outputs")
    parser.add_argument('--validate', action='store_true',
                        help="compare single run to separate runs")
    parser.add_argument('--timelimit',
                        help="After given seconds return the best answer found")
    parser.add_argument('--bumpstart',
                        help="Start tax bump after given years")
    parser.add_argument('--bumptax',
                        help="Increase taxes charged in all federal income tax brackets")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--spend',
                        help="Setting yearly spending will cause taxes to be minimized")
    group.add_argument('--roth',
                       help="Specify the total that should be left in the Roth account")
    parser.add_argument('conffile')
    args = parser.parse_args()

    if bool(args.bumptax) ^ bool(args.bumpstart):
        parser.error('--bumptax and --bumpstart must be given together')

    global S
    global vper, n1
    vper = 4        # variables per year (savings, ira, roth, ira2roth)
    n1 = 2          # before-retire years start here
    S = Data()
    S.load_file(args.conffile)

    global n0
    n0 = n1+S.workyr*vper   # post-retirement years start here

    res = solve(args)
    if args.csv:
        print_csv(res)
    else:
        print_ascii(res)

    if args.validate:
        for y in range(1,nyears):
            pass

if __name__== "__main__":
    main()
