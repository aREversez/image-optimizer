"""Batch state is saved at a bounded rate, with a guaranteed final flush
(review finding F9).

The whole results list was re-serialised and written after every file,
synchronously on the event loop: ~31 ms per save at 10,000 results, i.e.
about 30 GB written and 3 minutes of blocking over a 10,000-file batch."""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from image_optimizer import persistence
from .test_api_optimize import scan_and_wait, optimize_and_wait
from .test_batch_resume import _batch_state_path


def _folder(tmp_path, n):
    src = tmp_path / "src"
    src.mkdir()
    for i in range(n):
        Image.new("RGB", (24, 24), (i * 10 % 250, 9, 9)).save(src / f"f{i:02d}.png")
    return src


def _count_state_writes(monkeypatch, app_module):
    calls = []
    real = persistence.atomic_write_text

    def spy(path, text, *a, **k):
        if "batches" in str(path):
            calls.append(str(path))
        return real(path, text, *a, **k)

    monkeypatch.setattr(app_module.persistence, "atomic_write_text", spy)
    return calls


def test_saves_are_throttled_not_one_per_file(client, auth_headers, tmp_path, app_module, monkeypatch):
    calls = _count_state_writes(monkeypatch, app_module)
    src, out = _folder(tmp_path, 20), tmp_path / "out"
    scan_and_wait(client, auth_headers, src)
    r, d = optimize_and_wait(client, auth_headers, quality="medium", output_format="png",
                             output_dir=str(out), compression_mode="lossless")
    assert d["results"] and all(x["success"] for x in d["results"])
    # 1 initial + a handful of throttled saves; never one per file (20+).
    assert len(calls) < 10, len(calls)
    assert not _batch_state_path(out).exists(), "a fully finished batch is still cleared"


def test_final_flush_persists_every_status_when_the_batch_stays_resumable(
    client, auth_headers, tmp_path, app_module, monkeypatch,
):
    """Throttling must never lose the end state: with all copies failing the
    batch stays on disk and must show every file as failed, not a stale
    snapshot from the last throttled save."""
    def boom(s, d):
        raise OSError("disk full")
    monkeypatch.setattr(app_module.persistence, "atomic_copy", boom)
    src, out = _folder(tmp_path, 12), tmp_path / "out"
    scan_and_wait(client, auth_headers, src)
    optimize_and_wait(client, auth_headers, quality="medium", output_format="png",
                      output_dir=str(out), compression_mode="lossless")
    bs = json.loads(_batch_state_path(out).read_text(encoding="utf-8"))
    assert [f["status"] for f in bs["files"]] == ["failed"] * 12
    assert len(bs["results"]) == 12


def test_interval_zero_restores_save_after_every_file(client, auth_headers, tmp_path, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_BATCH_SAVE_MIN_INTERVAL", 0.0)
    calls = _count_state_writes(monkeypatch, app_module)
    src, out = _folder(tmp_path, 8), tmp_path / "out"
    scan_and_wait(client, auth_headers, src)
    optimize_and_wait(client, auth_headers, quality="medium", output_format="png",
                      output_dir=str(out), compression_mode="lossless")
    assert len(calls) >= 8
