"""Animated and multi-page inputs are refused, never flattened (review F6).

Pillow reads only the first frame, so an animated WebP/GIF/APNG or a
multi-page TIFF used to come out as a one-frame image with success=True:
the animation was lost with no signal."""
from __future__ import annotations

import asyncio

import pytest
from PIL import Image


def _frames(n):
    return [Image.new("RGB", (32, 32), (i * 40, 10, 10)) for i in range(n)]


def _animated(path, fmt, n=5):
    fr = _frames(n)
    kw = {"save_all": True, "append_images": fr[1:]}
    if fmt in ("webp", "gif", "png"):
        kw.update(duration=100, loop=0)
    fr[0].save(path, **kw)


@pytest.mark.parametrize("src_name,fmt", [
    ("anim.webp", "webp"), ("anim.gif", "gif"), ("anim.tiff", "tiff"), ("anim.png", "png"),
])
@pytest.mark.parametrize("out_fmt", ["png", "webp", "jpg"])
def test_multiframe_source_fails_with_a_clear_error(optimizer, tmp_path, src_name, fmt, out_fmt):
    src = tmp_path / src_name
    _animated(src, fmt)
    out = tmp_path / f"out.{out_fmt}"
    res = asyncio.run(optimizer.optimize_png(src, out, quality="medium", output_format=out_fmt))
    assert res["success"] is False
    assert "frames" in res["error"] and "5" in res["error"]
    assert not out.exists(), "nothing may be written for a refused input"


@pytest.mark.parametrize("src_name,fmt", [("one.webp", "webp"), ("one.gif", "gif"), ("one.tiff", "tiff")])
def test_single_frame_files_of_the_same_formats_still_work(optimizer, tmp_path, src_name, fmt):
    src = tmp_path / src_name
    Image.new("RGB", (32, 32), (9, 9, 9)).save(src)
    out = tmp_path / "out.webp"
    res = asyncio.run(optimizer.optimize_png(src, out, quality="medium", output_format="webp"))
    assert res["success"], res.get("error")


def test_mpo_jpeg_is_not_treated_as_animated(optimizer, tmp_path):
    """Phone photos with a depth map / gain map are JPEGs that Pillow opens
    as MPO with n_frames >= 2. They are ordinary photos (the first frame is
    the image), not animations, and must keep converting."""
    src = tmp_path / "phone.jpg"
    a = Image.new("RGB", (64, 64), (200, 0, 0))
    a.save(src, save_all=True, append_images=[Image.new("RGB", (64, 64), (0, 0, 200))], format="MPO")
    with Image.open(src) as im:
        assert im.format == "MPO" and im.n_frames == 2  # the premise
    out = tmp_path / "out.webp"
    res = asyncio.run(optimizer.optimize_png(src, out, quality="medium", output_format="webp"))
    assert res["success"], res.get("error")
    assert out.exists()
