#!/bin/bash
# Remote A/B on Mystik-Dan from an isolated code copy, detached; waits and prints the summary.
# runs/abr.sh TAG n "NAME:ENV..." ...      (env MULT, SEED, MODE, J optional)
cd "$(dirname "$0")/.."
tag=$1; n=$2; shift 2
R=buildathon/mech/runs/$tag
S="ssh -o ControlPath=none mystik-dan"
$S "mkdir -p $R/code"
rsync -az -e "ssh -o ControlPath=none" --exclude .venv --exclude runs --exclude .git ./ mystik-dan:$R/code/
loop=""
for v in "$@"; do
  name=${v%%:*}; envs=${v#*:}
  loop="$loop SIM_HOUSE_MULT=${MULT:-2.5} taskset -c 10-17,30-37 \$PY sim/run.py ${MODE:-practice} --bot \"$name=env $envs \$PY -u bot.py\" -n $n -j ${J:-16} --seed ${SEED:-101} > ../$name.txt 2>&1; echo \"$name \$(grep '±' ../$name.txt)\" >> ../summary.txt;"
done
$S "cd ~/$R/code && export MECH_KIT=~/buildathon/mech/starter/mechfight-kit PY=~/buildathon/mech/bot/.venv/bin/python && rm -f ../summary.txt && (setsid nohup bash -c '$loop echo DONE >> ../summary.txt' > /dev/null 2>&1 < /dev/null &)"
until $S "grep -q DONE ~/$R/summary.txt 2>/dev/null"; do sleep 30; done
$S "cat ~/$R/summary.txt"
