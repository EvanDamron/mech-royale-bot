#!/bin/bash
# Remote A/B on Mystik-Dan from an isolated code copy: runs/abr.sh TAG n "NAME:ENV..." ...
cd "$(dirname "$0")/.."
tag=$1; n=$2; shift 2
R=buildathon/mech/runs/$tag
ssh -o ControlPath=none mystik-dan "mkdir -p $R/code"
rsync -az -e "ssh -o ControlPath=none" --exclude .venv --exclude runs --exclude .git ./ mystik-dan:$R/code/
args=""
for v in "$@"; do args="$args '$v'"; done
ssh -o ControlPath=none mystik-dan "cd ~/$R/code && export MECH_KIT=~/buildathon/mech/starter/mechfight-kit; PY=~/buildathon/mech/bot/.venv/bin/python; for v in $args; do name=\${v%%:*}; envs=\${v#*:}; SIM_HOUSE_MULT=\${MULT:-2.5} taskset -c 10-17,30-37 \$PY sim/run.py ${MODE:-practice} --bot \"\$name=env \$envs \$PY -u bot.py\" -n $n -j ${J:-16} --seed ${SEED:-101} > ../\$name.txt 2>&1; echo \"\$name \$(grep '±' ../\$name.txt)\"; done"
