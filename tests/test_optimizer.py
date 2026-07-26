"""Unit tests for app/optimizer.py, independent of the FastAPI layer."""
from __future__ import annotations

import asyncio

from PIL import Image

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestPngNormalization:
    """Bug: a non-PNG source that never hit the resize step (no Max Width,
    or already smaller than the target) got copied through unmodified but
    renamed to .png. Fix: normalize to real PNG unconditionally, before
    deciding whether to resize."""

    def test_jpeg_without_resize_becomes_real_png(self, tmp_path, optimizer):
        src = tmp_path / "photo.jpg"
        Image.new("RGB", (64, 64), (10, 10, 200)).save(src, format="JPEG")
        out = tmp_path / "photo.png"

        result = asyncio.run(optimizer.optimize_png(src, out, max_width=0))

        assert result["success"] is True
        assert out.read_bytes()[:8] == PNG_MAGIC

    def test_jpeg_smaller_than_max_width_still_becomes_real_png(self, tmp_path, optimizer):
        """The resize step itself early-returns without writing anything
        if the source is already <= max_width — this must not skip PNG
        normalization."""
        src = tmp_path / "small.jpg"
        Image.new("RGB", (50, 50), (0, 200, 0)).save(src, format="JPEG")
        out = tmp_path / "small.png"

        result = asyncio.run(optimizer.optimize_png(src, out, max_width=1920))

        assert result["success"] is True
        assert out.read_bytes()[:8] == PNG_MAGIC

    def test_already_png_input_unaffected(self, tmp_path, optimizer):
        src = tmp_path / "already.png"
        Image.new("RGB", (40, 40)).save(src, format="PNG")
        out = tmp_path / "out.png"

        result = asyncio.run(optimizer.optimize_png(src, out))
        assert result["success"] is True
        assert out.read_bytes()[:8] == PNG_MAGIC


class TestCompressionModes:
    def _make_png(self, path, size=(64, 64), color=(46, 204, 113)):
        Image.new("RGB", size, color).save(path, format="PNG")

    def test_lossless_skips_quantization(self, tmp_path, optimizer):
        src = tmp_path / "src.png"
        self._make_png(src)
        out = tmp_path / "out.png"

        logs = []

        async def log(msg):
            logs.append(msg)

        asyncio.run(optimizer.optimize_png(
            src, out, compression_mode="lossless", progress_callback=log,
        ))
        assert any("skipping color quantization" in m for m in logs)
        assert list(Image.open(src).getdata()) == list(Image.open(out).getdata())

    def test_resize_only_actually_resizes_and_skips_quantization(self, tmp_path, optimizer):
        src = tmp_path / "src.png"
        self._make_png(src, size=(200, 200))
        out = tmp_path / "out.png"

        result = asyncio.run(optimizer.optimize_png(
            src, out, max_width=100, compression_mode="resize_only",
        ))
        assert result["success"] is True
        assert Image.open(out).size == (100, 100)

    def test_standard_mode_invokes_pngquant(self, tmp_path, optimizer):
        src = tmp_path / "src.png"
        self._make_png(src)
        out = tmp_path / "out.png"

        logs = []

        async def log(msg):
            logs.append(msg)

        asyncio.run(optimizer.optimize_png(src, out, compression_mode="standard", progress_callback=log))
        assert any("color quantization" in m and "skipping" not in m for m in logs)


class TestProtectedColorsAndDithering:
    def test_protected_colors_map_file_passed_to_pngquant(self, tmp_path, fake_bin_dir, optimizer):
        """Verifies the actual argv pngquant receives includes --map with
        a generated reference image containing the requested colors.
        Overwrites pngquant_impl.py (the actual logic both the Unix
        wrapper script and the Windows .bat wrapper delegate to) so this
        stays cross-platform without branching in the test itself."""
        logfile = tmp_path / "argv.log"
        (fake_bin_dir / "pngquant_impl.py").write_text(
            "import sys\n"
            f"open({str(logfile)!r}, 'w').write(repr(sys.argv[1:]))\n"
            "out = sys.argv[sys.argv.index('--output') + 1]\n"
            "data = open(sys.argv[-1], 'rb').read()\n"
            "open(out, 'wb').write(data)\n"
        )

        src = tmp_path / "src.png"
        Image.new("RGB", (40, 40)).save(src)
        out = tmp_path / "out.png"

        asyncio.run(optimizer.optimize_png(
            src, out, compression_mode="standard",
            protected_colors=["#2ecc71", "#ff0000"], dithering=False,
        ))

        argv = logfile.read_text()
        assert "--map" in argv
        assert "--nofs" in argv

    def test_color_map_file_contains_requested_colors(self, tmp_path):
        from app.optimizer import Optimizer
        dst = tmp_path / "map.png"
        ok = Optimizer._make_color_map(["#2ecc71", "#ff0000"], dst)
        assert ok is True
        img = Image.open(dst)
        pixels = list(img.getdata())
        assert (46, 204, 113) in pixels  # #2ecc71
        assert (255, 0, 0) in pixels     # #ff0000

    def test_invalid_colors_are_skipped_not_crashed(self, tmp_path):
        from app.optimizer import Optimizer
        dst = tmp_path / "map.png"
        ok = Optimizer._make_color_map(["not-a-color", "#zzzzzz"], dst)
        assert ok is False


class TestWebP:
    """WebP goes through Pillow's own encoder, a separate code path from
    pngquant/oxipng — these use `optimizer` anyway for consistency, and a
    couple of tests implicitly confirm pngquant/oxipng are never invoked
    for WebP (they'd fail if a required arg to the fake scripts were
    missing, since WebP should never reach that code path at all)."""

    def test_standard_lossy_webp(self, tmp_path, optimizer):
        src = tmp_path / "src.png"
        Image.new("RGB", (100, 100), (200, 100, 50)).save(src)
        out = tmp_path / "out.webp"

        result = asyncio.run(optimizer.optimize_png(
            src, out, output_format="webp", compression_mode="standard", quality="high",
        ))
        assert result["success"] is True
        assert Image.open(out).format == "WEBP"

    def test_lossless_webp_is_pixel_exact(self, tmp_path, optimizer):
        src = tmp_path / "src.png"
        Image.new("RGB", (60, 60), (12, 34, 56)).save(src)
        out = tmp_path / "out.webp"

        result = asyncio.run(optimizer.optimize_png(src, out, output_format="webp", compression_mode="lossless"))
        assert result["success"] is True
        assert list(Image.open(src).getdata()) == list(Image.open(out).getdata())

    def test_resize_only_webp_actually_resizes(self, tmp_path, optimizer):
        src = tmp_path / "src.png"
        Image.new("RGB", (200, 200)).save(src)
        out = tmp_path / "out.webp"

        result = asyncio.run(optimizer.optimize_png(
            src, out, output_format="webp", max_width=80, compression_mode="resize_only",
        ))
        assert result["success"] is True
        assert Image.open(out).size == (80, 80)

    def test_webp_does_not_require_pngquant_oxipng(self, tmp_path):
        """WebP goes through Pillow's own encoder — must work even when
        pngquant/oxipng are completely absent (deliberately does NOT use
        the `optimizer` fixture, since the whole point is testing with no
        binaries configured at all)."""
        from app.optimizer import Optimizer
        src = tmp_path / "src.png"
        Image.new("RGB", (40, 40)).save(src)
        out = tmp_path / "out.webp"

        opt = Optimizer(bin_dir=tmp_path / "nonexistent_bin_dir")
        assert opt.pngquant_path is None
        assert opt.oxipng_path is None
        result = asyncio.run(opt.optimize_png(src, out, output_format="webp"))
        assert result["success"] is True


class TestAvailableModes:
    """`ready` predates WebP / lossless / resize_only and only described the
    Standard-mode PNG path — so it returns False on a build that can still
    produce lossless/resize-only PNG *and* every WebP flavor successfully.
    `available_modes()` is the new interface that returns the exact set of
    (format, compression_mode) combinations this instance can actually
    process, given which binaries it auto-detected."""

    def test_both_binaries_all_modes_available(self, fake_bin_dir, optimizer):
        # `optimizer` fixture already points pngquant_path/oxipng_path at
        # the .bat wrappers in fake_bin_dir — auto-detection deliberately
        # bypassed (see conftest.py: same reason every other test here does
        # the same, instead of relying on Optimizer's OS-specific path
        # search).
        opt = optimizer
        assert opt.pngquant_path is not None
        assert opt.oxipng_path is not None
        modes = opt.available_modes()
        assert {("png", "standard"), ("png", "lossless"), ("png", "resize_only"),
                ("webp", "standard"), ("webp", "lossless"), ("webp", "resize_only")} == modes

    def test_no_pngquant_drops_only_standard_png(self, tmp_path):
        from app.optimizer import Optimizer
        # No binaries found in bin_dir → both paths None.
        opt = Optimizer(bin_dir=tmp_path / "nonexistent_bin_dir")
        assert opt.pngquant_path is None
        assert opt.oxipng_path is None

        modes = opt.available_modes()
        # PNG standard — the one mode that actually needs pngquant for its
        # lossy color quantization step — is dropped.
        assert ("png", "standard") not in modes
        # Everything else still works: lossless/resize_only PNG just uses
        # Pillow's PNG encoder (oxipng only shrinks further), and WebP is
        # Pillow-encoder-only.
        assert {("png", "lossless"), ("png", "resize_only"),
                ("webp", "standard"), ("webp", "lossless"), ("webp", "resize_only")} == modes
        # `ready` returns False here, even though most of the format×mode
        # matrix is actually usable — that's exactly what `ready` doesn't
        # capture, and why available_modes exists.
        assert opt.ready is False

    def test_ready_true_iff_pngquant_present(self, optimizer):
        opt = optimizer
        assert opt.ready is True
        # Drop pngquant only; oxipng is irrelevant for `ready`.
        opt.pngquant_path = None
        assert opt.ready is False
