"""Regression tests for EXIF metadata retention (optimization item #1).

Design rules captured by these tests (see OPTIMIZATION_PLAN.md):
- `keep_exif` defaults to False — current strip-everything behavior is
  preserved exactly.
- When `keep_exif=True`, a *curated* EXIF subset is retained; GPS,
  Orientation, and MakerNote are dropped.
- The double-rotation cross-cutting bug: `_ensure_png` / `_optimize_webp`
  already call `ImageOps.exif_transpose`, which bakes the EXIF Orientation
  into the pixel grid. Retention MUST NOT re-write the original
  Orientation tag, or a reader honoring EXIF would rotate the
  already-upright pixels a second time.
- Both encoding paths (PNG via pngquant/oxipng, WebP via Pillow) are
  exercised with and without source EXIF.
"""
from __future__ import annotations

import asyncio

import pytest
from PIL import Image

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# EXIF tag ids used in the tests.
TAG_ORIENTATION = 0x0112
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_GPS_INFO = 0x8825
TAG_EXIF_IFD = 0x8769
TAG_DATETIME_ORIGINAL = 0x9003
TAG_MAKER_NOTE = 0x927C


def _make_oriented_jpeg(path, base_size=(40, 80), orientation=6):
    """A portrait JPEG carrying EXIF: Orientation + camera Make/Model +
    DateTimeOriginal in the ExifIFD. Orientation=6 means "rotated 90deg
    CW", so after `exif_transpose` the upright pixels are landscape
    (w>h) — that dimension flip is what the no-double-rotation test
    keys off."""
    img = Image.new("RGB", base_size, (123, 200, 50))
    exif = img.getexif()
    exif[TAG_ORIENTATION] = orientation
    exif[TAG_MAKE] = "TestMake"
    exif[TAG_MODEL] = "TestModel"
    exif_ifd = exif.get_ifd(TAG_EXIF_IFD)
    exif_ifd[TAG_DATETIME_ORIGINAL] = "2026:01:02 03:04:05"
    exif_ifd[TAG_MAKER_NOTE] = b"x" * 200  # bulky, should be dropped
    img.save(path, format="JPEG", exif=exif.tobytes())


def _make_jpeg_with_gps(path):
    img = Image.new("RGB", (32, 32), (10, 20, 30))
    exif = img.getexif()
    exif[TAG_ORIENTATION] = 3
    gps = exif.get_ifd(TAG_GPS_INFO)
    gps[0x0001] = "N"  # GPSLatitudeRef (ASCII)
    gps[0x0006] = 12.34  # GPSAltitude (single rational — Pillow-serializable)
    img.save(path, format="JPEG", exif=exif.tobytes())


def _make_plain_png(path):
    Image.new("RGB", (50, 50), (5, 6, 7)).save(path, format="PNG")


def _read_exif(path):
    with Image.open(path) as img:
        return img.getexif()


class TestCleanExifUnit:
    """Unit test for the EXIF-cleaning step, independent of the encoding
    pipeline. Builds real JPEGs (so the Exif object is populated from a
    serialized file, not hand-stuffed) and asserts the cleaner drops
    Orientation / GPS / MakerNote while keeping camera tags."""

    def test_drops_orientation_and_keeps_camera_tags(self, tmp_path):
        from app.optimizer import Optimizer

        src = tmp_path / "src.jpg"
        _make_oriented_jpeg(src)
        exif = _read_exif(src)
        assert TAG_ORIENTATION in exif  # sanity: source really has it

        Optimizer._clean_exif(exif)

        assert TAG_ORIENTATION not in exif, "Orientation must be dropped (double-rotation guard)"
        assert exif.get(TAG_MAKE) == "TestMake"
        assert exif.get(TAG_MODEL) == "TestModel"

    def test_drops_gps(self, tmp_path):
        from app.optimizer import Optimizer

        src = tmp_path / "gps.jpg"
        _make_jpeg_with_gps(src)
        exif = _read_exif(src)
        gps_before = exif.get_ifd(TAG_GPS_INFO)
        assert gps_before, "sanity: source has GPS data"

        Optimizer._clean_exif(exif)

        assert not exif.get_ifd(TAG_GPS_INFO), "GPS IFD must be cleared"
        assert TAG_GPS_INFO not in exif, "GPSInfo pointer must be removed"

    def test_drops_maker_note(self, tmp_path):
        from app.optimizer import Optimizer

        src = tmp_path / "src.jpg"
        _make_oriented_jpeg(src)
        exif = _read_exif(src)
        assert exif.get_ifd(TAG_EXIF_IFD).get(TAG_MAKER_NOTE)  # sanity

        Optimizer._clean_exif(exif)

        assert TAG_MAKER_NOTE not in exif.get_ifd(TAG_EXIF_IFD)


class TestPngExifRetention:
    def test_default_strips_exif(self, tmp_path, optimizer):
        """keep_exif defaults to False — the PNG pipeline (pngquant --strip
        + oxipng --strip safe) leaves no EXIF. We use a JPEG source so the
        result is deterministic under the fake binaries (which copy bytes
        through rather than stripping): _ensure_png drops EXIF when
        normalizing, so the fakes copy an already-clean PNG."""
        src = tmp_path / "src.jpg"
        _make_oriented_jpeg(src)
        out = tmp_path / "out.png"

        result = asyncio.run(optimizer.optimize_png(src, out))
        assert result["success"]

        exif = _read_exif(out)
        assert not exif, "default run must produce no EXIF"

    def test_keep_exif_retains_camera_tags_without_orientation(self, tmp_path, optimizer):
        src = tmp_path / "src.jpg"
        _make_oriented_jpeg(src)
        out = tmp_path / "out.png"

        result = asyncio.run(optimizer.optimize_png(src, out, keep_exif=True))
        assert result["success"]

        exif = _read_exif(out)
        assert TAG_ORIENTATION not in exif, "Orientation must not be re-written (double-rotation)"
        assert exif.get(TAG_MAKE) == "TestMake"
        assert exif.get(TAG_MODEL) == "TestModel"
        assert exif.get_ifd(TAG_EXIF_IFD).get(TAG_DATETIME_ORIGINAL) == "2026:01:02 03:04:05"

    def test_keep_exif_no_double_rotation(self, tmp_path, optimizer):
        """The cross-cutting bug: pixels are already upright after
        exif_transpose, AND the retained EXIF must not carry an Orientation
        that would re-rotate. Source is portrait (40x80) with Orientation=6
        → upright output is landscape (80x40). If double-rotation happened
        (Orientation re-written), a compliant reader would show it wrong."""
        src = tmp_path / "portrait.jpg"
        _make_oriented_jpeg(src, base_size=(40, 80), orientation=6)
        out = tmp_path / "out.png"

        result = asyncio.run(optimizer.optimize_png(src, out, keep_exif=True))
        assert result["success"]

        with Image.open(out) as img:
            assert img.size == (80, 40), "pixels must be the upright (transposed) size"
        exif = _read_exif(out)
        assert TAG_ORIENTATION not in exif

    def test_keep_exif_with_no_source_exif_is_noop(self, tmp_path, optimizer):
        src = tmp_path / "plain.png"
        _make_plain_png(src)
        out = tmp_path / "out.png"

        result = asyncio.run(optimizer.optimize_png(src, out, keep_exif=True))
        assert result["success"]
        assert out.read_bytes()[:8] == PNG_MAGIC
        assert not _read_exif(out), "no source EXIF → no EXIF in output, no error"

    def test_keep_exif_replaces_existing_exif_chunk(self, tmp_path, optimizer):
        """A source PNG that already carries an eXIf chunk must end up with
        ONLY the cleaned EXIF, not a duplicate (the fake binaries copy the
        chunk through; the real ones strip it). The finalizer must remove
        any pre-existing eXIf before injecting the cleaned one."""
        src = tmp_path / "with_exif.png"
        img = Image.new("RGB", (40, 80), (1, 2, 3))
        exif = img.getexif()
        exif[TAG_ORIENTATION] = 6
        exif[TAG_MAKE] = "OriginalMake"
        img.save(src, format="PNG", exif=exif.tobytes())

        out = tmp_path / "out.png"
        result = asyncio.run(optimizer.optimize_png(src, out, keep_exif=True))
        assert result["success"]

        exif_out = _read_exif(out)
        assert TAG_ORIENTATION not in exif_out
        assert exif_out.get(TAG_MAKE) == "OriginalMake"
        # Exactly one eXIf chunk in the file (no duplicates).
        data = out.read_bytes()
        assert data.count(b"eXIf") == 1, "duplicate eXIf chunk — finalizer didn't strip first"


class TestWebPExifRetention:
    def test_default_strips_exif(self, tmp_path, optimizer):
        src = tmp_path / "src.jpg"
        _make_oriented_jpeg(src)
        out = tmp_path / "out.webp"

        result = asyncio.run(optimizer.optimize_png(src, out, output_format="webp"))
        assert result["success"]
        assert not _read_exif(out), "default WebP run must produce no EXIF"

    def test_keep_exif_retains_without_orientation(self, tmp_path, optimizer):
        src = tmp_path / "src.jpg"
        _make_oriented_jpeg(src)
        out = tmp_path / "out.webp"

        result = asyncio.run(optimizer.optimize_png(src, out, output_format="webp", keep_exif=True))
        assert result["success"]

        exif = _read_exif(out)
        assert TAG_ORIENTATION not in exif
        assert exif.get(TAG_MAKE) == "TestMake"

    def test_keep_exif_no_double_rotation(self, tmp_path, optimizer):
        src = tmp_path / "portrait.jpg"
        _make_oriented_jpeg(src, base_size=(40, 80), orientation=6)
        out = tmp_path / "out.webp"

        result = asyncio.run(optimizer.optimize_png(src, out, output_format="webp", keep_exif=True))
        assert result["success"]
        with Image.open(out) as img:
            assert img.size == (80, 40)
        exif = _read_exif(out)
        assert TAG_ORIENTATION not in exif

    def test_keep_exif_with_no_source_exif_is_noop(self, tmp_path, optimizer):
        src = tmp_path / "plain.png"
        _make_plain_png(src)
        out = tmp_path / "out.webp"

        result = asyncio.run(optimizer.optimize_png(src, out, output_format="webp", keep_exif=True))
        assert result["success"]
        assert Image.open(out).format == "WEBP"
        assert not _read_exif(out)


class TestExifViaApi:
    """End-to-end through the FastAPI layer: keep_exif plumbed through
    OptimizeRequest → _process_files → optimizer.optimize_png, and the
    cross-cutting guarantee that the run's state.results / ZIP are
    unaffected by the new flag."""

    def test_keep_exif_request_is_accepted_and_output_carries_exif(
        self, client, auth_headers, tmp_path
    ):
        from .conftest import wait_for

        d = tmp_path / "srcdir"
        d.mkdir()
        _make_oriented_jpeg(d / "portrait.jpg")

        client.post("/api/scan", json={"directory": str(d), "recursive": True}, headers=auth_headers)
        scanned = wait_for(lambda: (lambda r: not r["running"] and r)(
            client.get("/api/scan-progress", headers=auth_headers).json()
        ))
        assert len(scanned["files"]) == 1

        r = client.post(
            "/api/optimize", json={"keep_exif": True}, headers=auth_headers
        )
        assert r.status_code == 200
        prog = wait_for(lambda: (lambda p: not p["running"] and p)(
            client.get("/api/progress", headers=auth_headers).json()
        ))
        assert len(prog["results"]) == 1
        assert prog["results"][0]["success"]

        import app.main as m
        ws = m.SESSIONS[client.cookies.get("imgopt_session")].workspace
        out_file = ws / "output" / "portrait.png"
        assert out_file.exists()
        exif = _read_exif(out_file)
        assert TAG_ORIENTATION not in exif
        assert exif.get(TAG_MAKE) == "TestMake"

    def test_keep_exif_does_not_pollute_results_or_zip(self, client, auth_headers, tmp_path):
        """The new flag must not change result counts, output_version, or
        ZIP contents — the same cross-cutting guard the review asks for on
        every new feature touching the batch state machine. A single run
        with keep_exif=True must look exactly like one without it: one
        output_version bump, results count == file count, ZIP == those
        files. (Cross-run version arithmetic is confounded by /api/scan's
        reset() zeroing output_version, so this stays within one run.)"""
        from io import BytesIO
        from zipfile import ZipFile

        from .conftest import wait_for

        d = tmp_path / "srcdir"
        d.mkdir()
        _make_oriented_jpeg(d / "a.jpg")
        _make_plain_png(d / "b.png")

        client.post("/api/scan", json={"directory": str(d), "recursive": True}, headers=auth_headers)
        wait_for(lambda: (lambda r: not r["running"] and r)(
            client.get("/api/scan-progress", headers=auth_headers).json()
        ))

        r = client.post("/api/optimize", json={"keep_exif": True}, headers=auth_headers)
        assert r.status_code == 200
        prog = wait_for(lambda: (lambda p: not p["running"] and p)(
            client.get("/api/progress", headers=auth_headers).json()
        ))
        assert len(prog["results"]) == 2
        assert all(res["success"] for res in prog["results"])

        import app.main as m
        st = m.SESSIONS[client.cookies.get("imgopt_session")]
        ws_name = st.workspace.name
        # Exactly one bump for this run — the EXIF finalize step must not
        # register as a second run.
        assert st.output_version == 1, st.output_version

        zr = client.get(f"/api/download/{ws_name}")
        assert zr.status_code == 200
        names = {n.replace("\\", "/") for n in ZipFile(BytesIO(zr.content)).namelist()}
        assert names == {"a.png", "b.png"}
