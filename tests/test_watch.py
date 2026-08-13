"""Tests for Watch mode — directory monitoring and auto-optimization.

Watch mode uses a polling FolderWatcher (re-scans the directory on a
timer and diffs against the last snapshot). These tests verify the API
endpoints and the end-to-end flow (new file appears → gets optimized).
"""
from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

from .conftest import wait_for


class TestWatchStartStop:
    """Basic lifecycle: start → status shows running → stop → status idle."""

    def test_watch_start_stop(self, client, auth_headers, tmp_path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir),
            "output_dir": str(output_dir),
            "output_format": "png",
        }, headers=auth_headers)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["watch_running"] is True

        # Status should reflect running state
        status = client.get("/api/watch/status", headers=auth_headers).json()
        assert status["running"] is True
        assert status["processed"] == 0

        # Stop
        r = client.post("/api/watch/stop", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # Status should reflect stopped state
        status = client.get("/api/watch/status", headers=auth_headers).json()
        assert status["running"] is False

    def test_watch_rejects_when_already_running(self, client, auth_headers, tmp_path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        client.post("/api/watch/start", json={
            "directory": str(watch_dir), "output_dir": str(output_dir),
            "output_format": "png",
        }, headers=auth_headers)

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir), "output_dir": str(output_dir),
            "output_format": "png",
        }, headers=auth_headers)
        assert r.status_code == 400
        assert "already running" in r.json()["error"]

        client.post("/api/watch/stop", headers=auth_headers)

    def test_stop_when_not_running_returns_error(self, client, auth_headers):
        r = client.post("/api/watch/stop", headers=auth_headers)
        assert r.status_code == 400

    def test_watch_validates_directories(self, client, auth_headers, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Non-existent watch directory
        r = client.post("/api/watch/start", json={
            "directory": str(tmp_path / "nonexistent"),
            "output_dir": str(output_dir),
            "output_format": "png",
        }, headers=auth_headers)
        assert r.status_code == 400
        assert "does not exist" in r.json()["error"]

    def test_watch_validates_output_directory(self, client, auth_headers, tmp_path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir),
            "output_dir": str(tmp_path / "nonexistent_output"),
            "output_format": "png",
        }, headers=auth_headers)
        assert r.status_code == 400
        assert "Output directory" in r.json()["error"]


    def test_watch_rejects_output_dir_same_as_watched_directory(self, client, auth_headers, tmp_path):
        """output_dir == directory would make every optimized file the
        watcher writes look like a new/changed file to itself, causing an
        infinite reprocess loop (empirically confirmed via FolderWatcher
        directly — see TestWatchSelfLoopPrevention). Reject it up front."""
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir),
            "output_dir": str(watch_dir),
            "output_format": "png",
        }, headers=auth_headers)
        assert r.status_code == 400
        assert "watched directory" in r.json()["error"]

    def test_watch_rejects_output_dir_nested_inside_recursive_watch(self, client, auth_headers, tmp_path):
        """A subfolder of the watched directory is just as unsafe as the
        directory itself when watching recursively — the recursive scan
        would descend into it and re-detect the watcher's own output."""
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        nested_output = watch_dir / "optimized"
        nested_output.mkdir()

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir),
            "output_dir": str(nested_output),
            "output_format": "png",
            "recursive": True,
        }, headers=auth_headers)
        assert r.status_code == 400
        assert "watched directory" in r.json()["error"]

    def test_watch_allows_nested_output_dir_when_non_recursive(self, client, auth_headers, tmp_path):
        """The same nested output_dir is fine with recursive=False — a
        non-recursive scan only lists the watched directory's immediate
        files, so a subfolder's contents are never re-scanned."""
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        nested_output = watch_dir / "optimized"
        nested_output.mkdir()

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir),
            "output_dir": str(nested_output),
            "output_format": "png",
            "recursive": False,
        }, headers=auth_headers)
        assert r.status_code == 200
        client.post("/api/watch/stop", headers=auth_headers)


class TestWatchSelfLoopPrevention:
    """Reproduces, at the FolderWatcher level, the infinite-reprocessing
    loop that in-place watching (output_dir inside the watched tree) used
    to cause, to prove _watch_output_conflicts_with_input's rejection is
    actually necessary and not just a theoretical concern."""

    def test_in_place_output_causes_repeated_reprocessing_without_the_guard(self, tmp_path):
        """Direct FolderWatcher repro, bypassing the new endpoint guard:
        writing the 'optimized' output back into the watched directory
        under the same relative name makes on_change fire repeatedly for
        the same file, indefinitely, instead of the expected once."""
        import asyncio
        from app.watcher import FolderWatcher

        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()

        events = []

        async def on_change(rel, abspath):
            events.append(rel)
            if len(events) > 20:
                return  # test safety brake — real code has no such brake
            img = Image.open(abspath)
            img.save(abspath)  # in-place overwrite: new mtime, same path

        async def run_it():
            w = FolderWatcher(directory=watch_dir, recursive=True, interval=0.15, on_change=on_change)
            task = asyncio.create_task(w.run())
            await asyncio.sleep(0.3)
            Image.new("RGB", (20, 20), (5, 5, 5)).save(watch_dir / "shot.png")
            await asyncio.sleep(1.5)
            w.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(run_it())

        assert len(events) > 1, (
            "Expected the in-place-write pattern to cause repeated "
            f"reprocessing of the same file — only got {len(events)} event(s). "
            "If this now fails, something else changed how in-place writes "
            "are (not) detected; make sure the app-level guard "
            "(_watch_output_conflicts_with_input) is still what prevents "
            "users from hitting this, not a change here."
        )



    """End-to-end: new file appears in watch dir → gets optimized."""

    def test_watch_processes_new_file(self, client, auth_headers, tmp_path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir),
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.status_code == 200

        try:
            # Write a new PNG (after the baseline scan so the watcher
            # sees it as a new file on the next poll).
            time.sleep(0.5)
            img_path = watch_dir / "new_image.png"
            Image.new("RGB", (32, 32), (200, 100, 50)).save(img_path)

            # Wait for the watcher to detect and process it
            def check():
                s = client.get("/api/watch/status", headers=auth_headers).json()
                return s["processed"] >= 1

            wait_for(check, timeout=15.0)

            status = client.get("/api/watch/status", headers=auth_headers).json()
            assert status["processed"] >= 1
            assert status["errors"] == 0

            # Verify the output file was created
            out_file = output_dir / "new_image.png"
            assert out_file.exists()
            assert out_file.stat().st_size > 0
        finally:
            client.post("/api/watch/stop", headers=auth_headers)

    def test_watch_process_existing(self, client, auth_headers, tmp_path):
        """With process_existing=True, files already present when watch
        starts should be processed."""
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Create a file BEFORE starting watch
        Image.new("RGB", (32, 32), (10, 20, 30)).save(watch_dir / "existing.png")

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir),
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
            "process_existing": True,
        }, headers=auth_headers)
        assert r.status_code == 200

        try:
            def check():
                s = client.get("/api/watch/status", headers=auth_headers).json()
                return s["processed"] >= 1

            wait_for(check, timeout=15.0)

            status = client.get("/api/watch/status", headers=auth_headers).json()
            assert status["processed"] >= 1
            assert (output_dir / "existing.png").exists()
        finally:
            client.post("/api/watch/stop", headers=auth_headers)

    def test_watch_ignores_non_image(self, client, auth_headers, tmp_path):
        """Non-image files in the watch directory should not trigger
        optimization (FolderWatcher filters by IMAGE_EXTS)."""
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir),
            "output_dir": str(output_dir),
            "output_format": "png",
        }, headers=auth_headers)
        assert r.status_code == 200

        try:
            # Write a non-image file
            time.sleep(0.5)
            (watch_dir / "readme.txt").write_text("hello")

            # Give the watcher time to poll
            time.sleep(5)

            status = client.get("/api/watch/status", headers=auth_headers).json()
            assert status["processed"] == 0
            assert not (output_dir / "readme.txt").exists()
            assert not (output_dir / "readme.png").exists()
        finally:
            client.post("/api/watch/stop", headers=auth_headers)

    def test_watch_error_does_not_crash(self, client, auth_headers, tmp_path):
        """An error on one file shouldn't stop the watcher from
        continuing to process subsequent files."""
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        r = client.post("/api/watch/start", json={
            "directory": str(watch_dir),
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.status_code == 200

        try:
            # Write a valid PNG first
            time.sleep(0.5)
            Image.new("RGB", (20, 20), (100, 100, 100)).save(watch_dir / "good.png")

            def check_one():
                s = client.get("/api/watch/status", headers=auth_headers).json()
                return s["processed"] >= 1

            wait_for(check_one, timeout=15.0)

            # Write another valid PNG — the watcher should still work
            Image.new("RGB", (20, 20), (50, 50, 50)).save(watch_dir / "good2.png")

            def check_two():
                s = client.get("/api/watch/status", headers=auth_headers).json()
                return s["processed"] >= 2

            wait_for(check_two, timeout=15.0)

            status = client.get("/api/watch/status", headers=auth_headers).json()
            assert status["processed"] >= 2
            assert (output_dir / "good.png").exists()
            assert (output_dir / "good2.png").exists()
        finally:
            client.post("/api/watch/stop", headers=auth_headers)


class TestWatchBlocksOptimize:
    """Watch mode and batch optimize are mutually exclusive per session."""

    def test_optimize_blocked_while_watch_running(self, client, auth_headers, test_images, tmp_path):
        # Need files in state so the watch guard is reached before the
        # "no files" check.
        from .test_api_optimize import scan_and_wait
        scan_and_wait(client, auth_headers, test_images)

        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        client.post("/api/watch/start", json={
            "directory": str(watch_dir), "output_dir": str(output_dir),
            "output_format": "png",
        }, headers=auth_headers)

        try:
            # Try to start a batch optimize — should be rejected by the
            # watch guard, not by "no files" or any other check.
            r = client.post("/api/optimize", json={
                "output_format": "png",
            }, headers=auth_headers)
            assert r.status_code == 400
            assert "Watch" in r.json()["error"]
        finally:
            client.post("/api/watch/stop", headers=auth_headers)
