"""Encoder lookup: verified binaries first, no current-directory surprises (R3).

Two properties are load-bearing:

- A binary installed and digest-verified by binaries.py must win over a
  same-named file earlier on PATH, otherwise the verification buys nothing.
- On Windows the classic search order includes the current directory, so a
  pngquant.exe dropped next to a batch of downloaded images would be run.
  The lookup only accepts absolute PATH entries.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from image_optimizer.optimizer import Optimizer

EXE = ".exe" if sys.platform == "win32" else ""


def _fake_binary(directory: Path, name: str = "pngquant") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}{EXE}"
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    if sys.platform != "win32":
        path.chmod(0o755)
    return path


def test_pinned_directory_wins_over_path(tmp_path, monkeypatch):
    pinned = tmp_path / "pinned"
    on_path = tmp_path / "on-path"
    _fake_binary(pinned)
    _fake_binary(on_path)
    monkeypatch.setenv("PATH", str(on_path))

    opt = Optimizer(bin_dir=pinned)
    assert opt._find_binary("pngquant") == (pinned / f"pngquant{EXE}").resolve()


def test_falls_back_to_an_absolute_path_entry(tmp_path, monkeypatch):
    on_path = tmp_path / "on-path"
    expected = _fake_binary(on_path)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "empty"), str(on_path)]))

    opt = Optimizer(bin_dir=tmp_path / "nothing-here")
    assert opt._find_binary("pngquant") == expected.resolve()


def test_relative_path_entries_are_ignored(tmp_path, monkeypatch):
    """An empty or relative PATH entry is the current directory. Detecting a
    binary there is how a planted executable gets run."""
    cwd = tmp_path / "cwd"
    _fake_binary(cwd, "pngquant")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "relative-dir"]))

    opt = Optimizer(bin_dir=tmp_path / "nothing-here")
    assert opt._find_binary("pngquant") is None


def test_quoted_path_entries_are_accepted(tmp_path, monkeypatch):
    """Windows PATH entries are frequently quoted ("C:\\Program Files\\...")."""
    on_path = tmp_path / "program files"
    expected = _fake_binary(on_path)
    monkeypatch.setenv("PATH", f'"{on_path}"')

    opt = Optimizer(bin_dir=tmp_path / "nothing-here")
    assert opt._find_binary("pngquant") == expected.resolve()


def test_missing_binary_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    opt = Optimizer(bin_dir=tmp_path / "nothing-here")
    assert opt._find_binary("pngquant") is None


@pytest.mark.parametrize("name", ["oxipng", "cjpeg", "avifenc"])
def test_every_encoder_uses_the_same_rule(tmp_path, monkeypatch, name):
    on_path = tmp_path / "on-path"
    expected = _fake_binary(on_path, name)
    monkeypatch.setenv("PATH", str(on_path))
    opt = Optimizer(bin_dir=tmp_path / "nothing-here")
    assert opt._find_binary(name) == expected.resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute bit")
def test_non_executable_path_entry_is_skipped(tmp_path, monkeypatch):
    """`which` skips files without the execute bit; the PATH walk has to as
    well, or a stray non-executable `pngquant` wins over a working one later
    on PATH and every run fails with PermissionError instead of using it."""
    broken = tmp_path / "broken"
    good = tmp_path / "good"
    _fake_binary(broken).chmod(0o644)
    expected = _fake_binary(good)
    monkeypatch.setenv("PATH", os.pathsep.join([str(broken), str(good)]))

    opt = Optimizer(bin_dir=tmp_path / "nothing-here")
    assert opt._find_binary("pngquant") == expected.resolve()
