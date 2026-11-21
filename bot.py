"""Kentucky Fried Evan's Mech Royale bot.

Priorities: stay in the zone, survive (retreat behind cover to regenerate), farm cubes and a
laser/lightning early, and fight with led gun shots plus hitscan laser when a target is in view.
"""
import math
import os
import random
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mechfight as mf  # noqa: E402
from worldmap import World  # noqa: E402

TAU = 2 * math.pi
DEBUG = bool(int(os.environ.get("KFE_DEBUG", 0)))

UPGRADE_PRIO = [
    "armor", "plating", "nanites", "laser_damage", "laser_charge", "gun_damage", "gun_rate",
    "lightning_efficiency", "lightning_range", "gun_mag", "gyros", "servos", "reactor",
    "capacitor", "jets", "gun_velocity", "sensors", "lightning_chain", "laser_pierce",
    "mortar_reload", "mortar_splash", "mortar_velocity",
]
if os.environ.get("KFE_UPG") == "dmg":
    UPGRADE_PRIO = [
        "gun_damage", "laser_damage", "gun_rate", "lightning_efficiency", "laser_charge", "armor", "plating",
        "gun_mag", "lightning_range", "nanites", "gyros", "lightning_chain", "gun_velocity", "servos", "reactor",
        "capacitor", "jets", "sensors", "laser_pierce", "mortar_reload", "mortar_splash", "mortar_velocity",
    ]
WEAPON_PREF = {"laser": 3, "lightning": 2, "mortar": -1, "gun": -2}
if os.environ.get("KFE_PREF") == "lightning":
    WEAPON_PREF = {"laser": 2, "lightning": 3, "mortar": -1, "gun": -2}

PARAMS = {
    "retreat_hp": float(os.environ.get("KFE_RETREAT_HP", 0.35)),
    "recover_hp": float(os.environ.get("KFE_RECOVER_HP", 0.75)),
    "farm": int(os.environ.get("KFE_FARM", 1)),
    "multi": int(os.environ.get("KFE_MULTI", 1)),
    "multi_hp": float(os.environ.get("KFE_MULTI_HP", 0.6)),
    "hide_reload": int(os.environ.get("KFE_HIDE", 1)),
    "chase": int(os.environ.get("KFE_CHASE", 1)),
    "engage": float(os.environ.get("KFE_ENGAGE", 120)),
    "ret_fire": int(os.environ.get("KFE_RET_FIRE", 1)),
    "chase_hp": float(os.environ.get("KFE_CHASE_HP", 0.4)),
    "pos": int(os.environ.get("KFE_POS", 0)),
    "tgt_hp": float(os.environ.get("KFE_TGT_HP", 0.04)),
    "chase_d": float(os.environ.get("KFE_CHASE_D", 90)),
    "perch": int(os.environ.get("KFE_PERCH", 0)),
    "perch_t": float(os.environ.get("KFE_PERCH_T", 0)),
    "perch_margin": float(os.environ.get("KFE_PERCH_MARGIN", 12)),
    "farm_r_early": float(os.environ.get("KFE_FARM_R", 70)),
    "farm_t": float(os.environ.get("KFE_FARM_T", 120)),
    "kite": int(os.environ.get("KFE_KITE", 1)),
    "repel": float(os.environ.get("KFE_REPEL", 1.5)),
    "npc_avoid": int(os.environ.get("KFE_NPC_AVOID", 1)),
    "evade": int(os.environ.get("KFE_EVADE", 0)),
    "evade_mem": float(os.environ.get("KFE_EVADE_MEM", 3.0)),
    "loot_kill": int(os.environ.get("KFE_LOOT_KILL", 1)),
    "farm_hp": float(os.environ.get("KFE_FARM_HP", 0.85)),
    "fight_boost": float(os.environ.get("KFE_FIGHT_BOOST", 0)),
    "strafe_lo": float(os.environ.get("KFE_STRAFE_LO", 0.5)),
    "strafe_hi": float(os.environ.get("KFE_STRAFE_HI", 1.4)),
    "unseen": int(os.environ.get("KFE_UNSEEN", 1)),
}


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Bot:
    name = "KentuckyFriedEvan"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.id = hello["slot"]
        self.rng = random.Random(hello.get("seed", 0))
        t0 = time.time()
        self.W = World(hello["map"]["dir"])
        import numpy as np
        self.perch_bad = np.zeros(len(self.W.perch_x), bool)
        mf.log(f"world loaded in {time.time() - t0:.2f}s")
        self.path = None
        self.path_goal = None
        self.path_t = -99
        self.strafe = 1
        self.strafe_until = 0
        self.hist = deque(maxlen=12)
        self.stuck_until = -1
        self.stuck_dir = 1
        self.last_hurt = -99
        self.last_dealt = -99
        self.threats = {}  # id -> (t, bearing)
        self.retreating = False
        self.cover = None
        self.cover_t = -99
        self.sound_look = None
        self.jump_next = False
        self.slow_ticks = 0
        self.last_dmg = None
        self.enemy_pos = {}
        self.perch = None
        self.perch_t = -99
        self.climbing = False
        self.climb_since = None
        self.fight_spot = None
        self.fight_spot_t = -99
        self.loot_spots = []  # (t, (x, y))

    # ------------------------------------------------------------------ helpers
    def zone_now(self, z, t):
        ss, se = z["shrink_start"], z["shrink_end"]
        if t <= ss:
            f = 0.0
        elif t >= se or se <= ss:
            f = 1.0
        else:
            f = (t - ss) / (se - ss)
        c = (z["center"][0] + (z["next_center"][0] - z["center"][0]) * f,
             z["center"][1] + (z["next_center"][1] - z["center"][1]) * f)
        r = z["radius"] + (z["next_radius"] - z["radius"]) * f
        return c, r

    def safe_spot(self, p, obs, margin=6.0):
        z = obs["zone"]
        t = obs["t"]
        c, r = self.zone_now(z, t)
        if d2(p, c) > r - margin:
            return False
        nc, nr = z["next_center"], z["next_radius"]
        if t > z["shrink_start"] - 20 and d2(p, nc) > nr - margin:
            # outside the circle we are shrinking to: only OK if it is on our way in
            return d2(p, nc) < d2(self.pos, nc) + 5
        return True

    def steer(self, dx, dy, look=7.0):
        """Rotate the desired direction until the short walk ahead is clear."""
        n = math.hypot(dx, dy)
        if n < 1e-6:
            return 0.0, 0.0
        dx, dy = dx / n, dy / n
        x, y = self.pos[0], self.pos[1]
        W = self.W
        if not W.free_at(x, y):
            return dx, dy
        base = math.atan2(dy, dx)
        for off in (0, 0.35, -0.35, 0.7, -0.7, 1.05, -1.05, 1.4, -1.4, 1.9, -1.9):
            a = base + off * self.strafe if off else base
            ex, ey = x + math.cos(a) * look, y + math.sin(a) * look
            if W.walk_clear((x, y), (ex, ey)):
                return math.cos(a), math.sin(a)
        return dx, dy

    def nav_dir(self, goal, t):
        """Direction toward goal along an A* path (straight if the walk is clear)."""
        x, y = self.pos[0], self.pos[1]
        W = self.W
        dist = d2((x, y), goal)
        if dist < 1.5:
            return 0.0, 0.0, dist
        if dist < 60 and W.walk_clear((x, y), goal):
            self.path = None
            return (goal[0] - x) / dist, (goal[1] - y) / dist, dist
        if (self.path is None or self.path_goal is None or d2(goal, self.path_goal) > 6
                or t - self.path_t > 2.5):
            self.path = W.astar((x, y), goal)
            self.path_goal = tuple(goal)
            self.path_t = t
        p = self.path
        if not p:
            return (goal[0] - x) / dist, (goal[1] - y) / dist, dist
        while len(p) > 1 and d2((x, y), p[0]) < 3.0:
            p.pop(0)
        # string pulling: farthest of the next few waypoints we can walk to directly
        tgt = p[0]
        for k in range(min(len(p), 8) - 1, 0, -1):
            if d2((x, y), p[k]) < 45 and W.walk_clear((x, y), p[k]):
                tgt = p[k]
                del p[:k]
                break
        dd = d2((x, y), tgt)
        if dd < 1e-6:
            return 0.0, 0.0, dist
        return (tgt[0] - x) / dd, (tgt[1] - y) / dd, dist

    def lead(self, tp, tv, speed, h):
        mx, my, mz = self.pos[0], self.pos[1], self.pos[2] + self.muzzle_h
        tx, ty, tz = tp[0], tp[1], tp[2] + h * 0.5
        vx, vy, vz = tv
        if abs(vz) < 3:
            vz = 0.0
        tt = 0.05
        for _ in range(3):
            px, py, pz = tx + vx * tt, ty + vy * tt, tz + vz * tt
            dist = math.sqrt((px - mx) ** 2 + (py - my) ** 2 + (pz - mz) ** 2)
            tt = 0.05 + max(dist - 3.0, 0) / speed
        px, py, pz = tx + vx * tt, ty + vy * tt, tz + vz * tt
        hd = math.hypot(px - mx, py - my)
        return math.atan2(py - my, px - mx), math.atan2(pz - mz, max(hd, 0.1)), hd, (px, py, pz)

    def direct(self, tp, h):
        mx, my, mz = self.pos[0], self.pos[1], self.pos[2] + self.muzzle_h
        tx, ty, tz = tp[0], tp[1], tp[2] + h * 0.5
        hd = math.hypot(tx - mx, ty - my)
        return math.atan2(ty - my, tx - mx), math.atan2(tz - mz, max(hd, 0.1)), hd

    def aim_ok(self, yaw, pitch, hd, radius, h):
        ay, ap = self.aim
        ey = abs(wrap(ay - yaw)) * max(hd, 1)
        ep = abs(ap - pitch) * max(hd, 1)
        return ey < radius * 0.8 and ep < h * 0.35

    def find_cover(self, threats, obs, radii=(14, 24, 36, 50)):
        x, y, z = self.pos
        W = self.W
        best = None
        for r in radii:
            for k in range(16):
                a = k * TAU / 16
                cx, cy = x + math.cos(a) * r, y + math.sin(a) * r
                if not W.in_bounds(cx, cy, 5) or not W.free_at(cx, cy):
                    continue
                if not self.safe_spot((cx, cy), obs, 4):
                    continue
                cz = W.surface(cx, cy) + 4.5
                hidden = 0
                closer = 0
                for m in threats:
                    mp = m["pos"]
                    if not W.los((mp[0], mp[1], mp[2] + 8), (cx, cy, cz)):
                        hidden += 1
                    if d2((cx, cy), mp) < d2((x, y), mp):
                        closer += 1
                score = hidden * 100 - r - closer * 30
                if PARAMS["npc_avoid"]:
                    for n in self._npcs:
                        if d2((cx, cy), n["pos"]) < 48:
                            score -= 60
                if best is None or score > best[0]:
                    best = (score, (cx, cy), hidden)
            if best and best[2] == len(threats) and r >= radii[min(1, len(radii) - 1)]:
                break
        return best[1] if best else None

    def pick_perch(self, obs, zc, zr):
        """Choose a deep roof point inside the current and next zone, near us."""
        import numpy as np
        W = self.W
        z = obs["zone"]
        nc, nr = z["next_center"], z["next_radius"]
        px, py = W.perch_x, W.perch_y
        dn = np.hypot(px - nc[0], py - nc[1])
        dc = np.hypot(px - zc[0], py - zc[1])
        margin = PARAMS["perch_margin"] + (10 if z["shrink_end"] - obs["t"] < 20 else 0)
        ok = (dn < nr - margin) & (dc < zr - margin)
        if not ok.any():
            return None
        dme = np.hypot(px - self.pos[0], py - self.pos[1])
        cost = dme - W.perch_d * 4 + np.where(ok, 0, 1e9) + np.where(self.perch_bad, 1e9, 0)
        # prefer staying on the current perch
        if self.perch is not None:
            cost -= 40 * (np.hypot(px - self.perch[0], py - self.perch[1]) < 3)
        k = int(np.argmin(cost))
        if cost[k] > 1e8:
            return None
        return (float(px[k]), float(py[k]), float(W.perch_h[k]))

    def pick_fight_spot(self, target, enemies, obs, lo, hi):
        """Nearby spot with a shot at the target, few other enemies seeing it, and a wall close by."""
        W = self.W
        x, y, z = self.pos
        tp = target["pos"]
        tc = (tp[0], tp[1], tp[2] + target["_h"] * 0.5)
        others = [m for m in enemies if m is not target and m["_d"] < 200]
        best = None
        cands = [(x, y)]
        for r in (6.0, 12.0):
            for k in range(12):
                a = k * TAU / 12
                cands.append((x + math.cos(a) * r, y + math.sin(a) * r))
        for cx, cy in cands:
            if (cx, cy) != (x, y):
                if not W.free_at(cx, cy) or not W.walk_clear((x, y), (cx, cy)):
                    continue
                if not self.safe_spot((cx, cy), obs, 4):
                    continue
            cz = W.surface(cx, cy)
            if not W.los((cx, cy, cz + self.muzzle_h), tc):
                continue
            expo = 0
            for m in others:
                mp = m["pos"]
                if W.los((mp[0], mp[1], mp[2] + 8), (cx, cy, cz + 4.5)):
                    expo += 1
            dt_ = math.hypot(tc[0] - cx, tc[1] - cy)
            rp = max(0.0, lo - dt_, dt_ - hi) / 15.0
            # wall within ~5 m (something to duck behind)
            jc, ic = W.cell(cx, cy)
            near = W.wall[max(jc - 6, 0):jc + 7, max(ic - 6, 0):ic + 7].any()
            score = -2.0 * expo - rp + (0.8 if near else 0.0) - math.hypot(cx - x, cy - y) / 15.0
            if best is None or score > best[0]:
                best = (score, (cx, cy))
        return best[1] if best else None

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.perf_counter()
        t = obs["t"]
        me = obs["self"]
        W = self.W
        self.pos = me["pos"]
        self.aim = me["aim"]
        x, y, z = self.pos
        st = me["stats"]
        self.muzzle_h = st.get("muzzle_height", 7)
        hp_frac = me["hp"] / max(me["max_hp"], 1)
        act = {"tick": obs["tick"]}
        self.hist.append((t, x, y))

        # ---- offers
        offer = obs.get("offer")
        if offer:
            ids = [c["id"] for c in offer["choices"]]
            best = min(range(len(ids)), key=lambda i: UPGRADE_PRIO.index(ids[i]) if ids[i] in UPGRADE_PRIO else 99)
            act["pick"] = best

        # ---- events
        for e in obs["events"]:
            et = e["type"]
            if et == "damaged":
                if e.get("by") is not None:
                    self.last_hurt = t
                    self.threats[e["by"]] = (t, e.get("bearing"))
                    if e.get("bearing") is not None and e.get("weapon") != "npc":
                        self.last_dmg = (t, e["bearing"], e["by"])
                elif e.get("weapon") != "zone":
                    self.last_hurt = t
            elif et == "hit":
                self.last_dealt = t
            elif et == "kill" and PARAMS["loot_kill"]:
                p = self.enemy_pos.get(e.get("target"))
                if p is not None:
                    self.loot_spots.append((t, p))
        for k in [k for k, v in self.threats.items() if t - v[0] > 8]:
            del self.threats[k]

        # ---- weapons
        weapons = me["weapons"]
        slot = {w["kind"]: i for i, w in enumerate(weapons) if w}
        gun = weapons[slot["gun"]] if "gun" in slot else None
        laser = weapons[slot["laser"]] if "laser" in slot else None
        light = weapons[slot["lightning"]] if "lightning" in slot else None

        # ---- pickups
        reach = self.cfg["loot"]["pickup_radius"]
        want_pick = None
        cur2 = weapons[1]
        cur2_pref = WEAPON_PREF.get(cur2["kind"], -5) if cur2 else -5
        for p in obs["pickups"]:
            pref = WEAPON_PREF.get(p["kind"], -5)
            if pref <= cur2_pref or pref < 0 or p["kind"] in slot:
                continue
            if abs(p["pos"][2] - z) > 3.5:
                continue
            dd = d2(p["pos"], (x, y))
            if dd < reach - 0.3:
                busy = cur2 and cur2["kind"] == "laser" and cur2.get("charge", 0) > 0
                if not busy:
                    act["take"] = p["id"]
                    if cur2:
                        act["drop"] = 1
            if want_pick is None or dd < want_pick[0]:
                want_pick = (dd, p)

        # ---- perception
        enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        self._npcs = npcs
        eye = (x, y, z + st.get("eye_height", 8))
        muzzle = (x, y, z + self.muzzle_h)
        for m in enemies:
            m["_d"] = d2(m["pos"], (x, y))
            mh = 9 * (1 + 0.05 * m.get("cubes", 0))
            m["_h"] = mh
            m["_r"] = 2.5 * (1 + 0.05 * m.get("cubes", 0))
            m["_los"] = W.los(muzzle, (m["pos"][0], m["pos"][1], m["pos"][2] + mh * 0.5))
            m["_threat"] = m["id"] in self.threats
            # is it aiming at me?
            by = math.atan2(y - m["pos"][1], x - m["pos"][0])
            m["_aimed"] = abs(wrap(m["aim"][0] - by)) < 0.15
        recent_hurt = t - self.last_hurt < 4
        for m in enemies:
            self.enemy_pos[m["id"]] = (m["pos"][0], m["pos"][1], m["pos"][2])
        self.loot_spots = [(lt, lp) for lt, lp in self.loot_spots if t - lt < 20 and d2(lp, (x, y)) > 2.5]

        # target choice
        target = None
        cand = [m for m in enemies if m["_d"] < 230]
        if cand:
            def score(m):
                s = m["_d"]
                if not m["_los"]:
                    s += 120
                s += m["hp"] * PARAMS["tgt_hp"]
                if m["_threat"]:
                    s -= 80 if m["_los"] else 40
                return s
            target = min(cand, key=score)

        engaged = target is not None and (target["_d"] <= PARAMS["engage"] or target["_threat"] or recent_hurt
                                          or target["hp"] <= target["max_hp"] * 0.35)
        if PARAMS["evade"] and target is not None:
            engaged = (target["_d"] < 35 or (t - self.last_hurt < PARAMS["evade_mem"])
                       or (target["hp"] <= target["max_hp"] * 0.3 and target["_d"] < 90))

        # ---- retreat state
        exposed = [m for m in enemies if m["_los"] and m["_d"] < 160]
        if hp_frac < PARAMS["retreat_hp"] and (recent_hurt or any(m["_d"] < 120 for m in enemies)):
            self.retreating = True
        if PARAMS["multi"] and len(exposed) >= 2 and hp_frac < PARAMS["multi_hp"] and recent_hurt:
            self.retreating = True
        if self.retreating and (hp_frac > PARAMS["recover_hp"] or
                                (not enemies and t - self.last_hurt > 12)):
            self.retreating = False
            self.cover = None

        # ---- zone
        zone = obs["zone"]
        zc, zr = self.zone_now(zone, t)
        nc, nr = zone["next_center"], zone["next_radius"]
        walk = st.get("walk_speed", 11.7)
        d_next = d2((x, y), nc)
        anchor_r = min(nr * 0.5, 60) if nr > 30 else nr * 0.3
        if d_next > anchor_r:
            k = anchor_r / max(d_next, 1e-6)
            home = (nc[0] + (x - nc[0]) * k, nc[1] + (y - nc[1]) * k)
        else:
            home = (x, y)
        if not W.free_at(*home):
            gj, gi = W.nearest_free(*W.nav_cell(*home))
            home = W.nav_center(gj, gi)
        need = max(0.0, d_next - (nr - min(15, nr * 0.3))) / (walk * 0.75)
        time_left = zone["shrink_end"] - t
        outside_now = d2((x, y), zc) > zr - 4
        zone_urgent = outside_now or (need > 0 and need > time_left - 12) or not me["in_zone"]

        # ---- choose movement goal
        goal = None
        mode = "home"
        boost = False
        face = None  # (yaw, pitch)
        strafe_ok = False

        if zone_urgent:
            goal = home
            mode = "zone"
            boost = (not me["in_zone"]) or need > time_left - 6
        perch_mode = None
        if goal is None and PARAMS["perch"] and t >= PARAMS["perch_t"]:
            if self.perch is None or t - self.perch_t > 2.0:
                self.perch = self.pick_perch(obs, zc, zr)
                self.perch_t = t
            if self.perch is not None:
                ppx, ppy, pph = self.perch
                jc, ic = W.cell(x, y)
                on_roof = z > W.terrain(x, y) + 4 and abs(z - float(W.gtop[jc, ic])) < 1.5
                dp = d2((x, y), (ppx, ppy))
                if on_roof and abs(z - pph) < 2.0:
                    perch_mode = "perched"
                    goal = (ppx, ppy) if dp > 1.5 else (x, y)
                    mode = "perch"
                else:
                    ux, uy = (ppx - x) / max(dp, 1e-6), (ppy - y) / max(dp, 1e-6)
                    at_wall = dp < 45 and not W.walk_clear((x, y), (x + ux * 5, y + uy * 5))
                    if z >= pph - 0.3 and not me["grounded"]:
                        perch_mode = "glide"
                        mode = "glide"
                        goal = (ppx, ppy)
                    elif self.climbing or at_wall:
                        need_e = (pph - z) / 10.0 * st.get("jet_drain", 15) + 12
                        if self.climbing or me["energy"] >= min(need_e, me["max_energy"] - 1) or not me["in_zone"]:
                            self.climbing = z < pph + 1.0 and (me["energy"] > 1 or not me["grounded"])
                            perch_mode = "climb"
                            mode = "climb"
                            goal = (ppx, ppy)
                        else:
                            perch_mode = "wait"
                            mode = "chargeE"
                            goal = (x, y)
                    else:
                        perch_mode = "approach"
                        mode = "toperch"
                        goal = (ppx, ppy)
        if perch_mode in ("climb", "wait", "glide"):
            if self.climb_since is None:
                self.climb_since = t
            elif t - self.climb_since > 8.0 and self.perch is not None:
                import numpy as np
                bad = np.hypot(W.perch_x - self.perch[0], W.perch_y - self.perch[1]) < 12
                self.perch_bad |= bad
                self.perch = None
                self.climb_since = None
                self.climbing = False
        else:
            self.climb_since = None
        if perch_mode is None:
            self.climbing = False
        if goal is None and self.retreating and (enemies or recent_hurt):
            threats = [m for m in enemies if m["_d"] < 200] or []
            if threats:
                if self.cover is None or t - self.cover_t > 1.0:
                    self.cover = self.find_cover(threats, obs)
                    self.cover_t = t
                if self.cover:
                    goal = self.cover
                    mode = "cover"
                    boost = me["energy"] > 25
        # hide behind cover while the gun reloads (and the laser is not about to fire)
        if goal is None and PARAMS["hide_reload"] and target is not None and target["_los"] and not self.retreating \
                and gun is not None and gun.get("reload", 0) > 0.6 and target["_d"] < 170 \
                and (laser is None or laser["cooldown"] > 0.8) and not (light and target["_d"] < 40):
            if self.cover is None or t - self.cover_t > 1.0:
                self.cover = self.find_cover(exposed or [target], obs, radii=(10, 18, 28))
                self.cover_t = t
            if self.cover:
                goal = self.cover
                mode = "hide"
        if goal is None and PARAMS["evade"] and not engaged and not self.retreating:
            seen = [m for m in enemies if m["_d"] < 170 and m["_los"]]
            if seen:
                if self.cover is None or t - self.cover_t > 1.0:
                    self.cover = self.find_cover(seen, obs)
                    self.cover_t = t
                if self.cover:
                    goal = self.cover
                    mode = "evade"
        if goal is None and engaged and target["_los"] and not self.retreating:
            mode = "fight"
            # range band
            if light and (not laser):
                lo, hi = 12, 30
            elif laser:
                lo, hi = 55, 130
            else:
                lo, hi = 35, 90
            td = target["_d"]
            tx, ty = target["pos"][0], target["pos"][1]
            ux, uy = (tx - x) / max(td, 1e-6), (ty - y) / max(td, 1e-6)
            radial = 0.0
            if td > hi:
                radial = 1.0
            elif td < lo:
                radial = -1.0
            if PARAMS["kite"] and not light and "lightning" in (target.get("weapons") or []) and td < 58:
                radial = -1.0
                boost = me["energy"] > 25
            if t > self.strafe_until:
                self.strafe = -self.strafe if self.rng.random() < 0.7 else self.strafe
                self.strafe_until = t + self.rng.uniform(PARAMS["strafe_lo"], PARAMS["strafe_hi"])
            px, py = -uy * self.strafe, ux * self.strafe
            mx, my = ux * radial + px * 0.9, uy * radial + py * 0.9
            if PARAMS["pos"]:
                if self.fight_spot is None or t - self.fight_spot_t > 0.5:
                    self.fight_spot = self.pick_fight_spot(target, enemies, obs, lo, hi)
                    self.fight_spot_t = t
                if self.fight_spot is not None:
                    fx, fy = self.fight_spot[0] - x, self.fight_spot[1] - y
                    fd = math.hypot(fx, fy)
                    if fd > 2.0:
                        mx, my = fx / fd + px * 0.35, fy / fd + py * 0.35
                    else:
                        mx, my = px * 0.6, py * 0.6
            kite_boost = boost
            # do not strafe out of the zone
            gx, gy = x + mx * 10, y + my * 10
            if not self.safe_spot((gx, gy), obs, 3):
                cx, cy = home[0] - x, home[1] - y
                cn = math.hypot(cx, cy) or 1
                mx, my = mx * 0.3 + cx / cn, my * 0.3 + cy / cn
            goal = (x + mx * 12, y + my * 12)
            strafe_ok = True
            boost = kite_boost or (me["energy"] > 55 and any(m.get("charging") and m["_aimed"] for m in enemies))
            if PARAMS["fight_boost"] and me["energy"] > PARAMS["fight_boost"]:
                boost = True
        if goal is None and PARAMS["chase"] and target is not None and not target["_los"] and not self.retreating \
                and target["hp"] < target["max_hp"] * PARAMS["chase_hp"] and hp_frac > 0.6 \
                and target["_d"] < PARAMS["chase_d"] \
                and self.safe_spot(target["pos"], obs):
            goal = (target["pos"][0], target["pos"][1])
            mode = "chase"
        if goal is None and target is not None and not target["_los"] and not self.retreating:
            # keep facing it; drift toward home/loot
            face_yaw = math.atan2(target["pos"][1] - y, target["pos"][0] - x)
            face = (face_yaw, 0.0)

        farm_target = None
        if goal is None and PARAMS["farm"]:
            cands = []
            if want_pick and want_pick[0] < 90 and self.safe_spot(want_pick[1]["pos"], obs):
                cands.append((want_pick[0] * 0.7, "pick", want_pick[1]["pos"], None))
            for c in obs["cubes"]:
                if abs(c["pos"][2] - W.terrain(c["pos"][0], c["pos"][1])) > 3 and abs(c["pos"][2] - z) > 3:
                    continue
                dd = d2(c["pos"], (x, y))
                if dd < 80 and self.safe_spot(c["pos"], obs):
                    cands.append((dd * 0.6, "cube", c["pos"], None))
            for lt, lp in self.loot_spots:
                dd = d2(lp, (x, y))
                if dd < 120 and self.safe_spot(lp, obs) and not any(m["_los"] and m["_d"] < 100 for m in enemies):
                    cands.append((dd * 0.4, "loot", lp, None))
            farm_r = PARAMS["farm_r_early"] if t < PARAMS["farm_t"] else 70
            if not enemies and hp_frac > PARAMS["farm_hp"]:
                for c in obs["crates"]:
                    cz = c["pos"][2]
                    if cz - W.terrain(c["pos"][0], c["pos"][1]) > 3 and abs(cz - z) > 3:
                        continue
                    dd = d2(c["pos"], (x, y))
                    if dd < farm_r and self.safe_spot(c["pos"], obs):
                        cands.append((dd + 10, "crate", c["pos"], c))
                for n in npcs:
                    dd = d2(n["pos"], (x, y))
                    if dd < farm_r and self.safe_spot(n["pos"], obs):
                        cands.append((dd + 15, "npc", n["pos"], n))
            if cands:
                cands.sort(key=lambda c: c[0])
                _, kind, p, obj = cands[0]
                mode = kind
                if kind in ("crate", "npc"):
                    farm_target = obj
                    dd = d2(p, (x, y))
                    hop = 18 if kind == "crate" else 30
                    if dd > hop or not W.los(muzzle, (p[0], p[1], p[2] + 1.2)):
                        goal = (p[0], p[1])
                    else:
                        goal = (x, y)
                else:
                    goal = (p[0], p[1])

        # supply drops
        if goal is None and obs.get("drops"):
            for dr in obs["drops"]:
                dp = dr["pos"]
                dd = d2(dp, (x, y))
                if dd > 200 or not self.safe_spot(dp, obs, 8):
                    continue
                if dr.get("holder") not in (None, self.id):
                    continue
                crowd = sum(1 for m in enemies if d2(m["pos"], dp) < 45)
                if crowd == 0:
                    goal = (dp[0], dp[1])
                    mode = "drop"
                    if dd < dr.get("radius", 8) - 3:
                        goal = (x, y)
                    break

        if goal is None:
            goal = home
            mode = "home"
            if d2((x, y), home) < 6:
                # idle patrol: small circle so steps make little noise
                goal = (x, y)

        # ---- movement
        if strafe_ok:
            mvx, mvy = goal[0] - x, goal[1] - y
            mvx, mvy = self.steer(mvx, mvy)
        else:
            mvx, mvy, _ = self.nav_dir(goal, t)
            if PARAMS["repel"] and mode in ("home", "pick", "cube", "crate", "npc", "drop"):
                rx = ry = 0.0
                avoid = list(enemies)
                if PARAMS["npc_avoid"] and mode != "npc":
                    for n in npcs:
                        n["_d"] = d2(n["pos"], (x, y))
                        n["_npc"] = True
                        avoid.append(n)
                for m in avoid:
                    if m.get("_npc"):
                        R = 52.0
                    else:
                        R = 65.0 if "lightning" in (m.get("weapons") or []) and not light else 40.0
                    if m["_d"] < R:
                        w = (R - m["_d"]) / R
                        rx += (x - m["pos"][0]) / max(m["_d"], 1) * w
                        ry += (y - m["pos"][1]) / max(m["_d"], 1) * w
                if rx or ry:
                    mvx += rx * PARAMS["repel"]
                    mvy += ry * PARAMS["repel"]
                    n = math.hypot(mvx, mvy) or 1
                    mvx, mvy = mvx / n, mvy / n
            if mvx or mvy:
                mvx, mvy = self.steer(mvx, mvy, look=4.0)
        moving = abs(mvx) + abs(mvy) > 0.1

        jump = False
        if perch_mode == "glide":
            dpx, dpy = goal[0] - x, goal[1] - y
            dn_ = math.hypot(dpx, dpy) or 1
            mvx, mvy = dpx / dn_, dpy / dn_
            boost = False
            moving = True
        elif perch_mode == "climb":
            dpx, dpy = goal[0] - x, goal[1] - y
            dn_ = math.hypot(dpx, dpy) or 1
            mvx, mvy = dpx / dn_, dpy / dn_
            jump = me["grounded"]
            boost = True
            moving = True
        elif perch_mode in ("perched", "wait"):
            if goal == (x, y):
                mvx = mvy = 0.0
                moving = False
        # stuck detection
        if moving and len(self.hist) >= 10 and perch_mode != "climb":
            t_old, ox, oy = self.hist[0]
            if t - t_old > 0.8 and math.hypot(x - ox, y - oy) < 1.2 * (t - t_old):
                if t > self.stuck_until + 1.5:
                    self.stuck_until = t + 0.8
                    self.stuck_dir = -self.stuck_dir
                    self.path = None
        if t < self.stuck_until:
            jump = me["grounded"]
            boost = me["energy"] > 10
            a = math.atan2(mvy, mvx) + 0.6 * self.stuck_dir
            mvx, mvy = math.cos(a), math.sin(a)

        # ---- aim and fire
        fire = [False, False]
        aim = None
        gun_i = slot.get("gun")
        laser_i = slot.get("laser")
        light_i = slot.get("lightning")
        shoot_ok = not self.retreating or (target is not None and target["hp"] < 250) or \
            (target is not None and target["_d"] < 25) or (PARAMS["ret_fire"] and t - self.last_hurt < 2.0)
        if target is not None and not engaged:
            shoot_ok = False
        tgt_obj = None
        if target is not None:
            tgt_obj = target
        elif farm_target is not None:
            tgt_obj = farm_target
        if tgt_obj is not None:
            if tgt_obj is target:
                th, tr = target["_h"], target["_r"]
                tv = target["vel"]
                tlos = target["_los"]
            else:
                is_npc = farm_target.get("kind") == "npc"
                th = 3.5 if is_npc else 2.4
                tr = 1.8 if is_npc else 1.4
                tv = farm_target.get("vel", [0, 0, 0])
                tp = farm_target["pos"]
                tlos = W.los(muzzle, (tp[0], tp[1], tp[2] + th * 0.5))
            tp = tgt_obj["pos"]
            gspeed = gun["stats"]["speed"] if gun else 252
            lyaw, lpitch, lhd, _ = self.lead(tp, tv, gspeed, th)
            dyaw, dpitch, dhd = self.direct(tp, th)
            charging = laser is not None and laser.get("charge", 0) > 0
            use_direct = charging and laser.get("charge", 0) > laser["stats"]["charge"] - 0.25
            if use_direct or (gun is None):
                aim = [dyaw, dpitch]
            else:
                aim = [lyaw, lpitch]
            if tlos and (shoot_ok or tgt_obj is not target):
                # gun
                if gun is not None and gun.get("reload", 0) <= 0 and gun.get("ammo", 1) > 0:
                    if lhd < gun["stats"]["range"] and self.aim_ok(lyaw, lpitch, lhd, tr, th):
                        fire[gun_i] = True
                # lightning
                if light is not None and me["energy"] > 8:
                    lr = light["stats"]["range"]
                    cone = math.radians(light["stats"]["cone_deg"]) * 0.5
                    if dhd < lr - 2 and abs(wrap(self.aim[0] - dyaw)) < cone * 0.8:
                        fire[light_i] = True
                    if not laser and dhd < lr:
                        aim = [dyaw, dpitch]
            # laser
            if laser is not None:
                ch = laser.get("charge", 0)
                full = laser["stats"]["charge"]
                lrange = laser["stats"]["range"]
                if ch > 0:
                    hold_max = full + self.cfg["weapons"]["laser"].get("hold_s", 2)
                    if tlos and ch >= full - 0.02 and self.aim_ok(dyaw, dpitch, dhd, tr, th):
                        fire[laser_i] = False  # release
                    elif ch > hold_max - 0.2:
                        fire[laser_i] = False
                    else:
                        fire[laser_i] = True
                elif laser["cooldown"] <= 0.1 and tlos and dhd < lrange and (shoot_ok or tgt_obj is not target):
                    fire[laser_i] = True
        else:
            # keep the laser charge if we have one (waiting for the target to reappear)
            if laser is not None and laser.get("charge", 0) > 0:
                full = laser["stats"]["charge"]
                hold_max = full + self.cfg["weapons"]["laser"].get("hold_s", 2)
                fire[laser_i] = laser["charge"] < hold_max - 0.2

        hurt_unseen = None
        if PARAMS["unseen"] and self.last_dmg and t - self.last_dmg[0] < 2.0:
            seen_ids = {m["id"] for m in enemies if m["_los"]}
            if self.last_dmg[2] not in seen_ids:
                hurt_unseen = self.last_dmg[1]
        if hurt_unseen is not None and (target is None or not target["_los"]):
            aim = [hurt_unseen, 0.0]
            fire = [False, False]
            if laser is not None and laser.get("charge", 0) > 0:
                fire[laser_i] = True
        if aim is None:
            if face is not None:
                aim = [face[0], 0.0]
            else:
                # look at recent attacker, else sounds, else sweep along the move direction
                look = None
                for tid, (tt, br) in self.threats.items():
                    if t - tt < 3 and br is not None:
                        look = br
                if look is None:
                    for s in obs["sounds"]:
                        if s["kind"] in ("gun", "laser", "lightning", "mortar", "laser_charge", "step", "boost", "jet") \
                                and s["dist"] in ("near", "mid"):
                            self.sound_look = (t, s["bearing"])
                    if self.sound_look and t - self.sound_look[0] < 2.0:
                        look = self.sound_look[1]
                if look is None:
                    if moving:
                        base = math.atan2(mvy, mvx)
                    else:
                        base = math.atan2(nc[1] - y, nc[0] - x) if d_next > 5 else 0.0
                    look = base + 1.1 * math.sin(t * 0.9)
                aim = [look, 0.0]
            # reload when nothing to shoot
            if gun is not None and gun.get("reload", 0) <= 0 and gun.get("ammo", 99) < gun["stats"]["mag"] * 0.6:
                act["reload"] = True

        act["move"] = [round(mvx, 3), round(mvy, 3)]
        act["boost"] = bool(boost and moving)
        act["jump"] = bool(jump)
        act["aim"] = [round(aim[0], 4), round(clamp(aim[1], -0.7, 1.3), 4)]
        act["fire"] = fire
        dt = (time.perf_counter() - t0) * 1000
        if dt > 30:
            mf.log(f"slow act {dt:.1f} ms at t={t}")
        mf.draw.label(f"{mode} hp{me['hp']:.0f} {dt:.0f}ms")
        if DEBUG and obs["tick"] % 4 == 0:
            tinfo = ""
            if target is not None:
                tinfo = (f"tgt {target['id']} d{target['_d']:.0f} los{int(target['_los'])} thr{int(target['_threat'])} "
                         f"eng{int(engaged)} aimerr{abs(wrap(self.aim[0] - aim[0])):.3f}")
            mf.log(f"t{t:.1f} {mode} hp{me['hp']:.0f} fire{fire} ok{int(shoot_ok)} ret{int(self.retreating)} "
                   f"ammo{gun and gun.get('ammo')} rl{gun and gun.get('reload')} {tinfo} n_en{len(enemies)}")
        return act


if __name__ == "__main__":
    mf.run(Bot)
