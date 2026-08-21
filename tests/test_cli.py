"""Regression tests for CLI mode (_run_cli / `--source` / `--output`)."""
from __future__ import annotations

import argparse
import asyncio

from PIL import Image


def cli_args(**overrides) -> argparse.Namespace:
    """Defaults matching the argparse flags' own defaults in main(), so a
    test only needs to override what it actually cares about."""
    defaults = dict(
        source=None, output=None, quality="medium", format="png", mode="standard",
        max_width=0, dithering=True, protect_colors="", keep_exif=False, recursive=True,
        workers=4,
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
