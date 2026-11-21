"""Trace one practice match: prints the external bot's state each second."""
import math, sys, os, shlex
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Match
from house import HouseBot
seed = int(sys.argv[1]); cmd = shlex.split(sys.argv[2]); every = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
house = [HouseBot(l) for l in ["easy"]*4 + ["medium"]*4 + ["hard"]*4 + ["expert"]*3]
M = Match([("me", cmd)], seed=seed, house=house, verbose=True)
slot = list(M.ext)[0]
orig = M.damage
log = []
def dmg(m, d, att, w, src):
    if m.id == slot and w != "zone": log.append((round(M.t,1), att.id if att else None, att.name if att else None, w, round(d)))
    if att is not None and att.id == slot: log.append((round(M.t,1), "ME->", m.id, w, round(d)))
    orig(m, d, att, w, src)
M.damage = dmg
ost = M.step
last = [-1]
def step(dt):
    ost(dt)
    if M.t - last[0] >= every:
        last[0] = M.t
        me = M.mechs[slot]
        near = sorted((math.hypot(o.pos[0]-me.pos[0], o.pos[1]-me.pos[1]), o.id, o.name, round(o.hp)) for o in M.mechs if o.alive and o is not me)[:2]
        print(f"t={M.t:5.1f} hp={me.hp:6.0f} pos={[round(v) for v in me.pos]} aim={me.aim[0]:.2f} act={ {k:v for k,v in me.action.items() if k in ('move','fire','boost','jump')} } near={[(round(a),b,c,d) for a,b,c,d in near]} w={[w and w['kind'] for w in me.weapons]}", flush=True)
        for l in log: print("   ", l)
        log.clear()
        if not me.alive: raise SystemExit
M.step = step
M.run()
