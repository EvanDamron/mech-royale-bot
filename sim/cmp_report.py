"""Summarize giddy-vs-kfe2 decision divergences from compare.py logs."""
import json, math, sys, collections
def ang(a, b): return abs((a - b + math.pi) % (2 * math.pi) - math.pi)
cnt = collections.Counter(); ex = collections.defaultdict(list); n = 0
for fn in sys.argv[1:]:
    for line in open(fn):
        r = json.loads(line); n += 1
        g, k = r["g"], r["k"]; mode = r["kmode"]
        gf, kf = g.get("fire") or [False, False], k.get("fire") or [False, False]
        for i in range(2):
            if gf[i] != kf[i]:
                key = f"fire{i} g={int(bool(gf[i]))} k={int(bool(kf[i]))} [{mode}]"
                cnt[key] += 1; ex[key].append(r["t"])
        if g.get("aim") and k.get("aim") and ang(g["aim"][0], k["aim"][0]) > 0.35:
            key = f"aim differs [{mode}]"; cnt[key] += 1; ex[key].append(r["t"])
        gm, km = g.get("move") or [0, 0], k.get("move") or [0, 0]
        gn, kn = math.hypot(*gm), math.hypot(*km)
        if gn > 0.3 and kn > 0.3 and ang(math.atan2(gm[1], gm[0]), math.atan2(km[1], km[0])) > math.pi / 2:
            key = f"move opposite [{mode}]"; cnt[key] += 1; ex[key].append(r["t"])
        elif (gn > 0.3) != (kn > 0.3):
            key = f"move g={'go' if gn > 0.3 else 'stop'} k={'go' if kn > 0.3 else 'stop'} [{mode}]"; cnt[key] += 1; ex[key].append(r["t"])
        if bool(g.get("boost")) != bool(k.get("boost")):
            key = f"boost g={int(bool(g.get('boost')))} [{mode}]"; cnt[key] += 1
        if g.get("take") != k.get("take"):
            key = f"take g={g.get('take')} k={k.get('take')}"; cnt[key] += 1; ex[key].append(r["t"])
print(f"{n} ticks")
for key, c in cnt.most_common(40):
    print(f"{c:5d}  {key}   e.g. t={ex[key][:4]}")
