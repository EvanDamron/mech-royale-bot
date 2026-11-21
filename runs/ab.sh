#!/bin/bash
# usage: runs/ab.sh mode n "NAME:ENV..." ...   (runs a snapshot of bot.py with env vars per variant)
cd "$(dirname "$0")/.."
mode=$1; n=$2; shift 2
snap=runs/snap_$$; mkdir -p $snap && cp bot.py worldmap.py mechfight.py $snap/
for v in "$@"; do
  name=${v%%:*}; envs=${v#*:}
  .venv/bin/python sim/run.py $mode --bot "$name=env $envs .venv/bin/python -u $snap/bot.py" -n $n -j ${J:-8} --seed ${SEED:-1} > runs/ab_${mode}_$name.txt 2>&1
  echo "$name $(grep '±' runs/ab_${mode}_$name.txt)"
done
