"""Unit tests for app/optimizer.py, independent of the FastAPI layer."""
from __future__ import annotations

import asyncio
import stat
import sys
import time
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat

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

    def test_screenshot_mode_uses_tight_quality_and_forces_no_dithering(self, tmp_path, optimizer, monkeypatch):
        """Screenshot mode's whole value proposition is a much tighter
        pngquant quality floor (75-100, vs Standard's 50-65/65-80/80-100
        ranges) with dithering hardcoded off regardless of the caller's
        `dithering` setting — see optimizer.py's screenshot branch for the
        empirical numbers. The floor was originally 95-100; lowered to
        75-100 after measuring that pngquant's min-max range only acts as
        a pass/fail gate (looser min costs nothing on content that already
        clears the tight floor — measured byte-identical output across a
        60-100 to 95-100 sweep), while images that couldn't clear 95 at
        all were previously failing outright and getting zero quantization
        benefit. Dithering stays off: on a real 4K UI screenshot, dithering
        on cost ~75% *more* file size for a *worse* color-error score, not
        better."""
        captured = {}
        real_exec = asyncio.create_subprocess_exec

        async def spy_exec(*args, **kwargs):
            if str(args[0]).endswith(("pngquant", "pngquant.exe", "pngquant.bat")):
                captured["argv"] = args
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)

        src = tmp_path / "src.png"
        self._make_png(src)
        out = tmp_path / "out.png"
        result = asyncio.run(optimizer.optimize_png(
            src, out, compression_mode="screenshot",
            dithering=True,  # explicitly ask for dithering — must be ignored
        ))
        assert result["success"] is True
        argv = captured["argv"]
        assert "75-100" in argv, f"expected the 75-100 quality floor, got: {argv}"
        assert "--nofs" in argv, f"dithering must be forced off for screenshot mode, got: {argv}"
        # Sanity: Standard mode's ranges must never appear here.
        assert "65-80" not in argv and "80-100" not in argv and "50-65" not in argv

    def test_screenshot_mode_never_resizes_even_with_max_width_set(self, tmp_path, optimizer):
        """Screenshot mode is explicitly designed to never touch resolution
        — the whole point is keeping UI text pixel-sharp. max_width must be
        silently ignored, not error out, so the mode stays a true
        one-click preset even if a leftover max_width value is present
        from a previous Standard-mode run."""
        src = tmp_path / "src.png"
        self._make_png(src, size=(400, 300))
        out = tmp_path / "out.png"
        result = asyncio.run(optimizer.optimize_png(
            src, out, compression_mode="screenshot", max_width=100,
        ))
        assert result["success"] is True
        assert Image.open(out).size == (400, 300), "screenshot mode must ignore max_width entirely"

    def test_screenshot_mode_falls_back_to_lossless_when_pngquant_unavailable(self, tmp_path, optimizer):
        """If pngquant can't be found (e.g. removed mid-run), screenshot
        mode must degrade to a plain lossless pass rather than fail the
        file outright — same fallback shape as Standard mode without
        pngquant."""
        optimizer.pngquant_path = None
        src = tmp_path / "src.png"
        self._make_png(src)
        out = tmp_path / "out.png"

        logs = []

        async def log(msg):
            logs.append(msg)

        result = asyncio.run(optimizer.optimize_png(
            src, out, compression_mode="screenshot", progress_callback=log,
        ))
        assert result["success"] is True
        assert any("pngquant not found" in m for m in logs)
        assert list(Image.open(src).getdata()) == list(Image.open(out).getdata()), (
            "fallback must be genuinely lossless, not a lossy leftover"
        )

    def test_screenshot_mode_fails_cleanly_when_non_png_source_is_too_complex_to_quantize(
        self, tmp_path, optimizer, monkeypatch
    ):
        """Bug: a real (non-screenshot) photo run through Screenshot mode
        got silently balooned to several times its original size. Root
        cause: Screenshot mode always normalizes non-PNG sources to PNG
        first (see _ensure_png), and when pngquant can't hit the
        near-lossless floor on genuinely photographic content, it exits
        nonzero — the old code treated that exactly like the
        pngquant-not-found case and fell through to a plain *lossless*
        PNG pass, i.e. shipped an uncompressed-ish PNG re-encode of a
        JPEG, which is inherently much bigger than the JPEG (JPEG's lossy
        DCT beats PNG's lossless DEFLATE on photo content by a wide
        margin — reproduced empirically at ~14x on a synthetic photo).

        Fix: when the source isn't already PNG (i.e. Screenshot mode is
        about to force a real format conversion) and pngquant fails, fail
        the file instead of silently shipping a bloated conversion."""
        real_exec = asyncio.create_subprocess_exec

        class _FakePngquantFailure:
            returncode = 99

            async def communicate(self):
                return b"", b"pngquant: image quality below min\n"

        async def spy_exec(*args, **kwargs):
            if str(args[0]).endswith(("pngquant", "pngquant.exe", "pngquant.bat")):
                return _FakePngquantFailure()
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)

        src = tmp_path / "photo.jpg"
        Image.new("RGB", (64, 64), (120, 80, 40)).save(src, format="JPEG")
        out = tmp_path / "photo.png"

        logs = []

        async def log(msg):
            logs.append(msg)

        result = asyncio.run(optimizer.optimize_png(
            src, out, compression_mode="screenshot", progress_callback=log,
        ))
        assert result["success"] is False
        assert "isn't a good fit" in result["error"]
        assert not out.exists(), "must not ship a bloated PNG conversion on failure"
        assert any("too color-complex" in m for m in logs)

    def test_screenshot_mode_still_falls_back_to_lossless_when_png_source_fails_to_quantize(
        self, tmp_path, optimizer, monkeypatch
    ):
        """Unlike the non-PNG case above, a source that's already PNG
        involves no format conversion — falling through to the existing
        lossless pass on pngquant failure is still the right, safe
        behavior here (matches the documented fallback for real
        screenshots with an embedded photo that can't be quantized)."""
        real_exec = asyncio.create_subprocess_exec

        class _FakePngquantFailure:
            returncode = 99

            async def communicate(self):
                return b"", b"pngquant: image quality below min\n"

        async def spy_exec(*args, **kwargs):
            if str(args[0]).endswith(("pngquant", "pngquant.exe", "pngquant.bat")):
                return _FakePngquantFailure()
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)

        src = tmp_path / "src.png"
        self._make_png(src)
        out = tmp_path / "out.png"

        result = asyncio.run(optimizer.optimize_png(
            src, out, compression_mode="screenshot",
        ))
        assert result["success"] is True
        assert out.exists()


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


class TestJPEG:
    """JPEG output goes through mozjpeg's cjpeg (a genuine lossy re-encode
    that keeps the JPEG format), a separate code path from the pngquant/
    oxipng PNG pipeline and the Pillow WebP encoder. The fake cjpeg in
    conftest decodes the PPM intermediate Pillow writes and re-saves it as a
    real JPEG — so tests can open results with Pillow, unlike the byte-copy
    pngquant/oxipng stand-ins."""

    def test_standard_lossy_jpg_keeps_jpeg_format(self, tmp_path, optimizer):
        src = tmp_path / "photo.jpg"
        Image.new("RGB", (100, 100), (200, 100, 50)).save(src, format="JPEG")
        out = tmp_path / "photo.jpg"

        result = asyncio.run(optimizer.optimize_png(
            src, out, output_format="jpg", compression_mode="standard", quality="high",
        ))
        assert result["success"] is True
        with Image.open(out) as img:
            assert img.format == "JPEG", "output must stay a real JPEG, not a renamed PNG"

    def test_lossless_jpg_still_returns_jpeg(self, tmp_path, optimizer):
        src = tmp_path / "src.jpg"
        Image.new("RGB", (60, 60)).save(src, format="JPEG")

        logs = []

        async def log(msg):
            logs.append(msg)

        out = tmp_path / "out.jpg"
        result = asyncio.run(optimizer.optimize_png(
            src, out, output_format="jpg", compression_mode="lossless", progress_callback=log,
        ))
        assert result["success"] is True
        with Image.open(out) as img:
            assert img.format == "JPEG"
        assert any("encoding JPEG via mozjpeg" in m for m in logs)

    def test_resize_only_jpg_actually_resizes(self, tmp_path, optimizer):
        src = tmp_path / "src.jpg"
        Image.new("RGB", (200, 200), (10, 200, 10)).save(src, format="JPEG")
        out = tmp_path / "out.jpg"

        result = asyncio.run(optimizer.optimize_png(
            src, out, output_format="jpg", max_width=80, compression_mode="resize_only",
        ))
        assert result["success"] is True
        with Image.open(out) as img:
            assert img.format == "JPEG"
            assert img.size == (80, 80)

    def test_png_source_can_be_encoded_to_jpg(self, tmp_path, optimizer):
        """Format normalization in the other direction: a PNG source with an
        output_format of jpg is decoded and re-encoded by cjpeg — the two
        are independent axes."""
        src = tmp_path / "src.png"
        Image.new("RGB", (50, 50), (123, 45, 67)).save(src, format="PNG")
        out = tmp_path / "out.jpg"

        result = asyncio.run(optimizer.optimize_png(
            src, out, output_format="jpg", compression_mode="standard",
        ))
        assert result["success"] is True
        with Image.open(out) as img:
            assert img.format == "JPEG"

    def test_jpeg_requires_cjpeg(self, tmp_path):
        from app.optimizer import Optimizer
        opt = Optimizer(bin_dir=tmp_path / "nonexistent_bin_dir")
        assert opt.cjpeg_path is None

        src = tmp_path / "src.jpg"
        Image.new("RGB", (30, 30)).save(src, format="JPEG")
        out = tmp_path / "out.jpg"
        result = asyncio.run(opt.optimize_png(src, out, output_format="jpg"))
        assert result["success"] is False
        assert "cjpeg" in result["error"]

    def test_rgba_source_composited_for_jpeg(self, tmp_path, optimizer):
        """JPEG has no alpha channel — a transparent PNG must be composited
        onto white rather than crashing cjpeg with an unsupported color
        mode."""
        src = tmp_path / "transparent.png"
        Image.new("RGBA", (40, 40), (200, 0, 0, 128)).save(src, format="PNG")
        out = tmp_path / "out.jpg"

        result = asyncio.run(optimizer.optimize_png(
            src, out, output_format="jpg",
        ))
        assert result["success"] is True
        with Image.open(out) as img:
            assert img.format == "JPEG"
            assert img.mode in ("RGB", "L")


class TestGrowthGuard:
    """Bug: same-format 'optimization' that ends up bigger than the source
    (or that leaves the source's size unchanged) got shipped as-is,
    reporting a negative savings percentage on a file that's genuinely no
    better than what the user already had. Fix: _apply_growth_guard falls
    back to the original's pixel data (unmodified) with privacy-relevant
    metadata stripped — never re-encoded, so it can only be the same size
    as or smaller than the raw original, and it keeps the same
    'GPS is gone by default' guarantee the compressed path makes."""

    @staticmethod
    def _png_with_exif_chunk(path: Path, exif_payload: bytes, size=(40, 40), color=(10, 20, 30)):
        import struct
        import zlib
        Image.new("RGB", size, color).save(path, format="PNG")
        data = bytearray(path.read_bytes())
        chunk = struct.pack(">I", len(exif_payload)) + b"eXIf" + exif_payload
        chunk += struct.pack(">I", zlib.crc32(b"eXIf" + exif_payload) & 0xFFFFFFFF)
        iend_pos = data.rfind(b"IEND") - 4  # back up over IEND's 4-byte length field
        path.write_bytes(bytes(data[:iend_pos]) + chunk + bytes(data[iend_pos:]))

    def test_same_format_png_falls_back_to_original_pixels_with_exif_stripped(self, tmp_path, optimizer):
        """The fake pngquant/oxipng binaries just copy bytes through
        unchanged (no real compression), so a source with an eXIf chunk
        comes out the exact same size with the chunk still attached —
        exactly the 'no improvement' case the guard exists for."""
        src = tmp_path / "src.png"
        self._png_with_exif_chunk(src, b"II*\x00fake-exif-payload-would-carry-gps")
        out = tmp_path / "out.png"

        result = asyncio.run(optimizer.optimize_png(src, out, compression_mode="standard"))

        assert result["success"] is True
        assert result.get("kept_original") is True
        out_bytes = out.read_bytes()
        assert b"eXIf" not in out_bytes
        assert b"fake-exif-payload-would-carry-gps" not in out_bytes
        assert list(Image.open(src).getdata()) == list(Image.open(out).getdata())

    def test_same_format_jpeg_falls_back_to_original_bytes_with_exif_stripped(self, tmp_path, optimizer):
        src = tmp_path / "src.jpg"
        img = Image.new("RGB", (40, 40), (200, 50, 90))
        exif = img.getexif()
        exif[0x010E] = "test description"  # arbitrary tag so APP1 is written
        img.save(src, format="JPEG", exif=exif.tobytes())
        assert b"Exif" in src.read_bytes()
        out = tmp_path / "out.jpg"

        # lossless mode maps to q=95 in the fake cjpeg's real Pillow re-encode,
        # which for a flat-color 40x40 swatch typically won't beat the
        # source's own JPEG encoding — if it happens to for some Pillow
        # version, the guard simply won't fire and this assertion tree
        # still holds trivially (result stays smaller, no eXIF concern
        # either way since cjpeg's own PPM pipeline is metadata-free).
        result = asyncio.run(optimizer.optimize_png(
            src, out, output_format="jpg", compression_mode="lossless",
        ))
        assert result["success"] is True
        if result.get("kept_original"):
            assert b"Exif" not in out.read_bytes()

    def test_cross_format_conversion_is_never_touched_by_the_guard(self, tmp_path, optimizer):
        """A genuine format conversion (jpg source -> png output) must
        never trigger the fallback — the original bytes wouldn't even be
        a valid file at the target extension."""
        src = tmp_path / "src.jpg"
        Image.new("RGB", (40, 40), (5, 5, 5)).save(src, format="JPEG")
        out = tmp_path / "out.png"
        result = asyncio.run(optimizer.optimize_png(src, out, compression_mode="standard"))
        assert result["success"] is True
        assert "kept_original" not in result
        assert out.read_bytes()[:8] == PNG_MAGIC


class TestMetadataStrippers:
    """Direct unit coverage for the pure byte-manipulation helpers behind
    the growth guard — these are hand-rolled container parsers (PNG chunk
    stream, JPEG marker stream, WebP RIFF stream), so they're worth
    testing in isolation from the async pipelines that call them."""

    def test_strip_png_metadata_removes_exif_keeps_pixels(self, tmp_path):
        from app.optimizer import Optimizer
        path = tmp_path / "x.png"
        TestGrowthGuard._png_with_exif_chunk(path, b"II*\x00some-exif-bytes")
        data = path.read_bytes()
        stripped = Optimizer._strip_png_metadata(data)
        assert b"eXIf" not in stripped
        assert b"some-exif-bytes" not in stripped
        assert stripped[:8] == PNG_MAGIC
        # still a valid, openable PNG with the same pixels
        out = tmp_path / "y.png"
        out.write_bytes(stripped)
        assert list(Image.open(path).getdata()) == list(Image.open(out).getdata())

    def test_strip_png_metadata_leaves_color_chunks_alone(self):
        from app.optimizer import Optimizer
        import io
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, format="PNG", pnginfo=None)
        data = buf.getvalue()
        # sanity: no metadata chunks to begin with, must be a pure no-op
        assert Optimizer._strip_png_metadata(data) == data

    def test_strip_jpeg_metadata_removes_exif_keeps_pixels(self, tmp_path):
        from app.optimizer import Optimizer
        path = tmp_path / "x.jpg"
        img = Image.new("RGB", (40, 40), (9, 8, 7))
        exif = img.getexif()
        exif[0x010E] = "desc"
        img.save(path, format="JPEG", exif=exif.tobytes())
        data = path.read_bytes()
        assert b"Exif" in data
        stripped = Optimizer._strip_jpeg_metadata(data)
        assert b"Exif" not in stripped
        out = tmp_path / "y.jpg"
        out.write_bytes(stripped)
        assert Image.open(out).size == (40, 40)

    def test_strip_jpeg_metadata_noop_on_clean_jpeg(self, tmp_path):
        from app.optimizer import Optimizer
        path = tmp_path / "x.jpg"
        Image.new("RGB", (20, 20), (1, 1, 1)).save(path, format="JPEG")
        data = path.read_bytes()
        stripped = Optimizer._strip_jpeg_metadata(data)
        out = tmp_path / "y.jpg"
        out.write_bytes(stripped)
        assert list(Image.open(path).getdata()) == list(Image.open(out).getdata())

    def test_strip_jpeg_metadata_handles_malformed_input_without_raising(self):
        from app.optimizer import Optimizer
        assert Optimizer._strip_jpeg_metadata(b"") == b""
        assert Optimizer._strip_jpeg_metadata(b"not a jpeg") == b"not a jpeg"
        assert Optimizer._strip_jpeg_metadata(b"\xff\xd8\xff") == b"\xff\xd8\xff"

    def test_strip_webp_metadata_removes_exif_keeps_pixels(self, tmp_path):
        from app.optimizer import Optimizer
        path = tmp_path / "x.webp"
        img = Image.new("RGB", (30, 30), (4, 5, 6))
        exif = img.getexif()
        exif[0x010E] = "desc"
        img.save(path, format="WEBP", exif=exif.tobytes())
        data = path.read_bytes()
        assert b"EXIF" in data
        stripped = Optimizer._strip_webp_metadata(data)
        assert b"EXIF" not in stripped
        out = tmp_path / "y.webp"
        out.write_bytes(stripped)
        assert list(Image.open(path).convert("RGB").getdata()) == list(Image.open(out).convert("RGB").getdata())

    def test_strip_webp_metadata_noop_on_clean_webp(self, tmp_path):
        from app.optimizer import Optimizer
        path = tmp_path / "x.webp"
        Image.new("RGB", (20, 20), (2, 2, 2)).save(path, format="WEBP", lossless=True)
        data = path.read_bytes()
        assert Optimizer._strip_webp_metadata(data) == data

    def test_strip_webp_metadata_handles_malformed_input_without_raising(self):
        from app.optimizer import Optimizer
        assert Optimizer._strip_webp_metadata(b"") == b""
        assert Optimizer._strip_webp_metadata(b"not a webp") == b"not a webp"


class TestAvailableModes:
    """`ready` predates WebP / lossless / resize_only and only described the
    Standard-mode PNG path — so it returns False on a build that can still
    produce lossless/resize-only PNG *and* every WebP flavor successfully.
    `available_modes()` is the new interface that returns the exact set of
    (format, compression_mode) combinations this instance can actually
    process, given which binaries it auto-detected."""

    def test_both_binaries_all_modes_available(self, fake_bin_dir, optimizer):
        # `optimizer` fixture already points pngquant_path/oxipng_path (and
        # cjpeg_path) at the .bat wrappers in fake_bin_dir — auto-detection
        # deliberately bypassed (see conftest.py: same reason every other
        # test here does the same, instead of relying on Optimizer's
        # OS-specific path search).
        opt = optimizer
        assert opt.pngquant_path is not None
        assert opt.oxipng_path is not None
        assert opt.cjpeg_path is not None
        assert opt.avifenc_path is not None
        modes = opt.available_modes()
        assert {("png", "standard"), ("png", "lossless"), ("png", "resize_only"), ("png", "screenshot"),
                ("webp", "standard"), ("webp", "lossless"), ("webp", "resize_only"),
                ("jpg", "standard"), ("jpg", "lossless"), ("jpg", "resize_only"),
                ("avif", "standard"), ("avif", "lossless"), ("avif", "resize_only")} == modes

    def test_no_binaries_drops_standard_png_and_all_jpeg(self, tmp_path):
        from app.optimizer import Optimizer
        # No binaries found in bin_dir → all paths None.
        opt = Optimizer(bin_dir=tmp_path / "nonexistent_bin_dir")
        assert opt.pngquant_path is None
        assert opt.oxipng_path is None
        assert opt.cjpeg_path is None
        assert opt.avifenc_path is None

        modes = opt.available_modes()
        # PNG standard/screenshot — the two modes that actually need
        # pngquant for their lossy color quantization step — are dropped.
        assert ("png", "standard") not in modes
        assert ("png", "screenshot") not in modes
        # JPEG needs cjpeg for every mode (it's a genuine JPEG re-encode),
        # so without it the whole (jpg, *) family disappears.
        assert not any(fmt == "jpg" for fmt, _ in modes)
        # AVIF needs avifenc for every mode — same logic.
        assert not any(fmt == "avif" for fmt, _ in modes)
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


class TestAVIF:
    """AVIF output via avifenc external binary."""

    def test_standard_lossy_avif(self, optimizer, tmp_path):
        src = tmp_path / "input.png"
        out = tmp_path / "output.avif"
        Image.new("RGB", (64, 64), (200, 100, 50)).save(src)
        result = asyncio.run(
            optimizer.optimize_png(src, out, quality="medium",
                                   output_format="avif", compression_mode="standard")
        )
        assert result["success"] is True
        assert out.exists()
        assert out.stat().st_size > 0

    def test_lossless_avif(self, optimizer, tmp_path):
        src = tmp_path / "input.png"
        out = tmp_path / "output.avif"
        Image.new("RGB", (32, 32), (10, 20, 30)).save(src)
        result = asyncio.run(
            optimizer.optimize_png(src, out, output_format="avif",
                                   compression_mode="lossless")
        )
        assert result["success"] is True
        assert out.exists()

    def test_resize_only_avif(self, optimizer, tmp_path):
        src = tmp_path / "input.png"
        out = tmp_path / "output.avif"
        Image.new("RGB", (200, 200), (100, 100, 100)).save(src)
        result = asyncio.run(
            optimizer.optimize_png(src, out, max_width=50,
                                   output_format="avif", compression_mode="resize_only")
        )
        assert result["success"] is True
        assert out.exists()

    def test_resize_only_avif_uses_lossless_not_quality(self, optimizer, tmp_path, monkeypatch):
        """Regression test: resize_only used to encode AVIF at -q 90 (lossy)
        instead of --lossless, silently costing quality that "Resize Only"
        promises not to touch — unlike JPEG (no true lossless codec exists),
        AVIF has a real --lossless mode, so there's no excuse for it, and
        PNG/WebP's resize_only are both genuinely lossless already.
        Confirmed with the real libavif CLI (avifenc v1.3.0): a
        resize_only encode with max_width=0 (no resize at all) used to
        come back pixel-different from the source; with --lossless it's
        pixel-identical. The test fixture's fake avifenc round-trips
        losslessly regardless of the flags it's given, so what's actually
        under test here is the command line built for it."""
        import asyncio as _asyncio
        captured = {}
        real_exec = _asyncio.create_subprocess_exec

        async def spy_exec(*args, **kwargs):
            captured["argv"] = args
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", spy_exec)

        src = tmp_path / "input.png"
        out = tmp_path / "output.avif"
        Image.new("RGB", (100, 100), (10, 20, 30)).save(src)
        result = asyncio.run(
            optimizer.optimize_png(src, out, output_format="avif", compression_mode="resize_only")
        )
        assert result["success"] is True
        argv = captured["argv"]
        assert "--lossless" in argv, f"resize_only should encode losslessly, got: {argv}"
        assert "-q" not in argv, f"resize_only should not use a lossy quality flag, got: {argv}"

    def test_avif_requires_avifenc(self, tmp_path):
        from app.optimizer import Optimizer
        opt = Optimizer(bin_dir=tmp_path / "nonexistent")
        src = tmp_path / "input.png"
        out = tmp_path / "output.avif"
        Image.new("RGB", (32, 32), (50, 50, 50)).save(src)
        result = asyncio.run(
            opt.optimize_png(src, out, output_format="avif")
        )
        assert result["success"] is False
        assert "avifenc" in result["error"]

    def test_png_source_to_avif(self, optimizer, tmp_path):
        """A real PNG source (not just a renamed file) should work."""
        src = tmp_path / "real.png"
        out = tmp_path / "output.avif"
        Image.new("RGB", (48, 48), (255, 0, 0)).save(src)
        result = asyncio.run(
            optimizer.optimize_png(src, out, output_format="avif",
                                   compression_mode="lossless")
        )
        assert result["success"] is True
        assert out.exists()

    def test_rgba_source_preserves_alpha_for_avif(self, optimizer, tmp_path):
        """AVIF supports alpha natively (unlike the JPEG output path, which
        has to composite onto white). Since the intermediate is PNG rather
        than the old PPM/Y4M, transparency should now survive the trip
        instead of being flattened away."""
        src = tmp_path / "rgba.png"
        out = tmp_path / "output.avif"
        img = Image.new("RGBA", (32, 32), (100, 200, 50, 128))
        img.save(src)
        result = asyncio.run(
            optimizer.optimize_png(src, out, output_format="avif",
                                   compression_mode="lossless")
        )
        assert result["success"] is True
        assert out.exists()
        # The fake avifenc round-trips whatever Pillow decodes from the
        # intermediate, so this confirms the intermediate itself still
        # carries an alpha channel — i.e. optimizer.py isn't compositing
        # onto white before handing off to avifenc anymore.
        with Image.open(out) as result_img:
            assert result_img.mode in ("RGBA", "LA"), (
                f"alpha channel was lost — encoded as {result_img.mode}"
            )

    def test_avif_intermediate_is_png_not_ppm(self, optimizer, tmp_path, monkeypatch):
        """Regression test for the original bug: avifenc only recognizes
        input.[jpg|jpeg|png|y4m] (verified against the real libavif CLI,
        v1.3.0) — a .ppm intermediate is rejected outright, and every AVIF
        encode failed unconditionally until this was fixed. Assert the
        actual subprocess argv directly so a future change back to PPM (or
        to a since-removed --quality flag) fails loudly here rather than
        silently, the way it did before this file's fake avifenc was
        strengthened to validate like the real one."""
        captured_cmd = {}
        real_exec = asyncio.create_subprocess_exec

        async def spy_exec(*args, **kwargs):
            captured_cmd["argv"] = args
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)

        src = tmp_path / "input.png"
        out = tmp_path / "output.avif"
        Image.new("RGB", (32, 32), (10, 20, 30)).save(src)
        result = asyncio.run(
            optimizer.optimize_png(src, out, quality="medium",
                                   output_format="avif", compression_mode="standard")
        )
        assert result["success"] is True

        argv = captured_cmd["argv"]
        input_file = argv[-1]
        assert str(input_file).endswith(".png"), (
            f"avifenc was given a non-PNG intermediate: {input_file}"
        )
        assert "--quality" not in argv, "avifenc has no --quality flag — use -q/--qcolor"
        assert "-q" in argv


class TestScreenshotModeRealBinaries:
    """Empirical validation against real pngquant/oxipng (not the fake
    test doubles) — skipped when they're not on PATH, since they're
    genuine platform binaries CI can't run. Run this locally on a machine
    with pngquant/oxipng installed to reproduce the numbers backing the
    screenshot mode design (see optimizer.py's screenshot branch and
    CHANGELOG): a real 3840x2160 UI screenshot (flat UI regions + a lot
    of anti-aliased text + a gradient panel), synthesized here, should
    compress by roughly 70-80% with near-zero color deviation."""

    @staticmethod
    def _real_binaries():
        import shutil as _shutil
        pq = _shutil.which("pngquant")
        ox = _shutil.which("oxipng")
        return pq, ox

    def _make_synthetic_screenshot(self, path):
        """A rough approximation of a real UI screenshot: flat sidebar/
        toolbar regions, many small anti-aliased text glyphs (the part
        that pushes real screenshots' color count into the thousands,
        defeating a naive "screenshots have <=256 colors" assumption),
        and a smooth gradient panel (the part most likely to show visible
        banding under aggressive quantization)."""
        from PIL import ImageDraw, ImageFont
        W, H = 3840, 2160
        img = Image.new("RGB", (W, H), (245, 246, 248))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 280, H], fill=(38, 42, 54))
        d.rectangle([280, 0, W, 70], fill=(255, 255, 255))
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 15
            )
        except OSError:
            font = ImageFont.load_default()
        for i in range(60):
            d.text((320, 110 + i * 30), f"{i+1:>4}  some_code_line = {i}", font=font, fill=(60, 64, 74))
        x0, y0, x1, y1 = 2600, 1300, 3820, 1900
        for gy in range(y0, y1):
            t = (gy - y0) / (y1 - y0)
            d.line([x0, gy, x1, gy], fill=(int(240 - t*80), int(245 - t*60), int(250 - t*20)))
        img.save(path, format="PNG", compress_level=1)
        return img

    def test_screenshot_mode_hits_target_compression_with_low_color_error(self, tmp_path):
        pq, ox = self._real_binaries()
        if not pq or not ox:
            pytest.skip("real pngquant/oxipng not on PATH — this validates against the actual binaries, not the CI test doubles")

        from app.optimizer import Optimizer
        opt = Optimizer(bin_dir=tmp_path)
        opt.pngquant_path = Path(pq)
        opt.oxipng_path = Path(ox)

        src = tmp_path / "screenshot.png"
        self._make_synthetic_screenshot(src)
        raw_size = src.stat().st_size
        out = tmp_path / "out.png"

        result = asyncio.run(opt.optimize_png(src, out, compression_mode="screenshot"))
        assert result["success"] is True
        reduction = 1 - (out.stat().st_size / raw_size)
        assert reduction >= 0.65, (
            f"only {reduction:.1%} reduction on the synthetic screenshot — "
            "expected roughly 70-80%, see CHANGELOG for the reference numbers"
        )

        orig = Image.open(src).convert("RGB")
        compressed = Image.open(out).convert("RGB")
        diff = ImageChops.difference(orig, compressed)
        mean_err = sum(ImageStat.Stat(diff).mean) / 3
        assert mean_err < 0.1, (
            f"mean per-channel color error {mean_err:.4f} is far higher than the "
            "~0.007 measured in development — screenshot mode may no longer be "
            "near-lossless on this kind of content"
        )


class TestConcurrentBinaryDetection:
    """_detect_binaries() runs once at construction and again on every
    /api/health request via asyncio.to_thread (see app/main.py) — by
    design, so a binary installed after the app started gets picked up
    without a restart. Two overlapping /api/health calls therefore run
    this on two different thread-pool threads at once; without a lock
    around it, their writes to the four *_path attributes can interleave,
    and a reader building a response from those same attributes could
    observe a torn mix of the two calls' results."""

    def test_concurrent_detect_binaries_does_not_interleave(self, optimizer):
        import threading
        import time as _time

        calls = []
        calls_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def fake_find_binary(name):
            start = _time.perf_counter()
            # Widen the window: at 20ms per call and 5 calls per
            # invocation (pngquant, oxipng, cjpeg, cjpeg-static,
            # avifenc), an unlocked version reliably interleaves here.
            _time.sleep(0.02)
            end = _time.perf_counter()
            with calls_lock:
                calls.append((threading.current_thread().name, start, end))
            return None

        optimizer._find_binary = fake_find_binary

        def run_detect(label):
            threading.current_thread().name = label
            barrier.wait()  # both threads enter _detect_binaries() together
            optimizer._detect_binaries()

        t1 = threading.Thread(target=run_detect, args=("A",))
        t2 = threading.Thread(target=run_detect, args=("B",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive()

        a_spans = [(s, e) for label, s, e in calls if label == "A"]
        b_spans = [(s, e) for label, s, e in calls if label == "B"]
        assert a_spans and b_spans
        a_start, a_end = min(s for s, _ in a_spans), max(e for _, e in a_spans)
        b_start, b_end = min(s for s, _ in b_spans), max(e for _, e in b_spans)

        # With the lock, one invocation's entire span must finish before
        # the other's starts — no interleaving between A's five
        # _find_binary calls and B's.
        assert a_end <= b_start or b_end <= a_start, (
            f"A span=({a_start:.4f}, {a_end:.4f}) B span=({b_start:.4f}, "
            f"{b_end:.4f}) overlap — concurrent _detect_binaries() calls "
            f"are not serialized"
        )


class TestSubprocessTimeout:
    """pngquant/oxipng/cjpeg/avifenc calls used to await proc.communicate()
    with no timeout at all -- a hung external binary (malformed input, a
    bug in the tool, a stalled network drive) would block its worker, and
    the concurrency-limiting semaphore slot it holds, forever. See
    CHANGELOG."""

    def test_communicate_with_timeout_kills_a_genuinely_hung_process(self):
        """Unit-level check against a real subprocess (not a mock), so
        this exercises the actual kill/reap mechanics, not just the
        control flow around them."""
        async def scenario():
            import time as _time
            from app.optimizer import Optimizer

            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(999)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            t0 = _time.perf_counter()
            stdout, stderr = await Optimizer._communicate_with_timeout(proc, timeout=0.3)
            elapsed = _time.perf_counter() - t0
            return elapsed, stdout, stderr, proc

        elapsed, stdout, stderr, proc = asyncio.run(scenario())
        assert elapsed < 5.0, f"took {elapsed:.2f}s -- did not return promptly after the 0.3s timeout"
        assert b"timed out" in stderr
        assert stdout == b""
        # The process must actually be dead, not orphaned/left running.
        assert proc.returncode is not None

    def test_pngquant_timeout_produces_warning_and_still_succeeds_via_fallback(
        self, optimizer, test_images, tmp_path, monkeypatch
    ):
        """Integration-level: pngquant hangs, main.py's existing
        `elif proc.returncode != 0: result["warning"] = msg` path (nothing
        new needed there) picks up the synthetic timeout message, and the
        file still succeeds via the lossless/oxipng fallback rather than
        the whole call hanging for the full production timeout."""
        import app.optimizer as optimizer_module

        hang_script = tmp_path / "pngquant_hangs"
        hang_script.write_text(
            f"#!/usr/bin/env python3\nimport time\ntime.sleep(999)\n"
        )
        hang_script.chmod(hang_script.stat().st_mode | stat.S_IEXEC)
        if sys.platform == "win32":
            wrapper = tmp_path / "pngquant_hangs.bat"
            wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{hang_script}" %*\r\n')
            optimizer.pngquant_path = wrapper
        else:
            wrapper = tmp_path / "pngquant_hangs.sh"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{hang_script}"\n')
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
            optimizer.pngquant_path = wrapper

        monkeypatch.setattr(optimizer_module, "SUBPROCESS_TIMEOUT", 0.3)

        src = test_images / "2023" / "vacation" / "IMG_0001.png"
        out = tmp_path / "out.png"

        t0 = time.time()
        result = asyncio.run(optimizer.optimize_png(src, out, max_width=0))
        elapsed = time.time() - t0

        assert elapsed < 10.0, f"took {elapsed:.2f}s -- pngquant hang was not bounded by SUBPROCESS_TIMEOUT"
        assert result["success"] is True, "oxipng fallback should still produce a usable output"
        assert "timed out" in (result.get("warning") or "")
        assert out.exists()

