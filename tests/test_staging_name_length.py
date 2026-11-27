"""Temp names derived from a long destination name stay creatable."""
from __future__ import annotations

from image_optimizer import persistence


def test_staging_name_fits_the_filesystem_limit_for_long_names(tmp_path):
    dest = tmp_path / ("x" * 240 + ".png")  # a legal 244-byte name
    dest.write_bytes(b"d")
    staged = persistence.staging_path(dest)
    assert len(staged.name.encode()) <= 255
    assert staged.suffix == ".png" and staged != persistence.staging_path(dest)
    persistence.atomic_copy(dest, tmp_path / ("y" * 240 + ".png"))  # really writable


def test_atomic_write_text_tmp_name_fits_for_long_names(tmp_path):
    persistence.atomic_write_text(tmp_path / ("z" * 240 + ".json"), "{}")
