"""Gun accuracy of the external bot vs house bots in one practice match."""
import math, sys, os, shlex, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Match
from house import HouseBot
seed = int(sys.argv[1]); cmd = shlex.split(sys.argv[2])
house = [HouseBot(l) for l in ["easy"]*4 + ["medium"]*4 + ["hard"]*4 + ["expert"]*3]
M = Match([("me", cmd)], seed=seed, house=house)
slot = list(M.ext)[0]
shots = collections.Counter(); hits = collections.Counter()
ow = M.weapons_tick
def wt(m, dt):
    n0 = len(M.projectiles)
    ow(m, dt)
    for p in M.projectiles[n0:]:
        if p["kind"] == "gun":
            shots[m.id == slot] += 1
M.weapons_tick = wt
oh = M.hit_obj
def ho(kind, obj, dmg, att, w, src=None, self_ok=False):
    if w == "gun" and kind == "mech" and att is not None:
        hits[att.id == slot] += 1
    oh(kind, obj, dmg, att, w, src, self_ok)
M.hit_obj = ho
M.run()
me = M.mechs[slot]
print(f"seed {seed}: me shots {shots[True]} hits {hits[True]} ({hits[True]/max(1,shots[True]):.0%}); house shots {shots[False]} hits {hits[False]} ({hits[False]/max(1,shots[False]):.0%}); died {me.death_tick/20 if me.death_tick else None}")
