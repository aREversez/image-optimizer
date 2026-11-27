"""GIF and MPO are accepted inputs (found by hand-testing: both were silently
ignored by the scan). An MPO is a normal image; an animated GIF cannot be
represented and is reported as skipped, not failed."""
from __future__ import annotations

import asyncio
import json

from PIL import Image

from image_optimizer import watcher
from .test_cli import cli_args


def _src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (32, 32), (5, 6, 7)).save(src / "still.gif")
    # Distinct content per frame: Pillow merges identical frames into one.
    frames = [Image.effect_noise((16, 16), 80 + 30 * i).convert("P") for i in range(3)]
    frames[0].save(src / "anim.gif", save_all=True, append_images=frames[1:], duration=50, loop=0)
    Image.new("RGB", (32, 32), (9, 9, 9)).save(src / "stereo.mpo", "JPEG")  # JPEG bytes, .mpo name
    return src


def _run_cli(app_module, optimizer, src, out):
    return asyncio.run(app_module._run_cli(
        cli_args(source=str(src), output=str(out), json=True), optimizer_instance=optimizer,
    ))


def test_scan_lists_gif_and_mpo(app_module, tmp_path):
    names = {p.name for p in app_module._scan_images(_src(tmp_path), recursive=False)}
    assert {"still.gif", "anim.gif", "stereo.mpo"} <= names


def test_watcher_extensions_match_the_scan_extensions(app_module):
    assert watcher.IMAGE_EXTS == app_module.SCAN_EXTS


def test_cli_compresses_still_gif_and_mpo_and_skips_the_animation(app_module, optimizer, tmp_path, capsys):
    src = _src(tmp_path)
    out = tmp_path / "out"
    code = _run_cli(app_module, optimizer, src, out)
    report = json.loads(capsys.readouterr().out)
    files = {f["rel"]: f for f in report["files"]}
    assert files["still.gif"]["status"] in ("compressed", "kept_original")
    assert files["stereo.mpo"]["status"] in ("compressed", "kept_original")
    assert files["anim.gif"]["status"] == "skipped"
    assert "frames" in files["anim.gif"]["warnings"][0]
    assert report["summary"]["skipped"] == 1 and report["summary"]["failed"] == 0
    assert code == 0  # a skip is not a failure
    assert not (out / "anim.png").exists()
