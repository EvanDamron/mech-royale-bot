"""Internal house-bot policies for the local sim: a squad that shares vision and hunts external bots,
weakest house bot first (one early, more as the squad thins, all late). Guesswork modeled on the
track rules, not the real house bots."""
import math
import random

TAU = 2 * math.pi


def wrap(a):
    return (a + math.pi) % TAU - math.pi


LEVELS = {
    #          aim noise (rad), lead quality, react s, strafe, takes loot, rank
    "easy": dict(noise=0.07, lead=0.4, react=0.8, strafe=0.3, loot=False, rank=0),
    "medium": dict(noise=0.045, lead=0.7, react=0.5, strafe=0.6, loot=True, rank=1),
    "hard": dict(noise=0.025, lead=0.9, react=0.3, strafe=0.8, loot=True, rank=2),
    "passive": dict(noise=0.1, lead=0, react=9, strafe=0, loot=False, rank=9),
    "expert": dict(noise=0.012, lead=1.0, react=0.2, strafe=1.0, loot=True, rank=3),
}


import os
HOUSE_MULT = float(os.environ.get("SIM_HOUSE_MULT", 1.0))


class HouseBot:
    def __init__(self, level="medium", ffa=False):
        self.level = level
        self.p = dict(LEVELS[level])
        self.p["noise"] *= HOUSE_MULT
        self.p["react"] *= HOUSE_MULT
        self.name = f"house-{level}"
        self.ffa = ffa
        self.slot = None

    def clone(self, slot):
        h = HouseBot(self.level, self.ffa)
        h.slot = slot
        h.rng = random.Random(slot * 7919 + 13)
        h.path = None
        h.path_t = -99
        h.path_goal = None
        h.strafe = 1
        h.strafe_t = 0
        h.seen_t = {}
        h.wander = None
        return h

    # squad bookkeeping (stored on the match)
    def squad(self, M):
        sq = getattr(M, "_squad", None)
        if sq is None:
            sq = {"members": sorted([s for s in M.house], key=lambda s: (M.house[s].p["rank"], s))}
            M._squad = sq
        return sq

    def hunters(self, M):
        sq = self.squad(M)
        alive = [s for s in sq["members"] if M.mechs[s].alive]
        dead = len(sq["members"]) - len(alive)
        n = 1 + dead
        if M.t > 300:
            n = len(alive)
        elif M.t > 200:
            n = max(n, 3)
        return alive[:n]

    def nav(self, M, m, goal):
        W = M.W
        x, y = m.pos[0], m.pos[1]
        d = math.hypot(goal[0] - x, goal[1] - y)
        if d < 2:
            return 0.0, 0.0
        if d < 50 and W.walk_clear((x, y), goal):
            return (goal[0] - x) / d, (goal[1] - y) / d
        if self.path is None or M.t - self.path_t > 2.0 or self.path_goal is None or \
                math.hypot(goal[0] - self.path_goal[0], goal[1] - self.path_goal[1]) > 8:
            self.path = W.astar((x, y), goal, max_expand=3000)
            self.path_t = M.t
            self.path_goal = goal
        p = self.path
        while p and len(p) > 1 and math.hypot(p[0][0] - x, p[0][1] - y) < 3:
            p.pop(0)
        if not p:
            return (goal[0] - x) / d, (goal[1] - y) / d
        tx, ty = p[0]
        dd = math.hypot(tx - x, ty - y) or 1
        return (tx - x) / dd, (ty - y) / dd

    def act(self, M, m, obs):
        t = M.t
        a = {"tick": obs["tick"]}
        if obs["offer"]:
            a["pick"] = 0
        x, y, z = m.pos
        # take weapons nearby (prefer laser)
        if self.p["loot"] and m.weapons[1] is None:
            for p in M.pickups:
                if p["kind"] in ("laser", "lightning") and math.hypot(p["pos"][0] - x, p["pos"][1] - y) < 3.8:
                    a["take"] = p["id"]
                    break
        # pick a target: the hunted (external bots), shared vision
        externals = [M.mechs[s] for s in M.ext if M.mechs[s].alive]
        if self.ffa:
            externals = [o for o in M.mechs if o.alive and o is not m]
        if self.level == "passive":
            externals = []
        squad_members = [M.mechs[s] for s in M.house if M.mechs[s].alive]
        target = None
        visible = []
        for o in externals:
            if any(M.visible(sm, o.pos, o.stats["height"]) for sm in (squad_members if not self.ffa else [m])):
                visible.append(o)
        hunting = (m.id in self.hunters(M)) and not self.ffa
        if externals:
            weakest = min(externals, key=lambda o: o.hp)
            if hunting:
                target = weakest
            elif visible:
                hurt = getattr(m, "hurt_by", {})
                close = [o for o in visible if math.hypot(o.pos[0] - x, o.pos[1] - y) < 70
                         or t - hurt.get(o.id, -99) < 10]
                if close:
                    target = min(close, key=lambda o: math.hypot(o.pos[0] - x, o.pos[1] - y))
        # zone goal
        zc, zr = M.cur_c, M.cur_r
        nc, nr = M.next_c, M.next_r
        dz = math.hypot(x - nc[0], y - nc[1])
        mv = (0.0, 0.0)
        boost = False
        if target is not None:
            tx, ty = target.pos[0], target.pos[1]
            d = math.hypot(tx - x, ty - y)
            los = M.W.los((x, y, z + 7), (tx, ty, target.pos[2] + 4.5))
            want = 90 if any(w and w["kind"] == "laser" for w in m.weapons) else 60
            if any(w and w["kind"] == "lightning" for w in m.weapons):
                want = 30
            if not los or d > want + 20:
                mv = self.nav(M, m, (tx, ty))
                boost = d > 150 and m.energy > 50
            else:
                ux, uy = (tx - x) / d, (ty - y) / d
                if t > self.strafe_t:
                    self.strafe = -self.strafe
                    self.strafe_t = t + self.rng.uniform(0.6, 1.8)
                rad = -0.6 if d < want - 25 else 0.0
                s = self.p["strafe"]
                mv = (ux * rad - uy * self.strafe * s, uy * rad + ux * self.strafe * s)
            # aim
            st = m.stats
            mz = z + st["muzzle_height"]
            tz = target.pos[2] + target.stats["height"] * 0.5
            gun = next((w for w in m.weapons if w and w["kind"] == "gun"), None)
            spd = gun["stats"]["speed"] if gun else 252
            tt = d / spd * self.p["lead"]
            px, py = tx + target.vel[0] * tt, ty + target.vel[1] * tt
            yaw = math.atan2(py - y, px - x) + self.rng.gauss(0, self.p["noise"])
            pitch = math.atan2(tz - mz, max(d, 1)) + self.rng.gauss(0, self.p["noise"] * 0.5)
            laser = next((w for w in m.weapons if w and w["kind"] == "laser"), None)
            if laser and laser.get("charge", 0) > laser["stats"]["charge"] * 0.7:
                yaw = math.atan2(ty - y, tx - x) + self.rng.gauss(0, self.p["noise"])
            a["aim"] = [yaw, pitch]
            seen = target in visible or d < 45
            if seen:
                self.seen_t.setdefault(target.id, t)
            else:
                self.seen_t.pop(target.id, None)
            ready = seen and los and t - self.seen_t.get(target.id, t) >= self.p["react"]
            err = abs(wrap(m.aim[0] - yaw)) * d
            fire = [False, False]
            for i, w in enumerate(m.weapons):
                if not w:
                    continue
                if w["kind"] == "gun" and ready and err < 4 and d < w["stats"]["range"]:
                    fire[i] = True
                elif w["kind"] == "laser":
                    ch = w.get("charge", 0)
                    if ch > 0:
                        fire[i] = not (ch >= w["stats"]["charge"] and err < 2.5)
                    elif ready and w["cooldown"] <= 0:
                        fire[i] = True
                elif w["kind"] == "lightning" and ready and d < w["stats"]["range"]:
                    fire[i] = True
            a["fire"] = fire
        else:
            # roam: loot or move inside the zone
            goal = None
            if self.p["loot"] and m.weapons[1] is None:
                best = None
                for p in M.pickups:
                    if p["kind"] not in ("laser", "lightning"):
                        continue
                    d = math.hypot(p["pos"][0] - x, p["pos"][1] - y)
                    if d < 120 and math.hypot(p["pos"][0] - nc[0], p["pos"][1] - nc[1]) < max(nr, zr * 0.8) and (best is None or d < best[0]):
                        best = (d, p)
                if best:
                    goal = best[1]["pos"][:2]
            if goal is None:
                if self.wander is None or math.hypot(self.wander[0] - x, self.wander[1] - y) < 5 or \
                        math.hypot(self.wander[0] - nc[0], self.wander[1] - nc[1]) > nr * 0.8:
                    ang = self.rng.uniform(0, TAU)
                    r = nr * 0.6 * math.sqrt(self.rng.random())
                    self.wander = (nc[0] + r * math.cos(ang), nc[1] + r * math.sin(ang))
                goal = self.wander
            mv = self.nav(M, m, goal)
            boost = dz > nr and m.energy > 40
            a["aim"] = [math.atan2(mv[1], mv[0]) + 0.8 * math.sin(t + self.slot) if (mv[0] or mv[1]) else m.aim[0], 0.0]
            a["fire"] = [False, False]
            if M.cur_r and math.hypot(x - zc[0], y - zc[1]) > zr - 5:
                mv = self.nav(M, m, nc)
                boost = True
        a["move"] = list(mv)
        a["boost"] = boost
        # unstick
        if (mv[0] or mv[1]) and math.hypot(m.vel[0], m.vel[1]) < 1.0 and m.grounded and self.rng.random() < 0.2:
            a["jump"] = True
            a["boost"] = True
        return a
