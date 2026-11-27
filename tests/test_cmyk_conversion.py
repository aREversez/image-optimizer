"""CMYK sources are converted to sRGB with their embedded profile (found by
hand-testing: a bare convert("RGB") ignores the profile and came out about 30
levels off on average from a colour-managed rendering)."""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from PIL import Image, ImageCms

from image_optimizer import optimizer as opt_mod

FOGRA = Path("/usr/share/texlive/texmf-dist/tex/generic/colorprofiles/FOGRA39L_coated.icc")
needs_fogra = pytest.mark.skipif(not FOGRA.exists(), reason="no real CMYK ICC profile on this machine")


def _cmyk_jpeg(path: Path, colour, profile: bytes | None):
    kw = {"icc_profile": profile} if profile else {}
    Image.new("CMYK", (64, 64), colour).save(path, "JPEG", **kw)


def test_without_a_profile_the_plain_conversion_is_used():
    img = Image.new("CMYK", (4, 4), (255, 0, 0, 0))
    assert opt_mod.cmyk_to_rgb(img).getpixel((0, 0)) == img.convert("RGB").getpixel((0, 0))


def test_an_unusable_profile_falls_back_instead_of_failing():
    img = Image.new("CMYK", (4, 4), (255, 0, 0, 0))
    img.info["icc_profile"] = b"not an icc profile"
    assert opt_mod.cmyk_to_rgb(img).mode == "RGB"


def test_a_non_cmyk_profile_on_a_cmyk_image_is_not_used(monkeypatch):
    called = []
    monkeypatch.setattr(opt_mod.ImageCms, "profileToProfile", lambda *a, **k: called.append(1))
    img = Image.new("CMYK", (4, 4), (0, 0, 0, 0))
    hdr = bytearray(128); hdr[16:20] = b"RGB "
    img.info["icc_profile"] = bytes(hdr)
    assert opt_mod.cmyk_to_rgb(img).mode == "RGB" and not called


@needs_fogra
def test_profile_is_applied_to_the_conversion():
    prof = FOGRA.read_bytes()
    img = Image.new("CMYK", (4, 4), (255, 0, 0, 0))  # 100% cyan
    img.info["icc_profile"] = prof
    managed = opt_mod.cmyk_to_rgb(img).getpixel((0, 0))
    naive = img.convert("RGB").getpixel((0, 0))
    assert naive == (0, 255, 255)
    assert managed[1] < 215 and managed[0] < 60, managed  # FOGRA39 cyan is a deep (0,~158,~224)


@needs_fogra
@pytest.mark.parametrize("fmt", ["png", "webp", "jpg", "avif"])
def test_every_output_format_uses_the_managed_conversion(optimizer, tmp_path, fmt):
    src = tmp_path / "print.jpg"
    _cmyk_jpeg(src, (255, 0, 0, 0), FOGRA.read_bytes())
    out = tmp_path / f"o.{fmt}"
    res = asyncio.run(optimizer.optimize_image(src, out, quality="medium", output_format=fmt,
                                               compression_mode="lossless"))
    assert res["success"], res
    if fmt in ("png", "webp"):  # the fake jpg/avif encoders copy bytes, so only these are decodable RGB
        with Image.open(out) as im:
            px = im.convert("RGB").getpixel((8, 8))
        assert px[1] < 215, (fmt, px)
