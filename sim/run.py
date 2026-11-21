"""Run local matches.

  python sim/run.py practice --bot "python3 -u bot.py" -n 24 -j 8
      one external bot vs 15 house bots (4 easy, 4 medium, 4 hard, 3 expert) that hunt it
  python sim/run.py ffa --bot A="python3 -u bot.py" --bot B="python3 -u old/bot.py" -n 16 -j 8
      external bots fill slots round-robin; remaining slots are FFA house bots
"""
import argparse
import json
import multiprocessing as mp
import os
import shlex
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from engine import Match  # noqa: E402
from house import HouseBot  # noqa: E402


def parse_bots(specs):
    out = []
    for s in specs:
        if "=" in s.split(" ")[0]:
            name, cmd = s.split("=", 1)
        else:
            name, cmd = s, s
        out.append((name, shlex.split(cmd)))
    return out


def one(args):
    mode, bots, seed, copies = args
    if mode == "solo":
        house = [HouseBot("passive")]
        ext = bots[:1]
    elif mode == "practice":
        house = [HouseBot(l) for l in ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3]
        ext = bots[:1]
    else:
        house = [HouseBot("hard", ffa=True)]
        ext = []
        for k in range(copies):
            ext.append(bots[k % len(bots)])
    t0 = time.time()
    M = Match(ext, seed=seed, house=house)
    res = M.run()
    return {"seed": seed, "res": res, "secs": time.time() - t0, "end_t": M.t}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["practice", "ffa", "solo"])
    ap.add_argument("--bot", action="append", required=True)
    ap.add_argument("-n", type=int, default=8)
    ap.add_argument("-j", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--copies", type=int, default=16, help="ffa: number of external bot slots")
    ap.add_argument("--out")
    a = ap.parse_args()
    bots = parse_bots(a.bot)
    jobs = [(a.mode, bots, a.seed + i, a.copies) for i in range(a.n)]
    scores = {}
    extra = {}
    allres = []
    with mp.Pool(a.j) as pool:
        for r in pool.imap_unordered(one, jobs):
            allres.append(r)
            line = []
            for e in r["res"]:
                nm = e["name"]
                if a.mode in ("practice", "solo") and nm.startswith("house"):
                    continue
                scores.setdefault(nm, []).append(16 - e["place"])
                extra.setdefault(nm, []).append((e["kills"], e["cubes"], e["death_t"]))
                line.append(f"{nm}:p{e['place']} k{e['kills']} c{e['cubes']} d{e['death_t']:.0f}s late{e['late']}")
            print(f"seed {r['seed']} ({r['secs']:.0f}s, end {r['end_t']:.0f}s): " + " | ".join(line[:6]), flush=True)
    print("=== mean (16 - place) ===")
    for nm, v in sorted(scores.items(), key=lambda kv: -statistics.mean(kv[1])):
        se = statistics.stdev(v) / len(v) ** 0.5 if len(v) > 1 else 0
        ex = extra[nm]
        print(f"{nm:30s} {statistics.mean(v):6.2f} ± {se:4.2f}  (n={len(v)})  kills {statistics.mean(k for k, c, d in ex):.2f}"
              f"  cubes {statistics.mean(c for k, c, d in ex):.1f}  death_t median {statistics.median(d for k, c, d in ex):.0f}s")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(allres, f)


if __name__ == "__main__":
    main()
