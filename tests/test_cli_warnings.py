"""The CLI no longer degrades silently (review finding F12).

With only oxipng installed (the pip default on Linux/macOS) the default
--mode standard produced lossless-only output reported as "OK", and a
per-file warning from the optimizer was dropped. Now: an up-front warning,
per-file `warnings` in the --json report, and --strict to turn either into a
non-zero exit."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from PIL import Image

from .test_cli import cli_args


def _src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(src / "a.png")
    return src


class WarningOptimizer:
    """Copies bytes through and reports a per-file warning."""
    pngquant_path = oxipng_path = cjpeg_path = avifenc_path = None

    def available_modes(self):
        return {("png", "standard"), ("png", "lossless")}

    async def optimize_image(self, input_path, output_path, **kw):
        Path(output_path).write_bytes(Path(input_path).read_bytes())
        n = Path(output_path).stat().st_size
        return {"success": True, "original_size": n, "compressed_size": n, "error": None,
                "warning": "pngquant could not reach the quality floor, kept lossless result"}


def _run(app_module, args, opt, capsys):
    code = asyncio.run(app_module._run_cli(args, optimizer_instance=opt))
    out = capsys.readouterr()
    return code, out.out, out.err


class TestModeDegradation:
    def test_missing_pngquant_warns_up_front_and_in_json(self, app_module, optimizer, tmp_path, capsys):
        optimizer.pngquant_path = None  # the pip-install-on-Linux situation
        src, out = _src(tmp_path), tmp_path / "out"
        code, stdout, stderr = _run(
            app_module, cli_args(source=str(src), output=str(out), json=True), optimizer, capsys,
        )
        assert code == 0
        assert "pngquant not found" in stderr
        report = json.loads(stdout)
        assert any("pngquant not found" in w for w in report["warnings"])

    def test_full_toolchain_has_no_warnings(self, app_module, optimizer, tmp_path, capsys):
        src, out = _src(tmp_path), tmp_path / "out"
        code, stdout, stderr = _run(
            app_module, cli_args(source=str(src), output=str(out), json=True), optimizer, capsys,
        )
        report = json.loads(stdout)
        assert code == 0 and report["warnings"] == []
        assert all(f["warnings"] == [] for f in report["files"])
        assert report["summary"]["warned"] == 0


class TestPerFileWarnings:
    def test_optimizer_warning_reaches_log_and_json(self, app_module, tmp_path, capsys):
        src, out = _src(tmp_path), tmp_path / "out"
        code, stdout, stderr = _run(
            app_module, cli_args(source=str(src), output=str(out), json=True), WarningOptimizer(), capsys,
        )
        assert code == 0
        assert "quality floor" in stderr
        report = json.loads(stdout)
        assert report["files"][0]["warnings"] == ["pngquant could not reach the quality floor, kept lossless result"]
        assert report["summary"]["warned"] == 1


class TestStrict:
    def test_strict_fails_on_a_per_file_warning(self, app_module, tmp_path, capsys):
        src, out = _src(tmp_path), tmp_path / "out"
        code, stdout, stderr = _run(
            app_module, cli_args(source=str(src), output=str(out), strict=True), WarningOptimizer(), capsys,
        )
        # Human-readable lines go to stdout unless --json moves them to stderr.
        assert code == 1 and "--strict" in (stdout + stderr)

    def test_strict_fails_on_mode_degradation(self, app_module, optimizer, tmp_path, capsys):
        optimizer.pngquant_path = None
        src, out = _src(tmp_path), tmp_path / "out"
        code, _, _ = _run(app_module, cli_args(source=str(src), output=str(out), strict=True), optimizer, capsys)
        assert code == 1

    def test_without_strict_warnings_keep_exit_zero(self, app_module, tmp_path, capsys):
        src, out = _src(tmp_path), tmp_path / "out"
        code, _, _ = _run(app_module, cli_args(source=str(src), output=str(out)), WarningOptimizer(), capsys)
        assert code == 0

    def test_strict_passes_a_clean_run(self, app_module, optimizer, tmp_path, capsys):
        src, out = _src(tmp_path), tmp_path / "out"
        code, _, _ = _run(app_module, cli_args(source=str(src), output=str(out), strict=True), optimizer, capsys)
        assert code == 0
