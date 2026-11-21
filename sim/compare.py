"""Decision diff: giddy plays the match; our kfe2 sees the identical observation stream every tick and we log both
decisions. Run as an external bot in the sim:  --bot "cmp=python -u sim/compare.py"   (env CMP_LOG=path.jsonl,
KFE_GIDDY_BIN=path to a native giddy build)."""
import json
import os
import subprocess
import sys
import threading
import queue

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import kfe2  # noqa: E402

GIDDY = os.environ["KFE_GIDDY_BIN"]
LOG = open(os.environ.get("CMP_LOG", "/dev/null"), "a")


def reader(stream, q):
    for line in stream:
        if line.strip():
            q.put(line)
    q.put(None)


def main():
    inq = queue.Queue()
    threading.Thread(target=reader, args=(sys.stdin, inq), daemon=True).start()
    child = subprocess.Popen([GIDDY], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, bufsize=1)
    cq = queue.Queue()
    threading.Thread(target=reader, args=(child.stdout, cq), daemon=True).start()
    me = None
    while True:
        raw = inq.get()
        if raw is None:
            break
        msg = json.loads(raw)
        if msg.get("type") == "hello":
            child.stdin.write(raw if raw.endswith("\n") else raw + "\n")
            child.stdin.flush()
            me = kfe2.Bot(msg)
            while True:
                line = cq.get()
                if line is None or json.loads(line).get("type") == "ready":
                    break
            sys.stdout.write(json.dumps({"type": "ready", "name": "cmp"}) + "\n")
            sys.stdout.flush()
        elif msg.get("type") == "obs":
            child.stdin.write(raw if raw.endswith("\n") else raw + "\n")
            child.stdin.flush()
            mine = me.act(msg)
            while True:
                line = cq.get()
                if line is None:
                    return
                g = json.loads(line)
                if g.get("tick") == msg["tick"]:
                    break
            g.pop("debug", None)
            sys.stdout.write(json.dumps(g) + "\n")
            sys.stdout.flush()
            s = msg["self"]
            LOG.write(json.dumps({"t": msg["t"], "hp": s["hp"], "pos": s["pos"], "w": [w and w["kind"] for w in s["weapons"]],
                                  "mechs": [(m["id"], round(m["pos"][0]), round(m["pos"][1]), round(m.get("hp", 0)))
                                            for m in msg["mechs"] if m["kind"] == "mech"],
                                  "g": {k: g.get(k) for k in ("move", "aim", "fire", "boost", "take", "pick", "jump")},
                                  "k": {k: mine.get(k) for k in ("move", "aim", "fire", "boost", "take", "pick", "jump")},
                                  "kmode": me.mode}) + "\n")
        elif msg.get("type") == "end":
            break
    child.kill()


if __name__ == "__main__":
    main()
