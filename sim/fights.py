"""Per-second fight timeline for the external bot: hp, damage taken/dealt by attacker, kills, house hunters."""
import math, sys, os, shlex, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Match
from house import HouseBot
seed = int(sys.argv[1]); cmd = shlex.split(sys.argv[2])
house = [HouseBot(l) for l in ["easy"]*4 + ["medium"]*4 + ["hard"]*4 + ["expert"]*3]
M = Match([("me", cmd)], seed=seed, house=house)
slot = list(M.ext)[0]
taken = collections.Counter(); dealt = collections.Counter()
od = M.damage
def dmg(m, d, att, w, src):
    if m.id == slot and att is not None: taken[(att.id, w)] += d
    elif m.id == slot and w == "npc": taken[("npc", w)] += d
    if att is not None and att.id == slot: dealt[m.id] += d
    od(m, d, att, w, src)
M.damage = dmg
ost = M.step; last = [0]
def step(dt):
    ost(dt)
    if M.t - last[0] >= 2:
        last[0] = M.t
        me = M.mechs[slot]
        hunters = M.house[next(iter(M.house))].hunters(M) if M.house else []
        hs = [(h, M.mechs[h].name.split('-')[1][:3], round(math.hypot(M.mechs[h].pos[0]-me.pos[0], M.mechs[h].pos[1]-me.pos[1]))) for h in hunters]
        tk = {f"{k[0]}{k[1][0]}": round(v) for k, v in taken.items()}
        dl = {k: round(v) for k, v in dealt.items()}
        w = [x and x['kind'][:3] for x in me.weapons]
        print(f"t{M.t:4.0f} hp{me.hp:5.0f}/{me.stats['hp']:.0f} k{me.kills} c{me.cubes} {w} hunters{hs} taken{tk} dealt{dl}")
        taken.clear(); dealt.clear()
        if not me.alive: raise SystemExit
M.step = step
M.run()
