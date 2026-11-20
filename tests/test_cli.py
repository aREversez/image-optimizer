"""Regression tests for CLI mode (_run_cli / `--source` / `--output`)."""
from __future__ import annotations

import argparse
import asyncio
import json

import pytest
from PIL import Image


def cli_args(**overrides) -> argparse.Namespace:
    """Defaults matching the argparse flags' own defaults in main(), so a
    test only needs to override what it actually cares about."""
    defaults = dict(
        source=None, output=None, quality="medium", format="png", mode="standard",
        max_width=0, dithering=True, protect_colors="", keep_exif=False, recursive=True,
        workers=4, cache_dir=None, cache_max_size_mb=1024,
        exclude=[], include=[], json=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestCliValidation:
    """Error paths — these must all exit non-zero with a clear stderr
    message and must NOT reach the optimizer at all (no optimizer_instance
    passed means a bug here would try to construct a real Optimizer and
    likely still "work" by accident; capsys catches the message either way)."""

    def test_missing_output_is_rejected(self, app_module, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        code = asyncio.run(app_module._run_cli(cli_args(source=str(src))))
        assert code == 1
        assert "--output is required" in capsys.readouterr().err

    def test_nonexistent_source_is_rejected(self, app_module, tmp_path, capsys):
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(tmp_path / "nope"), output=str(tmp_path / "out")),
        ))
        assert code == 1
        assert "not a directory" in capsys.readouterr().err

    def test_source_that_is_a_file_not_a_directory_is_rejected(self, app_module, tmp_path, capsys):
        f = tmp_path / "a_file.png"
        Image.new("RGB", (10, 10)).save(f)
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(f), output=str(tmp_path / "out")),
        ))
        assert code == 1
        assert "not a directory" in capsys.readouterr().err

    def test_resize_only_without_max_width_is_rejected(self, app_module, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(tmp_path / "out"), mode="resize_only"),
        ))
        assert code == 1
        assert "Max Width" in capsys.readouterr().err

    def test_screenshot_mode_with_non_png_format_is_rejected(self, app_module, tmp_path, capsys):
        """Same _validate_optimize_settings() the web/watch entry points
        use — this is the point of that consolidation: a 4th entry point
        gets this rule for free instead of a 4th slightly-different copy."""
        src = tmp_path / "src"
        src.mkdir()
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(tmp_path / "out"), mode="screenshot", format="jpg"),
        ))
        assert code == 1
        assert "PNG-only" in capsys.readouterr().err

    def test_invalid_protect_colors_is_rejected(self, app_module, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(tmp_path / "out"), protect_colors="not-a-color"),
        ))
        assert code == 1
        assert "Protect Colors" in capsys.readouterr().err


class TestCliRun:
    """The actual batch-run path, against fake pngquant/oxipng (see
    conftest.optimizer) so these don't depend on real binaries."""

    def test_empty_source_dir_returns_zero_no_crash(self, app_module, optimizer, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(tmp_path / "out")), optimizer_instance=optimizer,
        ))
        assert code == 0
        assert "No supported images found" in capsys.readouterr().out

    def test_compresses_and_preserves_directory_structure(self, app_module, optimizer, tmp_path, capsys):
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        Image.new("RGB", (64, 64), (46, 204, 113)).save(src / "top.png")
        Image.new("RGB", (64, 64), (200, 50, 50)).save(src / "sub" / "nested.png")
        out = tmp_path / "out"

        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out)), optimizer_instance=optimizer,
        ))
        assert code == 0
        assert (out / "top.png").exists()
        assert (out / "sub" / "nested.png").exists()
        text = capsys.readouterr().out
        assert "Done: 2 succeeded, 0 failed, 2 total" in text

    def test_name_collision_gets_numbered_not_overwritten(self, app_module, optimizer, tmp_path):
        """Two source files that map to the same output name after the
        format-suffix swap must not silently overwrite one another —
        exercises _resolve_collision_free_output_paths through CLI mode."""
        src = tmp_path / "src"
        src.mkdir()
        Image.new("RGB", (10, 10)).save(src / "photo.png")
        Image.new("RGB", (10, 10)).save(src / "photo.jpg", format="JPEG")
        out = tmp_path / "out"

        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out)), optimizer_instance=optimizer,
        ))
        assert code == 0
        produced = sorted(p.name for p in out.glob("*.png"))
        assert produced == ["photo.png", "photo_2.png"]

    def test_recursive_false_only_scans_top_level(self, app_module, optimizer, tmp_path):
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        Image.new("RGB", (10, 10)).save(src / "top.png")
        Image.new("RGB", (10, 10)).save(src / "sub" / "nested.png")
        out = tmp_path / "out"

        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out), recursive=False), optimizer_instance=optimizer,
        ))
        assert code == 0
        assert (out / "top.png").exists()
        assert not (out / "sub" / "nested.png").exists()

    def test_resize_only_actually_resizes(self, app_module, optimizer, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        Image.new("RGB", (400, 300), (30, 144, 255)).save(src / "big.png")
        out = tmp_path / "out"

        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out), mode="resize_only", max_width=100),
            optimizer_instance=optimizer,
        ))
        assert code == 0
        with Image.open(out / "big.png") as img:
            assert img.size == (100, 75)  # aspect ratio preserved

    def test_output_format_conversion_changes_extension(self, app_module, optimizer, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        Image.new("RGB", (32, 32)).save(src / "img.png")
        out = tmp_path / "out"

        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out), format="webp"), optimizer_instance=optimizer,
        ))
        assert code == 0
        assert (out / "img.webp").exists()
        assert not (out / "img.png").exists()

    def test_nonzero_exit_code_when_any_file_fails(self, app_module, optimizer, tmp_path):
        """A corrupt/unreadable source file should fail that one item and
        still process the rest, but the overall exit code must reflect the
        partial failure (so this is detectable in a script/CI)."""
        src = tmp_path / "src"
        src.mkdir()
        Image.new("RGB", (10, 10)).save(src / "good.png")
        (src / "bad.png").write_bytes(b"not actually a png")
        out = tmp_path / "out"

        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out)), optimizer_instance=optimizer,
        ))
        assert code == 1
        assert (out / "good.png").exists()


class TestCliJson:
    """--json: one parseable JSON object on stdout, human output on stderr."""

    @staticmethod
    def _mixed_src(tmp_path):
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        # jpg -> png is a real conversion: genuine "compressed" result
        Image.new("RGB", (64, 64), (200, 30, 30)).save(src / "conv.jpg", format="JPEG")
        # png -> png through the pass-through fake encoders never shrinks, so
        # the growth guard hands back the stripped original: "kept_original"
        Image.new("RGB", (64, 64), (30, 200, 30)).save(src / "sub" / "same.png")
        (src / "bad.png").write_bytes(b"not actually a png")
        Image.new("RGB", (16, 16)).save(src / "skip.png")
        return src

    def _run(self, app_module, optimizer, src, out, **kw):
        return asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out), json=True, **kw),
            optimizer_instance=optimizer,
        ))

    def test_stdout_is_exactly_one_json_object_and_logs_go_to_stderr(
        self, app_module, optimizer, tmp_path, capsys
    ):
        src = self._mixed_src(tmp_path)
        self._run(app_module, optimizer, src, tmp_path / "out", exclude=["skip.png"])
        cap = capsys.readouterr()
        report = json.loads(cap.out)  # raises if anything else is on stdout
        assert set(report) == {"files", "summary"}
        assert "Done:" in cap.err and "[1/" in cap.err
        assert "Done:" not in cap.out

    def test_per_file_statuses_and_fields(self, app_module, optimizer, tmp_path, capsys):
        src = self._mixed_src(tmp_path)
        code = self._run(app_module, optimizer, src, tmp_path / "out", exclude=["skip.png"])
        assert code == 1  # bad.png failed; excluded must not count
        files = {f["rel"]: f for f in json.loads(capsys.readouterr().out)["files"]}
        assert set(files) == {"conv.jpg", "sub/same.png", "bad.png", "skip.png"}
        assert all(set(f) == {"rel", "output_name", "original_size", "compressed_size", "status", "error"}
                   for f in files.values())

        assert files["conv.jpg"]["status"] == "compressed"
        assert files["conv.jpg"]["output_name"] == "conv.png"
        assert files["sub/same.png"]["status"] == "kept_original"  # wins over "compressed"
        assert files["sub/same.png"]["compressed_size"] == (tmp_path / "out" / "sub" / "same.png").stat().st_size
        assert files["bad.png"]["status"] == "failed" and files["bad.png"]["error"]
        assert files["bad.png"]["compressed_size"] is None
        assert files["skip.png"]["status"] == "excluded"
        assert files["skip.png"]["output_name"] is None and files["skip.png"]["error"] is None

    def test_summary_counts_and_sizes(self, app_module, optimizer, tmp_path, capsys):
        src = self._mixed_src(tmp_path)
        self._run(app_module, optimizer, src, tmp_path / "out", exclude=["skip.png"])
        report = json.loads(capsys.readouterr().out)
        s = report["summary"]
        assert s["total"] == 4
        assert (s["compressed"], s["kept_original"], s["failed"], s["excluded"], s["cached"]) == (1, 1, 1, 1, 0)
        ok = [f for f in report["files"] if f["status"] in ("compressed", "kept_original")]
        assert s["original_size"] == sum(f["original_size"] for f in ok)
        assert s["compressed_size"] == sum(f["compressed_size"] for f in ok)
        assert s["saved_percent"] == round((1 - s["compressed_size"] / s["original_size"]) * 100, 1)

    def test_cached_status_on_second_run(self, app_module, optimizer, tmp_path, capsys):
        src = self._mixed_src(tmp_path)
        kw = dict(exclude=["skip.png"], cache_dir=str(tmp_path / "cache"))
        self._run(app_module, optimizer, src, tmp_path / "o1", **kw)
        capsys.readouterr()
        self._run(app_module, optimizer, src, tmp_path / "o2", **kw)
        report = json.loads(capsys.readouterr().out)
        st = {f["rel"]: f["status"] for f in report["files"]}
        assert st["conv.jpg"] == "cached" and st["sub/same.png"] == "cached"
        assert st["bad.png"] == "failed"  # failures are never served from cache
        assert report["summary"]["cached"] == 2

    def test_exit_code_ignores_excluded_and_cached(self, app_module, optimizer, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        Image.new("RGB", (16, 16)).save(src / "a.png")
        Image.new("RGB", (16, 16)).save(src / "b.png")
        kw = dict(exclude=["b.png"], cache_dir=str(tmp_path / "c"))
        assert self._run(app_module, optimizer, src, tmp_path / "o1", **kw) == 0
        assert self._run(app_module, optimizer, src, tmp_path / "o2", **kw) == 0

    def test_empty_source_still_emits_valid_report(self, app_module, optimizer, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        assert self._run(app_module, optimizer, src, tmp_path / "out") == 0
        cap = capsys.readouterr()
        assert json.loads(cap.out)["summary"]["total"] == 0
        assert "No supported images found" in cap.err

    def test_everything_excluded_reports_excluded(self, app_module, optimizer, tmp_path, capsys):
        src = self._mixed_src(tmp_path)
        assert self._run(app_module, optimizer, src, tmp_path / "out", exclude=["*"]) == 0
        s = json.loads(capsys.readouterr().out)["summary"]
        assert s["excluded"] == 4 and s["total"] == 4 and s["compressed"] == 0

    def test_non_ascii_names_keep_stdout_parseable(self, app_module, optimizer, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        Image.new("RGB", (16, 16)).save(src / "截图 1.png")
        self._run(app_module, optimizer, src, tmp_path / "out")
        files = json.loads(capsys.readouterr().out)["files"]
        assert files[0]["rel"] == "截图 1.png"

    def test_without_json_stdout_has_no_json(self, app_module, optimizer, tmp_path, capsys):
        src = self._mixed_src(tmp_path)
        asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(tmp_path / "out")), optimizer_instance=optimizer,
        ))
        cap = capsys.readouterr()
        assert "Done:" in cap.out
        with pytest.raises(json.JSONDecodeError):
            json.loads(cap.out)
        assert '"summary"' not in cap.out

    def test_setup_errors_stay_on_stderr_without_a_report(self, app_module, tmp_path, capsys):
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(tmp_path / "nope"), output=str(tmp_path / "o"), json=True),
        ))
        cap = capsys.readouterr()
        assert code == 1 and cap.out == "" and "not a directory" in cap.err
