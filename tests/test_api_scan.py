"""Regression tests for /api/scan and /api/scan-progress."""
from __future__ import annotations

import time

from PIL import Image

from .conftest import wait_for


def scan_and_wait(client, headers, directory, recursive=True):
    r = client.post("/api/scan", json={"directory": str(directory), "recursive": recursive}, headers=headers)
    d = wait_for(lambda: (lambda p: not p["running"] and p)(
        client.get("/api/scan-progress", headers=headers).json()
    ))
    return r, d


class TestScanReturnsQuickly:
    """Bug: scanning used to block the HTTP request until every thumbnail
    was generated, so a large folder made the UI look frozen with zero
    feedback for the entire scan duration."""

    def test_scan_post_returns_before_thumbnails_finish(self, client, auth_headers, tmp_path):
        big_dir = tmp_path / "many_images"
        big_dir.mkdir()
        for i in range(15):
            Image.new("RGB", (100, 100), (i, 0, 0)).save(big_dir / f"img{i:03d}.png")

        import time
        t0 = time.time()
        r = client.post("/api/scan", json={"directory": str(big_dir), "recursive": False}, headers=auth_headers)
        elapsed = time.time() - t0
        assert r.status_code == 200
        assert elapsed < 1.0, "scan should return almost immediately, not block until done"

        d = wait_for(lambda: (lambda p: not p["running"] and p)(
            client.get("/api/scan-progress", headers=auth_headers).json()
        ))
        assert len(d["files"]) == 15

    def test_progress_reports_intermediate_state(self, client, auth_headers, tmp_path, app_module, monkeypatch):
        big_dir = tmp_path / "many_images2"
        big_dir.mkdir()
        for i in range(20):
            Image.new("RGB", (150, 150), (i, 50, 100)).save(big_dir / f"img{i:03d}.png")

        # Deterministically slow down thumbnailing so an intermediate
        # progress state is reliably observable — without this, whether a
        # poll happens to land mid-scan is a genuine race on a fast
        # enough machine (see the identical reasoning in
        # TestScanGuards.test_second_scan_while_first_in_flight_is_rejected).
        import time as time_module
        real_gen_thumbnail = app_module._gen_thumbnail

        def slow_gen_thumbnail(src, dst, *a, **kw):
            time_module.sleep(0.03)
            return real_gen_thumbnail(src, dst, *a, **kw)

        monkeypatch.setattr(app_module, "_gen_thumbnail", slow_gen_thumbnail)

        client.post("/api/scan", json={"directory": str(big_dir), "recursive": False}, headers=auth_headers)
        seen_partial = False
        # Time-budgeted rather than iteration-budgeted: a fixed poll count
        # (this used to be `for _ in range(300)`) assumes each client.get()
        # round trip + the background scan task's own progress take roughly
        # constant wall-clock time. On a slower/more loaded CI runner that
        # assumption doesn't hold — the scan can legitimately still be
        # in-flight after 300 fast polls, which looks identical to "the
        # scan never finished" from the test's point of view even though
        # nothing is actually wrong. 20 files at 0.03s of injected sleep
        # each is 0.6s of guaranteed minimum scan time; 30s of budget is
        # generous headroom above that for CI variance without masking a
        # genuine hang (a real deadlock/regression still fails loudly,
        # just after 30s instead of instantly).
        deadline = time.time() + 30.0
        d = None
        while time.time() < deadline:
            d = client.get("/api/scan-progress", headers=auth_headers).json()
            if d["total"] > 0 and 0 < d["current"] < d["total"]:
                seen_partial = True
            if not d["running"]:
                break
        assert seen_partial, "never observed an intermediate progress state — scan may have regressed to blocking"
        assert d["total"] == 20 and len(d["files"]) == 20, (
            f"scan didn't finish within the time budget: {d}"
        )


class TestScanDeterministicOrder:
    """Concurrent thumbnail generation must not make file order/IDs
    non-deterministic — results are pre-allocated by index specifically to
    guarantee this."""

    def test_files_are_in_sorted_order_despite_concurrency(self, client, auth_headers, tmp_path):
        d = tmp_path / "ordered"
        d.mkdir()
        for i in range(12):
            Image.new("RGB", (50, 50)).save(d / f"img{i:03d}.png")
        r, result = scan_and_wait(client, auth_headers, d, recursive=False)
        names = [f["name"] for f in result["files"]]
        assert names == sorted(names)
        assert [f["id"] for f in result["files"]] == [str(i) for i in range(12)]


class TestScanFileMetadata:
    """The Files grid offers sorting by modified/created time and size,
    so every scanned entry must carry size + mtime + ctime."""

    def test_files_carry_size_and_timestamps(self, client, auth_headers, tmp_path):
        d = tmp_path / "meta"
        d.mkdir()
        Image.new("RGB", (50, 50)).save(d / "a.png")
        r, result = scan_and_wait(client, auth_headers, d, recursive=False)
        assert r.status_code == 200
        f = result["files"][0]
        assert f["size"] > 0
        assert f["mtime"] > 0
        assert f["ctime"] > 0

    def test_unreadable_file_entry_still_sorts_safely(self, client, auth_headers, tmp_path, app_module, monkeypatch):
        """If a scan item fails entirely (on_item_error path), its entry
        must still carry the sort fields — zeroed, so it lands at the
        bottom of "newest first" instead of breaking the comparator."""
        d = tmp_path / "meta2"
        d.mkdir()
        Image.new("RGB", (50, 50)).save(d / "boom.png")

        async def exploding_process_item(item):
            raise RuntimeError("boom")

        # Make the thumbnail step blow up so the worker pool's error
        # handler builds the entry (the real _gen_thumbnail swallows
        # errors internally and never triggers that path).
        real_run = app_module._run_worker_pool

        async def patched_run(items, process_item, **kw):
            return await real_run(items, exploding_process_item, **kw)

        monkeypatch.setattr(app_module, "_run_worker_pool", patched_run)

        r, result = scan_and_wait(client, auth_headers, d, recursive=False)
        assert r.status_code == 200
        f = result["files"][0]
        assert f["size"] == 0
        assert f["mtime"] == 0
        assert f["ctime"] == 0


class TestScanTolerance:
    """A single unreadable/corrupt image must not abort the whole scan —
    matches the same per-item error isolation used in the optimize
    worker pool."""

    def test_corrupt_image_does_not_abort_scan(self, client, auth_headers, tmp_path):
        d = tmp_path / "mixed"
        d.mkdir()
        Image.new("RGB", (50, 50)).save(d / "good.png")
        (d / "corrupt.png").write_bytes(b"not a real png")
        r, result = scan_and_wait(client, auth_headers, d, recursive=False)
        assert r.status_code == 200
        assert len(result["files"]) == 2


class TestScanGuards:
    def test_second_scan_while_first_in_flight_is_rejected(self, client, auth_headers, tmp_path, app_module, monkeypatch):
        d = tmp_path / "busy"
        d.mkdir()
        for i in range(10):
            Image.new("RGB", (80, 80)).save(d / f"img{i}.png")

        # Deterministically slow down thumbnailing so the first scan is
        # still in flight when the second request arrives — without this,
        # whether the first scan happens to finish before the second POST
        # lands is a genuine race (and on a fast machine with few/tiny
        # images, it can go either way).
        import time as time_module
        real_gen_thumbnail = app_module._gen_thumbnail

        def slow_gen_thumbnail(src, dst, *a, **kw):
            time_module.sleep(0.05)
            return real_gen_thumbnail(src, dst, *a, **kw)

        monkeypatch.setattr(app_module, "_gen_thumbnail", slow_gen_thumbnail)

        client.post("/api/scan", json={"directory": str(d), "recursive": False}, headers=auth_headers)
        r2 = client.post("/api/scan", json={"directory": str(d)}, headers=auth_headers)
        assert r2.status_code == 400
        wait_for(lambda: not client.get("/api/scan-progress", headers=auth_headers).json()["running"])

    def test_upload_while_scan_in_flight_is_rejected(self, client, auth_headers, tmp_path, app_module, monkeypatch):
        """Bug: /api/upload only guarded against `is_running` (optimize in
        progress), not `scan_running`. A scan-via-fire-and-forget task was
        still writing thumbnails into state.workspace, but a simultaneous
        upload silently reset() the state and swapped in a fresh workspace
        — the old one queued for delayed deletion. A worker could then
        finish writing into a directory the OS was about to nuke, plus the
        in-flight scan's results would land in the new session's view
        half-applied. Same fix as the second-scan guard above: reject."""
        d = tmp_path / "scanning"
        d.mkdir()
        for i in range(8):
            Image.new("RGB", (60, 60)).save(d / f"img{i}.png")

        import time as time_module
        real_gen_thumbnail = app_module._gen_thumbnail

        def slow_gen_thumbnail(src, dst, *a, **kw):
            time_module.sleep(0.05)
            return real_gen_thumbnail(src, dst, *a, **kw)

        monkeypatch.setattr(app_module, "_gen_thumbnail", slow_gen_thumbnail)

        client.post("/api/scan", json={"directory": str(d), "recursive": False}, headers=auth_headers)
        # Try to drop new files in mid-scan: a tiny in-memory PNG.
        from io import BytesIO
        buf = BytesIO()
        Image.new("RGB", (16, 16)).save(buf, format="PNG")
        buf.seek(0)
        r = client.post(
            "/api/upload",
            files={"files": ("drop.png", buf, "image/png")},
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert "scan" in r.json()["error"].lower(), r.json()["error"]
        wait_for(lambda: not client.get("/api/scan-progress", headers=auth_headers).json()["running"])

    def test_nonexistent_directory_rejected(self, client, auth_headers, tmp_path):
        r = client.post(
            "/api/scan", json={"directory": str(tmp_path / "does_not_exist")}, headers=auth_headers
        )
        assert r.status_code == 400
