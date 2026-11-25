"""Reusable-output validation catches truncated and corrupt files (review F8).

Image.verify() does nothing for JPEG, so a half-written JPEG counted as
reusable, and JPEG/WebP files whose body is garbage passed too."""
from __future__ import annotations

import os
import random
from pathlib import Path

import pytest
from PIL import Image

from image_optimizer.cache import is_valid_reusable_output


def _photoish(path: Path, fmt: str):
    rnd = random.Random(1)
    im = Image.new("RGB", (160, 160))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(160 * 160)])
    im.save(path, format=fmt, **({"quality": 90} if fmt in ("JPEG", "WEBP") else {}))


@pytest.mark.parametrize("fmt,ext,key", [("JPEG", "jpg", "jpg"), ("WEBP", "webp", "webp"), ("PNG", "png", "png")])
class TestValidity:
    def test_intact_file_is_reusable(self, tmp_path, fmt, ext, key):
        p = tmp_path / f"a.{ext}"
        _photoish(p, fmt)
        assert is_valid_reusable_output(p, key) is True

    def test_truncated_file_is_not_reusable(self, tmp_path, fmt, ext, key):
        p = tmp_path / f"a.{ext}"
        _photoish(p, fmt)
        data = p.read_bytes()
        p.write_bytes(data[: len(data) // 2])
        assert is_valid_reusable_output(p, key) is False

    def test_zero_byte_file_is_not_reusable(self, tmp_path, fmt, ext, key):
        p = tmp_path / f"a.{ext}"
        p.write_bytes(b"")
        assert is_valid_reusable_output(p, key) is False


def test_jpeg_with_valid_header_and_garbage_body_is_not_reusable(tmp_path):
    """Detectable for JPEG through a full decode. A complete lossy WebP whose
    middle is corrupted still decodes (VP8 has no checksum), so that case is
    deliberately not claimed; truncation, the realistic crash symptom, is."""
    fmt, ext, key = "JPEG", "jpg", "jpg"
    p = tmp_path / f"a.{ext}"
    _photoish(p, fmt)
    data = bytearray(p.read_bytes())
    cut = len(data) // 2
    # Keep the size/length fields and the end of the file intact, trash the middle.
    rnd = random.Random(2)
    data[cut: cut + len(data) // 3] = bytes(rnd.randrange(256) for _ in range(len(data) // 3))
    p.write_bytes(bytes(data))
    assert is_valid_reusable_output(p, key) is False


def test_jpeg_without_end_marker_is_not_reusable(tmp_path):
    p = tmp_path / "a.jpg"
    _photoish(p, "JPEG")
    data = p.read_bytes()
    assert data.endswith(b"\xff\xd9")
    p.write_bytes(data[:-2])
    assert is_valid_reusable_output(p, "jpg") is False


def test_webp_riff_size_larger_than_file_is_not_reusable(tmp_path):
    p = tmp_path / "a.webp"
    _photoish(p, "WEBP")
    data = p.read_bytes()
    p.write_bytes(data[:-10])
    assert is_valid_reusable_output(p, "webp") is False


def test_format_mismatch_is_still_rejected(tmp_path):
    p = tmp_path / "a.webp"
    _photoish(p, "PNG")
    assert is_valid_reusable_output(p, "webp") is False
