"""Where the ICC profile sits in a PNG, and what pngquant is allowed to do
to it (found by hand-testing with the real pngquant).

- pngquant colour-manages an input that carries an iCCP chunk: it converts
  the pixels to sRGB and replaces the profile with gAMA + sRGB. Re-attaching
  the original profile afterwards would then label sRGB pixels as (say)
  Adobe RGB. The input pngquant sees therefore must not carry the profile;
  its pixels stay in the source space and the profile is attached afterwards.
- The PNG spec requires iCCP before PLTE and IDAT. It was being inserted
  just before IEND.
"""
from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest
from PIL import Image

from .test_icc_profile import ICC, _read_icc


def _chunk_types(path: Path) -> list[str]:
    data = path.read_bytes()[8:]
    out, pos = [], 0
    while pos + 8 <= len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        out.append(data[pos + 4:pos + 8].decode("latin-1"))
        pos += 12 + n
    return out


def _run(optimizer, src, out, mode):
    res = asyncio.run(optimizer.optimize_image(
        src, out, quality="medium", output_format="png", compression_mode=mode,
    ))
    assert res["success"], res
    return res


@pytest.mark.parametrize("mode", ["standard", "screenshot", "lossless"])
def test_iccp_precedes_the_image_data(optimizer, tmp_path, mode):
    src = tmp_path / "p.png"
    Image.new("RGB", (64, 64), (200, 30, 90)).save(src, "PNG", icc_profile=ICC)
    out = tmp_path / "o.png"
    _run(optimizer, src, out, mode)
    kinds = _chunk_types(out)
    assert "iCCP" in kinds and "IDAT" in kinds
    assert kinds.index("iCCP") < kinds.index("IDAT"), kinds
    if "PLTE" in kinds:
        assert kinds.index("iCCP") < kinds.index("PLTE"), kinds
    assert kinds[0] == "IHDR"
    assert _read_icc(out) == ICC


@pytest.mark.parametrize("mode", ["standard", "screenshot"])
def test_pngquant_is_not_given_the_profile_and_no_srgb_claim_survives(optimizer, tmp_path, mode):
    """With a profile in its input pngquant would convert to sRGB and emit an
    sRGB chunk; the output must not claim both sRGB and the source profile."""
    src = tmp_path / "p.png"
    Image.new("RGB", (64, 64), (200, 30, 90)).save(src, "PNG", icc_profile=ICC)
    out = tmp_path / "o.png"
    _run(optimizer, src, out, mode)
    kinds = _chunk_types(out)
    assert "sRGB" not in kinds and "gAMA" not in kinds, kinds


def test_jpeg_source_to_png_standard_places_the_profile_before_idat(optimizer, tmp_path):
    src = tmp_path / "p.jpg"
    Image.new("RGB", (64, 64), (5, 99, 200)).save(src, "JPEG", icc_profile=ICC)
    out = tmp_path / "o.png"
    _run(optimizer, src, out, "standard")
    kinds = _chunk_types(out)
    assert kinds.index("iCCP") < kinds.index("IDAT") and "sRGB" not in kinds
    assert _read_icc(out) == ICC


def test_finalize_inserts_after_ihdr_and_drops_srgb(optimizer, tmp_path):
    from image_optimizer.optimizer import Optimizer
    src = tmp_path / "plain.png"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(src, "PNG")
    data = Optimizer._strip_png_chunks(src.read_bytes(), b"sRGB")
    import zlib
    body = b"sRGB\x00"
    srgb = (1).to_bytes(4, "big") + body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")
    data = data[:33] + srgb + data[33:]
    src.write_bytes(data)
    assert "sRGB" in _chunk_types(src)
    Optimizer._finalize_png_icc(src, ICC)
    kinds = _chunk_types(src)
    assert kinds[:2] == ["IHDR", "iCCP"] and "sRGB" not in kinds
    assert _read_icc(src) == ICC
    Image.open(src).load()  # still a valid PNG
