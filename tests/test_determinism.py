"""Byte-level determinism of the compression pipeline.

The CLI content-hash cache (image_optimizer/cache.py) is only correct if the
same input + the same settings always produce the same output bytes: a cache
hit hands back bytes produced by an earlier run, so any non-determinism would
make "hit" and "miss" observably different. These tests are the precondition
for that cache.

Two layers:

- Pipeline layer: the fake encoders from conftest, always runs (CI has no
  real binaries). Catches non-determinism introduced by the pipeline itself
  (temp-file naming leaking into output, timestamps, dict ordering, ...).
- Encoder layer (`real_encoders` marker): the real pngquant/oxipng/cjpeg/
  avifenc, skipped per tool when a binary is not installed. If a format is
  found non-deterministic here, drop it from cache.CACHEABLE_FORMATS.
"""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from PIL import Image

from image_optimizer import binaries
from image_optimizer.optimizer import Optimizer

# Captured at import time on purpose: conftest's autouse fixture patches
# Path.home() for every test, which would otherwise hide a real per-user
# binary cache from binary_dir() on Linux/macOS.
_REAL_BIN_DIR = binaries.binary_dir()

FORMATS = ["png", "webp", "jpg", "avif"]
MODES = ["standard", "lossless", "resize_only", "screenshot"]


def _applicable(fmt: str, mode: str) -> bool:
    # screenshot is PNG-only (see _validate_optimize_settings)
    return not (mode == "screenshot" and fmt != "png")


COMBOS = [(f, m) for f in FORMATS for m in MODES if _applicable(f, m)]


def _noisy_image(path: Path, size=(96, 72), seed=7) -> None:
    """Seeded noise: incompressible enough that every encoder does real work,
    and identical across runs."""
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                 for _ in range(size[0] * size[1])])
    img.save(path, format="PNG")


def _run(opt: Optimizer, src: Path, dst: Path, fmt: str, mode: str) -> dict:
    max_width = 48 if mode == "resize_only" else 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    return asyncio.run(opt.optimize_png(
        src, dst, quality="medium", max_width=max_width, compression_mode=mode,
        output_format=fmt,
    ))


def _assert_deterministic(opt: Optimizer, src: Path, tmp_path: Path, fmt: str, mode: str):
    out_a = tmp_path / "a" / f"out.{fmt}"
    out_b = tmp_path / "b" / f"out.{fmt}"
    res_a = _run(opt, src, out_a, fmt, mode)
    res_b = _run(opt, src, out_b, fmt, mode)
    assert res_a["success"], res_a.get("error")
    assert res_b["success"], res_b.get("error")
    assert out_a.read_bytes() == out_b.read_bytes(), (
        f"{fmt}/{mode}: two runs on identical input produced different bytes"
    )
    assert res_a.get("kept_original") == res_b.get("kept_original")


@pytest.fixture
def noisy_src(tmp_path: Path) -> Path:
    p = tmp_path / "noisy.png"
    _noisy_image(p)
    return p


class TestPipelineDeterminism:
    """Fake encoders: same input + same settings -> byte-identical output."""

    @pytest.mark.parametrize("fmt,mode", COMBOS)
    def test_two_runs_match(self, optimizer, noisy_src, tmp_path, fmt, mode):
        if (fmt, mode) not in optimizer.available_modes():
            pytest.skip(f"{fmt}/{mode} not available with the fake binaries")
        _assert_deterministic(optimizer, noisy_src, tmp_path, fmt, mode)

    def test_different_output_dir_does_not_change_bytes(self, optimizer, noisy_src, tmp_path):
        """The cache serves bytes to callers that use a fresh output dir every
        run, so the output location must not leak into the bytes."""
        a = tmp_path / "x" / "deep" / "one.png"
        b = tmp_path / "y" / "two.png"
        _run(optimizer, noisy_src, a, "png", "standard")
        _run(optimizer, noisy_src, b, "png", "standard")
        assert a.read_bytes() == b.read_bytes()

    @pytest.mark.parametrize("fmt", ["png", "jpg", "webp"])
    def test_kept_original_path_is_deterministic(self, optimizer, tmp_path, fmt):
        """A tiny already-optimal source makes the encoders grow it, which
        triggers the kept_original fallback (stripped copy of the source).
        That path is cached too, so it must be deterministic as well."""
        src = tmp_path / f"tiny.{fmt}"
        Image.new("RGB", (4, 4), (10, 20, 30)).save(
            src, format={"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[fmt],
        )
        if (fmt, "lossless") not in optimizer.available_modes():
            pytest.skip(f"{fmt} not available with the fake binaries")
        res = _run(optimizer, src, tmp_path / "probe" / f"o.{fmt}", fmt, "lossless")
        if not res.get("kept_original"):
            pytest.skip(f"{fmt}: encoder did not grow this fixture, fallback not reached")
        _assert_deterministic(optimizer, src, tmp_path, fmt, "lossless")


def _real_optimizer() -> Optimizer:
    return Optimizer(bin_dir=_REAL_BIN_DIR)


def _needs(opt: Optimizer, fmt: str, mode: str):
    if (fmt, mode) not in opt.available_modes():
        pytest.skip(f"real encoder for {fmt}/{mode} not installed")
    if fmt == "png" and mode in ("standard", "screenshot") and opt.pngquant_path is None:
        pytest.skip("pngquant not installed")
    if fmt == "png" and opt.oxipng_path is None:
        pytest.skip("oxipng not installed")


@pytest.mark.real_encoders
class TestRealEncoderDeterminism:
    """Same check against the genuine binaries. Skipped per tool when absent,
    so CI without them is not blocked. A failure here means that format must
    be removed from cache.CACHEABLE_FORMATS (and noted in the README)."""

    @pytest.mark.parametrize("fmt,mode", COMBOS)
    def test_two_runs_match(self, noisy_src, tmp_path, fmt, mode):
        opt = _real_optimizer()
        _needs(opt, fmt, mode)
        _assert_deterministic(opt, noisy_src, tmp_path, fmt, mode)
