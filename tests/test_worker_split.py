"""Tests for the split of compress vs thumbnail concurrency into two
independent knobs (optimization item #6).

The scan flow's worker pool must be sized by THUMBNAIL_WORKERS and the
compress flow's by CONCURRENT_WORKERS — splitting them lets I/O-bound
thumbnailing and CPU-bound compression be tuned separately, and stops a
scan in one session from starving a compress in another. These tests pin
the wiring (which knob feeds which pool) plus the config/CLI surface, so a
future refactor can't silently re-couple them.
"""
from __future__ import annotations

import json

from .conftest import wait_for


def scan_and_wait(client, headers, directory, recursive=True):
    client.post("/api/scan", json={"directory": str(directory), "recursive": recursive}, headers=headers)
    return wait_for(lambda: (lambda d: not d["running"] and d)(
        client.get("/api/scan-progress", headers=headers).json()
    ))


def _isolate_config_dir(monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


class TestPoolsUseIndependentKnobs:
    """Wraps `_run_worker_pool` to record the `n_workers` each caller passed,
    then asserts the scan pool got THUMBNAIL_WORKERS and the compress pool
    got CONCURRENT_WORKERS — even when the two are set to different values."""

    def test_scan_pool_uses_thumbnail_workers_compress_pool_uses_compress_workers(
        self, client, auth_headers, test_images, monkeypatch
    ):
        import app.main as m
        monkeypatch.setattr(m, "CONCURRENT_WORKERS", 5)
        monkeypatch.setattr(m, "THUMBNAIL_WORKERS", 2)

        real_pool = m._run_worker_pool
        recorded: list = []

        async def recording(items, process_item, **kwargs):
            recorded.append(kwargs.get("n_workers"))
            return await real_pool(items, process_item, **kwargs)

        monkeypatch.setattr(m, "_run_worker_pool", recording)

        # Scan → should record THUMBNAIL_WORKERS (2).
        scan_and_wait(client, auth_headers, test_images)
        assert recorded, "scan didn't invoke the worker pool"
        assert recorded[-1] == 2, f"scan pool should use THUMBNAIL_WORKERS(2), got {recorded[-1]}"

        # Optimize → should record CONCURRENT_WORKERS (5).
        client.post("/api/optimize", json={}, headers=auth_headers)
        wait_for(lambda: not client.get("/api/progress", headers=auth_headers).json()["running"])
        assert recorded[-1] == 5, f"compress pool should use CONCURRENT_WORKERS(5), got {recorded[-1]}"


class TestThumbnailWorkersConfig:
    def test_thumbnail_workers_loaded_from_config(self, monkeypatch, tmp_path):
        import app.main as m
        _isolate_config_dir(monkeypatch, tmp_path)
        m._config_file().write_text(json.dumps({"thumbnail_workers": 7}))
        cfg = m._load_app_config()
        assert cfg["thumbnail_workers"] == 7

    def test_invalid_thumbnail_workers_falls_back_to_default(self, monkeypatch, tmp_path):
        import app.main as m
        _isolate_config_dir(monkeypatch, tmp_path)
        m._config_file().write_text(json.dumps({"thumbnail_workers": -3}))
        cfg = m._load_app_config()
        assert cfg["thumbnail_workers"] == m.DEFAULT_CONFIG["thumbnail_workers"]

    def test_zero_thumbnail_workers_clamped(self, monkeypatch, tmp_path):
        import app.main as m
        _isolate_config_dir(monkeypatch, tmp_path)
        m._config_file().write_text(json.dumps({"thumbnail_workers": 0}))
        cfg = m._load_app_config()
        assert cfg["thumbnail_workers"] >= 1

    def test_example_file_still_matches_default_config(self):
        # The example-file sync test in test_config.py covers this too; this
        # assertion is a local guard so a thumbnail_workers addition can't
        # silently desync the example template.
        from pathlib import Path
        import app.main as m
        example = json.loads(
            (Path(__file__).resolve().parent.parent / "config.example.json").read_text(encoding="utf-8")
        )
        assert example == m.DEFAULT_CONFIG


class TestThumbnailWorkersCli:
    def test_thumbnail_workers_flag_sets_global(self, monkeypatch, tmp_path):
        """main() parses --thumbnail-workers and binds THUMBNAIL_WORKERS,
        independently of --workers. uvicorn.run is stubbed so the server
        doesn't actually start."""
        import sys
        import uvicorn
        import app.main as m
        _isolate_config_dir(monkeypatch, tmp_path)

        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
        monkeypatch.setattr(sys, "argv", ["app", "--thumbnail-workers", "3"])

        m.main()
        assert m.THUMBNAIL_WORKERS == 3
        # --workers unset → keeps the config default (4), independent.
        assert m.CONCURRENT_WORKERS == 4
