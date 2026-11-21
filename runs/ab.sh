#!/bin/bash
# usage: runs/ab.sh mode n "NAME=ENV..." ...   (each variant runs bot.py with env vars)
cd "$(dirname "$0")/.."
mode=$1; n=$2; shift 2
for v in "$@"; do
  name=${v%%:*}; envs=${v#*:}
  .venv/bin/python sim/run.py $mode --bot "$name=env $envs .venv/bin/python -u bot.py" -n $n -j 8 --seed ${SEED:-1} > runs/ab_${mode}_$name.txt 2>&1
  echo "$name $(grep '±' runs/ab_${mode}_$name.txt)"
done
