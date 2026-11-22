"""League-only engine fix: nothing spawns on the stadium.
The engine's world model treats the stadium stands as walkable ground, so about one rival bot in eight spawned up there, where
its own map says it cannot stand, and it froze until the zone killed it. Real showdown matches have no such deaths."""
import json, os
from engine import Match, MAP_DIR

_polys = []
for p in json.load(open(os.path.join(MAP_DIR, "v2", "world.json")))["prisms"]:
    if p["kind"] == "stadium":
        r = p["ring"]
        _polys.append(([(r[i], r[i + 1]) for i in range(0, len(r), 2)], min(r[0::2]) - 6, max(r[0::2]) + 6, min(r[1::2]) - 6, max(r[1::2]) + 6))


_X0 = min(p[1] for p in _polys); _X1 = max(p[2] for p in _polys)
_Y0 = min(p[3] for p in _polys); _Y1 = max(p[4] for p in _polys)


def on_stadium(x, y):
    # the whole stadium block, bowl included: the field inside the stands is enclosed, and a rival bot that spawns there
    # cannot path out according to its own map
    return _X0 <= x <= _X1 and _Y0 <= y <= _Y1


_orig = Match.rand_ground


def rand_ground(self, center=None, radius=None, tries=500):
    for _ in range(40):
        p = _orig(self, center, radius, tries)
        if p is None or not on_stadium(p[0], p[1]):
            return p
    return p


Match.rand_ground = rand_ground


# Second fix: a mech that drops off a roof edge lands inside its own collision radius of the wall it fell past, and the
# engine's move test then rejects every step (review: rival bots and our bot stood wedged for 4 to 8 s per match on average,
# up to 119 s at a time). A physics engine pushes an overlapping body out; do the same, at walking pace.
import math

_orig_step = Match.step
_DIRS = [(math.cos(k * math.pi / 8), math.sin(k * math.pi / 8)) for k in range(16)]


def step(self, dt):
    _orig_step(self, dt)
    if os.environ.get("NO_WEDGE_FIX"):
        return
    for m in self.mechs:
        if not m.alive or not m.grounded:
            continue
        r, h = m.stats["radius"], m.stats["height"]
        x, y, z = m.pos
        if not self.blocked_at(x, y, z, r, h):
            continue
        for d in (0.6, 1.2, 2.0, 3.0, 4.0, 5.0):
            best = None
            for cx, cy in _DIRS:
                if not self.blocked_at(x + cx * d, y + cy * d, z, r, h):
                    best = (cx, cy)
                    break
            if best:
                s = min(d, 0.6)
                m.pos[0] += best[0] * s
                m.pos[1] += best[1] * s
                break


Match.step = step
