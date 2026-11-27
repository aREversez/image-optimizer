"""Temp names derived from a long destination name stay creatable."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from image_optimizer import persistence


def _long_paths_writable(base: Path) -> bool:
    """Probe whether files past Windows MAX_PATH can exist under ``base``."""
    probe = base / ("long-path-probe-" + "x" * 240 + ".tmp")
    try:
        probe.write_bytes(b"p")
    except OSError:
        return False
    probe.unlink()
    return True


# The names below are legal 244-byte components, but under pytest's temp tree
# the full path passes MAX_PATH, which Windows only holds with long-path
# support enabled (the OS default is off; the GitHub runners turn it on).
requires_long_paths = pytest.mark.skipif(
    os.name == "nt" and not _long_paths_writable(Path(tempfile.gettempdir())),
    reason="this Windows install cannot hold paths past MAX_PATH",
)


@requires_long_paths
def test_staging_name_fits_the_filesystem_limit_for_long_names(tmp_path):
    dest = tmp_path / ("x" * 240 + ".png")  # a legal 244-byte name
    dest.write_bytes(b"d")
    staged = persistence.staging_path(dest)
    assert len(staged.name.encode()) <= 255
    assert staged.suffix == ".png" and staged != persistence.staging_path(dest)
    persistence.atomic_copy(dest, tmp_path / ("y" * 240 + ".png"))  # really writable


@requires_long_paths
def test_atomic_write_text_tmp_name_fits_for_long_names(tmp_path):
    persistence.atomic_write_text(tmp_path / ("z" * 240 + ".json"), "{}")
