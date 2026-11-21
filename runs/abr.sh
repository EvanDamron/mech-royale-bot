#!/bin/bash
# Remote A/B on Mystik-Dan: runs/abr.sh TAG n "NAME:ENV..." ...  (syncs code first, prints summaries)
cd "$(dirname "$0")/.."
tag=$1; n=$2; shift 2
rsync -az --exclude .venv --exclude runs --exclude .git ./ mystik-dan:buildathon/mech/bot/
args=""
for v in "$@"; do args="$args '$v'"; done
ssh mystik-dan "cd ~/buildathon/mech/bot && mkdir -p runs/$tag && for v in $args; do name=\${v%%:*}; envs=\${v#*:}; SIM_HOUSE_MULT=\${MULT:-2.5} taskset -c 10-17,30-37 .venv/bin/python sim/run.py ${MODE:-practice} --bot \"\$name=env \$envs .venv/bin/python -u bot.py\" -n $n -j 16 --seed ${SEED:-101} > runs/$tag/\$name.txt 2>&1; echo \"\$name \$(grep '±' runs/$tag/\$name.txt) dmed=\$(grep -o 'd[0-9]*s' runs/$tag/\$name.txt | tr -d ds | sort -n | awk '{a[NR]=\$1} END{print a[int((NR+1)/2)]}')\"; done"
