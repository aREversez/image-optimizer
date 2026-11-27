"""Crash-safe writes (review findings F8/F9, shared helper for F2).

A write that fails or is interrupted after the data started flowing must
leave the previous file intact and no temp file behind."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from image_optimizer import persistence


def _boom(*a, **k):
    raise OSError("simulated crash between write and rename")


class TestAtomicHelpers:
    def test_atomic_write_text_replaces_whole_file(self, tmp_path):
        f = tmp_path / "s.json"
        f.write_text("old")
        persistence.atomic_write_text(f, "new")
        assert f.read_text() == "new"
        assert [p.name for p in tmp_path.iterdir()] == ["s.json"]

    def test_failed_replace_keeps_old_content_and_leaves_no_temp(self, tmp_path, monkeypatch):
        f = tmp_path / "s.json"
        f.write_text("old")
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            persistence.atomic_write_text(f, "new")
        assert f.read_text() == "old"
        assert [p.name for p in tmp_path.iterdir()] == ["s.json"]

    def test_atomic_copy_failure_leaves_no_partial_destination(self, tmp_path, monkeypatch):
        src = tmp_path / "src.bin"
        src.write_bytes(b"x" * 1000)
        dest = tmp_path / "out" / "dest.bin"
        dest.parent.mkdir()
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            persistence.atomic_copy(src, dest)
        assert list(dest.parent.iterdir()) == []

    def test_atomic_copy_overwrites_existing_destination(self, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"new")
        dest = tmp_path / "dest.bin"
        dest.write_bytes(b"old")
        persistence.atomic_copy(src, dest)
        assert dest.read_bytes() == b"new"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["dest.bin", "src.bin"]

    def test_replace_retries_transient_permission_error(self, tmp_path, monkeypatch):
        src, dest = tmp_path / "a", tmp_path / "b"
        src.write_text("x")
        real, calls = os.replace, {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("held open")
            return real(a, b)

        monkeypatch.setattr(os, "replace", flaky)
        persistence.replace_with_retry(src, dest)
        assert dest.read_text() == "x" and calls["n"] == 3

    def test_staging_path_keeps_suffix_and_is_hidden_sibling(self, tmp_path):
        dest = tmp_path / "sub" / "photo.webp"
        s = persistence.staging_path(dest)
        assert s.parent == dest.parent and s.suffix == ".webp" and s.name.startswith(".")
        assert s != persistence.staging_path(dest)


class TestStateFilesUseAtomicWrites:
    def test_batch_state_survives_a_failed_save(self, app_module, tmp_path, monkeypatch):
        out = str(tmp_path / "out")
        bs = {"output_dir": out, "files": [{"path": "a", "status": "done"}], "results": []}
        app_module._save_batch_state(bs)
        before = app_module._batch_state_file(out).read_text(encoding="utf-8")
        bs["files"].append({"path": "b", "status": "pending"})
        monkeypatch.setattr(os, "replace", _boom)
        app_module._save_batch_state(bs)  # best-effort: must not raise
        assert app_module._batch_state_file(out).read_text(encoding="utf-8") == before
        assert json.loads(before)["files"] == [{"path": "a", "status": "done"}]

    def test_skip_fingerprints_survive_a_failed_save(self, app_module, tmp_path, monkeypatch):
        out = str(tmp_path / "out")
        app_module._save_skip_fingerprints(out, {"a.png": "f1"})
        with monkeypatch.context() as m:  # not undo(): that would also revert the config-dir isolation
            m.setattr(os, "replace", _boom)
            app_module._save_skip_fingerprints(out, {"a.png": "f1", "b.png": "f2"})
        assert app_module._load_skip_fingerprints(out) == {"a.png": "f1"}
