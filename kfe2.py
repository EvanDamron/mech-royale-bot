"""Kentucky Fried Evan bot, version 2.

Track-based engagement rules, crowd/losing retreat to cover (with a fewest-watchers fallback and a withdraw step when
outnumbered in the open), peek-aware fight positioning, bullet and charging-laser dodging, latency-aware lead, lethal
early laser release, cube grabs during fights, utility-based looting with a reachability check, and a separate
free-for-all policy once the match turns out not to be practice. Built on our own map model (worldmap.py).
"""
import heapq
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mechfight as mf  # noqa: E402
from worldmap import World  # noqa: E402

TAU = 2 * math.pi
LAT = 0.075          # our action lands about this long after the observation
DEBUG = bool(int(os.environ.get("KFE_DEBUG", 0)))
PARTIAL_COVER = bool(int(os.environ.get("K2_PARTIAL", 0)))  # fewest-watchers cover fallback (league: hurts, off)
WITHDRAW = bool(int(os.environ.get("K2_WITHDRAW", 1)))       # swamped, no cover, no target: back away instead of standing
USE_DROPS = bool(int(os.environ.get("K2_DROPS", 0)))       # go for supply drops (league: costs placement, off)
CROWD_N = int(os.environ.get("K2_CROWD", 3))                               # this many engaged/close enemies in view means retreat
UPG_ORDER = os.environ.get("K2_UPG", "survive")              # "survive" or "damage"
OPENING_HUNT = bool(int(os.environ.get("K2_OPENING", 0)))   # walk far for a laser in the first 20 s
# --- free-for-all extensions from the league reviews (strategy-review/league); 0 switches each one off ---
X_SEEK = float(os.environ.get("X_SEEK", 250))        # unarmed: walk this far for a laser or lightning (rival bots stop at 90 m)
X_UNARMED = float(os.environ.get("X_UNARMED", 110))  # unarmed: keep this far from every mech
X_AVOID = float(os.environ.get("X_AVOID", 80))       # armed, idle: keep this far from every mech (rival bots start fights at 60 m)
X_DROPS = int(os.environ.get("X_DROPS", 1))          # free-for-all: take supply drops (rival bots never do and stay 60 m away)
X_EXPLORE = int(os.environ.get("X_EXPLORE", 0))      # unarmed with no weapon in memory: walk and look instead of holding a spot
X_BREAK = int(os.environ.get("X_BREAK", 0))          # free-for-all: break off a losing or two-sided fight and stop shooting
X_FFA_N = int(os.environ.get("X_FFA_N", 1))           # deaths we did not cause that mean free-for-all (for testing late detection)
X_PRACT = float(os.environ.get("X_PRACT", 20))       # no unexplained death by this time = practice match (rival bots use 20 s)
X_STEALTH = int(os.environ.get("X_STEALTH", 1))      # before a free-for-all is confirmed: never sweep the aim across a mech
X_HUSH = int(os.environ.get("X_HUSH", 1))            # before a free-for-all is confirmed: never close on a mech in view at over 3 m/s
V_PICK = int(os.environ.get("V_PICK", 1))     # visual review: finish weapon pickups within 25 m (mid-fight too; no +space/+hush push)
V_CORNER = int(os.environ.get("V_CORNER", 1)) # visual review: cornered with 2+ threats -> withdraw instead of trading in place
V_REGEN = int(os.environ.get("V_REGEN", 1))   # visual review: under 50% HP and unhit 3 s, hold fire beyond 90 m (lets regen start)
V_GATE = int(os.environ.get("V_GATE", 1))     # 1 = V_CORNER and V_REGEN only in practice mode
# Hit rates measured on real matches (18 public showdown room captures, Nov 21): gun hits / shots by range band.
# House bots (expert + organizer probes) and team bots (other teams' entries). Laser: expert 17/119/98/31 releases.
HIT_RANGES = (40, 60, 80, 100, 150, 1e9)
GUN_HIT_HOUSE = (0.70, 0.59, 0.44, 0.37, 0.21, 0.11)    # 524/753, 931/1571, 908/2053, 538/1451, 427/1991, 87/788
GUN_HIT_TEAM = (0.76, 0.67, 0.56, 0.52, 0.43, 0.30)     # 54/71, 147/219, 163/290, 94/181, 119/274; 150+ capped (43/81 is thin)
LASER_HIT_HOUSE = (0.59, 0.59, 0.30, 0.30, 0.13, 0.06)  # 0-60 / 60-100 / 100-150 / 150+ m
LASER_HIT_TEAM = (0.80, 0.80, 0.80, 0.80, 0.70, 0.40)   # team-bot lasers track and hit 64-87% inside 150 m in top-16 matches
GUN_DPS_HOUSE, GUN_DPS_TEAM, LASER_DPS = 112.0, 150.0, 131.0   # house taps every 0.4 s; 315 per 2.4 s laser cycle


def band(table, d):
    for r, v in zip(HIT_RANGES, table):
        if d < r:
            return v
    return table[-1]


RESET = float(os.environ.get("K2_RESET", 0))   # practice: after a kill, break line of sight for this many seconds before re-engaging
ISO = float(os.environ.get("K2_ISO", 0))   # fight spots: penalty per other mech with a line on the spot (0 = the old 30, nearest 3 in view)
PLAN = float(os.environ.get("K2_PLAN", 0))   # HP of expected damage worth one meter of progress, inverted (0 = off)
X_SPIN = int(os.environ.get("X_SPIN", 1))            # free-for-all: full-circle scan whenever the torso is free, also while walking
X_FLOW = int(os.environ.get("X_FLOW", 1))            # full path field for long trips (the capped A* dead-ends)

# our own upgrade preferences: survivability first, then the weapon we actually carry
UPG_LASER = ["armor", "plating", "laser_damage", "laser_charge", "nanites", "gun_rate", "gun_damage", "gun_mag",
             "lightning_efficiency", "lightning_range", "gyros", "servos", "reactor", "capacitor", "jets", "gun_velocity",
             "sensors", "lightning_chain", "laser_pierce", "mortar_reload", "mortar_splash", "mortar_velocity"]
UPG_BASE = ["armor", "plating", "nanites", "gun_rate", "gun_damage", "gun_mag", "lightning_efficiency", "lightning_range",
            "laser_damage", "laser_charge", "gyros", "servos", "reactor", "capacitor", "jets", "gun_velocity", "sensors",
            "lightning_chain", "laser_pierce", "mortar_reload", "mortar_splash", "mortar_velocity"]


UPG_DAMAGE = ["laser_damage", "gun_damage", "gun_rate", "laser_charge", "armor", "plating", "lightning_efficiency",
              "gun_mag", "nanites", "lightning_range", "gyros", "servos", "reactor", "capacitor", "jets", "gun_velocity",
              "sensors", "lightning_chain", "laser_pierce", "mortar_reload", "mortar_splash", "mortar_velocity"]


def env(name, default):
    return type(default)(os.environ.get(name, default))


# practice (squad) and free-for-all parameter sets
SQUAD = dict(engage=60.0, laser_r=env("K2_LASER_R", 120.0), gun_r=85.0, light_r=28.0, max_t=240.0, hpw=0.18,
             low_bonus=200.0, stick=200.0, retreat_hp=env("K2_RETREAT", 0.38), recover_hp=0.8, loot_r=150.0,
             laser_seek=240.0, fight_loot=env("K2_FIGHT_LOOT", 70.0), farm_hp=0.5, crates_until=1e9, peek=False, ghost=False, lose_rule=True,
             lethal_k=0.7, finish=20.0)
FFA = dict(engage=60.0, laser_r=85.0, gun_r=85.0, light_r=28.0, max_t=215.0, hpw=60.0 / 1400, low_bonus=0.0,
           stick=0.0, retreat_hp=0.38, recover_hp=0.8, loot_r=90.0, laser_seek=0.0, fight_loot=0.0, farm_hp=0.7,
           crates_until=150.0, peek=True, ghost=True, lose_rule=False, lethal_k=0.8, finish=0.0)


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def hyp(x, y):
    return math.hypot(x, y)


class Flow:
    """Shortest-path field toward one goal over the whole nav grid, built a few milliseconds per decision."""

    def __init__(self, W, goal):
        self.W = W
        self.goal = (goal[0], goal[1])
        gj, gi = W.nearest_free(*W.nav_cell(goal[0], goal[1]))
        self.G = gj * W.gnx + gi
        self.dist = {self.G: 0.0}
        self.nxt = {self.G: -1}
        self.heap = [(0.0, self.G)]
        self.done = False

    def work(self, budget_s):
        W = self.W
        gnx, gny = W.gnx, W.gny
        free = W.nav_flat
        dist, nxt, heap = self.dist, self.nxt, self.heap
        t0 = time.perf_counter()
        n_it = 0
        SQ2 = 1.41421356
        while heap:
            n_it += 1
            if n_it % 64 == 0 and time.perf_counter() - t0 > budget_s:
                return
            dc, cur = heapq.heappop(heap)
            if dc > dist.get(cur, 1e18) + 1e-9:
                continue
            cj, ci = divmod(cur, gnx)
            for di, dj, w in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                              (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2)):
                ni, nj = ci + di, cj + dj
                if ni < 0 or nj < 0 or ni >= gnx or nj >= gny:
                    continue
                n = nj * gnx + ni
                if not free[n]:
                    continue
                if di and dj and not (free[cj * gnx + ni] and free[nj * gnx + ci]):
                    continue
                nd = dc + w
                if nd < dist.get(n, 1e18):
                    dist[n] = nd
                    nxt[n] = cur
                    heapq.heappush(heap, (nd, n))
        self.done = True

    def waypoints(self, x, y, k=12):
        W = self.W
        gj, gi = W.nearest_free(*W.nav_cell(x, y))
        n = gj * W.gnx + gi
        if n not in self.nxt or (not self.done and self.heap and self.dist[n] > self.heap[0][0]):
            return None
        out = []
        for _ in range(k):
            n = self.nxt.get(n, -1)
            if n < 0:
                out.append(self.goal)
                break
            j, i = divmod(n, W.gnx)
            out.append(W.nav_center(j, i))
        return out


class Track:
    __slots__ = ("id", "kind", "pos", "vel", "aim", "hp", "max_hp", "cubes", "weapons", "charging", "seen", "dist",
                 "los", "closing", "hit_me", "engage")

    def __init__(self, i):
        self.id = i
        self.hit_me = -100.0
        self.engage = False


class Bot:
    name = "KentuckyFriedEvan"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.slot = hello["slot"]
        self.rng = random.Random(hello.get("seed", 0) or self.slot + 1)
        self.W = World(hello["map"]["dir"])
        self.mech_cfg = self.cfg["mech"]
        self.tracks = {}
        self.npcs = {}
        self.crates = {}
        self.cubes = {}
        self.pickups = {}
        self.kills = 0
        self.start_alive = None
        self.other_dead = 0
        self.ffa = False
        self.P = dict(FFA)
        self.prac_on = False
        self.opening = False
        self.last_hurt = -100.0
        self.hurt_bearing = 0.0
        self.npc_hurt = -100.0
        self.npc_bearing = 0.0
        self.last_threat = -100.0
        self.retreat = False
        self.lose_at = -1.0
        self.strafe = 1
        self.strafe_until = 0.0
        self.stick_id = None
        self.stick_at = -100.0
        self.cover_goal = None
        self.cover_at = -100.0
        self.reset_until = -100.0
        self.loot_id = None
        self.loot_since = 0.0
        self.bad = {}
        self.path = None
        self.path_goal = None
        self.path_t = -100.0
        self.stuck_since = -1.0
        self.unstuck_until = -1.0
        self.unstuck_dir = (1.0, 0.0)
        self.hold = None
        self.hold_start = None
        self.home_xy = None
        self.home_at = -100.0
        self.mode = "idle"
        self.flows = {}
        self.break_until = -100.0
        self.spin_t0 = 0.0
        self.scan_dir = 1.0

    # ------------------------------------------------------------------ geometry helpers
    def height_of(self, cubes):
        return self.mech_cfg["height"] * (1 + self.cfg["cubes"]["size_per_cube"] * (cubes or 0))

    def radius_of(self, cubes):
        return self.mech_cfg["radius"] * (1 + self.cfg["cubes"]["size_per_cube"] * (cubes or 0))

    def center(self, t):
        if t.kind == "npc":
            return (t.pos[0], t.pos[1], t.pos[2] + self.cfg["npc"]["height"] * 0.5)
        return (t.pos[0], t.pos[1], t.pos[2] + self.height_of(t.cubes) * 0.5)

    def eye(self, t):
        return (t.pos[0], t.pos[1], t.pos[2] + self.height_of(getattr(t, "cubes", 0)) * 8 / 9)

    def walk_z(self, x, y):
        return self.W.terrain(x, y)

    # ------------------------------------------------------------------ zone
    def zone_at(self, z, t):
        if t <= z["shrink_start"] or z["shrink_end"] <= z["shrink_start"]:
            return self.hold
        f = clamp((t - z["shrink_start"]) / (z["shrink_end"] - z["shrink_start"]), 0, 1)
        hx, hy, hr = self.hold
        return (hx + (z["next_center"][0] - hx) * f, hy + (z["next_center"][1] - hy) * f,
                hr + (z["next_radius"] - hr) * f)

    def cur_zone(self, z, now):
        live = (z["center"][0], z["center"][1], z["radius"])
        if now <= z["shrink_start"]:
            return live
        if abs(live[2] - self.hold[2]) > 0.01 or abs(live[0] - self.hold[0]) > 0.01:
            return live
        return self.zone_at(z, now)

    def zone_deadline(self, z, x, y, pad):
        nc, nr = z["next_center"], z["next_radius"]
        if hyp(x - nc[0], y - nc[1]) <= nr - pad:
            return 1e18
        for k in range(25):
            t = z["shrink_start"] + (z["shrink_end"] - z["shrink_start"]) * k / 24
            c = self.zone_at(z, t)
            if hyp(x - c[0], y - c[1]) > c[2] - pad:
                return t
        return z["shrink_end"]

    def zone_penalty(self, z, x, y, now):
        c = self.cur_zone(z, now)
        p = 0.0
        d = hyp(x - c[0], y - c[1]) - c[2]
        if d > -5:
            p += 150 + 5 * (d + 5)
        dn = hyp(x - z["next_center"][0], y - z["next_center"][1]) - z["next_radius"]
        if dn > -5:
            soon = clamp(1 - (z["shrink_start"] - now) / 25, 0.1, 1)
            p += soon * (25 + 0.8 * (dn + 5))
        return p

    def zone_goal(self, z, x, y):
        nr = z["next_radius"]
        nc = z["next_center"]
        if nr <= 30:
            return nc[0], nc[1]
        margin = clamp(0.3 * nr, 3, 30)
        dx, dy = x - nc[0], y - nc[1]
        d = hyp(dx, dy)
        r = max(0.0, nr - margin - 5)
        if d < 1 or d <= r:
            return x, y
        return nc[0] + dx / d * r, nc[1] + dy / d * r

    # ------------------------------------------------------------------ movement helpers
    def travel(self, me, gx, gy):
        x, y = me["pos"][0], me["pos"][1]
        d = hyp(gx - x, gy - y)
        if d < 0.8:
            return 0.0, 0.0
        W = self.W
        if d < 70 and W.walk_clear((x, y), (gx, gy)):
            return (gx - x) / d, (gy - y) / d
        now = self.now
        if X_FLOW and d > 90:
            key = (round(gx / 8.0), round(gy / 8.0))
            fl = self.flows.get(key)
            if fl is None:
                if len(self.flows) >= 3:
                    self.flows.pop(next(iter(self.flows)))
                fl = self.flows[key] = Flow(W, (gx, gy))
            wp = fl.waypoints(x, y)
            if wp:
                tgt = wp[0]
                for k in range(len(wp) - 1, 0, -1):
                    if hyp(wp[k][0] - x, wp[k][1] - y) < 50 and W.walk_clear((x, y), wp[k]):
                        tgt = wp[k]
                        break
                dd = hyp(tgt[0] - x, tgt[1] - y)
                if dd > 1e-6:
                    return (tgt[0] - x) / dd, (tgt[1] - y) / dd
        if self.path is None or self.path_goal is None or hyp(gx - self.path_goal[0], gy - self.path_goal[1]) > 6 \
                or now - self.path_t > 2.5:
            self.path = W.astar((x, y), (gx, gy), max_expand=3500)
            self.path_goal = (gx, gy)
            self.path_t = now
        p = self.path
        if not p:
            return (gx - x) / d, (gy - y) / d
        while len(p) > 1 and hyp(p[0][0] - x, p[0][1] - y) < 3.0:
            p.pop(0)
        tgt = p[0]
        for k in range(min(len(p), 8) - 1, 0, -1):
            if hyp(p[k][0] - x, p[k][1] - y) < 45 and W.walk_clear((x, y), p[k]):
                tgt = p[k]
                del p[:k]
                break
        dd = hyp(tgt[0] - x, tgt[1] - y)
        if dd < 1e-6:
            return 0.0, 0.0
        return (tgt[0] - x) / dd, (tgt[1] - y) / dd

    def near_wall(self, x, y, r=4):
        W = self.W
        j, i = W.cell(x, y)
        return W.wall[max(j - r, 0):j + r + 1, max(i - r, 0):i + r + 1].any()

    def pref_range(self, me, t, weapons):
        light = weapons.get("lightning")
        laser = weapons.get("laser")
        r = self.P["gun_r"]
        if light is not None and me["energy"] > 12:
            r = self.P["light_r"]
        elif laser is not None:
            r = self.P["laser_r"]
        if t.kind == "npc":
            return 58.0
        if light is None:
            if "lightning" in (t.weapons or ()):
                r = max(r, 64.0)
            if "mortar" in (t.weapons or ()):
                r = max(r, 80.0)
        return r

    def fight_move(self, o, tg, seen, want_los, weapons):
        me = o["self"]
        x, y = me["pos"][0], me["pos"][1]
        W = self.W
        rp = self.pref_range(me, tg, weapons)
        bx, by = tg.pos[0] - x, tg.pos[1] - y
        bd = hyp(bx, by) + 1e-9
        ux, uy = bx / bd, by / bd
        if self.now >= self.strafe_until:
            if self.rng.random() < 0.6:
                self.strafe = -self.strafe
            self.strafe_until = self.now + 0.3 + self.rng.random() * 0.7
        sx, sy = -uy * self.strafe, ux * self.strafe
        tc = self.center(tg)
        mh = me["stats"].get("muzzle_height", 7)
        cube = None
        for c in self.cubes.values():
            d = hyp(c["pos"][0] - x, c["pos"][1] - y)
            if d < 40 and abs(c["pos"][2] - me["pos"][2]) < 4 and (cube is None or d < cube[0]):
                cube = (d, c["pos"])
        pool = seen
        if ISO:
            # practice review: half the damage we take comes from mechs we are not shooting; fight where only the target sees us
            pool = [t for t in self.tracks.values() if t.kind == "mech" and self.now - t.seen < 6]
        others = sorted((e for e in pool if e is not tg and hyp(e.pos[0] - x, e.pos[1] - y) < (200 if ISO else 170)),
                        key=lambda e: hyp(e.pos[0] - x, e.pos[1] - y))[:5 if ISO else 3]
        best = -1e18
        mx = my = 0.0
        blocked_side = False
        rings = 2 if (self.P["peek"] or ISO) else 1
        z = o["zone"]
        for k in range(-1, 16 * rings):
            if k >= 0:
                a = (k % 16) * math.pi / 8
                r = 6.0 * (1 + k // 16)
                dx, dy = math.cos(a), math.sin(a)
                cx, cy = x + dx * r, y + dy * r
                if not W.free_at(cx, cy) or not W.walk_clear((x, y), (cx, cy)):
                    if k < 16 and dx * sx + dy * sy > 0.9:
                        blocked_side = True
                    continue
            else:
                cx, cy, dx, dy, r = x, y, 0.0, 0.0, 0.0
            cz = self.walk_z(cx, cy)
            d = hyp(tg.pos[0] - cx, tg.pos[1] - cy)
            s = -1.1 * abs(d - rp) - r * 0.4
            los = W.los((cx, cy, cz + mh), tc)
            if los:
                s += 45 * want_los
            body = (cx, cy, cz + 9 * 0.6)
            for e in others:
                if W.los(self.eye(e), body):
                    s -= ISO or 30
            s -= self.zone_penalty(z, cx, cy, self.now)
            if cube is not None:
                s += 1.5 * (cube[0] - hyp(cube[1][0] - cx, cube[1][1] - cy))
            if self.near_wall(cx, cy, 2):
                s -= 5
            if want_los > 0:
                s += 9 * (dx * sx + dy * sy)
            if k < 0:
                s -= 8
                if want_los < 0 and not los:
                    s += 14
            if s > best:
                best, mx, my = s, dx, dy
        if blocked_side:
            self.strafe = -self.strafe
            self.strafe_until = self.now + 0.5
        return mx, my

    def cover_move(self, o, seen):
        """Walk to the nearest nav cell hidden from the (up to 4) nearest threats."""
        me = o["self"]
        x, y = me["pos"][0], me["pos"][1]
        threats = [t for t in seen if hyp(t.pos[0] - x, t.pos[1] - y) < 230]
        if not threats:
            return None
        threats.sort(key=lambda t: hyp(t.pos[0] - x, t.pos[1] - y))
        threats = threats[:4]
        W = self.W
        h = self.height_of(me.get("cubes", 0))

        def seen_by(cx, cy, limit):
            """Number of threats with a line on (cx, cy), stopping once it reaches limit."""
            cz = self.walk_z(cx, cy)
            n = 0
            for t in threats:
                e = self.eye(t)
                if W.los(e, (cx, cy, cz + h)) or W.los(e, (cx, cy, cz + h * 0.5)):
                    n += 1
                    if n >= limit:
                        return n
            return n

        def hidden(cx, cy):
            return seen_by(cx, cy, 1) == 0
        if self.cover_goal is not None and self.now - self.cover_at < 0.4:
            return self.travel_cover(me)
        self.cover_at = self.now
        z = o["zone"]
        if hidden(x, y) and self.zone_penalty(z, x, y, self.now) == 0:
            self.cover_goal = (x, y)
            return 0.0, 0.0
        sj, si = W.nearest_free(*W.nav_cell(x, y))
        gnx, gny = W.gnx, W.gny
        free = W.nav_flat
        start = sj * gnx + si
        seen_c = {start: 0}
        queue = [start]
        best = None
        partial = None
        checked = 0
        h0 = 0
        t_end = time.perf_counter() + 0.006
        while h0 < len(queue) and checked < 80 and time.perf_counter() < t_end:
            c = queue[h0]
            h0 += 1
            steps = seen_c[c]
            if steps > 25 or (best is not None and steps * 8 > best[0] + 30):
                break
            cj, ci = divmod(c, gnx)
            if steps > 0:
                cx, cy = W.nav_center(cj, ci)
                checked += 1
                lim = len(threats) if best is None and PARTIAL_COVER else 1
                if partial is not None:
                    lim = min(lim, partial[0])
                nv = seen_by(cx, cy, max(lim, 1))
                near = min(hyp(t.pos[0] - cx, t.pos[1] - cy) for t in threats)
                if nv == 0:
                    sc = steps * 8 + self.zone_penalty(z, cx, cy, self.now)
                    sc -= min(near, 80) * 0.25
                    if best is None or sc < best[0]:
                        best = (sc, cx, cy)
                elif PARTIAL_COVER and nv < len(threats):
                    sc = steps * 8 + self.zone_penalty(z, cx, cy, self.now) - min(near, 120) * 0.5
                    if partial is None or nv < partial[0] or (nv == partial[0] and sc < partial[1]):
                        partial = (nv, sc, cx, cy)
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                ni, nj = ci + di, cj + dj
                if ni < 0 or nj < 0 or ni >= gnx or nj >= gny:
                    continue
                n = nj * gnx + ni
                if n in seen_c or not free[n]:
                    continue
                if di and dj and not (free[cj * gnx + ni] and free[nj * gnx + ci]):
                    continue
                seen_c[n] = steps + 1
                queue.append(n)
        if best is None and partial is not None:
            best = (partial[1], partial[2], partial[3])
        if best is None:
            self.cover_goal = None
            return None
        self.cover_goal = (best[1], best[2])
        return self.travel_cover(me)

    def plan_move(self, o, mx, my, target, weapons, walk, z):
        """Expected damage taken minus dealt over the next ~1.2 s for each candidate step; keep the mode's direction
        unless another step is clearly safer. Review: half the damage we take comes from inside 60 m and half from mechs
        we are not shooting; the first hit decides most fights."""
        me = o["self"]
        x, y = me["pos"][0], me["pos"][1]
        now = self.now
        W = self.W
        foes = []
        for t in self.tracks.values():
            age = now - t.seen
            if t.kind != "mech" or age > 6:
                continue
            d = hyp(t.pos[0] - x, t.pos[1] - y)
            if d > 220:
                continue
            ws = t.weapons or ()
            facing = abs(wrap(math.atan2(t.pos[1] - y, t.pos[0] - x) - me["aim"][0])) < 0.5 and d < 190
            hot = t.engage or now - t.hit_me < 4 or d < 65 or (self.prac_on and d < 150) or (not self.prac_on and facing)
            wgt = (1.0 if hot else 0.35) * (1.0 if age < 0.3 else 0.6)
            foes.append((d, t, ws, wgt))
        if not foes:
            return None
        foes.sort(key=lambda f: f[0])
        foes = foes[:4]
        h = self.height_of(me.get("cubes", 0))
        mh = me["stats"].get("muzzle_height", 7)

        house = self.prac_on
        gtab, ltab = (GUN_HIT_HOUSE, LASER_HIT_HOUSE) if house else (GUN_HIT_TEAM, LASER_HIT_TEAM)
        gdps = GUN_DPS_HOUSE if house else GUN_DPS_TEAM

        def taken(cx, cy):
            cz = self.walk_z(cx, cy)
            body = (cx, cy, cz + h * 0.6)
            tot = 0.0
            for _, t, ws, wgt in foes:
                d = hyp(t.pos[0] - cx, t.pos[1] - cy)
                if not W.los(self.eye(t), body):
                    continue
                r = gdps * band(gtab, d) if d < 300 else 0.0
                if "laser" in ws and d < 280:
                    r += LASER_DPS * band(ltab, d)
                if "lightning" in ws and d < 32:
                    r += 120.0
                tot += r * wgt
            return tot * 1.2

        def dealt(cx, cy):
            if target is None or target.kind != "mech":
                return 0.0
            d = hyp(target.pos[0] - cx, target.pos[1] - cy)
            if not W.los((cx, cy, self.walk_z(cx, cy) + mh), self.center(target)):
                return 0.0
            r = 0.0
            if "gun" in weapons:
                r += 110.0 * (0.93 if d < 60 else 0.84 if d < 100 else 0.64 if d < 150 else 0.22 if d < 200 else 0.0)
            if "laser" in weapons and d < 280:
                r += 120.0
            if "lightning" in weapons and d < 30:
                r += 120.0
            return r * 1.2

        step = min(walk * 1.2, 10.0)
        n0 = hyp(mx, my)
        ux, uy = (mx / n0, my / n0) if n0 > 1e-6 else (0.0, 0.0)
        cands = [(mx, my)] if n0 > 1e-6 else []
        cands.append((0.0, 0.0))
        for k in range(16):
            a = k * TAU / 16
            cands.append((math.cos(a), math.sin(a)))
        best = None
        base_sc = None
        for cx_, cy_ in cands:
            n_ = hyp(cx_, cy_)
            px, py = x + cx_ * step, y + cy_ * step
            if n_ > 1e-6 and not W.walk_clear((x, y), (x + cx_ / n_ * 6, y + cy_ / n_ * 6)):
                continue
            prog = (cx_ * ux + cy_ * uy) * step
            sc = (taken(px, py) - dealt(px, py)) / PLAN - prog + self.zone_penalty(z, px, py, now) * 0.3
            if (cx_, cy_) == (mx, my):
                base_sc = sc
            if best is None or sc < best[0]:
                best = (sc, cx_, cy_)
        if best is None or base_sc is None or best[0] > base_sc - 3.0:
            return None       # keep the mode's choice unless another step is clearly better
        return best[1], best[2]

    def withdraw(self, o, seen):
        """Step away from the centroid of nearby enemies along a walkable, in-zone direction."""
        me = o["self"]
        x, y = me["pos"][0], me["pos"][1]
        near = [t for t in seen if hyp(t.pos[0] - x, t.pos[1] - y) < 200]
        if not near:
            return None
        cx = sum(t.pos[0] for t in near) / len(near) - x
        cy = sum(t.pos[1] for t in near) / len(near) - y
        base = math.atan2(-cy, -cx)
        best = None
        for off in (0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.6, -1.6):
            a = base + off
            ex, ey = x + math.cos(a) * 12, y + math.sin(a) * 12
            if not self.W.walk_clear((x, y), (ex, ey)):
                continue
            pen = self.zone_penalty(o["zone"], ex, ey, self.now)
            sc = abs(off) * 10 + pen
            if best is None or sc < best[0]:
                best = (sc, math.cos(a), math.sin(a))
        return (best[1], best[2]) if best else None

    def travel_cover(self, me):
        if self.cover_goal is None:
            return None
        if hyp(self.cover_goal[0] - me["pos"][0], self.cover_goal[1] - me["pos"][1]) < 1.5:
            return 0.0, 0.0
        return self.travel(me, self.cover_goal[0], self.cover_goal[1])

    def dodge(self, o):
        me = o["self"]
        cx, cy, cz = me["pos"][0], me["pos"][1], me["pos"][2] + 9 * 0.56
        reach = me["stats"].get("radius", 2.5) + 1.1
        dx = dy = 0.0
        laser = False
        for p in o["projectiles"]:
            if p["kind"] == "mortar":
                continue
            v = p["vel"]
            v2 = v[0] * v[0] + v[1] * v[1] + v[2] * v[2]
            if v2 < 1:
                continue
            pp = p["pos"]
            tc = ((cx - pp[0]) * v[0] + (cy - pp[1]) * v[1] + (cz - pp[2]) * v[2]) / v2
            if tc <= 0 or tc > 0.7:
                continue
            mxx, myy = pp[0] + v[0] * tc - cx, pp[1] + v[1] * tc - cy
            if hyp(mxx, myy) < reach:
                vh = hyp(v[0], v[1])
                if vh < 1:
                    continue
                px, py = -v[1] / vh, v[0] / vh
                if px * mxx + py * myy > 0:
                    px, py = -px, -py
                dx += px
                dy += py
        for m in o["mechs"]:
            if m["kind"] != "mech" or not m.get("charging"):
                continue
            to_me = math.atan2(cy - m["pos"][1], cx - m["pos"][0])
            if abs(wrap(m["aim"][0] - to_me)) > 0.15:
                continue
            dx += math.cos(to_me + math.pi / 2 * self.strafe) * 1.5
            dy += math.sin(to_me + math.pi / 2 * self.strafe) * 1.5
            laser = True
        n = hyp(dx, dy)
        if n > 0.01:
            return dx / n, dy / n, laser
        return 0.0, 0.0, False

    def lead(self, muzzle, tp, tv, speed, grounded):
        speed = speed or 252
        t = 0.0
        px = py = pz = 0.0
        for _ in range(4):
            tt = LAT + t
            px, py, pz = tp[0] + tv[0] * tt, tp[1] + tv[1] * tt, tp[2]
            if not grounded:
                pz += tv[2] * tt * 0.5
            d = math.sqrt((px - muzzle[0]) ** 2 + (py - muzzle[1]) ** 2 + (pz - muzzle[2]) ** 2)
            t = max(0.0, d - 3) / speed
        return math.atan2(py - muzzle[1], px - muzzle[0]), math.atan2(pz - muzzle[2], hyp(px - muzzle[0], py - muzzle[1]))

    def near_pick(self, me, slots, x, y, zf, r=25.0):
        """Closest wanted weapon pickup within r m at our level (visual review: fights began 5-15 m from a laser)."""
        best = None
        for p in self.pickups.values():
            d = hyp(p["pos"][0] - x, p["pos"][1] - y)
            if d < r and abs(p["pos"][2] - zf) < 3.5 and self.want_pickup(me, p.get("kind"), slots):
                r, best = d, p["pos"]
        return best

    def want_pickup(self, me, kind, slots):
        if kind == "gun":
            return slots[0] is None
        if any(w and w["kind"] == kind for w in slots):
            return False
        if kind == "mortar":
            return False
        rank = {"laser": 4, "lightning": 3, "mortar": 1}
        cur = rank.get(slots[1]["kind"], 0) if len(slots) > 1 and slots[1] else 0
        return rank.get(kind, 0) > cur

    def home(self, o):
        z = o["zone"]
        nc, nr = z["next_center"], z["next_radius"]
        me = o["self"]
        out = self.home_xy is None or hyp(self.home_xy[0] - nc[0], self.home_xy[1] - nc[1]) > nr * 0.8
        if out or self.now - self.home_at > 12:
            self.home_at = self.now

            def cost(px, py):
                crowd = 0.0
                for t in self.tracks.values():
                    if self.now - t.seen < 20:
                        crowd += max(0.0, 80 - hyp(t.pos[0] - px, t.pos[1] - py)) / 80
                edge = hyp(px - nc[0], py - nc[1]) / max(30, nr)
                wall = 0 if self.near_wall(px, py, 6) else 30
                return wall + 0.25 * hyp(px - me["pos"][0], py - me["pos"][1]) + 40 * crowd + 10 * edge
            best = None
            for _ in range(30):
                a = self.rng.random() * TAU
                r = math.sqrt(self.rng.random()) * nr * 0.7
                px, py = nc[0] + math.cos(a) * r, nc[1] + math.sin(a) * r
                if not self.W.in_bounds(px, py, 10) or not self.W.free_at(px, py):
                    continue
                c = cost(px, py)
                if best is None or c < best[0]:
                    best = (c, px, py)
            if best and (out or best[0] < cost(*self.home_xy) - 15):
                self.home_xy = (best[1], best[2])
        return self.home_xy

    def choose_loot(self, o, hp_frac, danger, slots):
        me = o["self"]
        x, y = me["pos"][0], me["pos"][1]
        z = o["zone"]
        now = self.now
        cur = self.cur_zone(z, now)
        best = None
        P = self.P
        W = self.W

        unarmed_ = slots[1] is None
        my_comp = W.comp_at(x, y)
        if my_comp < 0:
            my_comp = W.comp_main

        keep_off = (X_UNARMED if unarmed_ else X_AVOID) if not self.prac_on else 0.0

        def consider(kind, oid, pos, value):
            nonlocal best
            if self.bad.get(oid, -1) > now:
                return
            ko = keep_off
            if ko and kind == "pickup" and unarmed_:
                ko = 65.0     # a first weapon is worth going close for, just not inside a 60 m bubble
            if ko and any(now - t.seen < 10 and hyp(t.pos[0] - pos[0], t.pos[1] - pos[1]) < ko
                          for t in self.tracks.values()):
                return        # someone is on it: going there starts the fight we are avoiding
            d = hyp(pos[0] - x, pos[1] - y)
            if d > 260 or hyp(pos[0] - cur[0], pos[1] - cur[1]) > cur[2] - 6:
                return
            if now + d * 1.4 / me["stats"].get("walk_speed", 11.7) + 5 > self.zone_deadline(z, pos[0], pos[1], 6):
                return
            if pos[2] - W.terrain(pos[0], pos[1]) > 3.2:
                return
            if W.comp_at(pos[0], pos[1]) != my_comp:
                return
            u = value / (d + 18)
            if oid == self.loot_id:
                u *= 1.35
            if best is None or u > best[0]:
                best = (u, kind, oid, pos)
        unarmed = slots[1] is None
        for p in self.pickups.values():
            rad = P["loot_r"]
            seek = p["kind"] == "laser" and P["laser_seek"] > rad
            if seek:
                rad = P["laser_seek"]
            far = X_SEEK and unarmed and p["kind"] in ("laser", "lightning")
            if far:
                rad = max(rad, X_SEEK)
            if self.want_pickup(me, p["kind"], slots) and hyp(p["pos"][0] - x, p["pos"][1] - y) < rad:
                v = 130.0 if slots[1] is None else 60.0
                if seek:
                    v = max(v, 150.0)
                if far:
                    # league review: over a quarter of rival bot deaths are unarmed ones around 50 s, worth 3.7 points
                    v = 330.0 if p["kind"] == "laser" else 260.0
                    if any(now - t.seen < 10 and hyp(t.pos[0] - p["pos"][0], t.pos[1] - p["pos"][1]) < 65
                           for t in self.tracks.values()):
                        continue      # inside someone's 60 m bubble: not unarmed
                consider("pickup", p["id"], p["pos"], v)
        cube_v = 75.0 if me.get("cubes", 0) < self.cfg["cubes"]["stat_cap"] else 25.0
        for c in self.cubes.values():
            if hyp(c["pos"][0] - x, c["pos"][1] - y) < max(60.0, P["loot_r"] * 0.7):
                consider("cube", c["id"], c["pos"], cube_v)
        crate_any = X_EXPLORE and unarmed and not self.prac_on and not danger
        if (hp_frac > P["farm_hp"] and not danger and now < P["crates_until"]) or crate_any:
            for c in self.crates.values():
                if hyp(c["pos"][0] - x, c["pos"][1] - y) < (150.0 if crate_any else P["loot_r"]):
                    consider("crate", c["id"], c["pos"], 110.0 if crate_any else cube_v * 0.6)
            if me.get("cubes", 0) < self.cfg["cubes"]["stat_cap"]:
                for n in self.npcs.values():
                    if now - n.seen < 12 and hyp(n.pos[0] - x, n.pos[1] - y) < P["loot_r"]:
                        consider("npc", n.id, n.pos, 85.0)
        if (USE_DROPS or (X_DROPS and not self.prac_on)) and not danger:
            for d in o.get("drops") or []:
                dd = hyp(d["pos"][0] - x, d["pos"][1] - y)
                if d.get("holder") not in (None, self.slot) and d.get("lands_in", 0) <= 0:
                    continue
                if dd / me["stats"].get("walk_speed", 11.7) < d.get("lands_in", 0) + 25:
                    consider("drop", d["id"], d["pos"], 260.0)
        if best is None:
            self.loot_id = None
            return None
        if best[2] != self.loot_id:
            self.loot_id = best[2]
            self.loot_since = now
        elif now - self.loot_since > 30:
            self.bad[best[2]] = now + 60
            self.loot_id = None
            return None
        return best

    # ------------------------------------------------------------------ observation
    def observe(self, o):
        me = o["self"]
        now = o["t"]
        W = self.W
        mh = me["stats"].get("muzzle_height", 7)
        muzzle = (me["pos"][0], me["pos"][1], me["pos"][2] + mh)
        eye = (me["pos"][0], me["pos"][1], me["pos"][2] + me["stats"].get("eye_height", 8))
        for m in o["mechs"]:
            tab = self.npcs if m["kind"] == "npc" else self.tracks
            t = tab.get(m["id"])
            if t is None:
                t = Track(m["id"])
                tab[m["id"]] = t
            t.kind = m["kind"]
            t.pos = m["pos"]
            t.vel = m.get("vel", [0, 0, 0])
            t.aim = m.get("aim", [0, 0])
            t.hp = m.get("hp", 1400)
            t.max_hp = m.get("max_hp", 1400)
            t.cubes = m.get("cubes", 0)
            t.weapons = m.get("weapons", [])
            t.charging = m.get("charging", False)
            t.seen = now
            dx, dy = t.pos[0] - me["pos"][0], t.pos[1] - me["pos"][1]
            t.dist = hyp(dx, dy)
            t.los = W.los(muzzle, self.center(t))
            if not t.los and t.kind == "mech":
                t.los = W.los(muzzle, (t.pos[0], t.pos[1], t.pos[2] + self.height_of(t.cubes) * 0.85))
            t.closing = 0.0
            if t.dist > 1:
                t.closing = -((t.vel[0] - me["vel"][0]) * dx + (t.vel[1] - me["vel"][1]) * dy) / t.dist
        for tab, keep in ((self.tracks, 20), (self.npcs, 120)):
            for k in [k for k, t in tab.items() if now - t.seen > keep]:
                del tab[k]

        def upd(tab, items):
            for it in items:
                k = tab.setdefault(it["id"], {"id": it["id"]})
                k["pos"] = it["pos"]
                k["kind"] = it.get("kind")
                k["seen"] = now
            for oid in [oid for oid, k in tab.items() if k["seen"] != now]:
                k = tab[oid]
                d = hyp(k["pos"][0] - me["pos"][0], k["pos"][1] - me["pos"][1])
                if d < 40 and now - k["seen"] > 0.35 and W.los(eye, (k["pos"][0], k["pos"][1], k["pos"][2] + 1.5)):
                    del tab[oid]
        upd(self.crates, o["crates"])
        upd(self.cubes, o["cubes"])
        upd(self.pickups, o["pickups"])
        for e in o["events"]:
            et = e["type"]
            if et == "damaged":
                if e.get("weapon") == "npc" and e.get("bearing") is not None:
                    self.npc_hurt, self.npc_bearing = now, e["bearing"]
                if e.get("by") is not None:
                    self.last_hurt = now
                    if e.get("bearing") is not None:
                        self.hurt_bearing = e["bearing"]
                    t = self.tracks.get(e["by"]) or self.npcs.get(e["by"])
                    if t is not None:
                        t.hit_me = now
            elif et == "kill":
                self.reset_until = now + RESET
                self.kills += 1
                self.tracks.pop(e.get("target"), None)
        # free-for-all detection: deaths we did not cause (house squads only die to us)
        if self.start_alive is None:
            self.start_alive = o["alive"]
        if self.start_alive - o["alive"] - self.kills >= X_FFA_N:
            self.other_dead += 1
        else:
            self.other_dead = 0
        if self.other_dead >= 5:
            self.ffa = True
        on = not self.ffa and now > X_PRACT
        if on != self.prac_on:
            self.prac_on = on
            self.P = dict(SQUAD) if on else dict(FFA)
        opening = not self.ffa and not self.prac_on and self.start_alive - o["alive"] - self.kills <= 0
        if opening != self.opening:
            self.opening = opening
            if not self.prac_on:
                self.P = dict(FFA)
                if opening and OPENING_HUNT:
                    self.P.update(laser_seek=240.0, loot_r=150.0)
        z = o["zone"]
        if self.hold_start != z["shrink_start"]:
            self.hold_start = z["shrink_start"]
            self.hold = (z["center"][0], z["center"][1], z["radius"])
        elif now <= z["shrink_start"]:
            self.hold = (z["center"][0], z["center"][1], z["radius"])

    # ------------------------------------------------------------------ decision
    def act(self, o):
        t0 = time.perf_counter()
        act = {"tick": o["tick"]}
        self.now = now = o["t"]
        self.observe(o)
        me = o["self"]
        P = self.P
        W = self.W
        st = me["stats"]
        x, y, zf = me["pos"]
        z = o["zone"]
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        slots = me["weapons"]
        weapons = {w["kind"]: w for w in slots if w}
        gun, light, laser = weapons.get("gun"), weapons.get("lightning"), weapons.get("laser")
        late = z["next_radius"] <= 70 or o["alive"] <= 3

        if o.get("offer") and o["offer"].get("choices"):
            order = UPG_LASER if self.prac_on else UPG_BASE
            if UPG_ORDER == "damage":
                order = UPG_DAMAGE
            ch = o["offer"]["choices"]
            act["pick"] = min(range(len(ch)), key=lambda i: order.index(ch[i]["id"]) if ch[i]["id"] in order else 99)

        # ---- engagement and target
        seen = []
        target = hidden = None
        best_s = 1e18
        for t in self.tracks.values():
            if now - t.seen > 2.5:
                continue
            t.engage = False
            seen.append(t)
            if t.seen != now:
                t.los = False
                continue
            to_me = math.atan2(y - t.pos[1], x - t.pos[0])
            aimed = abs(wrap(t.aim[0] - to_me)) < 0.22
            if P["ghost"]:
                t.engage = (t.dist < P["engage"] or now - t.hit_me < 4 or (aimed and t.charging and t.dist < 120)
                            or (t.hp < 350 and t.dist < 120) or o["alive"] <= 2)
            elif t.dist < P["engage"] or now - t.hit_me < 6 or late:
                t.engage = True
            elif t.closing > 3 and t.dist < 180:
                t.engage = True
            elif t.hp < 0.4 * t.max_hp and t.dist < 160:
                t.engage = True
            elif aimed and t.dist < 190:
                t.engage = True
            if t.engage and not t.los and t.dist < P["max_t"] and (hidden is None or t.dist < hidden.dist):
                hidden = t
            if not t.engage or not t.los or t.dist > P["max_t"]:
                continue
            s = t.dist + P["hpw"] * t.hp
            if P["low_bonus"] and t.hp <= 0.3 * t.max_hp:
                s -= P["low_bonus"]
            if P["stick"] and t.id == self.stick_id and now - self.stick_at < 2:
                s -= P["stick"]
            if now - t.hit_me < 4:
                s -= 40
            if t.closing > 3:
                s -= 15
            if s < best_s:
                best_s, target = s, t
        if target is not None:
            self.stick_id, self.stick_at = target.id, now
        foe = target
        if foe is None and P["peek"]:
            foe = hidden
        danger = False
        crowd = 0
        for t in seen:
            if t.engage or now - t.hit_me < 4:
                danger = True
            if t.seen == now and (t.engage or (t.los and t.dist < 150)):
                crowd += 1
        if danger or now - self.last_hurt < 3:
            self.last_threat = now
        finishing = target is not None and target.dist < 120 and (target.hp < 200 or target.hp < me["hp"] * 0.5)
        outgunned = target is not None and "laser" in (target.weapons or ()) and laser is None \
            and target.dist > 55 and hp_frac < 0.8
        swamped = crowd >= CROWD_N or (crowd == CROWD_N - 1 and hp_frac < 0.65) or outgunned
        if not self.retreat and ((hp_frac < P["retreat_hp"] and now - self.last_threat < 2) or swamped):
            self.retreat = True
            self.cover_goal = None
        if self.retreat and not swamped and (hp_frac > P["recover_hp"] or (now - self.last_threat > 7 and hp_frac > 0.55)):
            self.retreat = False
        if P["lose_rule"]:
            near = sum(1 for t in seen if t.seen == now and t.dist < 160)
            losing = (near >= 2 and hp_frac < 0.55) or (near >= 3 and hp_frac < 0.8)
            if target is not None:
                tf = target.hp / max(1.0, target.max_hp)
                if hp_frac < 0.35 and tf > hp_frac + 0.1:
                    losing = True
                if tf < 0.25 and hp_frac > 0.2 and near <= 2:
                    losing = False
            if losing:
                self.lose_at = now
            self.retreat = self.lose_at > 0 and now - self.lose_at < 1.5
        if RESET and self.prac_on and now < self.reset_until and not finishing:
            # practice review: each kill brings another hunter and HP per kill doubles with each extra attacker;
            # get out of sight before the next one arrives so they come to us one at a time
            self.retreat = True

        # A rival bot keeps fighting only while we are inside 60 m, hit it in the last 4 s, or sit under 350 HP inside 120 m.
        # So a fight we are losing, or one with two of them, ends if we leave and hold fire.
        brk = False
        if X_BREAK and not self.prac_on and not late and not finishing:
            hitters = sum(1 for t in seen if now - t.hit_me < 4)
            tf = target.hp / max(1.0, target.max_hp) if target is not None else 1.0
            fair = hitters >= 1 and not (tf < hp_frac - 0.25 or (target is not None and target.hp < 450))
            if hitters >= 2 or (hitters >= 1 and target is not None and hp_frac < 0.6 and tf > hp_frac + 0.05) \
                    or (hitters >= 1 and me["hp"] < 520) or (X_BREAK >= 2 and fair):
                self.break_until = now + 5.0
            elif now < self.break_until and (any(now - t.seen < 3 and t.dist < 90 for t in seen) or now - self.last_hurt < 3):
                self.break_until = max(self.break_until, now + 2.0)
            brk = now < self.break_until
            if brk:
                self.retreat = True

        # ---- movement mode
        mx = my = 0.0
        boost = False
        mode = "idle"
        farm = None
        farm_pos = None
        zgx, zgy = self.zone_goal(z, x, y)
        zdist = hyp(zgx - x, zgy - y)
        walk = st.get("walk_speed", 11.7)
        t_need = zdist * 1.45 / walk + 4
        deadline = self.zone_deadline(z, x, y, 8)
        zone_must = not me["in_zone"] or now + t_need + 3 >= deadline
        zone_should = now + t_need + 14 >= deadline

        cm = None
        if now < self.unstuck_until:
            mx, my = self.unstuck_dir
            mode = "unstuck"
        elif zone_must and zdist > 2:
            mx, my = self.travel(me, zgx, zgy)
            boost = (not me["in_zone"] or now + t_need >= deadline) and me["energy"] > 5
            mode = "zone"
        elif self.retreat and not finishing and not late:
            cm = self.cover_move(o, seen)
            if cm is not None:
                mx, my = cm
                boost = me["energy"] > 10 and (mx or my)
                mode = "cover"
            elif target is not None:
                w = None
                if V_CORNER and (self.prac_on or not V_GATE) and sum(1 for t in seen if t.seen == now and t.dist < 160) >= 2:
                    w = self.withdraw(o, seen)
                if w is not None:
                    mx, my = w
                    boost = me["energy"] > 25
                    mode = "withdraw"
                else:
                    mx, my = self.fight_move(o, target, seen, 1, weapons)
                    mode = "cornered"
            else:
                mx, my = self.travel(me, zgx, zgy)
                mode = "flee"
                if WITHDRAW and not (mx or my):
                    w = self.withdraw(o, seen)
                    if w is not None:
                        mx, my = w
                        boost = me["energy"] > 40
                        mode = "withdraw"
        elif target is None and now - self.last_hurt < 2.5 and not late and P["ghost"]:
            ghost = Track(-1)
            ghost.kind = "mech"
            ghost.cubes = 0
            ghost.pos = [x + math.cos(self.hurt_bearing) * 80, y + math.sin(self.hurt_bearing) * 80, zf]
            cm = self.cover_move(o, [ghost] + seen)
            if cm is not None:
                mx, my = cm
                boost = me["energy"] > 30 and (mx or my)
                mode = "hide"
        if mode == "idle" and foe is not None:
            ready = gun is not None and gun.get("ammo", 0) > 0 and gun.get("reload", 0) <= 0.25 \
                and foe.dist <= gun["stats"]["range"] - 10
            hot = laser is not None and laser.get("charge", 0) >= laser["stats"]["charge"] - 0.3 \
                and foe.dist <= laser["stats"]["range"] - 5
            if hot or (light is not None and foe.dist <= light["stats"]["range"] + 6
                       and me["energy"] >= light["stats"]["energy"] * 3):
                ready = True
            if not P["peek"] and (light is not None or laser is not None):
                ready = True
            want_los = 1.0 if ready else -1.0
            if P["peek"] and not hot:
                for t in seen:
                    if t.seen == now and t.charging and abs(wrap(t.aim[0] - math.atan2(y - t.pos[1], x - t.pos[0]))) < 0.3:
                        want_los = -1.6
            if P["finish"] and foe.hp < 0.38 * foe.max_hp and hp_frac > 0.42 and crowd <= 2:
                keep = (P["gun_r"], P["laser_r"], P["light_r"])
                P["gun_r"] = P["laser_r"] = P["light_r"] = P["finish"]
                mx, my = self.fight_move(o, foe, seen, want_los, weapons)
                P["gun_r"], P["laser_r"], P["light_r"] = keep
            else:
                mx, my = self.fight_move(o, foe, seen, want_los, weapons)
            if light is not None and foe.dist > 50 and me["energy"] > 55 and want_los > 0:
                boost = True
            mode = "fight"
            pk = self.near_pick(me, slots, x, y, zf) if V_PICK else None
            if pk is not None:
                mx, my = self.travel(me, pk[0], pk[1])
                mode = "fight:pick"
            elif P["fight_loot"]:
                r = P["fight_loot"] * (0.57 if hp_frac <= 0.5 else 1.0)
                bc = None
                for c in self.cubes.values():
                    d = hyp(c["pos"][0] - x, c["pos"][1] - y)
                    if d < r:
                        r, bc = d, c["pos"]
                if bc is not None:
                    mx, my = self.travel(me, bc[0], bc[1])
                    mode = "fight:cube"
        elif mode == "idle" and zone_should and zdist > 2:
            mx, my = self.travel(me, zgx, zgy)
            mode = "zone"
        elif mode == "idle":
            lc = self.choose_loot(o, hp_frac, danger, slots)
            if lc is not None:
                _, kind, oid, pos = lc
                mode = "loot:" + kind
                if kind == "npc":
                    n = self.npcs.get(oid)
                    if n is not None and now - n.seen < 1 and n.los and n.dist < 75:
                        farm = n
                        mx, my = self.fight_move(o, n, seen, 1, weapons)
                    else:
                        mx, my = self.travel(me, pos[0], pos[1])
                elif kind == "crate":
                    d = hyp(pos[0] - x, pos[1] - y)
                    cp = (pos[0], pos[1], pos[2] + self.cfg["crates"]["height"] * 0.5)
                    if d < 50 and W.los((x, y, zf + st.get("muzzle_height", 7)), cp):
                        farm_pos = cp
                        if d < 12:
                            mx, my = (x - pos[0]) / max(d, 0.1), (y - pos[1]) / max(d, 0.1)
                    else:
                        mx, my = self.travel(me, pos[0], pos[1])
                else:
                    mx, my = self.travel(me, pos[0], pos[1])
                    if kind == "drop" and hyp(pos[0] - x, pos[1] - y) < 2.5:
                        mx = my = 0.0
            elif zdist > 3 and (not P["ghost"] or z["next_radius"] <= 30):
                mx, my = self.travel(me, zgx, zgy)
                mode = "zone-walk"
            elif P["ghost"] and X_EXPLORE and slots[1] is None and not self.prac_on and not late:
                # a rival bot that never finds a weapon holds its spot and dies unarmed (16 of 60 league matches): go look
                ex = getattr(self, "explore", None)
                if ex is None or now > self.explore_until or hyp(ex[0] - x, ex[1] - y) < 8:
                    ex = None
                    for _ in range(20):
                        a_ = self.rng.random() * TAU
                        r_ = math.sqrt(self.rng.random()) * z["next_radius"] * 0.8
                        px_, py_ = z["next_center"][0] + math.cos(a_) * r_, z["next_center"][1] + math.sin(a_) * r_
                        dd_ = hyp(px_ - x, py_ - y)
                        if dd_ < 50 or dd_ > 220 or not W.in_bounds(px_, py_, 5) or not W.free_at(px_, py_):
                            continue
                        if any(now - t.seen < 15 and hyp(t.pos[0] - px_, t.pos[1] - py_) < 110 for t in self.tracks.values()):
                            continue
                        ex = (px_, py_)
                        break
                    self.explore = ex
                    self.explore_until = now + 30
                if ex is not None:
                    mx, my = self.travel(me, ex[0], ex[1])
                    mode = "explore"
            elif P["ghost"]:
                h = self.home(o)
                if h and hyp(h[0] - x, h[1] - y) > 3:
                    mx, my = self.travel(me, h[0], h[1])
                    if not danger and now - self.last_hurt > 6:
                        q = min(1.0, 4.6 / walk)
                        mx, my = mx * q, my * q
                    mode = "home"

        if brk and mode not in ("unstuck", "zone"):
            # distance ends the fight, cover does not: a spot behind a wall 60 m from a rival bot that is still on us is a
            # place to be walked around and shot (review: 14 s in "cover" at 85 m, 1766 HP lost, 428 dealt)
            rx = ry = 0.0
            for t in self.tracks.values():
                if now - t.seen > 10:
                    continue
                dm = hyp(t.pos[0] - x, t.pos[1] - y)
                if dm < 150:
                    w = 0.3 + (150 - dm) / 150
                    rx += (x - t.pos[0]) / max(dm, 1.0) * w
                    ry += (y - t.pos[1]) / max(dm, 1.0) * w
            if not (rx or ry) and now - self.last_hurt < 4:
                rx, ry = -math.cos(self.hurt_bearing), -math.sin(self.hurt_bearing)
            if rx or ry:
                n = hyp(rx, ry)
                ax, ay = rx / n, ry / n
                if self.zone_penalty(z, x + ax * 25, y + ay * 25, now) > 40:
                    tx_, ty_ = -ay, ax
                    if self.zone_penalty(z, x + tx_ * 25, y + ty_ * 25, now) > self.zone_penalty(z, x - tx_ * 25, y - ty_ * 25, now):
                        tx_, ty_ = -tx_, -ty_
                    ax, ay = tx_, ty_
                if not W.walk_clear((x, y), (x + ax * 6, y + ay * 6)):
                    for da in (0.5, -0.5, 1.0, -1.0, 1.5, -1.5):
                        ca, sa = math.cos(da), math.sin(da)
                        bx_, by_ = ax * ca - ay * sa, ax * sa + ay * ca
                        if W.walk_clear((x, y), (x + bx_ * 6, y + by_ * 6)):
                            ax, ay = bx_, by_
                            break
                mx, my = ax, ay
                boost = me["energy"] > 5
                mode = "break"
        # free-for-all spacing: rival bots start a fight at 60 m. Unarmed we lose it (league: dealt/taken 0.38), so keep clear.
        close_pick = V_PICK and mode == "loot:pickup" and self.near_pick(me, slots, x, y, zf) is not None
        if not self.prac_on and not close_pick and mode not in ("unstuck", "zone", "cover", "hide", "fight", "fight:cube", "fight:pick", "cornered") \
                and not late:
            R = X_UNARMED if slots[1] is None else X_AVOID
            if R > 0:
                rx = ry = 0.0
                hurry = False
                for t in self.tracks.values():
                    if now - t.seen > 8:
                        continue
                    dm = hyp(t.pos[0] - x, t.pos[1] - y)
                    if dm < R:
                        w = 0.35 + (R - dm) / R
                        rx += (x - t.pos[0]) / max(dm, 1.0) * w
                        ry += (y - t.pos[1]) / max(dm, 1.0) * w
                        if dm < R - 8 or t.closing > 4:
                            hurry = True
                if rx or ry:
                    n = hyp(rx, ry)
                    ax, ay = rx / n, ry / n
                    # do not back out of the circle: slide along its edge instead
                    if self.zone_penalty(z, x + ax * 25, y + ay * 25, now) > 40:
                        tx_, ty_ = -ay, ax
                        if self.zone_penalty(z, x + tx_ * 25, y + ty_ * 25, now) > self.zone_penalty(z, x - tx_ * 25, y - ty_ * 25, now):
                            tx_, ty_ = -tx_, -ty_
                        ax, ay = tx_, ty_
                    if not W.walk_clear((x, y), (x + ax * 6, y + ay * 6)):
                        # review: "+space" walked into walls and stood there (stalls of 5 to 80 s)
                        for da in (0.5, -0.5, 1.0, -1.0, 1.5, -1.5):
                            ca, sa = math.cos(da), math.sin(da)
                            bx_, by_ = ax * ca - ay * sa, ax * sa + ay * ca
                            if W.walk_clear((x, y), (x + bx_ * 6, y + by_ * 6)):
                                ax, ay = bx_, by_
                                break
                    mx, my = ax, ay
                    boost = hurry and me["energy"] > 20
                    farm = None
                    farm_pos = None
                    mode += "+space"
        if X_HUSH and not self.prac_on and not close_pick and now > 19 and (not self.ffa or (self.start_alive - o["alive"] < 2 and now < 60)) \
                and mode not in ("unstuck", "zone", "cover", "hide", "fight", "fight:cube", "fight:pick", "cornered", "break"):
            # from 20 s until the first death every rival bot attacks whatever closes on it at over 3 m/s inside 180 m
            # (review: 4 of 10 deaths before 40 s were lasers from 110-120 m while we ran for a pickup). Its own walk
            # toward us counts, so the limit is on the relative speed: slow down, sidestep or back away to stay under it.
            cons = []
            for t in self.tracks.values():
                if now - t.seen > 0.35 or t.dist > 190 or t.dist < 1:
                    continue
                ux, uy = (t.pos[0] - x) / t.dist, (t.pos[1] - y) / t.dist
                cons.append((ux, uy, 2.2 + t.vel[0] * ux + t.vel[1] * uy))
            if cons:
                sp = walk * (1.5 if boost else 1.0)
                wx, wy = mx * sp, my * sp
                best = None
                cands = [(wx, wy), (wx * 0.5, wy * 0.5), (wx * 0.2, wy * 0.2), (0.0, 0.0)]
                for k in range(16):
                    a_ = k * TAU / 16
                    for q_ in (1.0, 0.5):
                        cands.append((math.cos(a_) * walk * q_, math.sin(a_) * walk * q_))
                for cx_, cy_ in cands:
                    viol = sum(max(0.0, cx_ * ux + cy_ * uy - c) for ux, uy, c in cons)
                    pen = 0.0
                    if cx_ or cy_:
                        n_ = hyp(cx_, cy_)
                        if not W.walk_clear((x, y), (x + cx_ / n_ * 5, y + cy_ / n_ * 5)):
                            pen = 4.0
                        elif self.zone_penalty(z, x + cx_ / n_ * 20, y + cy_ / n_ * 20, now) > 40:
                            pen = 4.0
                    sc = viol * 3.0 + hyp(cx_ - wx, cy_ - wy) / walk + pen
                    if best is None or sc < best[0]:
                        best = (sc, cx_, cy_)
                if (best[1], best[2]) != (wx, wy):
                    mx, my = best[1] / walk, best[2] / walk
                    n_ = hyp(mx, my)
                    if n_ > 1:
                        mx, my = mx / n_, my / n_
                    boost = False
                    mode += "+hush"
        if PLAN and mode not in ("unstuck", "zone", "cover", "hide"):
            pm = self.plan_move(o, mx, my, target, weapons, walk, z)
            if pm is not None:
                mx, my = pm
                mode += "+plan"
        if mode not in ("unstuck",):
            dx, dy, lz = self.dodge(o)
            if dx or dy:
                mx, my = mx * 0.4 + dx * 0.9, my * 0.4 + dy * 0.9
                n = hyp(mx, my)
                if n > 1:
                    mx, my = mx / n, my / n
                if lz and me["energy"] > 25:
                    boost = True

        jump = False
        if (mx or my) and hyp(me["vel"][0], me["vel"][1]) < 1.2 and me["grounded"]:
            if self.stuck_since < 0:
                self.stuck_since = now
            elif now - self.stuck_since > 0.7:
                a = self.rng.random() * TAU
                self.unstuck_dir = (math.cos(a), math.sin(a))
                self.unstuck_until = now + 0.5
                self.stuck_since = -1
                self.path = None
                jump = True
        else:
            self.stuck_since = -1

        # ---- aim and fire
        fire = [False, False]
        mh = st.get("muzzle_height", 7)
        muzzle = (x + me["vel"][0] * LAT, y + me["vel"][1] * LAT, zf + mh)
        aim = [me["aim"][0], 0.0]
        have_aim = False
        shootable = is_mech = defend = False
        if target is None and now - self.npc_hurt < 3:
            worst = None
            best_off = 0.7
            for n in self.npcs.values():
                if now - n.seen > 0.5 or not n.los or n.dist > 150:
                    continue
                off = 0.0 if now - n.hit_me < 3 else abs(wrap(math.atan2(n.pos[1] - y, n.pos[0] - x) - self.npc_bearing))
                if off < best_off:
                    best_off, worst = off, n
            if worst is not None:
                farm, farm_pos, defend = worst, None, True
        tp = tv = None
        tr = th = 0.0
        if target is not None:
            tr, th = self.radius_of(target.cubes), self.height_of(target.cubes)
            tp = [target.pos[0], target.pos[1], target.pos[2] + th * 0.5]
            tv = target.vel
            if not W.los(muzzle, tp):
                tp[2] = target.pos[2] + th * 0.85
            shootable = is_mech = True
        elif farm is not None:
            tr, th = self.cfg["npc"]["radius"], self.cfg["npc"]["height"]
            tp = [farm.pos[0], farm.pos[1], farm.pos[2] + th * 0.5]
            tv = farm.vel
            shootable = True
        elif farm_pos is not None:
            tr, th = self.cfg["crates"]["radius"], self.cfg["crates"]["height"]
            tp = list(farm_pos)
            tv = [0, 0, 0]
            shootable = True
        if not is_mech and target is None and (self.retreat or hp_frac < 0.5) and not defend:
            shootable = False
        stealth = X_STEALTH and not self.prac_on and now > 17 and (not self.ffa or (self.start_alive - o["alive"] < 2 and now < 60))
        if stealth and shootable and not is_mech and not defend:
            b0 = math.atan2(tp[1] - y, tp[0] - x)
            if any(now - t.seen < 4 and hyp(t.pos[0] - x, t.pos[1] - y) < 200
                   and abs(wrap(math.atan2(t.pos[1] - y, t.pos[0] - x) - b0)) < 0.3 for t in self.tracks.values()):
                shootable = False     # a crate or drone in line with a mech: shooting it reads as aiming at the mech
        if X_SPIN and not self.prac_on and shootable and not is_mech and not defend:
            # farming keeps the torso on a crate or drone for seconds at a time: break for one turn every 4 s
            if now - self.spin_t0 > 5.7:
                self.spin_t0 = now
            if now - self.spin_t0 > 4.0:
                shootable = False
        if brk and is_mech and target is not None and target.dist > 62 and not (target.hp < 200):
            shootable = False     # outside 60 m every hit we land restarts its 4 s clock on us; inside it shoots anyway
        if shootable:
            dist = hyp(tp[0] - x, tp[1] - y)
            grounded = abs(tv[2]) < 0.5
            gy = gp = 0.0
            gun_ok = False
            if gun is not None:
                gy, gp = self.lead(muzzle, tp, tv, gun["stats"]["speed"], grounded)
                gun_ok = dist <= gun["stats"]["range"] * 0.96
            dp = (tp[0] + tv[0] * (LAT + 0.05), tp[1] + tv[1] * (LAT + 0.05), tp[2])
            hy = math.atan2(dp[1] - muzzle[1], dp[0] - muzzle[0])
            hp = math.atan2(dp[2] - muzzle[2], hyp(dp[0] - muzzle[0], dp[1] - muzzle[1]))
            aim = [gy, gp] if gun is not None else [hy, hp]
            have_aim = True
            tol_y = math.atan2(tr * 0.75, max(dist, 1))
            tol_p = math.atan2(th * 0.4, max(dist, 1))
            tr_rate = st.get("turn_rate", 4) * 0.05
            pr_rate = st.get("pitch_rate", 3) * 0.05

            def slew(wy, wp):
                ey = max(0.0, abs(wrap(wy - me["aim"][0])) - tr_rate)
                ep = max(0.0, abs(wp - me["aim"][1]) - pr_rate)
                return ey < tol_y and ep < tol_p
            for i, w in enumerate(slots[:2]):
                if w is None:
                    continue
                k = w["kind"]
                if k == "gun":
                    if gun_ok and w.get("ammo", 0) > 0 and w.get("reload", 0) <= 0 and slew(gy, gp):
                        fire[i] = True
                elif k == "lightning":
                    cone = math.radians(w["stats"]["cone_deg"])
                    if dist < w["stats"]["range"] - 1.5 and abs(wrap(hy - me["aim"][0])) < cone * 0.5 - 0.05 \
                            and me["energy"] >= w["stats"]["energy"]:
                        fire[i] = True
                elif k == "laser":
                    if not is_mech or dist > w["stats"]["range"] * 0.97:
                        continue
                    full = w["stats"]["charge"]
                    ch = w.get("charge", 0)
                    s_ = w["stats"]
                    released = False
                    if ch >= s_["min_charge"] and ch < full - 0.051:
                        dmg = s_["damage"] * (s_["min_frac"] + (1 - s_["min_frac"]) * ch / full) * \
                            (1 + self.cfg["cubes"]["damage_per_cube"] * min(me.get("cubes", 0), self.cfg["cubes"]["stat_cap"]))
                        if "mortar" in (target.weapons or ()):
                            dmg *= 1 + s_.get("cookoff", 0.6)
                        if dmg * P["lethal_k"] >= target.hp and abs(wrap(hy - me["aim"][0])) < tol_y * 0.9 \
                                and abs(hp - me["aim"][1]) < tol_p:
                            aim = [hy, hp]
                            fire[i] = False
                            released = True
                    if released:
                        continue
                    if ch < full - 0.051:
                        fire[i] = True
                        if ch > full * 0.45:
                            aim = [hy, hp]
                    else:
                        aim = [hy, hp]
                        ey = abs(wrap(hy - me["aim"][0]))
                        ep = abs(hp - me["aim"][1])
                        fire[i] = not (ey < tol_y * 0.9 and ep < tol_p)
        if V_REGEN and (self.prac_on or not V_GATE) and target is not None and hp_frac < 0.5 and now - self.last_hurt > 3 \
                and target.dist > 90 and target.hp > 0.3 * target.max_hp:
            fire = [False, False]
        if target is None and foe is not None:
            # pre-charge the laser before the target steps back into view
            for i, w in enumerate(slots[:2]):
                if w and w["kind"] == "laser" and w["cooldown"] <= 0.05 and foe.dist < w["stats"]["range"] \
                        and w.get("charge", 0) < w["stats"]["charge"] + w["stats"].get("hold_s", 2) - 0.35:
                    fire[i] = True
        if not have_aim:
            watch = None
            for t in seen:
                if t.engage or now - t.hit_me < 6:
                    if watch is None or t.dist < watch.dist:
                        watch = t
            if watch is not None:
                aim[0] = math.atan2(watch.pos[1] - y, watch.pos[0] - x)
            elif now - self.npc_hurt < 2.5:
                aim[0] = self.npc_bearing
            elif now - self.last_hurt < 2.5:
                aim[0] = self.hurt_bearing
            elif (mx or my) and not (X_SPIN and not self.prac_on):
                aim[0] = math.atan2(my, mx) + 1.15 * math.sin(now * 1.3)
            else:
                aim[0] = me["aim"][0] + 0.35      # full circle about every 1.6 s: nothing walks up behind us unseen
                if stealth:
                    # from 20 s until the first death every rival bot fights whatever points its torso at it inside 190 m:
                    # scan back and forth between the mechs we know of instead of across them (they stay inside the cone)
                    brg = [math.atan2(t.pos[1] - y, t.pos[0] - x) for t in self.tracks.values()
                           if now - t.seen < 4 and hyp(t.pos[0] - x, t.pos[1] - y) < 200]
                    ok = False
                    for _ in range(2):
                        nxt = me["aim"][0] + 0.35 * self.scan_dir
                        if all(abs(wrap(nxt - b)) > 0.32 for b in brg):
                            ok = True
                            break
                        self.scan_dir = -self.scan_dir
                    aim[0] = nxt if ok else me["aim"][0]
            aim[1] = 0.0
        aim[1] = clamp(aim[1], st.get("min_pitch", -0.7), st.get("max_pitch", 1.3))

        if gun is not None and gun.get("reload", 0) <= 0 and gun.get("ammo", 0) < gun["stats"]["mag"]:
            if target is None and farm is None and farm_pos is None and \
                    (gun.get("ammo", 0) <= gun["stats"]["mag"] * 0.6 or now - self.last_threat > 3):
                act["reload"] = True

        # pickups within reach
        slot1 = slots[1]["kind"] if len(slots) > 1 and slots[1] else ""
        for p in o["pickups"]:
            if hyp(p["pos"][0] - x, p["pos"][1] - y) > self.cfg["loot"]["pickup_radius"] - 0.3 or abs(p["pos"][2] - zf) > 3.6:
                continue
            if not self.want_pickup(me, p["kind"], slots):
                continue
            if slot1:
                if slot1 == "laser" and slots[1].get("charge", 0) > 0:
                    break
                act["drop"] = 1
            act["take"] = p["id"]
            break

        if X_FLOW:
            if X_DROPS and not self.prac_on:
                for d in o.get("drops") or []:
                    key = (round(d["pos"][0] / 8.0), round(d["pos"][1] / 8.0))
                    if key not in self.flows and len(self.flows) < 3:
                        self.flows[key] = Flow(W, d["pos"])
            spare = 0.016 - (time.perf_counter() - t0)
            if spare > 0.002:
                for fl in list(self.flows.values()):
                    if not fl.done:
                        fl.work(min(spare, 0.008))
                        break
        mf.draw.label(f"{mode} hp{me['hp']:.0f}")
        act["move"] = [round(mx, 3), round(my, 3)]
        act["boost"] = bool(boost and (mx or my))
        act["jump"] = jump
        act["aim"] = [round(aim[0], 4), round(aim[1], 4)]
        act["fire"] = fire
        self.mode = mode
        if DEBUG and o["tick"] % 4 == 0:
            mf.log(f"t{now:.1f} {mode} hp{me['hp']:.0f} tgt{target and target.id} foe{foe and foe.id} crowd{crowd} "
                   f"ret{int(self.retreat)} ffa{int(self.ffa)} {1000 * (time.perf_counter() - t0):.1f}ms")
        return act


if __name__ == "__main__":
    mf.run(Bot)
