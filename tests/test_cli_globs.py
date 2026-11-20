"""CLI --exclude / --include globs."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image

from tests.test_cli import cli_args


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    for rel in [
        "top.png", "Logo.PNG", "draft_a.png",
        "icons/a.png", "icons/deep/b.png",
        "drafts/old/c.png", "drafts/d.png",
        "photos/2024/x.png",
    ]:
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), (len(rel) * 7 % 255, 40, 90)).save(f)
    return src


def _run(app_module, optimizer, src, out, **kw):
    return asyncio.run(app_module._run_cli(
        cli_args(source=str(src), output=str(out), **kw), optimizer_instance=optimizer,
    ))


def _produced(out: Path) -> set[str]:
    return {p.relative_to(out).as_posix() for p in out.rglob("*.png")}


class TestMatchesGlobs:
    def test_pattern_without_slash_matches_basename_at_any_depth(self, app_module):
        m = app_module._matches_globs
        assert m("a.bak", ["*.bak"])
        assert m("sub/dir/a.bak", ["*.bak"])
        assert not m("sub/a.png", ["*.bak"])

    def test_pattern_without_slash_does_not_match_directory_names(self, app_module):
        # "drafts*" is a basename pattern: the directory part is not considered.
        assert not app_module._matches_globs("drafts/d.png", ["drafts*"])

    def test_pattern_with_slash_matches_relative_path(self, app_module):
        m = app_module._matches_globs
        assert m("drafts/d.png", ["drafts/*"])
        assert m("drafts/old/c.png", ["drafts/*"])  # fnmatch: * crosses /
        assert not m("other/drafts/d.png", ["drafts/*"])  # anchored at --source
        assert m("icons/deep/b.png", ["icons/*/b.png"])

    def test_case_insensitive_both_ways(self, app_module):
        m = app_module._matches_globs
        assert m("Logo.PNG", ["logo.png"])
        assert m("logo.png", ["LOGO.*"])
        assert m("Icons/A.png", ["icons/a.PNG"])

    def test_backslash_in_pattern_is_treated_as_slash(self, app_module):
        assert app_module._matches_globs("icons/a.png", ["icons\\*.png"])

    def test_empty_patterns_are_ignored_and_empty_list_matches_nothing(self, app_module):
        m = app_module._matches_globs
        assert not m("a.png", [])
        assert not m("a.png", ["", "   "])


class TestExcludeInclude:
    def test_exclude_basename_any_depth(self, app_module, optimizer, tree, tmp_path):
        out = tmp_path / "out"
        assert _run(app_module, optimizer, tree, out, exclude=["b.png", "x.png"]) == 0
        got = _produced(out)
        assert "icons/deep/b.png" not in got and "photos/2024/x.png" not in got
        assert "icons/a.png" in got and "top.png" in got

    def test_exclude_path_pattern(self, app_module, optimizer, tree, tmp_path):
        out = tmp_path / "out"
        _run(app_module, optimizer, tree, out, exclude=["drafts/*"])
        got = _produced(out)
        assert not any(g.startswith("drafts/") for g in got)
        assert "draft_a.png" in got  # same prefix, different location

    def test_include_is_a_whitelist(self, app_module, optimizer, tree, tmp_path):
        out = tmp_path / "out"
        _run(app_module, optimizer, tree, out, include=["icons/*"])
        assert _produced(out) == {"icons/a.png", "icons/deep/b.png"}

    def test_multiple_includes_union(self, app_module, optimizer, tree, tmp_path):
        out = tmp_path / "out"
        _run(app_module, optimizer, tree, out, include=["icons/a.png", "top.png"])
        assert _produced(out) == {"icons/a.png", "top.png"}

    def test_exclude_beats_include(self, app_module, optimizer, tree, tmp_path):
        out = tmp_path / "out"
        _run(app_module, optimizer, tree, out, include=["icons/*"], exclude=["b.png"])
        assert _produced(out) == {"icons/a.png"}

    def test_case_insensitive_end_to_end(self, app_module, optimizer, tree, tmp_path):
        out = tmp_path / "out"
        _run(app_module, optimizer, tree, out, exclude=["LOGO.png"])
        assert "Logo.png" not in _produced(out) and "Logo.PNG" not in _produced(out)

    def test_excluded_files_make_no_output_and_do_not_fail(
        self, app_module, optimizer, tree, tmp_path, capsys
    ):
        out = tmp_path / "out"
        # an excluded corrupt file must not turn the exit code into 1
        (tree / "broken.png").write_bytes(b"not a png")
        code = _run(app_module, optimizer, tree, out, exclude=["broken.png"])
        assert code == 0
        assert not (out / "broken.png").exists()
        text = capsys.readouterr().out
        assert "0 failed" in text and "Excluded: 1 file(s)" in text

    def test_everything_excluded_is_a_clean_noop(self, app_module, optimizer, tree, tmp_path, capsys):
        out = tmp_path / "out"
        code = _run(app_module, optimizer, tree, out, exclude=["*"])
        assert code == 0
        assert not out.exists()
        assert "No images left after" in capsys.readouterr().out

    def test_no_patterns_changes_nothing(self, app_module, optimizer, tree, tmp_path, capsys):
        out = tmp_path / "out"
        assert _run(app_module, optimizer, tree, out) == 0
        assert len(_produced(out)) == 8
        assert "Excluded:" not in capsys.readouterr().out
