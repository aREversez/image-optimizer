"""The source's ICC colour profile survives compression (review F11).

Every pipeline decodes through Pillow and writes the decoded numbers back
out, so the pixels stay in the source's colour space. Dropping the profile
does not convert them to sRGB — it makes a wide-gamut (Adobe RGB, Display
P3) image look desaturated in any colour-managed viewer. The profile is
therefore carried onto every output format, and only --strip-icc drops it.
"""
from __future__ import annotations

import asyncio
import zlib

from PIL import Image, ImageCms

from image_optimizer.optimizer import Optimizer


def _srgb_profile() -> bytes:
    """A real ICC profile when littlecms is available, otherwise a blob that
    is still a valid enough payload for every container we write (the ICC
    bytes are opaque to Pillow and to the segment/chunk framing)."""
    try:
        return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    except Exception:
        return b"\x00\x00\x02\x0c" + b"fake-icc-profile" + b"acsp" + b"\x00" * 256


ICC = _srgb_profile()


def _source(path, fmt: str, icc: bytes = ICC):
    kwargs = {"icc_profile": icc} if icc else {}
    Image.new("RGB", (96, 64), (180, 40, 40)).save(
        path, {"png": "PNG", "jpg": "JPEG", "webp": "WEBP", "tiff": "TIFF"}[fmt], **kwargs,
    )
    return path


def _read_icc(path) -> bytes:
    with Image.open(path) as img:
        return img.info.get("icc_profile") or b""


def _run(optimizer, src, dst, fmt, **kwargs):
    res = asyncio.run(optimizer.optimize_png(
        src, dst, quality="medium", output_format=fmt,
        compression_mode="lossless", **kwargs,
    ))
    assert res["success"], res
    return res


def _png_chunks(path):
    data = path.read_bytes()
    pos, out = 8, []
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        out.append((data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]))
        pos += 12 + length
    return out


def test_jpeg_output_keeps_the_source_profile(optimizer, tmp_path):
    src = _source(tmp_path / "photo.jpg", "jpg")
    out = tmp_path / "out.jpg"
    _run(optimizer, src, out, "jpg")
    assert _read_icc(out) == ICC


def test_webp_output_keeps_the_source_profile(optimizer, tmp_path):
    src = _source(tmp_path / "photo.jpg", "jpg")
    out = tmp_path / "out.webp"
    _run(optimizer, src, out, "webp")
    assert _read_icc(out) == ICC


def test_png_output_keeps_the_source_profile(optimizer, tmp_path):
    src = _source(tmp_path / "photo.png", "png")
    out = tmp_path / "out.png"
    _run(optimizer, src, out, "png")
    assert _read_icc(out) == ICC


def test_png_from_a_jpeg_source_keeps_the_profile(optimizer, tmp_path):
    """The PNG path normalises a non-PNG source first (_ensure_png); that
    intermediate is where the profile has to be carried."""
    src = _source(tmp_path / "photo.jpg", "jpg")
    out = tmp_path / "out.png"
    _run(optimizer, src, out, "png")
    assert _read_icc(out) == ICC


def test_avif_output_keeps_the_source_profile(optimizer, tmp_path):
    src = _source(tmp_path / "photo.jpg", "jpg")
    out = tmp_path / "out.avif"
    _run(optimizer, src, out, "avif")
    assert _read_icc(out) == ICC


def test_strip_icc_drops_the_profile(optimizer, tmp_path):
    src = _source(tmp_path / "photo.jpg", "jpg")
    out = tmp_path / "out.webp"
    _run(optimizer, src, out, "webp", keep_icc=False)
    assert _read_icc(out) == b""


def test_source_without_a_profile_stays_without_one(optimizer, tmp_path):
    src = _source(tmp_path / "plain.jpg", "jpg", icc=b"")
    out = tmp_path / "out.jpg"
    _run(optimizer, src, out, "jpg")
    assert _read_icc(out) == b""


def test_png_icc_chunk_is_valid_and_not_duplicated(optimizer, tmp_path):
    """_finalize_png_icc has to write a decodable iCCP chunk and leave an
    existing one alone: pngquant/oxipng may or may not keep iCCP, and the
    finalizer is the last word."""
    src = _source(tmp_path / "photo.jpg", "jpg")
    out = tmp_path / "out.png"
    _run(optimizer, src, out, "png")
    iccp = [body for ctype, body in _png_chunks(out) if ctype == b"iCCP"]
    assert len(iccp) == 1, "exactly one iCCP chunk"
    name, rest = iccp[0].split(b"\x00", 1)
    assert name.lower() == b"icc profile"
    assert rest[0] == 0, "compression method must be 0 (zlib/deflate)"
    assert zlib.decompress(rest[1:]) == ICC


def test_multi_segment_jpeg_profile_round_trips(tmp_path):
    """An ICC profile larger than one APP2 segment's payload has to be split
    into numbered chunks. Adobe RGB is ~560 KB, so this is the normal case
    for exactly the wide-gamut images the fix is for."""
    big = ICC + b"\x00" * 150_000
    jpg = tmp_path / "big.jpg"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(jpg, "JPEG")
    Optimizer._embed_jpeg_icc(jpg, big)
    assert _read_icc(jpg) == big

    data = jpg.read_bytes()
    segments = []
    pos = 2
    while data[pos:pos + 2] == b"\xff\xe2":
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        segments.append(data[pos + 4:pos + 2 + length])
        pos += 2 + length
    assert len(segments) > 1
    assert [s[12] for s in segments] == list(range(1, len(segments) + 1))
    assert all(s[13] == len(segments) for s in segments)


def test_embedded_profile_does_not_disturb_pixels(optimizer, tmp_path):
    src = _source(tmp_path / "photo.jpg", "jpg")
    plain = tmp_path / "plain.jpg"
    _source(plain, "jpg", icc=b"")
    with_profile = tmp_path / "a.webp"
    without = tmp_path / "b.webp"
    _run(optimizer, src, with_profile, "webp")
    _run(optimizer, plain, without, "webp")
    with Image.open(with_profile) as a, Image.open(without) as b:
        assert a.tobytes() == b.tobytes()


def _profile_with_space(space: bytes) -> bytes:
    """A header-only ICC blob whose data colour space (bytes 16:20) is `space`."""
    hdr = bytearray(128)
    hdr[0:4] = (len(hdr) + 4).to_bytes(4, "big")
    hdr[16:20] = space
    hdr[36:40] = b"acsp"
    return bytes(hdr) + b"\x00\x00\x00\x00"


def test_cmyk_profile_is_not_attached_to_the_rgb_output(optimizer, tmp_path):
    """A CMYK source is converted to RGB before encoding, so its CMYK profile
    no longer describes the pixels. Attaching it anyway gets the output
    rejected or rendered with the wrong colours by colour-managed readers."""
    src = tmp_path / "print.jpg"
    Image.new("CMYK", (64, 64), (10, 200, 10, 0)).save(
        src, "JPEG", icc_profile=_profile_with_space(b"CMYK"),
    )
    for fmt in ("webp", "png", "jpg"):
        out = tmp_path / f"out.{fmt}"
        _run(optimizer, src, out, fmt)
        assert _read_icc(out) == b"", fmt


def test_gray_profile_is_not_attached_to_rgb_output(optimizer, tmp_path):
    src = tmp_path / "gray.png"
    Image.new("L", (64, 64), 120).save(src, "PNG", icc_profile=_profile_with_space(b"GRAY"))
    out = tmp_path / "out.webp"  # WebP is always written as RGB(A)
    _run(optimizer, src, out, "webp")
    assert _read_icc(out) == b""


def test_rgb_profile_is_still_kept(optimizer, tmp_path):
    prof = _profile_with_space(b"RGB ")
    src = tmp_path / "ok.png"
    Image.new("RGB", (64, 64), (1, 2, 3)).save(src, "PNG", icc_profile=prof)
    out = tmp_path / "out.webp"
    _run(optimizer, src, out, "webp")
    assert _read_icc(out) == prof


def test_strip_icc_drops_the_profile_in_every_format(optimizer, tmp_path):
    """Pillow's PNG writer re-attaches img.info["icc_profile"] on its own, so
    the intermediate PNGs (PNG output, AVIF input) kept the profile even with
    --strip-icc unless it is removed from info explicitly."""
    for src_fmt, out_fmt in (("jpg", "png"), ("png", "png"), ("jpg", "avif"), ("jpg", "webp"), ("jpg", "jpg")):
        src = _source(tmp_path / f"s_{src_fmt}_{out_fmt}.{src_fmt}", src_fmt)
        out = tmp_path / f"o_{src_fmt}.{out_fmt}"
        _run(optimizer, src, out, out_fmt, keep_icc=False)
        assert _read_icc(out) == b"", (src_fmt, out_fmt)
