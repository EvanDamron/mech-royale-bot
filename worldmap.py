"""Static map model shared by the bot and the local simulator.

Grids are 1 m cells aligned with the terrain vertex grid (x = minX + i, y = minY + j).
  T     terrain height
  gtop  top of the tallest ground-anchored prism over the cell (-inf if none)
  ovb   lowest base of an elevated prism (bridge deck, overhang) over the cell (+inf if none)
  ovt   top of that elevated prism (-inf if none)
"""
import heapq
import json
import math
import os

import numpy as np

NEG = -1e9
POS = 1e9


def _rasterize_poly(ring, x0, y0, nx, ny):
    """Even-odd point-in-polygon for the cell centers inside the ring's bounding box.
    Returns (j0, i0, mask) with mask shaped to the bbox."""
    r = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
    i0 = max(int(math.floor(r[:, 0].min() - x0)), 0)
    i1 = min(int(math.ceil(r[:, 0].max() - x0)) + 1, nx)
    j0 = max(int(math.floor(r[:, 1].min() - y0)), 0)
    j1 = min(int(math.ceil(r[:, 1].max() - y0)) + 1, ny)
    if i1 <= i0 or j1 <= j0:
        return j0, i0, np.zeros((0, 0), bool)
    xs = x0 + np.arange(i0, i1)
    ys = y0 + np.arange(j0, j1)
    X, Y = np.meshgrid(xs, ys)
    inside = np.zeros(X.shape, bool)
    xa, ya = r[:, 0], r[:, 1]
    xb, yb = np.roll(xa, -1), np.roll(ya, -1)
    for k in range(len(xa)):
        ax, ay, bx, by = xa[k], ya[k], xb[k], yb[k]
        if ay == by:
            continue
        cond = (ay > Y) != (by > Y)
        xint = ax + (Y - ay) * (bx - ax) / (by - ay)
        inside ^= cond & (X < xint)
    return j0, i0, inside


class World:
    def __init__(self, map_dir):
        with open(os.path.join(map_dir, "v2", "world.json")) as f:
            w = json.load(f)
        tr = w["terrain"]
        self.nx, self.ny = tr["nx"], tr["ny"]
        self.x0, self.y0 = tr["minX"], tr["minY"]
        self.bounds = w["bounds"]
        self.T = (np.fromfile(os.path.join(map_dir, "v2", tr["file"]), dtype="<i2")
                  .reshape(self.ny, self.nx).astype(np.float32) * tr.get("scale", 0.01))
        nx, ny = self.nx, self.ny
        gtop = np.full((ny, nx), NEG, np.float32)
        ovb = np.full((ny, nx), POS, np.float32)
        ovt = np.full((ny, nx), NEG, np.float32)
        for p in w["prisms"]:
            j0, i0, m = _rasterize_poly(p["ring"], self.x0, self.y0, nx, ny)
            if m.size == 0:
                continue
            for h in p.get("holes") or []:
                hj, hi, hm = _rasterize_poly(h, self.x0, self.y0, nx, ny)
                if hm.size == 0:
                    continue
                # clip hole mask into prism bbox
                a0, a1 = max(hj, j0), min(hj + hm.shape[0], j0 + m.shape[0])
                b0, b1 = max(hi, i0), min(hi + hm.shape[1], i0 + m.shape[1])
                if a1 > a0 and b1 > b0:
                    m[a0 - j0:a1 - j0, b0 - i0:b1 - i0] &= ~hm[a0 - hj:a1 - hj, b0 - hi:b1 - hi]
            sl = (slice(j0, j0 + m.shape[0]), slice(i0, i0 + m.shape[1]))
            tsub = self.T[sl]
            base, top = p["base"], p["top"]
            anchored = m & (base <= tsub + 1.5)
            elevated = m & ~anchored
            g = gtop[sl]
            g[anchored] = np.maximum(g[anchored], top)
            if elevated.any():
                b = ovb[sl]
                t = ovt[sl]
                b[elevated] = np.minimum(b[elevated], base)
                t[elevated] = np.maximum(t[elevated], top)
        self.gtop, self.ovb, self.ovt = gtop, ovb, ovt
        # surface for line of sight / ground walkers: terrain or anchored roof
        self.S = np.maximum(self.T, gtop)
        self.Sf = self.S.ravel()
        self.ovbf = self.ovb.ravel()
        self.ovtf = self.ovt.ravel()
        self.has_ov = bool((self.ovb < POS).any())
        self._tc = {}
        self.trees = w.get("trees")
        self._build_nav()
        self._build_roofs()

    # ---------- sampling ----------
    def cell(self, x, y):
        i = int(round(x - self.x0))
        j = int(round(y - self.y0))
        if i < 0: i = 0
        if j < 0: j = 0
        if i >= self.nx: i = self.nx - 1
        if j >= self.ny: j = self.ny - 1
        return j, i

    def terrain(self, x, y):
        fx = x - self.x0
        fy = y - self.y0
        i = int(math.floor(fx))
        j = int(math.floor(fy))
        if i < 0 or j < 0 or i >= self.nx - 1 or j >= self.ny - 1:
            j, i = self.cell(x, y)
            return float(self.T[j, i])
        ax, ay = fx - i, fy - j
        T = self.T
        return float((T[j, i] * (1 - ax) + T[j, i + 1] * ax) * (1 - ay)
                     + (T[j + 1, i] * (1 - ax) + T[j + 1, i + 1] * ax) * ay)

    def surface(self, x, y):
        j, i = self.cell(x, y)
        return max(self.terrain(x, y), float(self.gtop[j, i]))

    def in_bounds(self, x, y, margin=0.0):
        b = self.bounds
        return b["minX"] + margin <= x <= b["maxX"] - margin and b["minY"] + margin <= y <= b["maxY"] - margin

    # ---------- line of sight ----------
    def _tline(self, n):
        t = self._tc.get(n)
        if t is None:
            t = np.arange(1, n, dtype=np.float64) / n
            self._tc[n] = t
        return t

    def los(self, a, b, step=1.0):
        """True if the segment a->b (3D) is not blocked by terrain or prisms."""
        ax, ay, az = a
        bx, by, bz = b
        dx, dy = bx - ax, by - ay
        d = math.sqrt(dx * dx + dy * dy)
        n = int(d / step)
        if n < 2:
            return True
        t = self._tline(n)
        ii = (ax - self.x0 + 0.5 + dx * t).astype(np.intp)
        jj = (ay - self.y0 + 0.5 + dy * t).astype(np.intp)
        if not (0 <= ax - self.x0 < self.nx - 1 and 0 <= bx - self.x0 < self.nx - 1
                and 0 <= ay - self.y0 < self.ny - 1 and 0 <= by - self.y0 < self.ny - 1):
            np.clip(ii, 0, self.nx - 1, out=ii)
            np.clip(jj, 0, self.ny - 1, out=jj)
        zs = az + (bz - az) * t
        flat = jj * self.nx + ii
        if (zs < self.Sf[flat]).any():
            return False
        if self.has_ov:
            ob = self.ovbf[flat]
            if ((zs > ob) & (zs < self.ovtf[flat])).any():
                return False
        return True

    def ray_hit(self, a, b, step=1.0):
        """Fraction along a->b of the first blocked sample (1.0 if clear)."""
        ax, ay, az = a
        bx, by, bz = b
        d = math.sqrt((bx - ax) ** 2 + (by - ay) ** 2 + (bz - az) ** 2)
        n = int(d / step)
        if n < 1:
            return 1.0
        t = np.linspace(0.0, 1.0, n + 1)[1:]
        xs = ax + (bx - ax) * t
        ys = ay + (by - ay) * t
        zs = az + (bz - az) * t
        ii = np.clip(np.rint(xs - self.x0).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint(ys - self.y0).astype(np.int32), 0, self.ny - 1)
        blk = (zs < self.S[jj, ii]) | ((zs > self.ovb[jj, ii]) & (zs < self.ovt[jj, ii]))
        k = np.argmax(blk)
        if not blk[k]:
            return 1.0
        return float(t[k])

    # ---------- navigation (ground walker) ----------
    NAV = 4  # meters per nav cell

    def _build_nav(self):
        T, g = self.T, self.gtop
        wall = g > T + 1.2
        # low overhangs (bridge decks at head height) are walls for walkers underneath
        wall |= (self.ovb < T + 9.5) & (self.ovt > T + 1.2)
        gy, gx = np.gradient(T)
        wall |= np.hypot(gx, gy) > 1.1
        # map edge
        b = self.bounds
        xs = self.x0 + np.arange(self.nx)
        ys = self.y0 + np.arange(self.ny)
        wall[:, (xs < b["minX"] + 3) | (xs > b["maxX"] - 3)] = True
        wall[(ys < b["minY"] + 3) | (ys > b["maxY"] - 3), :] = True
        self.wall = wall
        # clearance: dilate by 3 cells (radius 2.5 + margin)
        r = 3
        blk = wall.copy()
        for dj in range(-r, r + 1):
            for di in range(-r, r + 1):
                if di * di + dj * dj > r * r or (di == 0 and dj == 0):
                    continue
                blk |= np.roll(np.roll(wall, dj, 0), di, 1)
        self.blocked = blk
        s = self.NAV
        self.gnx = self.nx // s
        self.gny = self.ny // s
        c = s // 2
        sub = blk[c:self.gny * s:s, c:self.gnx * s:s]
        # a nav cell is free if its center and most of its samples are free
        self.nav_free = ~sub
        self.nav_flat = self.nav_free.ravel().tolist()
        # distance (in 1 m cells) to the nearest wall, rough, for cover search
        self.wall_near = self.wall

    def _build_roofs(self, min_h=6.0, max_h=42.0, min_depth=6):
        """Roof interior depth (m from a drop or step) and perch candidates on a 4 m lattice."""
        g, T = self.gtop, self.T
        h = g - T
        roof = (h > min_h) & (h < max_h)
        depth = np.zeros(g.shape, np.int16)
        cur = roof.copy()
        for k in range(1, 16):
            nb = cur.copy()
            for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb &= np.roll(np.roll(cur, dj, 0), di, 1) & \
                    (np.abs(np.roll(np.roll(g, dj, 0), di, 1) - g) < 1.5)
            cur = nb
            if not cur.any():
                break
            depth[cur] = k
        self.roof_depth = depth
        jj, ii = np.nonzero(depth[::4, ::4] >= min_depth)
        jj, ii = jj * 4, ii * 4
        self.perch_x = (self.x0 + ii).astype(np.float64)
        self.perch_y = (self.y0 + jj).astype(np.float64)
        self.perch_h = g[jj, ii].astype(np.float64)
        self.perch_d = depth[jj, ii].astype(np.float64)

    def free_at(self, x, y):
        j, i = self.cell(x, y)
        return not self.blocked[j, i]

    def nav_cell(self, x, y):
        s = self.NAV
        gi = int((x - self.x0) // s)
        gj = int((y - self.y0) // s)
        gi = min(max(gi, 0), self.gnx - 1)
        gj = min(max(gj, 0), self.gny - 1)
        return gj, gi

    def nav_center(self, gj, gi):
        s = self.NAV
        return (self.x0 + gi * s + s // 2, self.y0 + gj * s + s // 2)

    def nearest_free(self, gj, gi, maxr=12):
        if self.nav_free[gj, gi]:
            return gj, gi
        for r in range(1, maxr + 1):
            best = None
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    j, i = gj + dj, gi + di
                    if 0 <= j < self.gny and 0 <= i < self.gnx and self.nav_free[j, i]:
                        d = dj * dj + di * di
                        if best is None or d < best[0]:
                            best = (d, j, i)
            if best:
                return best[1], best[2]
        return gj, gi

    def astar(self, start, goal, max_expand=6000):
        """start/goal: world (x, y). Returns list of world waypoints (excluding start) or None."""
        gnx, gny = self.gnx, self.gny
        sj, si = self.nearest_free(*self.nav_cell(*start))
        tj, ti = self.nearest_free(*self.nav_cell(*goal))
        free = self.nav_flat
        S = sj * gnx + si
        G = tj * gnx + ti
        if S == G:
            return [goal]
        h0 = math.hypot(ti - si, tj - sj)
        openq = [(h0, 0.0, S)]
        came = {S: -1}
        gcost = {S: 0.0}
        SQ2 = 1.41421356
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2))
        expanded = 0
        best = (h0, S)
        while openq:
            f, gc, cur = heapq.heappop(openq)
            if cur == G:
                break
            if gc > gcost.get(cur, 1e18) + 1e-9:
                continue
            expanded += 1
            if expanded > max_expand:
                break
            cj, ci = divmod(cur, gnx)
            for di, dj, w in nbrs:
                ni, nj = ci + di, cj + dj
                if ni < 0 or nj < 0 or ni >= gnx or nj >= gny:
                    continue
                n = nj * gnx + ni
                if not free[n]:
                    continue
                if di and dj and not (free[cj * gnx + ni] and free[nj * gnx + ci]):
                    continue
                ng = gc + w
                if ng < gcost.get(n, 1e18):
                    gcost[n] = ng
                    came[n] = cur
                    dx, dy = abs(ti - ni), abs(tj - nj)
                    h = (dx + dy) + (SQ2 - 2) * min(dx, dy)
                    if h < best[0]:
                        best = (h, n)
                    heapq.heappush(openq, (ng + h, ng, n))
        end = G if G in came else best[1]
        path = []
        n = end
        while n != -1 and n != S:
            j, i = divmod(n, gnx)
            path.append(self.nav_center(j, i))
            n = came[n]
        path.reverse()
        if end == G:
            path.append(tuple(goal))
        return path

    def walk_clear(self, a, b, step=1.0):
        """Straight ground walk a->b (x, y) stays on free 1 m cells."""
        ax, ay = a
        bx, by = b
        d = math.hypot(bx - ax, by - ay)
        n = max(int(d / step), 1)
        t = np.linspace(0.0, 1.0, n + 1)
        ii = np.clip(np.rint(ax + (bx - ax) * t - self.x0).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint(ay + (by - ay) * t - self.y0).astype(np.int32), 0, self.ny - 1)
        return not self.blocked[jj, ii].any()
