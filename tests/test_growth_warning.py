"""Converting to another format can make a file larger (a photographic JPEG
becomes a much bigger PNG). The same-format growth guard cannot help there -
the original cannot stand in for a different format - so the run says so
instead of reporting a plain success (found by hand-testing: jpg -> png
grew 30.8% with no message)."""
from __future__ import annotations

import asyncio
import json

from PIL import Image

from .test_cli import cli_args


def _photo_jpg(path):
    Image.effect_noise((128, 128), 60).convert("RGB").save(path, "JPEG", quality=40)


def _run(optimizer, src, out, fmt):
    return asyncio.run(optimizer.optimize_image(
        src, out, quality="medium", output_format=fmt, compression_mode="standard",
    ))


def test_a_larger_cross_format_result_carries_a_warning(optimizer, tmp_path):
    src = tmp_path / "p.jpg"
    _photo_jpg(src)
    res = _run(optimizer, src, tmp_path / "o.png", "png")
    assert res["success"] and res["compressed_size"] > res["original_size"]
    assert res["warning"] and "larger" in res["warning"] and ".jpg" in res["warning"] and ".png" in res["warning"]


def test_a_smaller_result_has_no_growth_warning(optimizer, tmp_path):
    src = tmp_path / "flat.png"
    Image.new("RGB", (256, 256), (10, 20, 30)).save(src, "PNG", compress_level=0)
    res = _run(optimizer, src, tmp_path / "o.png", "png")
    assert res["success"] and not res.get("warning")


def test_an_existing_warning_is_kept(optimizer, tmp_path, monkeypatch):
    src = tmp_path / "p.jpg"
    _photo_jpg(src)
    real = type(optimizer)._optimize_image

    async def with_warning(self, *a, **k):
        r = await real(self, *a, **k)
        r["warning"] = "pngquant: quality too low"
        return r

    monkeypatch.setattr(type(optimizer), "_optimize_image", with_warning)
    res = _run(optimizer, src, tmp_path / "o.png", "png")
    assert res["warning"] == "pngquant: quality too low"


def test_cli_report_lists_the_growth_as_a_file_warning(app_module, optimizer, tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    _photo_jpg(src / "p.jpg")
    code = asyncio.run(app_module._run_cli(
        cli_args(source=str(src), output=str(tmp_path / "out"), json=True), optimizer_instance=optimizer,
    ))
    rec = json.loads(capsys.readouterr().out)["files"][0]
    assert code == 0 and rec["status"] == "compressed"
    assert any("larger" in w for w in rec["warnings"])
