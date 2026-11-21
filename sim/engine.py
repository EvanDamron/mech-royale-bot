"""Approximate local re-implementation of the mechfight/1 match server, for comparing bots.

Not the real server: physics, vision (trees ignored), and house bots are simplified from protocol.md.
Usage: see sim/run.py.
"""
import gzip
import json
import math
import os
import random
import select
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from worldmap import World  # noqa: E402

KIT = os.environ.get("MECH_KIT") or os.path.abspath(os.path.join(HERE, "..", "..", "starter", "mechfight-kit"))
MAP_DIR = os.path.join(KIT, "arena")
TAU = 2 * math.pi


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def load_config():
    with gzip.open(os.path.join(KIT, "check", "stream.jsonl.gz"), "rt") as f:
        hello = json.loads(f.readline())
    return hello["config"]


def get_path(d, path):
    for p in path.split("."):
        d = d[p]
    return d


class Mech:
    def __init__(self, mid, pos, cfg):
        self.id = mid
        self.cfg = cfg
        self.base_mech = dict(cfg["mech"])
        self.base_weapons = {k: dict(v) for k, v in cfg["weapons"].items()}
        self.upgrades = []
        self.cubes = 0
        self.pos = list(pos)
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [random.uniform(-math.pi, math.pi), 0.0]
        self.cmd_aim = list(self.aim)
        self.recompute()
        self.hp = self.stats["hp"]
        self.energy = self.stats["energy"]
        self.grounded = True
        self.weapons = [self.new_weapon("gun"), None]
        self.alive = True
        self.death_tick = None
        self.last_combat = -99.0
        self.last_spend = -99.0
        self.action = {}
        self.fire_prev = [False, False]
        self.last_answer_t = 0.0
        self.coasting = False
        self.kills = 0
        self.offers = []  # pending offers
        self.offer = None
        self.offer_seq = 0
        self.events = []
        self.sounds = []
        self.beams = []
        self.seen = {}  # id -> last seen time
        self.step_t = 0.0
        self.short_until = -1
        self.name = "?"
        self.place = None
        self.late = 0

    def recompute(self):
        m = dict(self.base_mech)
        w = {k: dict(v) for k, v in self.base_weapons.items()}
        root = {"mech": m, "weapons": w}
        ups = {u["id"]: u for u in self.cfg["upgrades"]}
        for uid in self.upgrades:
            for e in ups[uid]["effects"]:
                parts = e["path"].split(".")
                d = root
                for p in parts[:-1]:
                    d = d[p]
                if "mul" in e:
                    d[parts[-1]] *= e["mul"]
                if "add" in e:
                    d[parts[-1]] += e["add"]
        s = 1 + self.cfg["cubes"]["size_per_cube"] * self.cubes
        hpmul = 1 + self.cfg["cubes"]["hp_per_cube"] * min(self.cubes, self.cfg["cubes"]["stat_cap"])
        m["hp"] = m["hp"] * hpmul
        m["radius"] = self.base_mech["radius"] * s
        m["height"] = self.base_mech["height"] * s
        m["eye_height"] = self.base_mech["eye_height"] * s
        m["muzzle_height"] = self.base_mech["muzzle_height"] * s
        self.stats = m
        self.wstats = w
        if hasattr(self, "weapons"):
            for wp in self.weapons:
                if wp:
                    wp["stats"] = self.wstats[wp["kind"]]

    def new_weapon(self, kind):
        w = {"kind": kind, "cooldown": 0.0, "stats": self.wstats[kind]}
        if kind == "gun":
            w["ammo"] = self.wstats["gun"]["mag"]
            w["reload"] = 0.0
        if kind == "laser":
            w["charge"] = 0.0
        return w

    @property
    def dmg_mult(self):
        return 1 + self.cfg["cubes"]["damage_per_cube"] * min(self.cubes, self.cfg["cubes"]["stat_cap"])


class Match:
    def __init__(self, bots, seed=0, house=None, verbose=False, budget_ms=50, strict_time=False,
                 record=None):
        """bots: list of (name, argv) for external bots; house: list of HouseBot policies filling
        remaining slots (internal, full-state)."""
        self.cfg = load_config()
        self.rng = random.Random(seed)
        random.seed(seed)
        self.seed = seed
        self.W = World(MAP_DIR)
        self.verbose = verbose
        self.budget = budget_ms / 1000.0
        self.strict = strict_time
        self.tick = 0
        self.t = 0.0
        self.next_id = {"npc": 100, "crate": 1000, "cube": 5000, "pick": 8000, "drop": 20000}
        self.projectiles = []
        self.cubes = []
        self.crates = []
        self.pickups = []
        self.npcs = []
        self.drops = []
        self.record = record
        n = self.cfg["match"]["mechs"]
        self.setup_zone()
        spawns = self.spawn_points(n)
        slots = list(range(n))
        self.rng.shuffle(slots)
        self.mechs = [Mech(i, spawns[i], self.cfg) for i in range(n)]
        self.ext = {}  # slot -> process
        self.house = {}
        for k, (name, argv) in enumerate(bots):
            s = slots[k]
            self.mechs[s].name = name
            self.ext[s] = argv
        hb = house or []
        for k, s in enumerate(slots[len(bots):]):
            pol = hb[k % len(hb)] if hb else None
            if pol is not None:
                pol = pol.clone(s)
                self.house[s] = pol
                self.mechs[s].name = pol.name
        self.setup_loot()
        self.procs = {}
        self._vis_cache = {}
        self._vis_tick = -1

    # ------------------------------------------------------------------ setup
    def rand_ground(self, center=None, radius=None, tries=500):
        W = self.W
        b = W.bounds
        for _ in range(tries):
            if center is None:
                x = self.rng.uniform(b["minX"] + 30, b["maxX"] - 30)
                y = self.rng.uniform(b["minY"] + 30, b["maxY"] - 30)
            else:
                a = self.rng.uniform(0, TAU)
                r = radius * math.sqrt(self.rng.random())
                x, y = center[0] + r * math.cos(a), center[1] + r * math.sin(a)
            if W.in_bounds(x, y, 30) and W.free_at(x, y):
                return [x, y, W.terrain(x, y)]
        return None

    def spawn_points(self, n):
        zc, zr = self.zone_c, self.zone_r * self.cfg["match"]["spawn_zone_fraction"]
        sep = self.cfg["match"]["spawn_min_separation"]
        pts = []
        while len(pts) < n:
            for _ in range(2000):
                p = self.rand_ground(zc, zr)
                if p and all(math.hypot(p[0] - q[0], p[1] - q[1]) >= sep for q in pts):
                    pts.append(p)
                    break
            else:
                sep *= 0.9
        return pts

    def setup_zone(self):
        z = self.cfg["zone"]
        self.phases = z["phases"]
        self.zone_c = list(z["start_center"])
        self.zone_r = z["start_radius"]
        self.phase = 0
        self.phase_start = 0.0
        self.pick_next()

    def pick_next(self):
        ph = self.phases[self.phase]
        nr = ph["radius"]
        for _ in range(200):
            a = self.rng.uniform(0, TAU)
            r = (self.zone_r - nr) * math.sqrt(self.rng.random())
            c = [self.zone_c[0] + r * math.cos(a), self.zone_c[1] + r * math.sin(a)]
            b = self.W.bounds
            if b["minX"] + 60 < c[0] < b["maxX"] - 60 and b["minY"] + 60 < c[1] < b["maxY"] - 60:
                break
        self.next_c = c
        self.next_r = nr
        self.shrink_start = self.phase_start + ph["hold_s"]
        self.shrink_end = self.shrink_start + ph["shrink_s"]
        self.zone_dps = ph["dps"]
        self.from_c = list(self.zone_c)
        self.from_r = self.zone_r

    def zone_update(self):
        t = self.t
        if t < self.shrink_start:
            self.cur_c, self.cur_r = self.from_c, self.from_r
        elif t < self.shrink_end:
            f = (t - self.shrink_start) / (self.shrink_end - self.shrink_start)
            self.cur_c = [self.from_c[0] + (self.next_c[0] - self.from_c[0]) * f,
                          self.from_c[1] + (self.next_c[1] - self.from_c[1]) * f]
            self.cur_r = self.from_r + (self.next_r - self.from_r) * f
        else:
            self.zone_c, self.zone_r = list(self.next_c), self.next_r
            self.cur_c, self.cur_r = self.zone_c, self.zone_r
            if self.phase + 1 < len(self.phases):
                self.phase += 1
                self.phase_start = self.shrink_end
                self.pick_next()
            else:
                self.from_c, self.from_r = self.zone_c, self.zone_r
                self.shrink_start = self.shrink_end = 1e9

    def nid(self, k):
        v = self.next_id[k]
        self.next_id[k] += 1
        return v

    def setup_loot(self):
        c = self.cfg
        kinds = [k for k, w in c["loot"]["weights"].items() for _ in range(int(w * 10))]
        for _ in range(c["crates"]["count"]):
            p = self.rand_ground()
            if p:
                self.crates.append({"id": self.nid("crate"), "pos": p, "hp": c["crates"]["hp"]})
        for _ in range(c["loot"]["floor_count"]):
            p = self.rand_ground()
            if p:
                self.pickups.append({"id": self.nid("pick"), "kind": self.rng.choice(kinds), "pos": p})
        for _ in range(c["npc"]["count"]):
            p = self.rand_ground()
            if p:
                self.npcs.append({"id": self.nid("npc"), "pos": p, "home": list(p), "hp": c["npc"]["hp"],
                                  "vel": [0, 0, 0], "aim": [0, 0], "target": None, "cd": 0.0,
                                  "wander": list(p), "alive": True})
        self.drop_times = list(c["drops"]["times"])
        self.kinds = kinds

    # ------------------------------------------------------------------ processes
    def start(self):
        hello_base = {"type": "hello", "protocol": "mechfight/1",
                      "map": {"dir": MAP_DIR, "files": ["v2/world.json", "v2/terrain.i16"]},
                      "config": self.cfg}
        for s, argv in self.ext.items():
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=open(os.devnull, "w") if not self.verbose else None,
                                 bufsize=1, text=True)
            self.procs[s] = p
            h = dict(hello_base, slot=s, seed=self.rng.randrange(1 << 31))
            p.stdin.write(json.dumps(h) + "\n")
            p.stdin.flush()
        for s, p in self.procs.items():
            line = p.stdout.readline()
            try:
                msg = json.loads(line)
                if msg.get("name"):
                    self.mechs[s].name = msg["name"][:32]
            except ValueError:
                pass

    def stop(self):
        for s, p in self.procs.items():
            m = self.mechs[s]
            try:
                p.stdin.write(json.dumps({"type": "end", "place": m.place, "kills": m.kills}) + "\n")
                p.stdin.close()
            except Exception:
                pass
        for p in self.procs.values():
            try:
                p.wait(timeout=2)
            except Exception:
                p.kill()

    # ------------------------------------------------------------------ main loop
    def run(self):
        self.start()
        dt = 1.0 / self.cfg["match"]["tick_hz"]
        dur = self.cfg["match"]["duration_s"]
        self.zone_update()
        try:
            while True:
                alive = [m for m in self.mechs if m.alive]
                if len(alive) <= 1 or self.t >= dur:
                    break
                if self.tick % self.cfg["match"]["decide_every"] == 0:
                    self.decide()
                for _ in range(self.cfg["match"]["decide_every"]):
                    self.step(dt)
                    self.tick += 1
                    self.t = self.tick * dt
                    self.zone_update()
        finally:
            self.finish()
            self.stop()
        return self.results()

    def finish(self):
        alive = [m for m in self.mechs if m.alive]
        # survivors ranked by HP
        alive.sort(key=lambda m: -m.hp)
        place = 1
        for m in alive:
            m.place = place
            place += 1
        dead = sorted([m for m in self.mechs if not m.alive], key=lambda m: -m.death_tick)
        i = 0
        while i < len(dead):
            j = i
            while j < len(dead) and dead[j].death_tick == dead[i].death_tick:
                j += 1
            for k in range(i, j):
                dead[k].place = place
            place += j - i
            i = j
        # same-tick deaths share the best place among them (16 - place points)

    def results(self):
        return [{"slot": m.id, "name": m.name, "place": m.place, "kills": m.kills, "cubes": m.cubes,
                 "death_t": (m.death_tick or self.tick) / 20.0, "late": m.late,
                 "drops": getattr(m, "drops_taken", 0)} for m in self.mechs]

    # ------------------------------------------------------------------ decisions
    def decide(self):
        obs_by = {}
        for m in self.mechs:
            if not m.alive:
                continue
            if m.offer is None and m.offers:
                m.offer = m.offers.pop(0)
                m.offer["expires"] = self.t + self.cfg["bot"]["offer_timeout_s"]
            if m.offer is not None and self.t > m.offer["expires"]:
                self.apply_upgrade(m, m.offer["choices"][0]["id"])
                m.offer = None
            obs_by[m.id] = self.observe(m)
        # external bots
        pending = {}
        for s, p in self.procs.items():
            m = self.mechs[s]
            if not m.alive or p.poll() is not None:
                continue
            try:
                p.stdin.write(json.dumps(obs_by[s], separators=(",", ":")) + "\n")
                p.stdin.flush()
                pending[p.stdout.fileno()] = s
            except BrokenPipeError:
                pass
        answers = {}
        t0 = time.perf_counter()
        limit = self.budget if self.strict else max(self.budget * 6, 0.3)
        while pending:
            left = limit - (time.perf_counter() - t0)
            if left <= 0:
                break
            r, _, _ = select.select(list(pending.keys()), [], [], left)
            for fd in r:
                s = pending[fd]
                line = self.procs[s].stdout.readline()
                if not line:
                    del pending[fd]
                    continue
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                if a.get("tick") == self.tick:
                    answers[s] = a
                    del pending[fd]
                    if time.perf_counter() - t0 > self.budget:
                        self.mechs[s].late += 1
        for s in pending.values():
            self.mechs[s].late += 1
        for s, pol in self.house.items():
            m = self.mechs[s]
            if m.alive:
                answers[s] = pol.act(self, m, obs_by[s])
        for m in self.mechs:
            if not m.alive:
                continue
            a = answers.get(m.id)
            if a is None:
                # coast: keep continuous parts, then idle
                if self.t - m.last_answer_t > self.cfg["bot"]["coast_s"]:
                    m.action = {"aim": m.action.get("aim", m.cmd_aim)}
                else:
                    m.action = {k: m.action[k] for k in ("move", "boost", "aim") if k in m.action}
                continue
            m.last_answer_t = self.t
            self.apply_action(m, a)
        for m in self.mechs:
            m.events = []
            m.sounds = []
            m.beams = []

    def apply_action(self, m, a):
        prev_aim = m.action.get("aim", m.cmd_aim)
        m.action = a
        if "aim" not in a:
            a["aim"] = prev_aim
        mv = a.get("move", [0, 0])
        if mv is None:
            mv = [0, 0]
        if "aim" in a and a["aim"]:
            m.cmd_aim = [float(a["aim"][0]), max(m.stats["min_pitch"], min(m.stats["max_pitch"], float(a["aim"][1])))]
        if a.get("pick") is not None and m.offer is not None:
            i = int(a["pick"])
            if 0 <= i < len(m.offer["choices"]):
                self.apply_upgrade(m, m.offer["choices"][i]["id"])
                m.offer = None
        if a.get("reload") and m.weapons[0] is not None:
            for w in m.weapons:
                if w and w["kind"] == "gun" and w["reload"] <= 0 and w["ammo"] < w["stats"]["mag"]:
                    w["reload"] = w["stats"]["reload"]
                    self.sound(m.pos, "reload")
        if a.get("take") is not None:
            self.do_take(m, a["take"], a.get("drop"))
        elif a.get("drop") is not None:
            sl = int(a["drop"])
            w = m.weapons[sl] if 0 <= sl < 2 else None
            if w and not (w["kind"] == "laser" and w.get("charge", 0) > 0):
                self.drop_weapon(m, sl)

    def do_take(self, m, pid, drop):
        p = next((p for p in self.pickups if p["id"] == pid), None)
        if p is None:
            m.events.append({"type": "take_failed", "id": pid, "reason": "gone"})
            return
        if math.hypot(p["pos"][0] - m.pos[0], p["pos"][1] - m.pos[1]) > self.cfg["loot"]["pickup_radius"] \
                or abs(p["pos"][2] - m.pos[2]) > 4:
            m.events.append({"type": "take_failed", "id": pid, "reason": "range"})
            return
        if any(w and w["kind"] == p["kind"] for w in m.weapons):
            m.events.append({"type": "take_failed", "id": pid, "reason": "have"})
            return
        sl = None
        if drop is not None and 0 <= int(drop) < 2:
            sl = int(drop)
            w = m.weapons[sl]
            if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                m.events.append({"type": "take_failed", "id": pid, "reason": "busy"})
                return
            if w:
                self.drop_weapon(m, sl)
        else:
            for i in range(2):
                if m.weapons[i] is None:
                    sl = i
                    break
        if sl is None:
            m.events.append({"type": "take_failed", "id": pid, "reason": "full"})
            return
        self.pickups.remove(p)
        w = p.get("w") or m.new_weapon(p["kind"])
        w["stats"] = m.wstats[p["kind"]]
        w["cooldown"] = max(self.cfg["loot"]["equip_s"], w["stats"].get("cooldown", 0))
        if w["kind"] == "laser":
            w["charge"] = 0.0
        m.weapons[sl] = w
        m.events.append({"type": "took", "id": pid, "kind": p["kind"], "slot": sl})

    def drop_weapon(self, m, sl):
        w = m.weapons[sl]
        m.weapons[sl] = None
        pid = self.nid("pick")
        self.pickups.append({"id": pid, "kind": w["kind"], "pos": list(m.pos), "w": w})
        m.events.append({"type": "dropped", "id": pid, "kind": w["kind"], "slot": sl})

    def apply_upgrade(self, m, uid):
        m.upgrades.append(uid)
        hp_frac_before = m.stats["hp"]
        m.recompute()
        m.hp += max(0, m.stats["hp"] - hp_frac_before)
        m.events.append({"type": "upgrade", "id": uid})

    def make_offer(self, m):
        held = {w["kind"] for w in m.weapons if w}
        avail = []
        for u in self.cfg["upgrades"]:
            if m.upgrades.count(u["id"]) >= u["max"]:
                continue
            if u.get("requires") and u["requires"] not in held:
                continue
            avail.append(u)
        self.rng.shuffle(avail)
        ch = [{"id": u["id"], "name": u["name"], "desc": u["desc"]} for u in avail[:3]]
        if ch:
            m.offer_seq += 1
            m.offers.append({"id": m.offer_seq, "expires": 0, "choices": ch})

    def add_cube(self, m, n=1):
        before = m.cubes
        old_max = m.stats["hp"]
        m.cubes += n
        m.recompute()
        m.hp += max(0, m.stats["hp"] - old_max)
        for th in self.cfg["cubes"]["upgrade_thresholds"]:
            if before < th <= m.cubes:
                self.make_offer(m)

    # ------------------------------------------------------------------ observation
    def visible(self, m, p, top=None, fov=True):
        """Can mech m see point p (feet)? top: height of object. Cached per tick."""
        key = (m.id, id(p), top)
        c = self._vis_cache
        if self._vis_tick != self.tick:
            c.clear()
            self._vis_tick = self.tick
        r = c.get(key)
        if r is None:
            r = c[key] = self._visible(m, p, top)
        return r

    def _visible(self, m, p, top=None):
        ex, ey, ez = m.pos[0], m.pos[1], m.pos[2] + m.stats["eye_height"]
        dx, dy = p[0] - ex, p[1] - ey
        d = math.hypot(dx, dy)
        near = self.cfg["vision"]["near_range"] + m.stats.get("near_range_bonus", 0)
        rng = self.cfg["vision"]["range"] + m.stats.get("vision_bonus", 0)
        if d > rng:
            return False
        if d > near:
            if abs(wrap(math.atan2(dy, dx) - m.aim[0])) > math.radians(self.cfg["vision"]["fov_deg"]) / 2:
                return False
        h = top or 1.0
        if self.W.los((ex, ey, ez), (p[0], p[1], p[2] + h * 0.5)):
            return True
        return self.W.los((ex, ey, ez), (p[0], p[1], p[2] + h))

    def observe(self, m):
        t = self.t
        mechs = []
        mem = self.cfg["vision"]["memory_s"]
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if self.visible(m, o.pos, o.stats["height"]):
                m.seen[o.id] = t
            if t - m.seen.get(o.id, -99) <= mem:
                mechs.append({"id": o.id, "kind": "mech", "pos": [round(v, 2) for v in o.pos],
                              "vel": [round(v, 2) for v in o.vel], "aim": [round(o.aim[0], 3), round(o.aim[1], 3)],
                              "hp": round(o.hp, 1), "max_hp": round(o.stats["hp"], 1), "cubes": o.cubes,
                              "weapons": [w["kind"] for w in o.weapons if w],
                              "charging": any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in o.weapons)})
        for n in self.npcs:
            if not n["alive"]:
                continue
            if math.hypot(n["pos"][0] - m.pos[0], n["pos"][1] - m.pos[1]) > 210:
                continue
            if self.visible(m, n["pos"], 3.5):
                m.seen[n["id"]] = t
            if t - m.seen.get(n["id"], -99) <= mem:
                mechs.append({"id": n["id"], "kind": "npc", "pos": [round(v, 2) for v in n["pos"]],
                              "vel": n["vel"], "aim": n["aim"], "hp": n["hp"], "max_hp": self.cfg["npc"]["hp"]})

        def vis_list(items, h, fields):
            out = []
            for it in items:
                p = it["pos"]
                if abs(p[0] - m.pos[0]) > 205 or abs(p[1] - m.pos[1]) > 205:
                    continue
                if self.visible(m, p, h):
                    out.append({k: it[k] for k in fields})
            return out
        projs = []
        for pr in self.projectiles:
            if pr["owner"] == m.id:
                continue
            p = pr["pos"]
            if math.hypot(p[0] - m.pos[0], p[1] - m.pos[1]) < 200:
                projs.append({"id": pr["id"], "kind": pr["kind"], "pos": [round(v, 2) for v in p],
                              "vel": [round(v, 2) for v in pr["vel"]]})
        st = dict(m.stats)
        weapons = []
        for w in m.weapons:
            if w is None:
                weapons.append(None)
                continue
            o = {"kind": w["kind"], "cooldown": round(max(0, w["cooldown"]), 3), "stats": w["stats"]}
            if w["kind"] == "gun":
                o["ammo"] = w["ammo"]
                o["reload"] = round(max(0, w["reload"]), 3)
            if w["kind"] == "laser":
                o["charge"] = round(w.get("charge", 0), 3)
            weapons.append(o)
        in_zone = math.hypot(m.pos[0] - self.cur_c[0], m.pos[1] - self.cur_c[1]) <= self.cur_r
        obs = {
            "type": "obs", "tick": self.tick, "t": round(self.t, 2),
            "alive": sum(1 for o in self.mechs if o.alive),
            "self": {"id": m.id, "pos": [round(v, 2) for v in m.pos], "vel": [round(v, 2) for v in m.vel],
                     "aim": [round(m.aim[0], 4), round(m.aim[1], 4)], "hp": round(m.hp, 1),
                     "max_hp": round(m.stats["hp"], 1), "energy": round(m.energy, 1),
                     "max_energy": m.stats["energy"], "grounded": m.grounded, "cubes": m.cubes,
                     "upgrades": list(m.upgrades), "weapons": weapons, "in_zone": in_zone, "stats": st},
            "mechs": mechs,
            "projectiles": projs,
            "crates": vis_list(self.crates, 2.4, ("id", "pos", "hp")),
            "cubes": vis_list(self.cubes, 0.5, ("id", "pos")),
            "pickups": vis_list(self.pickups, 1.0, ("id", "kind", "pos")),
            "sounds": m.sounds,
            "beams": m.beams,
            "events": m.events,
            "drops": [{"id": d["id"], "pos": d["pos"], "radius": self.cfg["drops"]["radius"],
                       "lands_in": round(max(0, d["lands"] - self.t), 2), "capture_s": self.cfg["drops"]["capture_s"],
                       "progress": round(d["progress"], 2), "holder": d["holder"]} for d in self.drops],
            "zone": {"center": [round(v, 2) for v in self.from_c], "radius": self.from_r,
                     "next_center": [round(v, 2) for v in self.next_c], "next_radius": self.next_r,
                     "shrink_start": self.shrink_start, "shrink_end": self.shrink_end, "dps": self.zone_dps},
            "offer": m.offer and {"id": m.offer["id"], "expires": m.offer["expires"], "choices": m.offer["choices"]},
        }
        return obs

    # ------------------------------------------------------------------ sounds
    def sound(self, pos, kind, src=None):
        rng = self.cfg["sound"]["range"].get(kind, 100)
        for m in self.mechs:
            if not m.alive or m is src:
                continue
            d = math.hypot(pos[0] - m.pos[0], pos[1] - m.pos[1])
            if d > rng * m.stats.get("hearing", 1) ** 0 or d < 0.01:
                if d >= 0.01:
                    continue
            noise = math.radians(self.cfg["sound"]["bearing_noise_deg"]) * m.stats.get("hearing", 1)
            br = math.atan2(pos[1] - m.pos[1], pos[0] - m.pos[0]) + self.rng.gauss(0, noise)
            dist = "near" if d < self.cfg["sound"]["near"] else "mid" if d < self.cfg["sound"]["mid"] else "far"
            m.sounds.append({"kind": kind, "bearing": round(wrap(br), 3), "dist": dist, "age": 0.05})

    # ------------------------------------------------------------------ physics
    def blocked_at(self, x, y, z, r, h):
        W = self.W
        if not W.in_bounds(x, y, r):
            return True
        ri = int(math.ceil(r))
        j0, i0 = W.cell(x, y)
        S, ovb, ovt = W.S, W.ovb, W.ovt
        lim = z + self.cfg["mech"]["step_up"]
        head = z + h
        for dj in range(-ri, ri + 1):
            for di in range(-ri, ri + 1):
                if di * di + dj * dj > (r + 0.3) ** 2:
                    continue
                j, i = j0 + dj, i0 + di
                if j < 0 or i < 0 or j >= W.ny or i >= W.nx:
                    return True
                if S[j, i] > lim:
                    return True
                if ovb[j, i] < head and ovt[j, i] > lim:
                    return True
        return False

    def floor_at(self, x, y, z):
        W = self.W
        j, i = W.cell(x, y)
        f = W.terrain(x, y)
        g = float(W.gtop[j, i])
        lim = z + self.cfg["mech"]["step_up"]
        if g > f and g <= lim:
            f = g
        ot = float(W.ovt[j, i])
        if ot > f and ot <= lim:
            f = ot
        return f

    def step(self, dt):
        cfg = self.cfg
        for m in self.mechs:
            if not m.alive:
                continue
            st = m.stats
            a = m.action
            # aim slew
            dy = wrap(m.cmd_aim[0] - m.aim[0])
            mx = st["turn_rate"] * dt
            m.aim[0] = wrap(m.aim[0] + max(-mx, min(mx, dy)))
            dp = m.cmd_aim[1] - m.aim[1]
            mp = st["pitch_rate"] * dt
            m.aim[1] += max(-mp, min(mp, dp))
            # movement
            mv = a.get("move") or [0, 0]
            mvx, mvy = float(mv[0]), float(mv[1])
            n = math.hypot(mvx, mvy)
            if n > 1:
                mvx, mvy = mvx / n, mvy / n
            charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in m.weapons)
            boosting = bool(a.get("boost")) and m.energy > 0
            spd = st["walk_speed"]
            if boosting and m.grounded and n > 0.05:
                spd = st["boost_speed"]
                m.energy -= st["boost_drain"] * dt
                m.last_spend = self.t
            if charging:
                spd *= cfg["mech"]["charge_move_mult"]
            tvx, tvy = mvx * spd, mvy * spd
            acc = (st["ground_accel"] if m.grounded else st["air_accel"]) * dt
            ddx, ddy = tvx - m.vel[0], tvy - m.vel[1]
            dn = math.hypot(ddx, ddy)
            if dn > acc:
                ddx, ddy = ddx / dn * acc, ddy / dn * acc
            m.vel[0] += ddx
            m.vel[1] += ddy
            if a.get("jump") and m.grounded:
                m.vel[2] = st["jump_speed"]
                m.grounded = False
                a["jump"] = False
            if boosting and not m.grounded:
                m.vel[2] = min(m.vel[2] + st["jet_accel"] * dt, st["max_rise_speed"])
                m.energy -= st["jet_drain"] * dt
                m.last_spend = self.t
                if self.tick % 10 == 0:
                    self.sound(m.pos, "jet", m)
            if not m.grounded:
                m.vel[2] -= st["gravity"] * dt
            m.energy = max(0.0, m.energy)
            if self.t - m.last_spend > st["energy_regen_delay"]:
                m.energy = min(st["energy"], m.energy + st["energy_regen"] * dt)
            # horizontal move with sliding
            r, h = st["radius"], st["height"]
            nx, ny = m.pos[0] + m.vel[0] * dt, m.pos[1] + m.vel[1] * dt
            if (nx, ny) != (m.pos[0], m.pos[1]):
                if not self.blocked_at(nx, ny, m.pos[2], r, h):
                    m.pos[0], m.pos[1] = nx, ny
                elif not self.blocked_at(nx, m.pos[1], m.pos[2], r, h):
                    m.pos[0] = nx
                    m.vel[1] = 0
                elif not self.blocked_at(m.pos[0], ny, m.pos[2], r, h):
                    m.pos[1] = ny
                    m.vel[0] = 0
                else:
                    m.vel[0] = m.vel[1] = 0
            # vertical
            fl = self.floor_at(m.pos[0], m.pos[1], m.pos[2])
            nz = m.pos[2] + m.vel[2] * dt
            if m.vel[2] > 0:
                # ceiling
                j, i = self.W.cell(m.pos[0], m.pos[1])
                if float(self.W.ovb[j, i]) < nz + h and float(self.W.ovb[j, i]) > m.pos[2] + h - 0.5:
                    m.vel[2] = 0
                    nz = m.pos[2]
            if nz <= fl:
                m.pos[2] = fl
                if m.vel[2] < 0:
                    m.vel[2] = 0
                m.grounded = True
            else:
                if m.grounded and nz - fl < 0.6 and m.vel[2] <= 0:
                    m.pos[2] = fl  # stick to slopes going down
                else:
                    m.pos[2] = nz
                    m.grounded = False
            # step sounds
            sp = math.hypot(m.vel[0], m.vel[1])
            if m.grounded and sp > cfg["sound"]["step_speed"]:
                m.step_t += dt
                if m.step_t >= cfg["sound"]["step_every"]:
                    m.step_t = 0
                    self.sound(m.pos, "boost" if boosting else "step", m)
            # zone
            dz = math.hypot(m.pos[0] - self.cur_c[0], m.pos[1] - self.cur_c[1])
            if dz > self.cur_r:
                self.damage(m, self.zone_dps * dt, None, "zone", None)
                m.last_combat = self.t
            elif self.t - m.last_combat > st["regen_delay"]:
                m.hp = min(st["hp"], m.hp + st["regen_per_s"] * dt)
            # weapons
            self.weapons_tick(m, dt)
        # push apart
        al = [m for m in self.mechs if m.alive]
        for i in range(len(al)):
            for j in range(i + 1, len(al)):
                a, b = al[i], al[j]
                dx, dy = b.pos[0] - a.pos[0], b.pos[1] - a.pos[1]
                d = math.hypot(dx, dy)
                rr = a.stats["radius"] + b.stats["radius"]
                if d < rr and abs(a.pos[2] - b.pos[2]) < 9:
                    if d < 1e-3:
                        dx, dy, d = 1.0, 0.0, 1.0
                    push = (rr - d) / 2
                    for mm, sg in ((a, -1), (b, 1)):
                        nx = mm.pos[0] + sg * dx / d * push
                        ny = mm.pos[1] + sg * dy / d * push
                        if not self.blocked_at(nx, ny, mm.pos[2], mm.stats["radius"], mm.stats["height"]):
                            mm.pos[0], mm.pos[1] = nx, ny
        self.projectiles_tick(dt)
        self.npcs_tick(dt)
        self.loot_tick(dt)

    # ------------------------------------------------------------------ weapons
    def muzzle(self, m, extra=0.0):
        cy, sy = math.cos(m.aim[0]), math.sin(m.aim[0])
        cp, sp = math.cos(m.aim[1]), math.sin(m.aim[1])
        d = (cp * cy, cp * sy, sp)
        h = m.stats["muzzle_height"] + extra
        o = (m.pos[0], m.pos[1], m.pos[2] + h)
        out = m.stats["radius"] + 0.5
        f = self.W.ray_hit(o, (o[0] + d[0] * out, o[1] + d[1] * out, o[2] + d[2] * out), step=0.5)
        out *= max(0.0, f - 0.05)
        return [o[0] + d[0] * out, o[1] + d[1] * out, o[2] + d[2] * out], d

    def weapons_tick(self, m, dt):
        fire = m.action.get("fire") or [False, False]
        for sl in range(2):
            w = m.weapons[sl]
            if w is None:
                continue
            trig = bool(fire[sl]) if sl < len(fire) else False
            w["cooldown"] = max(0.0, w["cooldown"] - dt)
            s = w["stats"]
            k = w["kind"]
            if k == "gun":
                if w["reload"] > 0:
                    w["reload"] -= dt
                    if w["reload"] <= 0:
                        w["reload"] = 0.0
                        w["ammo"] = s["mag"]
                    continue
                if trig and w["cooldown"] <= 0 and w["ammo"] > 0:
                    o, d = self.muzzle(m)
                    sp = math.radians(s["spread_deg"])
                    yaw = m.aim[0] + self.rng.gauss(0, sp)
                    pit = m.aim[1] + self.rng.gauss(0, sp)
                    d = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                    self.projectiles.append({"id": self.nid("pick") + 100000, "kind": "gun", "owner": m.id,
                                             "pos": o, "vel": [d[0] * s["speed"], d[1] * s["speed"], d[2] * s["speed"]],
                                             "left": s["range"], "damage": s["damage"] * m.dmg_mult})
                    w["ammo"] -= 1
                    w["cooldown"] += s["cooldown"]
                    self.sound(m.pos, "gun", m)
                    if w["ammo"] <= 0:
                        w["reload"] = s["reload"]
                        self.sound(m.pos, "reload", m)
            elif k == "laser":
                if self.t < m.short_until:
                    w["charge"] = 0.0
                    continue
                if w["charge"] > 0:
                    w["charge"] += dt
                    full = s["charge"]
                    if (not trig and w["charge"] >= s["min_charge"]) or w["charge"] >= full + s["hold_s"]:
                        self.fire_laser(m, w)
                elif trig and w["cooldown"] <= 0:
                    w["charge"] = dt
                    if self.tick % 4 == 0:
                        self.sound(m.pos, "laser_charge", m)
            elif k == "lightning":
                if trig and w["cooldown"] <= 0 and m.energy >= s["energy"]:
                    m.energy -= s["energy"]
                    m.last_spend = self.t
                    w["cooldown"] = s["cooldown"]
                    self.fire_lightning(m, w)
            elif k == "mortar":
                if trig and w["cooldown"] <= 0:
                    w["cooldown"] = s["cooldown"]
                    o, d = self.muzzle(m, extra=m.stats["height"] + 0.5 - m.stats["muzzle_height"])
                    self.projectiles.append({"id": self.nid("pick") + 100000, "kind": "mortar", "owner": m.id,
                                             "pos": o, "vel": [d[0] * s["speed"], d[1] * s["speed"], d[2] * s["speed"]],
                                             "ttl": s["ttl"], "stats": dict(s), "mult": m.dmg_mult})
                    self.sound(m.pos, "mortar", m)

    def targets(self, exclude=None):
        out = []
        for o in self.mechs:
            if o.alive and o is not exclude:
                out.append(("mech", o, o.pos, o.stats["radius"], o.stats["height"]))
        for n in self.npcs:
            if n["alive"]:
                out.append(("npc", n, n["pos"], self.cfg["npc"]["radius"], self.cfg["npc"]["height"]))
        for c in self.crates:
            out.append(("crate", c, c["pos"], self.cfg["crates"]["radius"], self.cfg["crates"]["height"]))
        return out

    @staticmethod
    def seg_cyl(p0, p1, c, r, h):
        """Return fraction along p0->p1 where it enters the vertical cylinder, or None."""
        dx, dy, dz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        fx, fy = p0[0] - c[0], p0[1] - c[1]
        a = dx * dx + dy * dy
        best = None
        if a < 1e-9:
            if fx * fx + fy * fy <= r * r:
                best = 0.0
        else:
            b = 2 * (fx * dx + fy * dy)
            cc = fx * fx + fy * fy - r * r
            disc = b * b - 4 * a * cc
            if disc < 0:
                return None
            sq = math.sqrt(disc)
            t0 = (-b - sq) / (2 * a)
            t1 = (-b + sq) / (2 * a)
            if t1 < 0 or t0 > 1:
                return None
            best = max(t0, 0.0)
            # check z at entry and within
            for tt in (best, min(t1, 1.0)):
                z = p0[2] + dz * tt
                if c[2] <= z <= c[2] + h:
                    return tt
            return None
        z = p0[2]
        if best is not None and c[2] <= z <= c[2] + h:
            return best
        return None

    def first_hit(self, p0, p1, exclude):
        hits = []
        for kind, obj, pos, r, h in self.targets(exclude):
            if abs(pos[0] - p0[0]) > abs(p1[0] - p0[0]) + 10 and abs(pos[1] - p0[1]) > abs(p1[1] - p0[1]) + 10:
                continue
            f = self.seg_cyl(p0, p1, pos, r, h)
            if f is not None:
                hits.append((f, kind, obj))
        hits.sort(key=lambda x: x[0])
        return hits

    def projectiles_tick(self, dt):
        keep = []
        g = self.cfg["weapons"]["mortar"]["gravity"]
        for pr in self.projectiles:
            p0 = pr["pos"]
            if pr["kind"] == "mortar":
                pr["vel"][2] -= g * dt
                pr["ttl"] -= dt
            p1 = [p0[0] + pr["vel"][0] * dt, p0[1] + pr["vel"][1] * dt, p0[2] + pr["vel"][2] * dt]
            owner = self.mechs[pr["owner"]] if pr["owner"] is not None and pr["owner"] < 100 else None
            wf = self.W.ray_hit(p0, p1, step=1.0)
            hits = self.first_hit(p0, p1, owner if pr["kind"] != "mortar" or pr.get("age", 0) > 0.2 else owner)
            pr["age"] = pr.get("age", 0) + dt
            hits = [h for h in hits if h[0] <= wf]
            if pr["kind"] == "npc":
                hits = [h for h in hits if h[1] == "mech"]
            if hits:
                f, kind, obj = hits[0]
                hp = [p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f, p0[2] + (p1[2] - p0[2]) * f]
                if pr["kind"] == "mortar":
                    self.explode(pr, hp, direct=(kind, obj))
                else:
                    self.hit_obj(kind, obj, pr["damage"], owner, pr["kind"], src=p0)
                continue
            if wf < 1.0:
                if pr["kind"] == "mortar":
                    hp = [p0[0] + (p1[0] - p0[0]) * wf, p0[1] + (p1[1] - p0[1]) * wf, p0[2] + (p1[2] - p0[2]) * wf]
                    self.explode(pr, hp, None)
                continue
            pr["pos"] = p1
            if pr["kind"] in ("gun", "npc"):
                pr["left"] -= math.hypot(*(pr["vel"])) * dt
                if pr["left"] <= 0:
                    continue
            elif pr["ttl"] <= 0:
                continue
            keep.append(pr)
        self.projectiles = keep

    def explode(self, pr, at, direct):
        s = pr["stats"]
        owner = self.mechs[pr["owner"]]
        mult = pr["mult"]
        self.sound(at, "explosion")
        if direct:
            self.hit_obj(direct[0], direct[1], s["damage"] * mult, owner, "mortar", src=at)
        R = s["splash_radius"]
        for kind, obj, pos, r, h in self.targets(None):
            if direct and obj is direct[1]:
                continue
            c = (pos[0], pos[1], pos[2] + h / 2)
            d = math.sqrt((c[0] - at[0]) ** 2 + (c[1] - at[1]) ** 2 + (c[2] - at[2]) ** 2) - r
            if d > R:
                continue
            fall = 1 - (1 - s["splash_edge"]) * max(0, d) / R
            if not self.W.los((at[0], at[1], at[2] + 0.5), c):
                continue
            dmg = s["splash"] * fall * mult
            if obj is owner:
                dmg *= self.cfg["mech"]["self_damage_mult"]
            self.hit_obj(kind, obj, dmg, owner, "mortar", src=at, self_ok=True)
            if kind == "mech":
                dx, dy = c[0] - at[0], c[1] - at[1]
                dn = math.hypot(dx, dy) or 1
                obj.vel[0] += dx / dn * s["knock"] * fall
                obj.vel[1] += dy / dn * s["knock"] * fall
                obj.vel[2] += s["knock_up"] * fall
                obj.grounded = False
                obj.energy = max(0, obj.energy - s["drain"] * fall)

    def fire_laser(self, m, w):
        s = w["stats"]
        frac = min(1.0, w["charge"] / s["charge"])
        dmg = s["damage"] * (s["min_frac"] + (1 - s["min_frac"]) * frac) * m.dmg_mult
        w["charge"] = 0.0
        w["cooldown"] = s["cooldown"]
        o, d = self.muzzle(m)
        end = [o[0] + d[0] * s["range"], o[1] + d[1] * s["range"], o[2] + d[2] * s["range"]]
        wf = self.W.ray_hit(o, end, step=1.0)
        hits = [h for h in self.first_hit(o, end, m) if h[0] <= wf]
        pierce = int(s.get("pierce", 0))
        for f, kind, obj in hits[:1 + pierce]:
            mult = 1.0
            if kind == "mech" and any(x and x["kind"] == "mortar" for x in obj.weapons):
                mult += s["cookoff"]
            self.hit_obj(kind, obj, dmg * mult, m, "laser", src=o)
        self.sound(m.pos, "laser", m)
        f = hits[0][0] if hits else wf
        path = [o, [o[0] + (end[0] - o[0]) * f, o[1] + (end[1] - o[1]) * f, o[2] + (end[2] - o[2]) * f]]
        for v in self.mechs:
            if v.alive and v is not m and math.hypot(v.pos[0] - m.pos[0], v.pos[1] - m.pos[1]) < 250:
                v.beams.append({"by": m.id, "kind": "laser", "path": [[round(c, 2) for c in p] for p in path], "age": 0.05})

    def fire_lightning(self, m, w):
        s = w["stats"]
        o = (m.pos[0], m.pos[1], m.pos[2] + m.stats["muzzle_height"])
        best = None
        cone = math.radians(s["cone_deg"]) / 2
        for kind, obj, pos, r, h in self.targets(m):
            d = math.hypot(pos[0] - o[0], pos[1] - o[1])
            if d > s["range"]:
                continue
            if abs(wrap(math.atan2(pos[1] - o[1], pos[0] - o[0]) - m.aim[0])) > cone:
                continue
            c = (pos[0], pos[1], pos[2] + h / 2)
            if not self.W.los(o, c):
                continue
            if best is None or d < best[0]:
                best = (d, kind, obj, c)
        self.sound(m.pos, "lightning", m)
        if best is None:
            return
        dmg = s["damage"] * m.dmg_mult
        chain = [best]
        hit = {id(best[2])}
        cur = best
        for _ in range(int(s["chain"])):
            nxt = None
            for kind, obj, pos, r, h in self.targets(m):
                if id(obj) in hit:
                    continue
                c = (pos[0], pos[1], pos[2] + h / 2)
                d = math.dist(c, cur[3])
                if d < s["chain_range"] and (nxt is None or d < nxt[0]):
                    nxt = (d, kind, obj, c)
            if nxt is None:
                break
            hit.add(id(nxt[2]))
            chain.append(nxt)
            cur = nxt
        for k, (_, kind, obj, c) in enumerate(chain):
            self.hit_obj(kind, obj, dmg * (s["chain_falloff"] ** k), m, "lightning", src=o)
            if kind == "mech" and any(x and x["kind"] == "laser" for x in obj.weapons):
                obj.short_until = self.t + s["short_s"]
                for x in obj.weapons:
                    if x and x["kind"] == "laser":
                        x["charge"] = 0.0

    def hit_obj(self, kind, obj, dmg, attacker, weapon, src=None, self_ok=False):
        if kind == "mech":
            if obj is attacker and not self_ok:
                return
            self.damage(obj, dmg, attacker, weapon, src)
        elif kind == "npc":
            obj["hp"] -= dmg
            if attacker is not None:
                obj["target"] = attacker.id
                attacker.events.append({"type": "hit", "target": obj["id"], "amount": round(dmg, 1), "weapon": weapon})
                attacker.last_combat = self.t
            if obj["hp"] <= 0 and obj["alive"]:
                obj["alive"] = False
                for _ in range(self.cfg["npc"]["cubes"]):
                    self.spawn_cube(obj["pos"])
        elif kind == "crate":
            obj["hp"] -= dmg
            if attacker is not None:
                attacker.events.append({"type": "hit", "target": obj["id"], "amount": round(dmg, 1), "weapon": weapon})
            if obj["hp"] <= 0 and obj in self.crates:
                self.crates.remove(obj)
                self.sound(obj["pos"], "crate")
                for _ in range(self.cfg["crates"]["cubes"]):
                    self.spawn_cube(obj["pos"])
                if self.rng.random() < self.cfg["loot"]["crate_weapon_chance"]:
                    self.pickups.append({"id": self.nid("pick"), "kind": self.rng.choice(self.kinds),
                                         "pos": list(obj["pos"])})

    def spawn_cube(self, pos):
        p = [pos[0] + self.rng.uniform(-1.5, 1.5), pos[1] + self.rng.uniform(-1.5, 1.5), pos[2]]
        self.cubes.append({"id": self.nid("cube"), "pos": p})

    def damage(self, m, dmg, attacker, weapon, src):
        if not m.alive:
            return
        rear = False
        if weapon != "zone":
            if src is not None:
                ang = math.atan2(src[1] - m.pos[1], src[0] - m.pos[0])
                if abs(wrap(ang - m.aim[0])) > math.radians(m.stats["front_arc_deg"]) / 2:
                    rear = True
                    dmg *= m.stats["rear_mult"]
            dmg *= max(0.0, 1 - m.stats.get("armor", 0))
            m.last_combat = self.t
            if attacker is not None:
                if not hasattr(m, "hurt_by"):
                    m.hurt_by = {}
                m.hurt_by[attacker.id] = self.t
                attacker.last_combat = self.t
                attacker.events.append({"type": "hit", "target": m.id, "amount": round(dmg, 1), "weapon": weapon,
                                        **({"rear": True} if rear else {})})
            br = None
            if src is not None:
                br = round(math.atan2(src[1] - m.pos[1], src[0] - m.pos[0]), 3)
            m.events.append({"type": "damaged", "amount": round(dmg, 1), "weapon": weapon, "bearing": br,
                             "by": attacker.id if attacker is not None else None, **({"rear": True} if rear else {})})
        else:
            if self.tick % 2 == 0:
                m.events.append({"type": "damaged", "amount": round(dmg * 2, 1), "weapon": "zone", "bearing": None, "by": None})
        m.hp -= dmg
        if m.hp <= 0:
            self.kill(m, attacker)

    def kill(self, m, attacker):
        m.alive = False
        m.hp = 0
        m.death_tick = self.tick
        self.sound(m.pos, "death")
        n = m.cubes + self.cfg["cubes"]["death_drop_extra"]
        for _ in range(n):
            self.spawn_cube(m.pos)
        for w in m.weapons:
            if w and w["kind"] != "gun":
                w["charge"] = 0.0 if w["kind"] == "laser" else w.get("charge")
                self.pickups.append({"id": self.nid("pick"), "kind": w["kind"], "pos": list(m.pos), "w": w})
        if attacker is not None and attacker is not m:
            attacker.kills += 1
            attacker.events.append({"type": "kill", "target": m.id})

    # ------------------------------------------------------------------ npcs
    def npcs_tick(self, dt):
        c = self.cfg["npc"]
        for n in self.npcs:
            if not n["alive"]:
                continue
            n["cd"] = max(0, n["cd"] - dt)
            home = n["home"]
            tgt = None
            if n["target"] is not None:
                o = self.mechs[n["target"]]
                d = math.hypot(o.pos[0] - n["pos"][0], o.pos[1] - n["pos"][1])
                dh = math.hypot(n["pos"][0] - home[0], n["pos"][1] - home[1])
                if not o.alive or d > c["deaggro_range"] or dh > c["chase_leash"]:
                    n["target"] = None
                else:
                    tgt = o
            if tgt is None and self.tick % 10 == 0:
                dh = math.hypot(n["pos"][0] - home[0], n["pos"][1] - home[1])
                if dh < c["leash"]:
                    best = None
                    for o in self.mechs:
                        if not o.alive:
                            continue
                        d = math.hypot(o.pos[0] - n["pos"][0], o.pos[1] - n["pos"][1])
                        if d < c["aggro_range"] and (best is None or d < best[0]):
                            if self.W.los((n["pos"][0], n["pos"][1], n["pos"][2] + 3), (o.pos[0], o.pos[1], o.pos[2] + 4.5)):
                                best = (d, o)
                    if best:
                        tgt = best[1]
                        n["target"] = tgt.id
            if tgt is not None:
                gx, gy = tgt.pos[0], tgt.pos[1]
                d = math.hypot(gx - n["pos"][0], gy - n["pos"][1])
                if d < 25:
                    gx, gy = n["pos"][0], n["pos"][1]
                if n["cd"] <= 0:
                    o = (n["pos"][0], n["pos"][1], n["pos"][2] + 3)
                    tc = (tgt.pos[0], tgt.pos[1], tgt.pos[2] + 4.5)
                    if self.W.los(o, tc):
                        n["cd"] = c["cooldown"]
                        dx, dy, dz = tc[0] - o[0], tc[1] - o[1], tc[2] - o[2]
                        dd = math.sqrt(dx * dx + dy * dy + dz * dz) or 1
                        sp = math.radians(c["spread_deg"])
                        yaw = math.atan2(dy, dx) + self.rng.gauss(0, sp)
                        pit = math.asin(dz / dd) + self.rng.gauss(0, sp)
                        v = c["projectile_speed"]
                        self.projectiles.append({"id": self.nid("pick") + 100000, "kind": "npc", "owner": None,
                                                 "pos": list(o), "vel": [math.cos(pit) * math.cos(yaw) * v, math.cos(pit) * math.sin(yaw) * v, math.sin(pit) * v],
                                                 "left": 200, "damage": c["damage"]})
                        self.sound(o, "npc_gun")
            else:
                if math.hypot(n["wander"][0] - n["pos"][0], n["wander"][1] - n["pos"][1]) < 2 or self.rng.random() < 0.002:
                    a = self.rng.uniform(0, TAU)
                    r = self.rng.uniform(0, c["leash"])
                    n["wander"] = [home[0] + r * math.cos(a), home[1] + r * math.sin(a)]
                gx, gy = n["wander"][0], n["wander"][1]
            dx, dy = gx - n["pos"][0], gy - n["pos"][1]
            d = math.hypot(dx, dy)
            if d > 0.5:
                vx, vy = dx / d * c["speed"], dy / d * c["speed"]
                nx, ny = n["pos"][0] + vx * dt, n["pos"][1] + vy * dt
                if self.W.free_at(nx, ny):
                    n["pos"] = [nx, ny, self.W.terrain(nx, ny)]
                    n["vel"] = [round(vx, 2), round(vy, 2), 0]
                else:
                    n["wander"] = list(home)
            else:
                n["vel"] = [0, 0, 0]

    # ------------------------------------------------------------------ loot/drops
    def loot_tick(self, dt):
        pr = self.cfg["cubes"]["pickup_radius"]
        if self.cubes:
            keep = []
            for c in self.cubes:
                best = None
                for m in self.mechs:
                    if not m.alive:
                        continue
                    d = math.hypot(c["pos"][0] - m.pos[0], c["pos"][1] - m.pos[1])
                    if d <= pr and abs(c["pos"][2] - m.pos[2]) <= 4 and (best is None or d < best[0]):
                        best = (d, m)
                if best:
                    m = best[1]
                    self.add_cube(m)
                    m.events.append({"type": "pickup", "cubes": m.cubes})
                else:
                    keep.append(c)
            self.cubes = keep
        cd = self.cfg["drops"]
        if self.drop_times and self.t >= self.drop_times[0]:
            self.drop_times.pop(0)
            p = self.rand_ground(self.next_c, self.next_r * cd["spread"]) or self.rand_ground(self.cur_c, self.cur_r * 0.5)
            if p:
                self.drops.append({"id": self.nid("drop"), "pos": p, "lands": self.t + cd["fall_s"],
                                   "progress": 0.0, "holder": None})
        for d in list(self.drops):
            if self.t < d["lands"]:
                continue
            if not d.get("landed"):
                d["landed"] = True
                self.sound(d["pos"], "drop")
            inside = [m for m in self.mechs if m.alive and
                      math.hypot(m.pos[0] - d["pos"][0], m.pos[1] - d["pos"][1]) <= cd["radius"]]
            if len(inside) == 1:
                m = inside[0]
                if d["holder"] != m.id:
                    d["holder"] = m.id
                    d["progress"] = 0.0
                d["progress"] += dt
                if d["progress"] >= cd["capture_s"]:
                    self.drops.remove(d)
                    self.add_cube(m, cd["cubes"])
                    m.hp = m.stats["hp"]
                    m.energy = m.stats["energy"]
                    m.events.append({"type": "drop", "id": d["id"], "cubes": m.cubes})
                    m.drops_taken = getattr(m, "drops_taken", 0) + 1
            elif len(inside) == 0:
                d["holder"] = None
                d["progress"] = 0.0
