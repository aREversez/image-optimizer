"""Regression tests for the _process_files concurrent worker pool.

Two real bugs lived here at different points:
1. Files were processed strictly sequentially despite Optimizer having a
   Semaphore(4) implying concurrency was intended.
2. After fixing #1 with asyncio.gather(*workers), an unhandled exception
   from any single file made gather() return immediately without waiting
   for (or cancelling) the other workers — they kept running orphaned in
   the background, their results silently lost, while the batch already
   reported itself "done".
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_state(tmp_path, n_files):
    from app.main import AppState
    state = AppState()
    state.workspace = tmp_path / "ws"
    state.workspace.mkdir()
    files = [
        {"id": str(i), "name": f"img{i}.png", "path": str(tmp_path / "src.png")}
        for i in range(n_files)
    ]
    state.total = n_files
    return state, files


class TestConcurrency:
    def test_files_are_processed_concurrently_not_sequentially(self, tmp_path, fake_bin_dir):
        import app.main as m
        from app.optimizer import Optimizer

        async def slow_optimize(input_path, output_path, **kwargs):
            await asyncio.sleep(0.3)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5, "error": None, "warning": None}

        opt = Optimizer(bin_dir=fake_bin_dir)
        opt.optimize_png = slow_optimize
        m.optimizer = opt

        (tmp_path / "src.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
        state, files = _make_state(tmp_path, 8)

        t0 = time.time()
        asyncio.run(m._process_files(state, files, "medium", 0, "png"))
        elapsed = time.time() - t0

        assert state.current == 8
        assert len(state.results) == 8
        # Sequential would take ~2.4s (8 * 0.3s); 4 concurrent workers
        # should take roughly 2 batches ~0.6-0.9s. Generous upper bound to
        # avoid CI flakiness while still catching a regression to serial
        # processing.
        assert elapsed < 1.8, f"took {elapsed:.2f}s — looks sequential, not concurrent"

    def test_one_file_raising_does_not_orphan_the_others(self, tmp_path, fake_bin_dir):
        import app.main as m
        from app.optimizer import Optimizer

        call_count = {"n": 0}

        async def flaky_optimize(input_path, output_path, **kwargs):
            call_count["n"] += 1
            n = call_count["n"]
            await asyncio.sleep(0.05)
            if n == 3:
                raise RuntimeError("simulated failure not caught inside optimize_png")
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5, "error": None, "warning": None}

        opt = Optimizer(bin_dir=fake_bin_dir)
        opt.optimize_png = flaky_optimize
        m.optimizer = opt

        (tmp_path / "src.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
        state, files = _make_state(tmp_path, 10)

        asyncio.run(m._process_files(state, files, "medium", 0, "png"))

        assert state.current == 10, "every file should be accounted for, none silently orphaned"
        assert len(state.results) == 10
        ids = sorted(int(r["id"]) for r in state.results)
        assert ids == list(range(10))
        successes = sum(1 for r in state.results if r["success"])
        failures = sum(1 for r in state.results if not r["success"])
        assert successes == 9
        assert failures == 1
        assert state.is_running is False

    def test_cancel_stops_new_work_but_finishes_in_flight(self, tmp_path, fake_bin_dir):
        import app.main as m
        from app.optimizer import Optimizer

        async def slow_optimize(input_path, output_path, **kwargs):
            await asyncio.sleep(0.2)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5, "error": None, "warning": None}

        opt = Optimizer(bin_dir=fake_bin_dir)
        opt.optimize_png = slow_optimize
        m.optimizer = opt

        (tmp_path / "src.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
        state, files = _make_state(tmp_path, 12)

        async def run_and_cancel():
            task = asyncio.create_task(m._process_files(state, files, "medium", 0, "png"))
            await asyncio.sleep(0.25)  # first batch of 4 in flight
            state.cancelled = True
            await task

        asyncio.run(run_and_cancel())
        assert state.current < 12, "cancel should have stopped some files from ever starting"
        assert state.is_running is False
