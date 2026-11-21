"""Kentucky Fried Evan bot, version 2: our own Python implementation.

Ideas studied from giddygiddyboy's MIT bot (see vendor/giddy) and rebuilt here in our own code on our map model
(worldmap.py): track-based engagement rules, crowd/losing retreat to cover, peek-aware fight positioning, bullet and
laser dodging, latency-aware lead, lethal early laser release, cube grabs during fights, utility-based looting, and a
separate free-for-all policy once the match turns out not to be practice.
"""
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

UPG_LASER = ["plating", "armor", "laser_damage", "laser_charge", "nanites", "gun_damage", "gun_rate",
             "lightning_efficiency", "lightning_range", "servos", "gun_mag", "gun_velocity", "gyros", "lightning_chain",
             "reactor", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity", "sensors", "jets",
             "laser_pierce"]
UPG_BASE = ["plating", "armor", "gun_damage", "nanites", "gun_rate", "lightning_efficiency", "lightning_range",
            "laser_damage", "servos", "laser_charge", "gun_mag", "gun_velocity", "gyros", "lightning_chain", "reactor",
            "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity", "sensors", "jets", "laser_pierce"]


def env(name, default):
    return type(default)(os.environ.get(name, default))


# practice (squad) and free-for-all parameter sets
SQUAD = dict(engage=60.0, laser_r=env("K2_LASER_R", 120.0), gun_r=85.0, light_r=28.0, max_t=240.0, hpw=0.18,
             low_bonus=200.0, stick=200.0, retreat_hp=0.38, recover_hp=0.8, loot_r=150.0, laser_seek=240.0,
             fight_loot=70.0, farm_hp=0.5, crates_until=1e9, peek=False, ghost=False, lose_rule=True,
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
        others = sorted((e for e in seen if e is not tg and hyp(e.pos[0] - x, e.pos[1] - y) < 170),
                        key=lambda e: hyp(e.pos[0] - x, e.pos[1] - y))[:3]
        best = -1e18
        mx = my = 0.0
        blocked_side = False
        rings = 2 if self.P["peek"] else 1
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
                    s -= 30
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

        def hidden(cx, cy):
            cz = self.walk_z(cx, cy)
            for t in threats:
                e = self.eye(t)
                if W.los(e, (cx, cy, cz + h)) or W.los(e, (cx, cy, cz + h * 0.5)):
                    return False
            return True
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
                if hidden(cx, cy):
                    sc = steps * 8 + self.zone_penalty(z, cx, cy, self.now)
                    near = min(hyp(t.pos[0] - cx, t.pos[1] - cy) for t in threats)
                    sc -= min(near, 80) * 0.25
                    if best is None or sc < best[0]:
                        best = (sc, cx, cy)
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
        if best is None:
            self.cover_goal = None
            return None
        self.cover_goal = (best[1], best[2])
        return self.travel_cover(me)

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

        def consider(kind, oid, pos, value):
            nonlocal best
            if self.bad.get(oid, -1) > now:
                return
            d = hyp(pos[0] - x, pos[1] - y)
            if d > 260 or hyp(pos[0] - cur[0], pos[1] - cur[1]) > cur[2] - 6:
                return
            if now + d * 1.4 / me["stats"].get("walk_speed", 11.7) + 5 > self.zone_deadline(z, pos[0], pos[1], 6):
                return
            if pos[2] - W.terrain(pos[0], pos[1]) > 3.2:
                return
            u = value / (d + 18)
            if oid == self.loot_id:
                u *= 1.35
            if best is None or u > best[0]:
                best = (u, kind, oid, pos)
        for p in self.pickups.values():
            rad = P["loot_r"]
            seek = p["kind"] == "laser" and P["laser_seek"] > rad
            if seek:
                rad = P["laser_seek"]
            if self.want_pickup(me, p["kind"], slots) and hyp(p["pos"][0] - x, p["pos"][1] - y) < rad:
                v = 130.0 if slots[1] is None else 60.0
                if seek:
                    v = max(v, 150.0)
                consider("pickup", p["id"], p["pos"], v)
        cube_v = 75.0 if me.get("cubes", 0) < self.cfg["cubes"]["stat_cap"] else 25.0
        for c in self.cubes.values():
            if hyp(c["pos"][0] - x, c["pos"][1] - y) < max(60.0, P["loot_r"] * 0.7):
                consider("cube", c["id"], c["pos"], cube_v)
        if hp_frac > P["farm_hp"] and not danger and now < P["crates_until"]:
            for c in self.crates.values():
                if hyp(c["pos"][0] - x, c["pos"][1] - y) < P["loot_r"]:
                    consider("crate", c["id"], c["pos"], cube_v * 0.6)
            if me.get("cubes", 0) < self.cfg["cubes"]["stat_cap"]:
                for n in self.npcs.values():
                    if now - n.seen < 12 and hyp(n.pos[0] - x, n.pos[1] - y) < P["loot_r"]:
                        consider("npc", n.id, n.pos, 85.0)
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
                self.kills += 1
                self.tracks.pop(e.get("target"), None)
        # free-for-all detection: deaths we did not cause (house squads only die to us)
        if self.start_alive is None:
            self.start_alive = o["alive"]
        if self.start_alive - o["alive"] - self.kills > 0:
            self.other_dead += 1
        else:
            self.other_dead = 0
        if self.other_dead >= 5:
            self.ffa = True
        on = not self.ffa and now > 20
        if on != self.prac_on:
            self.prac_on = on
            self.P = dict(SQUAD) if on else dict(FFA)
        opening = not self.ffa and not self.prac_on and self.start_alive - o["alive"] - self.kills <= 0
        if opening != self.opening:
            self.opening = opening
            if not self.prac_on:
                self.P = dict(FFA)
                if opening:
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
        swamped = crowd >= 3 or (crowd == 2 and hp_frac < 0.65) or outgunned
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
                mx, my = self.fight_move(o, target, seen, 1, weapons)
                mode = "cornered"
            else:
                mx, my = self.travel(me, zgx, zgy)
                mode = "flee"
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
            if P["fight_loot"]:
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
            elif zdist > 3 and (not P["ghost"] or z["next_radius"] <= 30):
                mx, my = self.travel(me, zgx, zgy)
                mode = "zone-walk"
            elif P["ghost"]:
                h = self.home(o)
                if h and hyp(h[0] - x, h[1] - y) > 3:
                    mx, my = self.travel(me, h[0], h[1])
                    if not danger and now - self.last_hurt > 6:
                        q = min(1.0, 4.6 / walk)
                        mx, my = mx * q, my * q
                    mode = "home"

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
            elif mx or my:
                aim[0] = math.atan2(my, mx) + 1.15 * math.sin(now * 1.3)
            else:
                aim[0] = me["aim"][0] + 0.35
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
