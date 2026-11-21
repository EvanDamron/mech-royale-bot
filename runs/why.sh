#!/bin/bash
# runs/why.sh seed : summarize how the bot died in one calibrated practice match
cd "$(dirname "$0")/.."
SIM_HOUSE_MULT=${MULT:-2.5} .venv/bin/python sim/trace.py $1 "env KFE_DEBUG=1 $EXTRA .venv/bin/python -u bot.py" 1 2>&1 | .venv/bin/python -c "
import sys,ast,collections
dmg=collections.Counter(); mine=collections.Counter(); modes=collections.Counter(); lines=[]; last=''
for l in sys.stdin:
    l=l.rstrip()
    if l.startswith('t=') : last=l[:100]
    elif l.startswith('    ('):
        e=ast.literal_eval(l.strip())
        if e[1]=='ME->': mine[e[3]]+=e[4]
        else: dmg[(e[2],e[3])]+=e[4]
    elif l.startswith('t') and ' hp' in l:
        modes[l.split()[1]]+=1; lines.append(l)
print('seed $1 death', last)
print(' taken', dict(dmg)); print(' dealt', dict(mine)); print(' modes', dict(modes))
for l in lines[-12::2]: print('  ', l[:200])
"
