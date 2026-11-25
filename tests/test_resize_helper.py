"""One resize rule for every output path (review finding F7).

--max-width limits the width only (the CLI help wrongly said "longest side")
and the height was int(h * ratio), which reaches 0 for very wide images and
failed with "height and width must be > 0". Four copies of the arithmetic
existed; they now share image_optimizer.optimizer.fit_width."""
from __future__ import annotations

import asyncio

import pytest
from PIL import Image

from image_optimizer import optimizer as opt_mod


class TestFitWidth:
    def test_no_resize_when_already_narrow_enough(self):
        assert opt_mod.fit_width((1000, 4000), 1500) is None
        assert opt_mod.fit_width((1500, 10), 1500) is None
        assert opt_mod.fit_width((100, 100), 0) is None

    def test_scales_proportionally(self):
        assert opt_mod.fit_width((2000, 1000), 1000) == (1000, 500)

    def test_height_never_collapses_to_zero(self):
        assert opt_mod.fit_width((5000, 1), 100) == (100, 1)
        assert opt_mod.fit_width((100000, 3), 10) == (10, 1)


@pytest.mark.parametrize("fmt", ["png", "webp"])
def test_panorama_resize_succeeds_end_to_end(optimizer, tmp_path, fmt):
    src = tmp_path / "pano.png"
    Image.new("RGB", (5000, 1), (1, 2, 3)).save(src)
    out = tmp_path / f"pano.{fmt}"
    res = asyncio.run(optimizer.optimize_png(
        src, out, quality="medium", max_width=100,
        compression_mode="resize_only" if fmt == "png" else "standard",
    ))
    assert res["success"], res.get("error")
    with Image.open(out) as im:
        assert im.size == (100, 1)
