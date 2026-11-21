"""Like compare.py but also dumps the raw observation stream (gz) so single ticks can be replayed into kfe2."""
import gzip, json, os, sys
sys.argv = sys.argv
os.environ.setdefault("CMP_LOG", "/dev/null")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import compare
REC = gzip.open(os.environ["REC_PATH"], "wt")
_orig_loads = compare.json.loads
class Tee:
    def __init__(self, s): self.s = s
    def __iter__(self):
        for line in self.s:
            REC.write(line if line.endswith("\n") else line + "\n"); REC.flush()
            yield line
compare.sys.stdin = Tee(sys.stdin)
compare.main()
REC.close()
