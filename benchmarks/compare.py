"""
Compare Onion against gzip / xz / zstd / bz2 on files you give it.

    pip install zstandard
    python benchmarks/compare.py file1 file2 ...

Prints % size reduction (higher is better) and best-of-3 compress time.
"""
import bz2
import gzip
import lzma
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import zstandard
except ImportError:
    zstandard = None


def best(fn, k=3):
    t = 1e9
    for _ in range(k):
        s = time.perf_counter()
        fn()
        t = min(t, time.perf_counter() - s)
    return t * 1000


def onion_size_and_time(path):
    td = tempfile.mkdtemp()
    out = os.path.join(td, "x.onion")
    env = {**os.environ, "PYTHONPATH": ROOT}
    cmd = [sys.executable, "-m", "ace.cli", "-c", path, "-o", out]
    base = best(lambda: subprocess.run([sys.executable, "-c", "pass"], capture_output=True))
    ms = best(lambda: subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True)) - base
    subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True)
    return os.path.getsize(out), ms


def main(paths):
    cols = ["onion", "gzip9", "xz9", "bz2-9"] + (["zstd19"] if zstandard else [])
    print("%-22s %9s " % ("file", "raw") + " ".join("%14s" % c for c in cols))
    for p in paths:
        d = open(p, "rb").read()
        n = len(d)
        osz, oms = onion_size_and_time(p)
        funcs = {
            "gzip9": lambda: gzip.compress(d, 9),
            "xz9": lambda: lzma.compress(d, preset=9),
            "bz2-9": lambda: bz2.compress(d, 9),
        }
        if zstandard:
            funcs["zstd19"] = lambda: zstandard.ZstdCompressor(level=19).compress(d)
        cells = ["%5.1f%% %5.0fms" % (100 * (1 - osz / n), oms)]
        for c in cols[1:]:
            size = len(funcs[c]())
            cells.append("%5.1f%% %5.0fms" % (100 * (1 - size / n), best(funcs[c])))
        print("%-22s %9d " % (os.path.basename(p)[:22], n) + " ".join("%14s" % c for c in cells))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
