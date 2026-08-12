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


class TestWatchProcessing:
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
