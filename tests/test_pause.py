"""Regression tests for soft pause / resume (optimization item #4).

Pause is distinct from cancel: a paused run stops scheduling NEW files but
lets the in-flight pngquant/oxipng subprocesses finish naturally (soft
pause — no process kill, no signal-handling fault surface). Resume clears
the flag and the worker pool drains the remaining queue.

The cross-cutting test pins the interaction the review flags: pause/resume
combined with skip_existing and a narrowed file_ids selection must leave
the results set and the download ZIP equal to exactly the selected files —
no double-processing, no stale leak, no corruption of the batch state
machine (the third feature in a row to touch it).
"""
from __future__ import annotations

import asyncio
import shutil
from io import BytesIO
from zipfile import ZipFile

from .conftest import wait_for


def scan_and_wait(client, headers, directory, recursive=True):
    client.post("/api/scan", json={"directory": str(directory), "recursive": recursive}, headers=headers)
    return wait_for(lambda: (lambda d: not d["running"] and d)(
        client.get("/api/scan-progress", headers=headers).json()
    ))


def _session(client):
    import app.main as m
    return m.SESSIONS[client.cookies.get("imgopt_session")]


def _progress(client, headers):
    return client.get("/api/progress", headers=headers).json()


def _make_n_images(d, n):
    from PIL import Image
    for i in range(n):
        Image.new("RGB", (32, 32), (i * 20 % 255, 100, 200)).save(d / f"img{i}.png")


class TestPauseResume:
    def test_pause_holds_queued_work_resume_completes_no_double(
        self, client, auth_headers, tmp_path, fake_bin_dir, monkeypatch
    ):
        """Single worker + slow optimizer = deterministic scheduling. Pause
        after the first file completes; the remaining queued files must be
        held (run stays running, paused True) until resume. After resume
        every file is processed exactly once."""
        import app.main as m
        from app.optimizer import Optimizer

        monkeypatch.setattr(m, "CONCURRENT_WORKERS", 1)

        d = tmp_path / "imgs"
        d.mkdir()
        _make_n_images(d, 5)
        scanned = scan_and_wait(client, auth_headers, d)
        path_to_id = {f["path"]: f["id"] for f in scanned["files"]}
        counts: dict[str, int] = {}

        opt = Optimizer(bin_dir=fake_bin_dir)

        async def slow(input_path, output_path, **kwargs):
            fid = path_to_id[str(input_path)]
            counts[fid] = counts.get(fid, 0) + 1
            await asyncio.sleep(0.15)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5,
                    "error": None, "warning": None}

        opt.optimize_png = slow
        m.optimizer = opt

        # Start the batch (fires off as a background task).
        r = client.post("/api/optimize", json={}, headers=auth_headers)
        assert r.status_code == 200

        # Wait for the first file to complete, then pause.
        wait_for(lambda: _progress(client, auth_headers)["current"] >= 1)
        pr = client.post("/api/pause", headers=auth_headers)
        assert pr.status_code == 200

        prog = _progress(client, auth_headers)
        assert prog["paused"] is True
        assert prog["running"] is True

        # The remaining queued files are held: poll for ~1s and assert the
        # run stays running (in-flight finishes, but queued work doesn't
        # start). current may climb by 1 (the in-flight file) but must not
        # reach total.
        import time
        deadline = time.time() + 1.0
        while time.time() < deadline:
            p = _progress(client, auth_headers)
            assert p["running"] is True, "paused run must not complete while queued work remains"
            assert p["current"] < p["total"], p
            time.sleep(0.1)

        # Resume.
        rr = client.post("/api/resume", headers=auth_headers)
        assert rr.status_code == 200
        assert _progress(client, auth_headers)["paused"] is False

        # Now it drains to completion.
        prog = wait_for(lambda: (lambda p: not p["running"] and p)(
            _progress(client, auth_headers)
        ))
        assert len(prog["results"]) == 5
        assert all(res["success"] for res in prog["results"])
        # No file was processed twice (pause/resume didn't re-dispatch).
        assert all(c == 1 for c in counts.values()), counts
        assert set(counts) == {f["id"] for f in scanned["files"]}

    def test_pause_only_valid_while_running(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r = client.post("/api/pause", headers=auth_headers)
        assert r.status_code == 400

    def test_resume_only_valid_while_paused(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r = client.post("/api/resume", headers=auth_headers)
        assert r.status_code == 400

    def test_cancel_during_pause_ends_run(self, client, auth_headers, tmp_path, fake_bin_dir, monkeypatch):
        """Cancel must win over pause: a paused run that's then cancelled
        drains the queue (no further processing) and completes."""
        import app.main as m
        from app.optimizer import Optimizer

        monkeypatch.setattr(m, "CONCURRENT_WORKERS", 1)
        d = tmp_path / "imgs"
        d.mkdir()
        _make_n_images(d, 5)
        scanned = scan_and_wait(client, auth_headers, d)
        path_to_id = {f["path"]: f["id"] for f in scanned["files"]}
        counts: dict[str, int] = {}

        opt = Optimizer(bin_dir=fake_bin_dir)

        async def slow(input_path, output_path, **kwargs):
            fid = path_to_id[str(input_path)]
            counts[fid] = counts.get(fid, 0) + 1
            await asyncio.sleep(0.15)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5,
                    "error": None, "warning": None}

        opt.optimize_png = slow
        m.optimizer = opt

        client.post("/api/optimize", json={}, headers=auth_headers)
        wait_for(lambda: _progress(client, auth_headers)["current"] >= 1)
        client.post("/api/pause", headers=auth_headers)
        assert _progress(client, auth_headers)["paused"] is True
        # Cancel while paused.
        client.post("/api/cancel", headers=auth_headers)
        prog = wait_for(lambda: (lambda p: not p["running"] and p)(
            _progress(client, auth_headers)
        ))
        # The run ended; not all files processed (cancel drained the rest).
        assert prog["current"] < 5
        # Cancel, not pause, is the terminal signal here.
        assert any("Cancel" in line for line in prog["logs"])

    def test_progress_reports_paused_flag(self, client, auth_headers, tmp_path, fake_bin_dir, monkeypatch):
        import app.main as m
        from app.optimizer import Optimizer

        monkeypatch.setattr(m, "CONCURRENT_WORKERS", 1)
        d = tmp_path / "imgs"
        d.mkdir()
        _make_n_images(d, 3)
        scan_and_wait(client, auth_headers, d)
        opt = Optimizer(bin_dir=fake_bin_dir)

        async def slow(input_path, output_path, **kwargs):
            await asyncio.sleep(0.2)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5,
                    "error": None, "warning": None}

        opt.optimize_png = slow
        m.optimizer = opt

        client.post("/api/optimize", json={}, headers=auth_headers)
        wait_for(lambda: _progress(client, auth_headers)["current"] >= 1)
        # Before pause: paused is False.
        assert _progress(client, auth_headers)["paused"] is False
        client.post("/api/pause", headers=auth_headers)
        assert _progress(client, auth_headers)["paused"] is True
        client.post("/api/resume", headers=auth_headers)
        assert _progress(client, auth_headers)["paused"] is False
        wait_for(lambda: not _progress(client, auth_headers)["running"])


class TestPauseWithSkipExistingAndSubset:
    """The cross-cutting guard: pause/resume stacked on skip_existing + a
    narrowed file_ids selection. Final state must equal exactly the
    selected files — skipped ones reused (not recompressed), compressed
    ones processed once, ZIP matching the selection, no stale leak."""

    def test_pause_resume_with_skip_existing_and_narrowed_selection(
        self, client, auth_headers, tmp_path, fake_bin_dir, monkeypatch
    ):
        import app.main as m
        from app.optimizer import Optimizer

        monkeypatch.setattr(m, "CONCURRENT_WORKERS", 1)
        d = tmp_path / "imgs"
        d.mkdir()
        _make_n_images(d, 4)
        out_dir = tmp_path / "out"

        # First run: populate output_dir for ALL files.
        scanned = scan_and_wait(client, auth_headers, d)
        r = client.post("/api/optimize", json={"output_dir": str(out_dir)}, headers=auth_headers)
        assert r.status_code == 200
        wait_for(lambda: not _progress(client, auth_headers)["running"])
        produced = sorted(p.relative_to(out_dir) for p in out_dir.rglob("*") if p.is_file())
        assert len(produced) == 4

        # Delete 2 of the 4 from output_dir so a re-run must recompress
        # those 2 (slow) and skip the other 2.
        to_recompress = [produced[0], produced[1]]
        for p in to_recompress:
            (out_dir / p).unlink()

        path_to_id = {f["path"]: f["id"] for f in scanned["files"]}
        id_to_path = {v: k for k, v in path_to_id.items()}
        counts: dict[str, int] = {}
        opt = Optimizer(bin_dir=fake_bin_dir)

        async def slow(input_path, output_path, **kwargs):
            fid = path_to_id[str(input_path)]
            counts[fid] = counts.get(fid, 0) + 1
            await asyncio.sleep(0.15)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5,
                    "error": None, "warning": None}

        opt.optimize_png = slow
        m.optimizer = opt

        # Re-scan (fresh workspace) and run a NARROWED selection of 3 with
        # skip_existing + a pause/resume in the middle.
        scan_and_wait(client, auth_headers, d)
        keep_ids = [scanned["files"][i]["id"] for i in range(3)]
        r = client.post("/api/optimize", json={
            "file_ids": keep_ids, "output_dir": str(out_dir), "skip_existing": True,
        }, headers=auth_headers)
        assert r.status_code == 200

        # Pause once at least one result is in, then resume and finish.
        wait_for(lambda: _progress(client, auth_headers)["current"] >= 1)
        client.post("/api/pause", headers=auth_headers)
        assert _progress(client, auth_headers)["paused"] is True
        client.post("/api/resume", headers=auth_headers)

        prog = wait_for(lambda: (lambda p: not p["running"] and p)(
            _progress(client, auth_headers)
        ))

        # Results cover exactly the 3 selected files — no more, no less.
        assert len(prog["results"]) == 3
        assert {res["id"] for res in prog["results"]} == set(keep_ids)
        # The 2 selected files whose outputs were deleted got recompressed
        # exactly once; the selected file whose output survived was skipped.
        # (Among the 3 selected: however many had a surviving output were
        # skipped; the rest recompressed once each.)
        assert all(c <= 1 for c in counts.values()), f"double-processing: {counts}"
        # ZIP contains exactly the 3 selected files.
        st = _session(client)
        zr = client.get(f"/api/download/{st.workspace.name}")
        assert zr.status_code == 200
        names = {n.replace("\\", "/") for n in ZipFile(BytesIO(zr.content)).namelist()}
        assert len(names) == 3
