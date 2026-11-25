"""Thumbnails apply EXIF orientation (review finding F10)."""
from __future__ import annotations

from PIL import Image


def test_thumbnail_honours_exif_orientation(app_module, tmp_path):
    """400x200 JPEG with orientation 6 (rotate 90 CW) must give a portrait
    thumbnail, like the optimized output, not a sideways one (F10)."""
    src = tmp_path / "phone.jpg"
    im = Image.new("RGB", (400, 200), (200, 30, 30))
    exif = Image.Exif()
    exif[0x0112] = 6
    im.save(src, exif=exif)
    dst = tmp_path / "thumb" / "t.jpg"
    app_module._gen_thumbnail(src, dst)
    with Image.open(dst) as t:
        assert t.size[0] < t.size[1], t.size
        assert t.size == (100, 200)


def test_thumbnail_without_orientation_is_unchanged(app_module, tmp_path):
    src = tmp_path / "plain.jpg"
    Image.new("RGB", (400, 200), (1, 2, 3)).save(src)
    dst = tmp_path / "t.jpg"
    app_module._gen_thumbnail(src, dst)
    with Image.open(dst) as t:
        assert t.size == (200, 100)
