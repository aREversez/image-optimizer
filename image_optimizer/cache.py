"""Shared helpers for reusing previously produced output files.

`settings_fingerprint` and `is_valid_reusable_output` started life in main.py
for the GUI's skip_existing feature; they live here so the CLI content-hash
cache can use the exact same definitions (one source of truth for "which
settings change the pixels" and "is this file safe to hand back").
main.py re-imports them under their old private names.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from PIL import Image


def settings_fingerprint(
    quality: str, compression_mode: str, max_width: int,
    protected_colors: Optional[list], dithering: bool, keep_exif: bool, output_format: str,
) -> str:
    """Fingerprints the effective per-file compression settings that
    produced (or would produce) a given output. Used by skip_existing to
    tell "the source hasn't changed AND these are the same settings that
    made this file last time" apart from "the source happens to be
    unchanged but the user asked for something different this run" — the
    two look identical if you only check the source's mtime, which is all
    the reuse check used to do.

    Order-independent on protected_colors (case/whitespace-normalized) so
    ["#FF0000", "#00ff00"] and ["#00FF00", "#ff0000"] fingerprint the same,
    matching how the color-map step itself treats them."""
    colors = sorted((c or "").strip().lower() for c in (protected_colors or []))
    payload = json.dumps({
        "quality": quality,
        "compression_mode": compression_mode,
        "max_width": max_width,
        "protected_colors": colors,
        "dithering": bool(dithering),
        "keep_exif": bool(keep_exif),
        "output_format": output_format,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def is_valid_reusable_output(path: Path, expected_format: str) -> bool:
    """Best-effort corruption check for a skip_existing reuse candidate.

    A non-empty file isn't strong enough evidence that it's safe to reuse:
    if pngquant/oxipng was killed mid-write on a previous run (crash, OOM
    kill, power loss), it can leave behind a file that's non-empty but
    truncated or structurally broken. Reusing that file would silently
    hand the user a corrupt "optimized" image instead of recompressing it.

    Uses Pillow's verify() (structural/header check, doesn't decode full
    pixel data — cheap) and confirms the format actually matches what
    this run expects, so e.g. a stray same-named file from a different
    output_format never gets reused by mistake."""
    try:
        with Image.open(path) as img:
            img.verify()
            fmt = (img.format or "").upper()
    except Exception:
        return False
    expected = {
        "png": "PNG",
        "webp": "WEBP",
        "jpg": "JPEG",
        "jpeg": "JPEG",
        "avif": "AVIF",
    }.get(expected_format, "PNG")
    return fmt == expected
