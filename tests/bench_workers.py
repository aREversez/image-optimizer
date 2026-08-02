"""Benchmark for the compress vs thumbnail worker-pool split
(optimization item #6).

Generates N small images, then times a scan (thumbnailing) and an optimize
(compression) end-to-end at several COMPRESS_WORKERS / THUMBNAIL_WORKERS
settings, printing a comparison table. The point isn't absolute numbers
(those depend on real pngquant/oxipng and disk) — it's to confirm the two
knobs are independently tunable and to give a before/after harness for any
future concurrency change.

Run:  python tests/bench_workers.py [N]
      (default N=200; raise it for steadier numbers, lower it for a quick check)

Uses the project's real Optimizer (real pngquant/oxipng if present in bin/,
else the conftest-style stand-in scripts). The stand-ins copy bytes through,
so the numbers measure worker-pool overhead + Pillow normalization, not real
lossy compression — still enough to see whether splitting the pools changes
end-to-end throughput.
"""
from __future__ import annotations

import asyncio
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image

from app.main import AppState, _process_files, _scan_and_thumbnail, _scan_images
from app.optimizer import Optimizer


def make_images(d: Path, n: int):
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        Image.new("RGB", (48, 48), (i * 7 % 255, i * 13 % 255, i * 19 % 255)).save(d / f"img{i:04d}.png")


def time_it(fn, repeats=1):
    """Time a sync callable (which may call asyncio.run internally). Returns
    the min over repeats to reduce noise."""
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return min(times)


def bench(n: int):
    tmp = Path(tempfile.mkdtemp(prefix="imgopt_bench_"))
    src = tmp / "src"
    make_images(src, n)

    # Use the real Optimizer; fall back to stand-in binaries if pngquant/oxipng
    # aren't installed (keeps the benchmark runnable in any environment).
    import app.main as m
    opt = Optimizer()
    if opt.pngquant_path is None or opt.oxipng_path is None:
        print("[bench] pngquant/oxipng not found — using copy-through stand-ins "
              "(numbers reflect pool overhead, not real compression)")
        _install_standins(opt, tmp)
    m.optimizer = opt

    print(f"\n=== Image Optimizer worker-pool benchmark (N={n} images) ===")
    print(f"{'THUMB':>6} {'COMP':>6} {'scan(s)':>10} {'compress(s)':>12}")
    print("-" * 40)

    for thumb_w, comp_w in [(1, 4), (4, 1), (4, 4), (8, 8), (2, 8)]:
        m.THUMBNAIL_WORKERS = thumb_w
        m.CONCURRENT_WORKERS = comp_w

        # Scan timing: fresh workspace + state each time so nothing is reused.
        def do_scan():
            st = AppState()
            st.workspace = Path(tempfile.mkdtemp(prefix="imgopt_ws_"))
            asyncio.run(_scan_and_thumbnail(st, src, recursive=True))
            shutil.rmtree(st.workspace, ignore_errors=True)

        scan_t = time_it(do_scan)

        # Compress timing: fresh workspace + state, files from a scan.
        def do_compress():
            st = AppState()
            st.workspace = Path(tempfile.mkdtemp(prefix="imgopt_ws_"))
            st.files = [
                {"id": str(i), "name": f"img{i:04d}.png", "path": str(src / f"img{i:04d}.png"), "size": 0}
                for i in range(n)
            ]
            st.total = n
            asyncio.run(_process_files(st, st.files, "medium", 0, "png"))
            shutil.rmtree(st.workspace, ignore_errors=True)

        comp_t = time_it(do_compress)
        print(f"{thumb_w:>6} {comp_w:>6} {scan_t:>10.3f} {comp_t:>12.3f}")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nInterpretation: scan(s) should track THUMB; compress(s) should track COMP.")
    print("If scan time is flat regardless of THUMB, thumbnailing isn't the bottleneck.")


_STANDIN_PNGQUANT = '''#!/usr/bin/env python3
import sys
out = sys.argv[sys.argv.index("--output") + 1]
data = open(sys.argv[-1], "rb").read()
open(out, "wb").write(data)
'''
_STANDIN_OXIPNG = '''#!/usr/bin/env python3
import sys
out = sys.argv[sys.argv.index("--out") + 1]
data = open(sys.argv[-1], "rb").read()
open(out, "wb").write(data)
'''


def _install_standins(opt: Optimizer, tmp: Path):
    import stat
    bindir = tmp / "fakebin"
    bindir.mkdir(exist_ok=True)
    pq_impl = bindir / "pngquant_impl.py"
    ox_impl = bindir / "oxipng_impl.py"
    pq_impl.write_text(_STANDIN_PNGQUANT)
    ox_impl.write_text(_STANDIN_OXIPNG)
    if sys.platform == "win32":
        (bindir / "pngquant.bat").write_text(f'@echo off\r\n"{sys.executable}" "{pq_impl}" %*\r\n')
        (bindir / "oxipng.bat").write_text(f'@echo off\r\n"{sys.executable}" "{ox_impl}" %*\r\n')
        opt.pngquant_path = bindir / "pngquant.bat"
        opt.oxipng_path = bindir / "oxipng.bat"
    else:
        for name, impl in [("pngquant", pq_impl), ("oxipng", ox_impl)]:
            p = bindir / name
            p.write_text(f'#!/usr/bin/env python3\nimport subprocess,sys\nsys.exit(subprocess.call([{sys.executable!r},{str(impl)!r},*sys.argv[1:]]))\n')
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        opt.pngquant_path = bindir / "pngquant"
        opt.oxipng_path = bindir / "oxipng"


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    bench(n)
