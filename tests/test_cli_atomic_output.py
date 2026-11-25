"""CLI final outputs are written via a staging file (review finding F8).

The encoder used to be pointed straight at the destination, so a failed or
killed run left a partial file under the real output name - which a later
--skip/cache check or the user could mistake for a finished result."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from PIL import Image

from .test_cli import cli_args


class PartialWriter:
    """Optimizer stand-in that writes half a file to whatever path it is
    given and then fails, the way a killed encoder would."""

    pngquant_path = oxipng_path = cjpeg_path = avifenc_path = None

    def available_modes(self):
        return {("png", "standard"), ("png", "lossless")}

    async def optimize_png(self, input_path, output_path, **kw):
        Path(output_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"partial")
        return {"success": False, "error": "encoder killed", "original_size": 123, "compressed_size": None}


def _src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(src / "a.png")
    return src


class TestNoPartialFileAtDestination:
    def test_failed_encode_leaves_nothing_in_output(self, app_module, tmp_path, capsys):
        src, out = _src(tmp_path), tmp_path / "out"
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out)), optimizer_instance=PartialWriter(),
        ))
        assert code == 1
        assert sorted(p.name for p in out.rglob("*") if p.is_file()) == []

    def test_failed_final_rename_is_a_failed_record_and_leaves_nothing(
        self, app_module, optimizer, tmp_path, capsys, monkeypatch,
    ):
        src, out = _src(tmp_path), tmp_path / "out"
        def boom(staged, dest):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(app_module.persistence, "replace_with_retry", boom)
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out), json=True), optimizer_instance=optimizer,
        ))
        assert code == 1
        report = json.loads(capsys.readouterr().out)
        f = report["files"][0]
        assert f["status"] == "failed" and "No space left" in f["error"]
        assert [p for p in out.rglob("*") if p.is_file()] == []

    def test_success_leaves_only_the_final_file(self, app_module, optimizer, tmp_path):
        src, out = _src(tmp_path), tmp_path / "out"
        code = asyncio.run(app_module._run_cli(
            cli_args(source=str(src), output=str(out)), optimizer_instance=optimizer,
        ))
        assert code == 0
        assert sorted(p.name for p in out.rglob("*") if p.is_file()) == ["a.png"]

    def test_cache_hit_also_goes_through_staging(self, app_module, optimizer, tmp_path):
        src, out, cache = _src(tmp_path), tmp_path / "out", tmp_path / "cache"
        for _ in range(2):
            code = asyncio.run(app_module._run_cli(
                cli_args(source=str(src), output=str(out), cache_dir=str(cache)),
                optimizer_instance=optimizer,
            ))
            assert code == 0
        assert sorted(p.name for p in out.rglob("*") if p.is_file()) == ["a.png"]
