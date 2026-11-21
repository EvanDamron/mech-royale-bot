"""Relay: giddygiddyboy's MIT bot (vendor/giddy) plays practice; our Python bot runs alongside on every observation
(state kept warm) and takes over once a free-for-all is detected (if FFA_DRIVER == "kfe"), or whenever the driver
fails to answer in time or crashes."""
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bot as kfe  # noqa: E402

GIDDY = os.environ.get("KFE_GIDDY_BIN") or os.path.join(HERE, "vendor", "giddy", "giddy")
GIDDY_SALT = int(os.environ.get("KFE_GIDDY_SALT", 1))   # offsets the driver's RNG seed
FFA_DRIVER = os.environ.get("KFE_FFA_DRIVER", "giddy")   # "giddy" or "kfe"
DEADLINE = float(os.environ.get("KFE_RELAY_MS", 38)) / 1000.0
NAME = "KentuckyFriedEvan"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def send(msg):
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def reader(stream, q):
    for line in stream:
        if line.strip():
            q.put(line)
    q.put(None)


def main():
    inq = queue.Queue()
    threading.Thread(target=reader, args=(sys.stdin, inq), daemon=True).start()
    child = None
    cq = queue.Queue()
    me = None
    child_ok = False
    last = {}
    stats = {"giddy": 0, "kfe": 0}
    while True:
        batch = [inq.get()]
        while True:
            try:
                batch.append(inq.get_nowait())
            except queue.Empty:
                break
        latest = None
        for raw in batch:
            if raw is None:
                if child:
                    child.kill()
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "hello":
                try:
                    child = subprocess.Popen([GIDDY], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True, bufsize=1)
                    threading.Thread(target=reader, args=(child.stdout, cq), daemon=True).start()
                    h = dict(msg)
                    if GIDDY_SALT:
                        h["seed"] = (int(h.get("seed", 0)) + GIDDY_SALT) % (1 << 32)
                    child.stdin.write(json.dumps(h, separators=(",", ":")) + "\n")
                    child.stdin.flush()
                except Exception as e:
                    log("driver start failed:", e)
                    child = None
                try:
                    kfe.mf.draw.caps = msg.get("config", {}).get("debug", {})
                    me = kfe.Bot(msg)
                except Exception as e:
                    log("kfe init failed:", e)
                # wait for the driver's ready (it loads the map), then answer ours
                t0 = time.time()
                while child is not None and time.time() - t0 < 15:
                    try:
                        line = cq.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if line is None:
                        break
                    try:
                        if json.loads(line).get("type") == "ready":
                            child_ok = True
                            break
                    except ValueError:
                        pass
                send({"type": "ready", "name": NAME})
            elif kind == "obs":
                latest = msg
                latest_raw = raw
            elif kind == "end":
                log(f"relay answers: {stats}")
                if child:
                    try:
                        child.stdin.write(raw if raw.endswith("\n") else raw + "\n")
                        child.stdin.flush()
                    except Exception:
                        pass
                return
        if latest is None:
            continue
        t_in = time.time()
        tick = latest["tick"]
        use_child = child_ok and child is not None and child.poll() is None
        if use_child and FFA_DRIVER == "kfe" and me is not None and getattr(me, "ffa", False):
            use_child = False
        if use_child:
            try:
                child.stdin.write(latest_raw if latest_raw.endswith("\n") else latest_raw + "\n")
                child.stdin.flush()
            except Exception:
                use_child = False
                child_ok = False
        def run_mine():
            if me is None:
                return None
            try:
                kfe.mf.draw.clear()
                r = dict(me.act(latest) or {})
                kfe.mf.draw.take()
                return r
            except Exception as e:
                log("kfe act failed:", e)
                return None
        action = None
        if use_child:
            while True:
                left = DEADLINE - (time.time() - t_in)
                if left <= 0:
                    break
                try:
                    line = cq.get(timeout=left)
                except queue.Empty:
                    break
                if line is None:
                    child_ok = False
                    break
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                if a.get("tick") == tick:
                    action = a
                    break
        if action is not None:
            # answer first, then keep our bot's state warm
            stats["giddy"] += 1
            action["tick"] = tick
            action.pop("debug", None)
            last = action
            send(action)
            run_mine()
            continue
        mine = run_mine()
        stats["kfe"] += 1
        if action is None:
            action = mine if mine is not None else {k: last[k] for k in ("move", "aim", "boost") if k in last}
        action["tick"] = tick
        action.pop("debug", None)
        last = action
        send(action)


if __name__ == "__main__":
    main()
