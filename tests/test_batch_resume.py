"""Tests for batch resume — persist progress to disk and pick up where a
crashed / cancelled batch left off.

Batch state is saved to ~/.image-optimizer/batch_state.json after each file
completes. On resume, the backend loads this file, reconstructs state.files,
and only processes files whose status is "pending" or "failed".
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from PIL import Image

from .conftest import wait_for
from .test_api_optimize import scan_and_wait, optimize_and_wait


def _batch_state_path():
    from app.main import _batch_state_file
    return _batch_state_file()


def _load_batch_state():
    p = _batch_state_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _save_batch_state(bs):
    _batch_state_path().write_text(json.dumps(bs, indent=2), encoding="utf-8")


class TestBatchStatePersisted:
    """batch_state.json is updated after each file completes."""

    def test_batch_state_created_and_cleared_on_full_run(self, client, auth_headers, test_images, tmp_path):
        """A successful run with output_dir creates then clears batch state."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        scan_and_wait(client, auth_headers, test_images)
        optimize_and_wait(client, auth_headers,
                          output_dir=str(output_dir),
                          output_format="png",
                          compression_mode="lossless")

        # All done → batch state should be cleared
        bs = _load_batch_state()
        assert bs is None

    def test_batch_state_endpoint_no_batch(self, client, auth_headers):
        r = client.get("/api/batch-state", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["has_batch"] is False


class TestResume:
    """POST /api/optimize with resume=True picks up pending files."""

    def test_resume_skips_completed(self, client, auth_headers, test_images, tmp_path):
        """Resume should only process files with status pending/failed."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Create 4 images
        imgs = []
        for i in range(4):
            p = test_images / f"resume_{i}.png"
            Image.new("RGB", (32, 32), (i * 50, 60, 60)).save(p)
            imgs.append(p)

        scan_and_wait(client, auth_headers, test_images)

        # Manually create a batch state where 2 files are "done" and 2 are "pending"
        files_state = test_images  # keep reference
        bs = {
            "session_id": "test-session",
            "input_dir": str(test_images),
            "output_dir": str(output_dir),
            "output_format": "png",
            "quality": "medium",
            "compression_mode": "lossless",
            "files": [
                {"id": "f0", "path": str(imgs[0]), "name": f"resume_0.png", "size": 100, "status": "done"},
                {"id": "f1", "path": str(imgs[1]), "name": f"resume_1.png", "size": 100, "status": "done"},
                {"id": "f2", "path": str(imgs[2]), "name": f"resume_2.png", "size": 100, "status": "pending"},
                {"id": "f3", "path": str(imgs[3]), "name": f"resume_3.png", "size": 100, "status": "pending"},
            ],
            "results": [
                {"id": "0", "name": "resume_0.png", "original_path": str(imgs[0]),
                 "success": True, "original_size": 100, "compressed_size": 80},
                {"id": "1", "name": "resume_1.png", "original_path": str(imgs[1]),
                 "success": True, "original_size": 100, "compressed_size": 80},
            ],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        _save_batch_state(bs)

        # Pre-create output files for the "done" entries (simulating the
        # previous run that produced them)
        for i in range(2):
            Image.new("RGB", (32, 32), (i * 50, 60, 60)).save(output_dir / f"resume_{i}.png")

        # Verify batch-state endpoint sees it
        r = client.get("/api/batch-state", headers=auth_headers)
        d = r.json()
        assert d["has_batch"] is True
        assert d["done"] == 2
        assert d["pending"] == 2

        # Resume
        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.status_code == 200
        # Should only process 2 files (the pending ones)
        assert r.json()["total"] == 2

        # Wait for completion
        wait_for(lambda: (lambda p: not p["running"] and p)(
            client.get("/api/progress", headers=auth_headers).json()
        ), timeout=30.0)

        # All 4 output files should exist
        for i in range(4):
            assert (output_dir / f"resume_{i}.png").exists()

        # Batch state should be cleared (all done now)
        bs = _load_batch_state()
        assert bs is None

    def test_resume_without_batch_state_returns_error(self, client, auth_headers):
        """Resume with no saved batch state should return 400."""
        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": "/tmp/whatever",
        }, headers=auth_headers)
        assert r.status_code == 400
        assert "No resumable" in r.json()["error"]

    def test_resume_wrong_output_dir_returns_error(self, client, auth_headers, test_images, tmp_path):
        """Resume with a different output_dir than the saved batch should fail."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        wrong_dir = tmp_path / "wrong"
        wrong_dir.mkdir()

        img = test_images / "dir_test.png"
        Image.new("RGB", (32, 32), (100, 100, 100)).save(img)

        bs = {
            "session_id": "test",
            "input_dir": str(test_images),
            "output_dir": str(output_dir),
            "output_format": "png",
            "quality": "medium",
            "compression_mode": "lossless",
            "files": [
                {"id": "f0", "path": str(img), "name": "dir_test.png", "size": 100, "status": "pending"},
            ],
            "results": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        _save_batch_state(bs)

        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(wrong_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.status_code == 400
        assert "doesn't match" in r.json()["error"]

    def test_resume_no_pending_files_returns_error(self, client, auth_headers, test_images, tmp_path):
        """Resume when all files are already done should return 400."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        img = test_images / "all_done.png"
        Image.new("RGB", (32, 32), (100, 100, 100)).save(img)

        bs = {
            "session_id": "test",
            "input_dir": str(test_images),
            "output_dir": str(output_dir),
            "output_format": "png",
            "quality": "medium",
            "compression_mode": "lossless",
            "files": [
                {"id": "f0", "path": str(img), "name": "all_done.png", "size": 100, "status": "done"},
            ],
            "results": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        _save_batch_state(bs)

        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.status_code == 400
        assert "No pending" in r.json()["error"]

    def test_resume_handles_failed_files(self, client, auth_headers, test_images, tmp_path):
        """Resume should re-process files that previously failed."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        imgs = []
        for i in range(3):
            p = test_images / f"fail_{i}.png"
            Image.new("RGB", (32, 32), (i * 70, 40, 40)).save(p)
            imgs.append(p)

        bs = {
            "session_id": "test",
            "input_dir": str(test_images),
            "output_dir": str(output_dir),
            "output_format": "png",
            "quality": "medium",
            "compression_mode": "lossless",
            "files": [
                {"id": "f0", "path": str(imgs[0]), "name": "fail_0.png", "size": 100, "status": "done"},
                {"id": "f1", "path": str(imgs[1]), "name": "fail_1.png", "size": 100, "status": "failed"},
                {"id": "f2", "path": str(imgs[2]), "name": "fail_2.png", "size": 100, "status": "pending"},
            ],
            "results": [
                {"id": "0", "name": "fail_0.png", "original_path": str(imgs[0]),
                 "success": True, "original_size": 100, "compressed_size": 80},
            ],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        _save_batch_state(bs)

        # Pre-create output for the "done" entry
        Image.new("RGB", (32, 32), (0, 40, 40)).save(output_dir / "fail_0.png")

        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.status_code == 200
        # Should process 2 files (1 failed + 1 pending)
        assert r.json()["total"] == 2

        wait_for(lambda: (lambda p: not p["running"] and p)(
            client.get("/api/progress", headers=auth_headers).json()
        ), timeout=30.0)

        # All output files should exist
        for i in range(3):
            assert (output_dir / f"fail_{i}.png").exists()

    def test_new_run_without_output_dir_ignores_batch_state(self, client, auth_headers, test_images, tmp_path):
        """A normal run without output_dir doesn't create batch state."""
        scan_and_wait(client, auth_headers, test_images)
        optimize_and_wait(client, auth_headers,
                          output_format="png",
                          compression_mode="lossless")

        # No batch state should be created (no output_dir)
        bs = _load_batch_state()
        assert bs is None
