"""Paired comparison of two run.py outputs on the same seeds: python runs/paired.py A.txt B.txt"""
import re, sys, statistics
def load(fn):
    out = {}
    for l in open(fn):
        m = re.match(r"seed (\d+) .*?:p(\d+) k(\d+) c(\d+) d(\d+)s", l)
        if m:
            out[int(m.group(1))] = (16 - int(m.group(2)), int(m.group(5)))
    return out
a, b = load(sys.argv[1]), load(sys.argv[2])
common = sorted(set(a) & set(b))
d = [a[s][0] - b[s][0] for s in common]
dt = [a[s][1] - b[s][1] for s in common]
se = statistics.stdev(d) / len(d) ** 0.5 if len(d) > 1 else 0
print(f"n={len(d)}  A-B score {statistics.mean(d):+.2f} ± {se:.2f}   A {statistics.mean(a[s][0] for s in common):.2f}  B {statistics.mean(b[s][0] for s in common):.2f}   death-time diff median {statistics.median(dt):+.0f}s")
