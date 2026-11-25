"""Output-folder safety (review finding F1).

The same rule must hold in every entry point: a run may never write into
the folder it reads from (originals replaced) nor into a subfolder that the
next recursive scan would pick up again (generation loss). Only Watch mode
had this guard; these tests pin it for the CLI and the web batch too, and
check the shared helper directly.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

from image_optimizer import planning
from .conftest import wait_for
from .test_cli import cli_args
from .test_api_optimize import scan_and_wait, optimize_and_wait


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    Image.new("RGB", (64, 64), (200, 50, 50)).save(src / "a.png")
    Image.new("RGB", (64, 64), (50, 200, 50)).save(src / "sub" / "b.png")
    return src


class TestPlanningHelper:
    def test_equal_folders_conflict(self, tmp_path):
        assert planning.output_dir_conflict(tmp_path, tmp_path, recursive=False)
        assert planning.output_dir_conflict(tmp_path, tmp_path, recursive=True)

    def test_equal_via_dotdot_and_trailing_parts(self, tmp_path):
        (tmp_path / "x").mkdir()
        assert planning.output_dir_conflict(tmp_path / "x", tmp_path / "x" / ".." / "x", True)

    def test_nested_conflicts_only_when_recursive(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        assert planning.output_dir_conflict(tmp_path, out, recursive=True)
        assert planning.output_dir_conflict(tmp_path, out, recursive=False) is None

    def test_sibling_and_prefix_lookalike_are_fine(self, tmp_path):
        a, b = tmp_path / "photos", tmp_path / "photos_out"
        a.mkdir()
        b.mkdir()
        assert planning.output_dir_conflict(a, b, recursive=True) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_symlinked_output_resolving_into_source_conflicts(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        link = tmp_path / "link"
        link.symlink_to(src, target_is_directory=True)
        assert planning.output_dir_conflict(src, link, recursive=False)
        nested = src / "out"
        nested.mkdir()
        link2 = tmp_path / "link2"
        link2.symlink_to(nested, target_is_directory=True)
        assert planning.output_dir_conflict(src, link2, recursive=True)

    def test_planned_output_that_is_a_source_is_reported(self, tmp_path):
        s = tmp_path / "a.png"
        s.write_bytes(b"x")
        assert planning.planned_outputs_hit_sources([tmp_path / "other.png", s], [s]) == s
        assert planning.planned_outputs_hit_sources([tmp_path / "other.png"], [s]) is None


class TestCliRejectsUnsafeOutput:
    def test_output_equal_to_source_is_rejected_and_originals_untouched(
        self, app_module, optimizer, tmp_path, capsys,
    ):
        src = _make_src(tmp_path)
        before = {p: _digest(p) for p in src.rglob("*.png")}
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(src)), optimizer_instance=optimizer,
        ))
        assert code == 1
        assert "source folder" in capsys.readouterr().err
        assert {p: _digest(p) for p in src.rglob("*.png")} == before
        assert sorted(p.name for p in src.rglob("*")) == sorted(["a.png", "sub", "b.png"])

    def test_output_nested_in_source_is_rejected_when_recursive(
        self, app_module, optimizer, tmp_path, capsys,
    ):
        src = _make_src(tmp_path)
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(src / "out")), optimizer_instance=optimizer,
        ))
        assert code == 1
        assert "inside the source" in capsys.readouterr().err
        assert not (src / "out").exists()  # rejected before anything was created

    def test_output_nested_in_source_is_allowed_when_not_recursive(
        self, app_module, optimizer, tmp_path,
    ):
        src = _make_src(tmp_path)
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(src / "out"), recursive=False),
            optimizer_instance=optimizer,
        ))
        assert code == 0
        assert (src / "out" / "a.png").exists()

    def test_second_run_does_not_recompress_its_own_output(self, app_module, optimizer, tmp_path):
        """The observed F1 symptom: out/a.png, out/out/a.png after two runs."""
        src = _make_src(tmp_path)
        out = tmp_path / "out"
        for _ in range(2):
            assert asyncio.run(app_module._run_cli(
                cli_args(source=str(src), output=str(out)), optimizer_instance=optimizer,
            )) == 0
        assert not (out / "out").exists()

    def test_source_inside_output_overwriting_an_original_is_rejected(
        self, app_module, optimizer, tmp_path, capsys,
    ):
        """output=/p, source=/p/q holding both q/a.png and q/q/a.png: the
        planned output for q/q/a.png is /p/q/a.png, which IS an original.
        Harmless layouts (no such overlap) stay allowed - see next test."""
        out = tmp_path / "p"
        src = out / "q"
        (src / "q").mkdir(parents=True)
        Image.new("RGB", (32, 32), (9, 9, 9)).save(src / "a.png")
        Image.new("RGB", (32, 32), (7, 7, 7)).save(src / "q" / "a.png")
        before = {p: _digest(p) for p in src.rglob("*.png")}
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out)), optimizer_instance=optimizer,
        ))
        assert code == 1
        assert "overwrite" in capsys.readouterr().err
        assert {p: _digest(p) for p in src.rglob("*.png")} == before

    def test_source_inside_output_without_overlap_is_allowed(self, app_module, optimizer, tmp_path):
        out = tmp_path / "o"
        src = out / "src"
        src.mkdir(parents=True)
        Image.new("RGB", (32, 32), (1, 2, 3)).save(src / "a.png")
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out)), optimizer_instance=optimizer,
        ))
        assert code == 0
        assert (out / "a.png").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_symlinked_output_into_source_is_rejected(self, app_module, optimizer, tmp_path, capsys):
        src = _make_src(tmp_path)
        link = tmp_path / "link"
        link.symlink_to(src, target_is_directory=True)
        before = {p: _digest(p) for p in src.rglob("*.png")}
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(link)), optimizer_instance=optimizer,
        ))
        assert code == 1
        assert {p: _digest(p) for p in src.rglob("*.png")} == before


class TestWebRejectsUnsafeOutput:
    def test_output_equal_to_scanned_folder_is_rejected(self, client, auth_headers, tmp_path):
        src = _make_src(tmp_path)
        before = {p: _digest(p) for p in src.rglob("*.png")}
        scan_and_wait(client, auth_headers, src)
        r, _ = optimize_and_wait(
            client, auth_headers, quality="medium", output_format="png",
            output_dir=str(src),
        )
        assert r.status_code == 400
        assert "source folder" in r.json()["error"]
        assert {p: _digest(p) for p in src.rglob("*.png")} == before

    def test_output_nested_in_recursively_scanned_folder_is_rejected(self, client, auth_headers, tmp_path):
        src = _make_src(tmp_path)
        scan_and_wait(client, auth_headers, src, recursive=True)
        r, _ = optimize_and_wait(
            client, auth_headers, quality="medium", output_format="png",
            output_dir=str(src / "out"),
        )
        assert r.status_code == 400
        assert not (src / "out").exists()

    def test_output_nested_in_non_recursively_scanned_folder_is_allowed(self, client, auth_headers, tmp_path):
        src = _make_src(tmp_path)
        scan_and_wait(client, auth_headers, src, recursive=False)
        r, d = optimize_and_wait(
            client, auth_headers, quality="medium", output_format="png",
            output_dir=str(src / "out"),
        )
        assert r.status_code == 200
        assert (src / "out" / "a.png").exists()

    def test_copy_step_refuses_to_replace_an_original(self, client, auth_headers, tmp_path, app_module, monkeypatch):
        """Defense in depth: with the up-front checks bypassed, the final
        copy itself must still refuse to replace a source file. (The fake
        encoders copy bytes through, so a replaced file would be byte-
        identical; the refusal is asserted through the run log and the
        file's inode instead.)"""
        src = _make_src(tmp_path)
        scan_and_wait(client, auth_headers, src, recursive=False)
        monkeypatch.setattr(app_module.planning, "output_dir_conflict", lambda *a, **k: None)
        monkeypatch.setattr(app_module.planning, "planned_outputs_hit_sources", lambda *a, **k: None)
        inode_before = os.stat(src / "a.png").st_ino
        r, d = optimize_and_wait(
            client, auth_headers, quality="medium", output_format="png",
            output_dir=str(src),
        )
        assert r.status_code == 200
        logs = client.get("/api/progress", headers=auth_headers).json()["logs"]
        assert any("refusing to overwrite the original" in line for line in logs)
        assert os.stat(src / "a.png").st_ino == inode_before


class TestWatchStillUsesSharedRule:
    def test_watch_helper_delegates_to_shared_rule(self, app_module, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        assert app_module._watch_output_conflicts_with_input(tmp_path, tmp_path, False) is True
        assert app_module._watch_output_conflicts_with_input(tmp_path, out, True) is True
        assert app_module._watch_output_conflicts_with_input(tmp_path, out, False) is False
