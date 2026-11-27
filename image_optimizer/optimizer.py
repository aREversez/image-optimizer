from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import struct
import sys
import threading
import zlib
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageCms, ImageOps

from image_optimizer import binaries

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
HEX_COLOR_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")

# How long a single pngquant/oxipng/cjpeg/avifenc invocation is allowed to
# run before it's treated as hung rather than legitimately slow. Generous
# enough for a large image on slow storage; a compression call for one
# file still running past this is far more likely stuck (malformed input,
# a bug in the binary, a stalled network drive) than working. Mirrors the
# timeout already used for the much cheaper `where`/`which` binary lookup
# in _find_binary.
SUBPROCESS_TIMEOUT = 120

# Pixel count at which an encoder gets the base timeout; larger images get a
# proportionally longer one (see Optimizer._timeout_for), capped at 8x. 4 MP
# is roughly a phone photo, the size the 120 s default was tuned against.
PIXELS_PER_TIMEOUT_UNIT = 4_000_000

# After a timed-out encoder is killed, how long to wait for it to be reaped
# before giving up on it (see Optimizer._kill_and_reap).
REAP_TIMEOUT = 2.0

# EXIF tag ids used by the retention logic below. Orientation is stripped
# because `exif_transpose` already bakes it into the pixel grid — re-writing
# it would double-rotate in any reader that
# honors EXIF orientation. GPSInfo is dropped for privacy (images may be
# publicly shared). MakerNote is vendor-specific bulk with no display value.
EXIF_TAG_ORIENTATION = 0x0112
EXIF_TAG_GPS_INFO = 0x8825
EXIF_TAG_EXIF_IFD = 0x8769
EXIF_TAG_MAKER_NOTE = 0x927C


def cmyk_to_rgb(img):
    """Convert a CMYK image to RGB for the RGB-only output formats.

    Image.convert("RGB") applies a naive formula that ignores the embedded
    profile; against a colour-managed rendering it was about 30 levels off on
    average. With a CMYK profile the conversion goes profile -> sRGB (relative
    colorimetric with black point compensation, what print-to-screen
    conversions normally use). Without one, or if the profile cannot be used,
    it falls back to the plain conversion. The result is plain sRGB, so no
    profile is carried forward (see Optimizer._capture_icc)."""
    profile = img.info.get("icc_profile")
    if profile and profile[16:20] == b"CMYK":
        try:
            out = ImageCms.profileToProfile(
                img,
                ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                ImageCms.createProfile("sRGB"),
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                outputMode="RGB",
                flags=ImageCms.Flags.BLACKPOINTCOMPENSATION,
            )
            if out is not None:
                return out
        except Exception:
            pass
    return img.convert("RGB")


def fit_width(size: tuple[int, int], max_width: int):
    """Size to resize to so the width is at most max_width (aspect ratio
    kept), or None when no resize is needed. Only the width is limited: a
    tall portrait is left alone. The height is clamped to >= 1 so an extreme
    panorama (5000x1 -> 100 wide) does not collapse to height 0, which Pillow
    rejects. Single source for every output path."""
    w, h = size
    if max_width <= 0 or w <= max_width:
        return None
    return (max_width, max(1, int(h * (max_width / w))))


class Optimizer:
    def __init__(self, bin_dir: Optional[Path] = None, max_concurrency: int = 4):
        # Searched in order, before PATH. The per-user data directory is where
        # binaries fetched on first run land (see binaries.py) and is therefore
        # the only copy whose digest was verified; a same-named executable
        # earlier on PATH must not shadow it. The repo's own bin/ is searched
        # next so an existing source checkout / dev workflow that drops the
        # .exe next to the code keeps working unchanged.
        if bin_dir:
            self._bin_dirs = [Path(bin_dir)]
        elif getattr(sys, "frozen", False):
            self._bin_dirs = [Path(sys._MEIPASS) / "bin"]
        else:
            self._bin_dirs = [
                binaries.binary_dir(),
                Path(__file__).resolve().parent.parent / "bin",
            ]
        # Kept as the headline location for logging; _bin_dirs is authoritative.
        self.bin_dir = self._bin_dirs[0]
        self.pngquant_path: Optional[Path] = None
        self.oxipng_path: Optional[Path] = None
        self.cjpeg_path: Optional[Path] = None
        self.avifenc_path: Optional[Path] = None
        # Guards _detect_binaries: it's called once here (main thread) and
        # again on every /api/health request via asyncio.to_thread (see
        # image_optimizer/main.py), each on its own thread-pool thread. A real
        # threading.Lock (not asyncio.Lock, which doesn't coordinate across
        # OS threads) keeps two overlapping detections from interleaving
        # their writes to the four *_path attributes below, which a reader
        # building a /api/health response from those same attributes could
        # otherwise observe half-updated.
        self._detect_lock = threading.Lock()
        self._detect_binaries()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        # Decode/resize/encode-in-Pillow work is CPU- and memory-heavy and
        # runs in the event loop's default thread pool, which `_semaphore`
        # does not cover (it only bounds the external encoders). Without a
        # second bound, the CLI dispatches one task per file and every one of
        # them can be decoding a full-size image at the same time — peak
        # memory ends up proportional to the batch, not to --workers.
        self._decode_semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _run_bounded(self, func, *args):
        """Run blocking Pillow work in a thread, at most `max_concurrency`
        at a time. Same limit as the encoder semaphore, so a run's peak
        memory follows --workers instead of the number of files."""
        async with self._decode_semaphore:
            return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def _timeout_for(self, input_path: Path) -> float:
        """Encoder timeout scaled by pixel count.

        A fixed 120 s is either far too generous for a 200x200 icon or far
        too short for a 50 MP photo going through pngquant at --speed 1,
        where a legitimate pass can take minutes and would be killed and
        reported as a failure. Scale the base timeout linearly from 4 MP
        (1x) up to 8x, and let --timeout move the base."""
        base = float(SUBPROCESS_TIMEOUT)
        try:
            with Image.open(input_path) as img:
                pixels = img.size[0] * img.size[1]
        except Exception:
            return base
        scale = min(8.0, max(1.0, pixels / PIXELS_PER_TIMEOUT_UNIT))
        return base * scale

    @property
    def ready(self) -> bool:
        """True iff the lossy PNG path (Standard compression mode) is usable,
        i.e. pngquant is available. Kept for backwards compatibility — it
        only answers "can this build do the most aggressive Standard-mode
        PNG pipeline?", nothing else. Callers wanting the modern answer
        ("which format×mode combinations actually work right now?") should
        use `available_modes()` instead.

        Note in particular: `ready` says nothing about lossless / resize_only
        PNG compression (those only need oxipng or even just Pillow), nor
        about WebP (which needs no external binary at all — Pillow's built-in
        encoder is always available). A build with neither pngquant nor
        oxipng installed can still optimize WebP files successfully while
        reporting `ready == False`.
        """
        return self.pngquant_path is not None

    def available_modes(self) -> set[tuple[str, str]]:
        """Returns the set of (output_format, compression_mode) tuples this
        Optimizer instance can actually produce output for right now, given
        which external binaries were auto-detected.

        - WebP (any compression_mode) always works — Pillow's built-in
          WebP encoder is the only thing needed.
        - PNG lossless / resize_only need oxipng OR Pillow's PNG encoder
          (oxipng just shrinks further; both modes still produce a valid
          PNG without it, only bigger), so they're always available too.
        - PNG standard needs pngquant specifically — oxipng only optimizes,
          it doesn't do the lossy color quantization that's the whole
          point of Standard mode.
        - PNG screenshot needs pngquant too — it's a much tighter-quality
          pngquant pass, not a different tool (see optimize_image's
          screenshot branch for the quality floor and why dithering is
          forced off there).

        This is the API new code should call instead of `ready` when deciding
        whether to enable a particular (format, mode) option in the UI or
        accept it on the server side.
        """
        modes = {("webp", "standard"), ("webp", "lossless"), ("webp", "resize_only")}
        modes |= {("png", "lossless"), ("png", "resize_only")}
        if self.pngquant_path is not None:
            modes |= {("png", "standard"), ("png", "screenshot")}
        # JPEG output needs mozjpeg's cjpeg — every JPEG mode is a lossy
        # re-encode (JPEG is an inherently lossy codec; "lossless" maps to
        # the highest quality pass, see _optimize_jpeg). No cjpeg → no JPG.
        if self.cjpeg_path is not None:
            modes |= {("jpg", "standard"), ("jpg", "lossless"), ("jpg", "resize_only")}
        # AVIF output needs avifenc — all three modes are available when
        # the binary is found. AVIF is a modern, efficient codec that
        # typically produces smaller files than PNG or WebP at equivalent
        # quality, but encoding is slower. Unlike JPEG (no true lossless
        # codec exists, so its "lossless"/"resize_only" fall back to a
        # very-high-quality pass), AVIF has a real --lossless mode, and
        # both "lossless" and "resize_only" use it — same guarantee as
        # PNG/WebP: no quality cost beyond whatever resizing was asked for.
        if self.avifenc_path is not None:
            modes |= {("avif", "standard"), ("avif", "lossless"), ("avif", "resize_only")}
        return modes

    def _find_binary(self, name: str) -> Optional[Path]:
        """Locate an encoder: the pinned directories first, PATH second.

        Order matters. The pinned directory is populated by binaries.py,
        which verifies a SHA-256 before anything becomes executable; PATH is
        whatever the user's shell, an installer, or a malicious archive put
        there. A downloaded-and-verified pngquant must therefore win over a
        same-named file earlier on PATH, not lose to it.

        The PATH walk is done here rather than with shutil.which because on
        Windows shutil.which prepends the current working directory — for a
        GUI that was launched from a folder of downloaded images, that means
        a planted pngquant.exe next to them would be executed."""
        exe_name = f"{name}.exe" if sys.platform == "win32" else name
        for directory in self._bin_dirs:
            local = directory / exe_name
            try:
                if local.is_file():
                    return local.resolve()
            except OSError:
                continue
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            entry = entry.strip().strip('"')
            if not entry:
                # An empty entry means "the current directory" — exactly the
                # case this walk exists to avoid.
                continue
            directory = Path(entry)
            if not directory.is_absolute():
                continue
            candidate = directory / exe_name
            try:
                # `which` only reports executable files; keep that, so a stray
                # non-executable file does not shadow a working one later on
                # PATH (Windows has no execute bit - .exe is the test there).
                if candidate.is_file() and (sys.platform == "win32" or os.access(candidate, os.X_OK)):
                    return candidate.resolve()
            except OSError:
                continue
        return None

    @staticmethod
    async def _communicate_with_timeout(
        proc: "asyncio.subprocess.Process", timeout: Optional[float] = None
    ) -> tuple[bytes, bytes, bool]:
        """await proc.communicate() with a timeout, so a hung external
        binary (malformed input, a bug in the tool itself, a stalled
        network drive) fails just the one file being processed instead of
        blocking its worker -- and the concurrency-limiting semaphore slot
        it holds -- forever. On timeout the process is killed and reaped
        (so it doesn't linger as a zombie), and a synthetic stderr message
        is returned so every call site's existing
        `if <output missing/returncode != 0>: msg = stderr.decode()` error
        path picks this up exactly like any other subprocess failure, with
        no extra branching needed at each of the four call sites.

        On cancellation (e.g. the user hits Cancel): wait_for propagates
        CancelledError the same as a bare communicate() would, so existing
        cancel handling is unaffected -- but the child is now also killed
        on the way out. Without this, cancelling unblocks the *worker*
        (the await raises) but never signals the child process itself,
        which would otherwise keep running as an orphan in the background.

        The third element of the return value, `timed_out`, lets callers
        remove any partial file the binary may have left at its own
        output path before checking whether that path exists -- a killed
        process can leave a non-empty but truncated/corrupt file there,
        which an exists()-only (or exists()-and-size>0) check can't tell
        apart from a completed one.

        `timeout=None` reads the module global SUBPROCESS_TIMEOUT lazily at
        call time rather than as a Python default argument value (which
        would freeze whatever it was at class-definition time) -- same
        reasoning as CONCURRENT_WORKERS in main.py's _run_worker_pool, and
        what lets tests shrink this to something fast without threading an
        explicit timeout through optimize_image/_optimize_jpeg/_optimize_webp/
        _optimize_avif.

        Returns (stdout, stderr, timed_out)."""
        if timeout is None:
            timeout = SUBPROCESS_TIMEOUT
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout, stderr, False
        except asyncio.TimeoutError:
            await Optimizer._kill_and_reap(proc)
            return b"", f"timed out after {timeout:g}s (process killed)".encode(), True
        except asyncio.CancelledError:
            await Optimizer._kill_and_reap(proc)
            raise

    @staticmethod
    async def _kill_and_reap(proc: "asyncio.subprocess.Process") -> None:
        """Force-terminate a subprocess and wait (briefly) for it to exit,
        so it's neither left running as an orphan nor left as a zombie.
        The wait is bounded by REAP_TIMEOUT: this is best effort, not a
        guarantee, and it says so on stderr when it gives up.

        kill() reaches only the process we started. If that process had
        spawned a descendant (a .bat/.cmd or shim wrapper on Windows, a
        shell wrapper without `exec`), the descendant survives and keeps
        the stdout/stderr pipes open. On Python releases without the
        gh-119710 fix proc.wait() does not resolve until those pipes close
        -- asyncio wakes the waiters from _call_connection_lost, which needs
        every pipe disconnected; gh-119710 also wakes them from
        _process_exited -- so an unbounded wait here blocked the worker (and
        its semaphore slot) for as long as the descendant lived, which
        defeated the point of the timeout. Measured: 3.11.18 and 3.12.3
        block, 3.13.18 does not (patch-release dependent).

        Giving up on the wait is safe: asyncio records the exit status
        (and reaps the child) before the pipes can block wait(), so what
        survives the timeout is a bookkeeping orphan, not a running
        process.

        The pipes are closed on every path, not just the one that gave up.
        A survivor keeps them open either way: when wait() resolves without
        the pipes closing (gh-119710 wakes it from _process_exited) the
        abandoned transport is still finalized after the event loop is
        closed and logs "Event loop is closed" / "I/O operation on closed
        pipe". Closing goes through the transport's private handle, best
        effort, because asyncio has no public way to do it."""
        try:
            proc.kill()
        except OSError:
            # ProcessLookupError (already gone) is the common case, but
            # Windows can also refuse with PermissionError, and letting
            # that escape would replace the caller's "timed out" result
            # with an exception, skipping the encoder fallback path.
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=REAP_TIMEOUT)
        except asyncio.TimeoutError:
            status = getattr(proc, "returncode", None)
            print(
                f"[Warning] killed encoder pid {getattr(proc, 'pid', '?')} was not "
                f"reaped within {REAP_TIMEOUT:g}s "
                f"(exit status {'unknown' if status is None else status}); "
                f"abandoning the wait",
                file=sys.stderr,
            )
        except Exception:
            pass
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _detect_binaries(self):
        # See the comment on self._detect_lock in __init__: two concurrent
        # /api/health requests must not run this body at the same time.
        with self._detect_lock:
            self.pngquant_path = self._find_binary("pngquant")
            self.oxipng_path = self._find_binary("oxipng")
            # mozjpeg releases/prebuilt zip files name the encoder either
            # `cjpeg` or `cjpeg-static` (the static variant shipped by
            # several Windows/AiZ builds) — try both spellings so either is
            # picked up.
            self.cjpeg_path = self._find_binary_any(["cjpeg", "cjpeg-static"])
            self.avifenc_path = self._find_binary("avifenc")

    def _find_binary_any(self, names: list[str]) -> Optional[Path]:
        for name in names:
            path = self._find_binary(name)
            if path is not None:
                return path
        return None

    @staticmethod
    def _is_png(path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                return f.read(8) == PNG_MAGIC
        except Exception:
            return False

    # (format -> extensions) used by _apply_growth_guard to tell whether a
    # result is a genuine format conversion (nothing safe to fall back to)
    # or a same-format optimize (safe to fall back to a stripped copy of
    # the original).
    _SAME_FORMAT_EXTS = {
        "png": {".png"},
        "jpg": {".jpg", ".jpeg"},
        "webp": {".webp"},
    }

    _PNG_METADATA_CHUNKS = (b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME")

    @staticmethod
    def _strip_png_metadata(data: bytes) -> bytes:
        """Remove EXIF/text/timestamp chunks from a PNG's bytes, same set
        pngquant --strip / oxipng --strip safe remove. Color-management
        chunks (iCCP, gAMA, cHRM, sRGB) are left alone — they're not
        privacy-sensitive and removing them isn't what --strip is for."""
        for chunk_type in Optimizer._PNG_METADATA_CHUNKS:
            data = Optimizer._strip_png_chunks(data, chunk_type)
        return data

    @staticmethod
    def _strip_jpeg_metadata(data: bytes) -> bytes:
        """Remove all APPn metadata segments (Exif, XMP, ICC profile,
        Photoshop/IPTC, ...) and COM comments from a JPEG's bytes, keeping
        APP0/JFIF and everything from SOS onward untouched. Matches what
        the normal cjpeg-from-PPM pipeline already produces (it starts
        from raw pixels with zero metadata) — this gets the same result
        on a copy of the original bytes instead of a full re-encode.
        Malformed input is returned unchanged rather than raising."""
        if len(data) < 4 or data[0:2] != b"\xff\xd8":
            return data
        out = bytearray(data[0:2])
        pos = 2
        n = len(data)
        strip_markers = set(range(0xE1, 0xF0)) | {0xFE}  # APP1-APP15, COM
        while pos + 2 <= n:
            if data[pos] != 0xFF:
                out += data[pos:]
                return bytes(out)
            marker = data[pos + 1]
            if marker == 0xFF:  # fill byte between markers
                out.append(0xFF)
                pos += 1
                continue
            if marker == 0xD8:  # stray SOI
                out += data[pos:pos + 2]
                pos += 2
                continue
            if marker == 0xDA:  # SOS — metadata segments are done, entropy
                # data (with any RST markers) plus EOI follow verbatim.
                out += data[pos:]
                return bytes(out)
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                # standalone markers with no length field (shouldn't
                # appear before SOS, but handled just in case)
                out += data[pos:pos + 2]
                pos += 2
                continue
            if pos + 4 > n:
                break
            seg_len = int.from_bytes(data[pos + 2:pos + 4], "big")
            total = 2 + seg_len
            if pos + total > n:
                break  # malformed/truncated — bail, append the raw remainder below
            if marker not in strip_markers:
                out += data[pos:pos + total]
            pos += total
        out += data[pos:]
        return bytes(out)

    @staticmethod
    def _strip_webp_metadata(data: bytes) -> bytes:
        """Remove EXIF/XMP RIFF chunks from a WebP's bytes. VP8/VP8L/VP8X/
        ALPH/ANIM/ANMF/ICCP chunks are left alone. Malformed input, or
        input with nothing to strip, is returned unchanged."""
        if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
            return data
        body = bytearray()
        pos = 12
        n = len(data)
        removed_any = False
        while pos + 8 <= n:
            fourcc = data[pos:pos + 4]
            size = int.from_bytes(data[pos + 4:pos + 8], "little")
            total = 8 + size + (size & 1)  # chunks are padded to an even length
            if pos + total > n:
                body += data[pos:]
                pos = n
                break
            if fourcc in (b"EXIF", b"XMP "):
                removed_any = True
            else:
                body += data[pos:pos + total]
            pos += total
        if pos < n:
            body += data[pos:]
        if not removed_any:
            return data
        riff_size = 4 + len(body)  # "WEBP" + chunks, excluding the 8-byte RIFF header itself
        return b"RIFF" + riff_size.to_bytes(4, "little") + b"WEBP" + bytes(body)

    def _apply_growth_guard(
        self,
        input_path: Path,
        output_path: Path,
        output_format: str,
        result: dict,
        keep_exif: bool,
        cleaned_exif: bytes,
        icc_profile: bytes = b"",
    ) -> None:
        """If the just-produced output isn't actually smaller than the
        original, and no format conversion was requested (the source is
        already `output_format`), replace it with the original's pixel
        data unchanged but with privacy-relevant metadata (EXIF/GPS, XMP,
        text comments) stripped the same way the normal pipeline
        promises — never re-encoded, so the result can only ever be the
        same size as or smaller than the raw original.

        Skipped for a genuine format conversion (e.g. Screenshot mode
        forcing jpg -> png): the original bytes wouldn't be a valid file
        at output_path's extension, so there's nothing safe to fall back
        to there — that case is handled by each pipeline's own logic
        instead (see optimize_image's pngquant-failure branch).

        AVIF isn't handled here — its ISOBMFF box structure needs a real
        box parser to strip safely, which is more machinery than this
        narrow a case (avifenc rarely loses to an already-AVIF source)
        justifies right now.

        Best-effort: any failure here is swallowed and the original
        (larger) result stands rather than turning a successful
        compression into a failure."""
        if not result.get("success") or output_format not in ("png", "jpg", "webp"):
            return
        if result["compressed_size"] < result["original_size"]:
            return
        exts = self._SAME_FORMAT_EXTS.get(output_format, set())
        if input_path.suffix.lower() not in exts:
            return
        try:
            original_bytes = input_path.read_bytes()
            if output_format == "png":
                if original_bytes[:8] != PNG_MAGIC:
                    return
                stripped = self._strip_png_metadata(original_bytes)
            elif output_format == "jpg":
                stripped = self._strip_jpeg_metadata(original_bytes)
            else:
                stripped = self._strip_webp_metadata(original_bytes)
            output_path.write_bytes(stripped)
            if icc_profile and output_format == "jpg":
                # _strip_jpeg_metadata drops every APPn segment, which is
                # where a JPEG's ICC profile lives (APP2). PNG (iCCP) and
                # WebP (ICCP) are not touched by their strippers, so only
                # JPEG needs the profile put back.
                self._embed_jpeg_icc(output_path, icc_profile)
            if keep_exif and cleaned_exif:
                if output_format == "png":
                    self._finalize_png_exif(output_path, cleaned_exif)
                elif output_format == "jpg":
                    self._finalize_jpeg_exif(output_path, cleaned_exif)
                # WebP has no standalone EXIF-chunk injector (the normal
                # pipeline attaches it via Pillow's save(exif=...) during
                # encode) — this narrow fallback leaves WebP metadata-free
                # rather than risk a malformed RIFF/VP8X rewrite.
            result["compressed_size"] = os.path.getsize(output_path)
            result["kept_original"] = True
        except Exception:
            pass

    @staticmethod
    def _frame_count(path: Path) -> int:
        """Number of frames/pages in the file; 1 when it cannot be read (the
        normal pipeline then reports the real decode error)."""
        try:
            with Image.open(path) as img:
                # MPO is a JPEG with extra pictures (depth map, gain map,
                # burst thumbnails). Pillow reports n_frames >= 2 for it, but
                # the first frame IS the photo, so it is not an animation.
                if img.format == "MPO":
                    return 1
                return int(getattr(img, "n_frames", 1) or 1)
        except Exception:
            return 1

    @staticmethod
    def _ensure_png(src: Path, dst: Path, icc_profile: bytes = b""):
        """Re-encode any Pillow-readable image (jpg/bmp/tiff/...) into a real PNG.

        This is required because pngquant/oxipng only understand PNG input; without
        this step, non-PNG sources that skip the resize path get silently copied
        through unchanged but renamed to .png (invalid/mislabeled output).
        """
        with Image.open(src) as img:
            # Bake in the EXIF orientation (phone JPEGs) — PNG has no
            # orientation tag, so without this a portrait photo comes out
            # sideways in the optimized output.
            img = ImageOps.exif_transpose(img)
            if img.mode == "CMYK":
                img = cmyk_to_rgb(img)
            # Drop EXIF at normalization. The compress pipeline
            # (pngquant --strip / oxipng --strip safe) strips it anyway in
            # production, but the test stand-in binaries only copy bytes
            # through — removing it here keeps default (keep_exif=False)
            # behavior deterministic across both. A cleaned copy is
            # re-attached at the very end of the pipeline only when
            # keep_exif=True (see _finalize_png_exif / _optimize_webp).
            img.info.pop("exif", None)
            # Pillow's PNG writer falls back to img.info["icc_profile"] when
            # no icc_profile= is given, so the profile has to be removed
            # from info for --strip-icc (or a profile not describing RGB
            # pixels) to take effect.
            img.info.pop("icc_profile", None)
            save_kwargs = {"icc_profile": icc_profile} if icc_profile else {}
            img.save(dst, format="PNG", **save_kwargs)

    @staticmethod
    def _resize_image(src: Path, dst: Path, max_width: int, icc_profile: bytes = b""):
        with Image.open(src) as img:
            new_size = fit_width(img.size, max_width)
            if new_size is None:
                return
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            img.info.pop("exif", None)
            # Pillow's PNG writer falls back to img.info["icc_profile"] when
            # no icc_profile= is given, so the profile has to be removed
            # from info for --strip-icc (or a profile not describing RGB
            # pixels) to take effect.
            img.info.pop("icc_profile", None)
            save_kwargs = {"icc_profile": icc_profile} if icc_profile else {}
            img.save(dst, format="PNG", **save_kwargs)

    @staticmethod
    def _make_color_map(colors: list[str], dst: Path) -> bool:
        """Build a tiny reference PNG containing the given hex colors, one pixel each.

        Passed to pngquant via --map so it treats these colors as important and
        prioritizes keeping them in the generated palette (not a hard guarantee).
        """
        rgbs = []
        for c in colors:
            c = c.strip()
            if not HEX_COLOR_RE.match(c):
                continue
            c = c.lstrip("#")
            rgbs.append(tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)))
        if not rgbs:
            return False
        img = Image.new("RGB", (len(rgbs), 1))
        img.putdata(rgbs)
        img.save(dst, format="PNG")
        return True

    # --- EXIF retention helpers ---
    @staticmethod
    def _clean_exif(exif) -> None:
        """Strip EXIF fields that must NOT survive into optimized output.
        Mutates the PIL Exif object in place.

        - Orientation: `exif_transpose` already baked it into the pixels;
          re-writing it would double-rotate in any reader honoring EXIF.
        - GPSInfo: privacy — full GPS retention isn't wanted by default
          for images that may be publicly shared.
        - MakerNote: vendor-specific bulk, no display value.
        """
        exif.pop(EXIF_TAG_ORIENTATION, None)
        # get_ifd() caches the GPS sub-IFD on the Exif object. On Pillow 11.x,
        # popping the GPSInfo pointer alone leaves that cached IFD behind, and
        # tobytes() writes it back out if anything read it first. Empty it
        # before dropping the pointer so this holds whatever touched the
        # object earlier. Only when the tag is present: asking for a missing
        # IFD would create an empty one that Pillow could serialize.
        if EXIF_TAG_GPS_INFO in exif:
            try:
                exif.get_ifd(EXIF_TAG_GPS_INFO).clear()
            except Exception:
                pass
        exif.pop(EXIF_TAG_GPS_INFO, None)
        try:
            exif.get_ifd(EXIF_TAG_EXIF_IFD).pop(EXIF_TAG_MAKER_NOTE, None)
        except Exception:
            # ExifIFD may be absent; nothing to clean there.
            pass

    @staticmethod
    def _capture_cleaned_exif(src: Path) -> bytes:
        """Read the source's EXIF, drop the sensitive/redundant tags, and
        return the cleaned bytes (raw TIFF, starting with the II/MM byte
        order mark — ready for a PNG eXIf chunk or a WebP exif= save arg).
        Returns b"" if the source has no EXIF or can't be read — callers
        treat that as 'no EXIF to attach' and skip the finalize step.

        PIL's `Exif.tobytes()` prepends the JPEG APP1 `Exif\\x00\\x00`
        marker; that prefix is wrong for both the PNG eXIf chunk and
        WebP's exif= arg (both want raw TIFF), so it's stripped here."""
        try:
            with Image.open(src) as img:
                exif = img.getexif()
                if not exif:
                    return b""
                Optimizer._clean_exif(exif)
                raw = exif.tobytes() if exif else b""
                if raw.startswith(b"Exif\x00\x00"):
                    raw = raw[6:]
                return raw
        except Exception:
            return b""

    @staticmethod
    def _capture_icc(src: Path) -> bytes:
        """The source's embedded ICC colour profile, or b"" when it has none.

        Every pipeline decodes through Pillow and re-encodes the resulting
        pixel values unchanged, so those values stay in the source's colour
        space. Dropping the profile therefore does not convert anything to
        sRGB — it makes a wide-gamut photo *look* desaturated when a
        colour-managed viewer assumes sRGB. So the profile travels with the
        pixels unless the caller explicitly asks to strip it."""
        try:
            with Image.open(src) as img:
                profile = img.info.get("icc_profile")
                if not profile:
                    return b""
                profile = bytes(profile)
        except Exception:
            return b""
        # Every pipeline writes RGB pixels (CMYK and palette sources are
        # converted first, and WebP/JPEG/AVIF are RGB containers), so only a
        # profile whose data colour space is RGB still describes the output.
        # A CMYK or grayscale profile on RGB pixels is wrong, and strict
        # readers drop or reject the image. The space is the 4-byte signature
        # at offset 16 of the ICC header.
        if profile[16:20] != b"RGB ":
            return b""
        return profile

    @staticmethod
    def _strip_png_chunks(data: bytes, chunk_type: bytes) -> bytes:
        """Return `data` (a PNG file's bytes) with every chunk of
        `chunk_type` removed. Walks the chunk stream once, stops at IEND.
        Malformed input is returned with whatever was reconstructed up to
        the break — never raises."""
        out = bytearray(data[:8])  # PNG signature
        pos = 8
        n = len(data)
        while pos + 8 <= n:
            length = int.from_bytes(data[pos:pos + 4], "big")
            ctype = data[pos + 4:pos + 8]
            total = 8 + length + 4  # length field + type + data + crc
            if pos + total > n:
                break  # malformed/truncated — bail
            if ctype != chunk_type:
                out += data[pos:pos + total]
            if ctype == b"IEND":
                break
            pos += total
        return bytes(out)

    @staticmethod
    def _find_png_chunk_offset(data: bytes, chunk_type: bytes) -> Optional[int]:
        """Walk the PNG chunk stream (the same structural walk
        `_strip_png_chunks` does) and return the byte offset of the first
        chunk of `chunk_type` — pointing at its 4-byte length field — or
        None if it isn't found or the stream is malformed/truncated.

        Unlike a raw `bytes.find(b"IEND")`, this can't be fooled by those
        four bytes appearing inside another chunk's *data* (e.g. a stray
        tEXt/iTXt comment that happens to contain the literal text
        "IEND") — it only matches an actual chunk header."""
        pos = 8
        n = len(data)
        while pos + 8 <= n:
            length = int.from_bytes(data[pos:pos + 4], "big")
            ctype = data[pos + 4:pos + 8]
            total = 8 + length + 4
            if pos + total > n:
                return None  # malformed/truncated
            if ctype == chunk_type:
                return pos
            pos += total
        return None

    @staticmethod
    def _finalize_png_exif(png_path: Path, exif_bytes: bytes) -> None:
        """Attach cleaned EXIF to a finished PNG by inserting a single
        eXIf chunk before IEND. Any pre-existing eXIf chunks are removed
        first, so the result is deterministic regardless of what the
        compress pipeline left behind (real pngquant/oxipng strip
        metadata; the test stand-in binaries copy it through). Best-effort:
        never raises into the caller — a failure to attach EXIF must not
        turn a successful compression into a failure."""
        if not exif_bytes or exif_bytes[:2] not in (b"II", b"MM"):
            return
        try:
            data = png_path.read_bytes()
            if data[:8] != PNG_MAGIC:
                return
            data = Optimizer._strip_png_chunks(data, b"eXIf")
            chunk_body = exif_bytes
            chunk = struct.pack(">I", len(chunk_body)) + b"eXIf" + chunk_body
            chunk += struct.pack(">I", zlib.crc32(b"eXIf" + chunk_body) & 0xFFFFFFFF)
            iend_start = Optimizer._find_png_chunk_offset(data, b"IEND")
            if iend_start is None:
                return
            png_path.write_bytes(data[:iend_start] + chunk + data[iend_start:])
        except Exception:
            pass

    @staticmethod
    def _finalize_png_icc(png_path: Path, icc_bytes: bytes) -> None:
        """Make sure a finished PNG carries `icc_bytes` as its iCCP chunk.

        The intermediate resizes/normalisations write the profile directly
        (Pillow's icc_profile= argument), but pngquant's --strip and
        oxipng --strip can drop iCCP, so the last word belongs here: if the
        output already carries a profile, leave it alone (it came from the
        same source); otherwise insert one right after IHDR. An iCCP payload is
        a NUL-terminated Latin-1 name, a compression method byte, and the
        zlib-compressed profile. Best-effort — never raises."""
        if not icc_bytes:
            return
        try:
            data = png_path.read_bytes()
            if data[:8] != PNG_MAGIC:
                return
            if Optimizer._find_png_chunk_offset(data, b"iCCP") is not None:
                return
            chunk_body = b"ICC profile\x00\x00" + zlib.compress(icc_bytes, 9)
            chunk = struct.pack(">I", len(chunk_body)) + b"iCCP" + chunk_body
            chunk += struct.pack(">I", zlib.crc32(b"iCCP" + chunk_body) & 0xFFFFFFFF)
            # The PNG spec wants iCCP before PLTE and IDAT, and sRGB/gAMA/cHRM
            # must not accompany it (an sRGB chunk would claim the opposite
            # of the profile). Right behind IHDR satisfies all of that.
            for stale in (b"sRGB", b"gAMA", b"cHRM"):
                data = Optimizer._strip_png_chunks(data, stale)
            if data[12:16] != b"IHDR":
                return
            at = 8 + 8 + int.from_bytes(data[8:12], "big") + 4
            png_path.write_bytes(data[:at] + chunk + data[at:])
        except Exception:
            pass

    @staticmethod
    def _write_without_icc(src: Path, dst: Path) -> bool:
        """Write a copy of PNG `src` without its iCCP chunk to `dst`.
        Returns False (and writes nothing) when there is nothing to strip.

        pngquant colour-manages an input that carries a profile - it converts
        the pixels to sRGB and replaces the profile with gAMA + sRGB. Feeding
        it this copy keeps the pixels in the source's own space, so the
        original profile can be attached to the result afterwards and still
        describe them."""
        try:
            data = src.read_bytes()
            if data[:8] != PNG_MAGIC or Optimizer._find_png_chunk_offset(data, b"iCCP") is None:
                return False
            dst.write_bytes(Optimizer._strip_png_chunks(data, b"iCCP"))
            return True
        except Exception:
            return False

    @staticmethod
    def _remove_png_icc(png_path: Path) -> None:
        """Drop any iCCP chunk from a finished PNG. Best-effort, never raises."""
        try:
            data = png_path.read_bytes()
            if data[:8] != PNG_MAGIC or Optimizer._find_png_chunk_offset(data, b"iCCP") is None:
                return
            png_path.write_bytes(Optimizer._strip_png_chunks(data, b"iCCP"))
        except Exception:
            pass

    async def optimize_image(
        self,
        input_path: Path,
        output_path: Path,
        quality: str = "medium",
        max_width: int = 0,
        compression_mode: str = "standard",
        protected_colors: Optional[list[str]] = None,
        dithering: bool = True,
        output_format: str = "png",
        progress_callback: Optional[Callable] = None,
        keep_exif: bool = False,
        keep_icc: bool = True,
    ) -> dict:
        if compression_mode not in ("standard", "lossless", "resize_only", "screenshot"):
            compression_mode = "standard"

        # Refuse inputs the pipeline cannot represent. Pillow decodes only the
        # first frame, so an animated WebP/GIF/APNG or a multi-page TIFF would
        # otherwise be flattened to one frame and reported as a success.
        frames = await self._run_bounded(self._frame_count, input_path)
        if frames > 1:
            size = os.path.getsize(input_path)
            return {
                "success": False,
                "original_size": size,
                "compressed_size": size,
                "error": (
                    f"animated or multi-page image ({frames} frames): not supported, "
                    "left untouched to avoid dropping frames"
                ),
                "warning": None,
                # A deliberate refusal, not an encoder failure: callers that
                # distinguish the two (the CLI report) can say "skipped".
                "unsupported": True,
            }

        # Capture a cleaned EXIF copy from the source up front (before any
        # pipeline step that strips/transposes it). Attached to the output
        # at the very end — Orientation is already baked into pixels by
        # exif_transpose, so the retained EXIF never carries Orientation
        # (double-rotation guard).
        cleaned_exif = b""
        if keep_exif:
            cleaned_exif = self._capture_cleaned_exif(input_path)

        # Same idea for the colour profile: read it once from the source and
        # let each pipeline carry it onto its output. Pillow decodes into the
        # profile's own numbers and we write those numbers back out, so the
        # profile is what keeps the result looking the way the source did.
        icc_profile = self._capture_icc(input_path) if keep_icc else b""

        if output_format == "webp":
            return await self._optimize_webp(
                input_path, output_path, quality, max_width, compression_mode,
                progress_callback, keep_exif=keep_exif, cleaned_exif=cleaned_exif,
                icc_profile=icc_profile,
            )

        if output_format in ("jpg", "jpeg"):
            return await self._optimize_jpeg(
                input_path, output_path, quality, max_width, compression_mode,
                progress_callback, keep_exif=keep_exif, cleaned_exif=cleaned_exif,
                icc_profile=icc_profile,
            )

        if output_format == "avif":
            return await self._optimize_avif(
                input_path, output_path, quality, max_width, compression_mode,
                progress_callback, keep_exif=keep_exif, cleaned_exif=cleaned_exif,
                icc_profile=icc_profile,
            )

        original_size = os.path.getsize(input_path)
        result = {
            "success": False,
            "original_size": original_size,
            "compressed_size": original_size,
            "error": None,
            "warning": None,
        }
        temp_files: list = []

        try:
            working_path = input_path

            # Step 0: guarantee we're working with a real PNG from here on, regardless
            # of the source format and regardless of whether a resize happens next.
            if not self._is_png(working_path):
                normalized = output_path.with_suffix(".src.png")
                await self._run_bounded(self._ensure_png, working_path, normalized, icc_profile)
                if normalized.exists():
                    if progress_callback:
                        await progress_callback("converted to PNG")
                    working_path = normalized
                    temp_files.append(normalized)

            # Screenshot mode never resizes — the whole point is preserving
            # every pixel of UI text at full resolution (see the quantization
            # step below for where the size reduction actually comes from).
            # max_width is ignored entirely for this mode, matching the
            # "one click, no manual tuning" design (see PNG8-conversion
            # comment below for the empirical numbers behind this).
            if max_width > 0 and compression_mode != "screenshot":
                resized = output_path.with_suffix(".resized.png")
                await self._run_bounded(self._resize_image, working_path, resized, max_width, icc_profile)
                if resized.exists():
                    if progress_callback:
                        await progress_callback(f"resized to <= {max_width}px")
                    working_path = resized
                    temp_files.append(resized)

            if compression_mode in ("standard", "screenshot") and self.pngquant_path:
                pngquant_tmp = output_path.with_suffix(".pngquant.png")
                if compression_mode == "screenshot":
                    # Near-lossless floor for typical UI screenshots (flat
                    # chrome + anti-aliased text). Was 95-100 originally;
                    # lowered to 75-100 after measuring pngquant's actual
                    # behavior on real screenshot content:
                    #
                    # For images that already clear a tight floor (flat
                    # UI, icons, even moderate soft-shadow/gradient cards),
                    # pngquant's min-max range only acts as a pass/fail
                    # gate, not a target it optimizes down toward — it
                    # already searches for the best palette it can find
                    # regardless of how loose `min` is. Measured
                    # byte-for-byte identical output across a 60-100 to
                    # 95-100 sweep on several synthetic screenshots, so
                    # this floor change costs those images nothing.
                    #
                    # What actually changes is the harder subset: images
                    # whose true color complexity (embedded photo/video
                    # thumbnails, richer gradients) couldn't clear 95 at
                    # all. At 95-100 those failed outright and fell all
                    # the way through to an oxipng-only lossless pass —
                    # effectively 0% quantization gain on exactly the
                    # files that most needed it. At 75-100 the same
                    # content succeeds: measured 59.5% size reduction at
                    # PSNR 36.7dB on a synthetic thumbnail-heavy
                    # screenshot, with the resulting color error
                    # concentrated almost entirely inside the photo-like
                    # regions (mean abs error ~3.3/255 there) rather than
                    # spread across the image — text/UI chrome measured
                    # ~0.07/255, i.e. still visually exact where it
                    # matters most for a screenshot.
                    #
                    # If an image's complexity is too high to clear even
                    # 75 (rare — think a screenshot that's mostly a full
                    # photo), pngquant exits nonzero; see the branch below
                    # for how that's handled (fails cleanly for a
                    # non-PNG source rather than risking a bloated
                    # lossless PNG conversion, falls through to the
                    # existing lossless pass for a PNG source).
                    #
                    # Dithering is deliberately OFF here regardless of the
                    # caller's `dithering` setting: Floyd-Steinberg spreads
                    # quantization error into neighboring pixels to visually
                    # smooth out banding, which is the right tradeoff for
                    # photographic gradients but actively hurts this content
                    # — it scatters noise into what would otherwise be huge,
                    # near-free-to-compress flat UI regions, and roughens
                    # text edges. Measured on a synthetic screenshot: with
                    # dithering on, output was ~75% *larger* than with it
                    # off, for a *worse* mean color-error score, not better
                    # — there's no tradeoff being given up here, dithering
                    # is strictly worse for this content.
                    q_range = "75-100"
                    use_dithering = False
                else:
                    quality_map = {"high": "80-100", "medium": "65-80", "low": "50-65"}
                    q_range = quality_map.get(quality, "65-80")
                    use_dithering = dithering

                if progress_callback:
                    await progress_callback("color quantization...")

                cmd = [
                    str(self.pngquant_path),
                    "--quality", q_range,
                    "--speed", "1",
                    "--strip",
                    "--skip-if-larger",
                ]
                if not use_dithering:
                    cmd.append("--nofs")

                map_path: Optional[Path] = None
                if protected_colors:
                    map_path = output_path.with_suffix(".mapcolors.png")
                    made = await self._run_bounded(self._make_color_map, protected_colors, map_path)
                    if made:
                        cmd += ["--map", str(map_path)]
                        temp_files.append(map_path)
                    else:
                        map_path = None

                pq_input = working_path
                if icc_profile:
                    noicc = output_path.with_suffix(".noicc.png")
                    if await asyncio.to_thread(self._write_without_icc, working_path, noicc):
                        temp_files.append(noicc)
                        pq_input = noicc
                cmd += ["--output", str(pngquant_tmp), str(pq_input)]

                async with self._semaphore:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr, timed_out = await self._communicate_with_timeout(proc, timeout=await asyncio.to_thread(self._timeout_for, working_path))

                if timed_out:
                    # A killed pngquant can leave a partial file at its own
                    # --output path mid-write; remove it so the exists()
                    # check below can't mistake a truncated PNG for a
                    # completed quantization pass.
                    try:
                        pngquant_tmp.unlink(missing_ok=True)
                    except Exception:
                        pass

                if pngquant_tmp.exists():
                    working_path = pngquant_tmp
                    temp_files.append(pngquant_tmp)
                elif proc.returncode != 0:
                    msg = stderr.decode().strip()
                    if compression_mode == "screenshot" and not self._is_png(input_path):
                        # pngquant couldn't hit the 95-100 near-lossless floor
                        # within 256 colors. For a source that's genuinely
                        # PNG already, falling through to the plain lossless
                        # pass below is safe (no format change, and it's
                        # still covered by the general "don't ship a bigger
                        # file" guard at the end of this function). But for
                        # a non-PNG source forced through this PNG-only mode
                        # (e.g. a JPEG that turns out not to be a UI
                        # screenshot), that fallback would silently convert
                        # it to a *lossless* PNG — and lossless PNG can't
                        # compete with the source format's own lossy
                        # encoding on content this color-complex. Reproduced
                        # empirically: a synthetic photo that failed this
                        # same pngquant call came out ~14x larger as a
                        # lossless PNG than as the original JPEG. Fail the
                        # file instead of shipping that.
                        if progress_callback:
                            await progress_callback(
                                "screenshot mode: image too color-complex for a "
                                "near-lossless palette, skipped (try Standard or "
                                "Lossless mode instead)"
                            )
                        result["success"] = False
                        result["error"] = (
                            "Screenshot mode isn't a good fit for this image — its "
                            "colors are too complex for a near-lossless palette "
                            "(this usually means it's a photo, not a UI "
                            "screenshot). Try Standard or Lossless mode instead."
                        )
                        return result
                    if progress_callback:
                        await progress_callback(f"pngquant: {msg} (skipped)")
                    result["warning"] = msg
            elif progress_callback:
                if compression_mode == "screenshot":
                    await progress_callback("skipping color quantization (pngquant not found — falling back to lossless)")
                elif compression_mode != "standard":
                    await progress_callback(f"skipping color quantization ({compression_mode} mode)")
                else:
                    await progress_callback("skipping color quantization (pngquant not found)")

            if self.oxipng_path and working_path.suffix.lower() == ".png":
                oxipng_tmp = output_path.with_suffix(".oxipng.png")

                if progress_callback:
                    await progress_callback("depth compression...")

                async with self._semaphore:
                    proc = await asyncio.create_subprocess_exec(
                        str(self.oxipng_path),
                        "-o", "4",
                        "-a",
                        "--strip", "safe",
                        "--out", str(oxipng_tmp),
                        str(working_path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr, timed_out = await self._communicate_with_timeout(proc, timeout=await asyncio.to_thread(self._timeout_for, working_path))

                if timed_out:
                    # Same partial-write hazard as pngquant above: a killed
                    # oxipng can leave a truncated file at --out.
                    try:
                        oxipng_tmp.unlink(missing_ok=True)
                    except Exception:
                        pass

                if oxipng_tmp.exists():
                    # Registered as a temp BEFORE the replace: if replace()
                    # fails (e.g. output_path unexpectedly blocked), the
                    # finally-sweep still removes it. On success the file
                    # has moved, so the sweep's exists() check skips it.
                    temp_files.append(oxipng_tmp)
                    oxipng_tmp.replace(output_path)
                else:
                    # Unlike pngquant above, a failed oxipng pass (timeout,
                    # non-zero exit, or anything else that left no --out
                    # file) previously fell through to this copy with no
                    # signal at all -- stdout/stderr weren't even captured.
                    # The file still came out compressed (via whatever
                    # pngquant/normalization already produced), but
                    # oxipng's own depth-compression pass was silently
                    # skipped. Surface it the same way pngquant already
                    # does above.
                    msg = stderr.decode().strip()
                    if progress_callback:
                        await progress_callback(f"oxipng: {msg or 'skipped'} (skipped)")
                    result["warning"] = msg or "oxipng produced no output"
                    shutil.copy2(working_path, output_path)
            else:
                if working_path != output_path:
                    shutil.copy2(working_path, output_path)

            # Re-attach the cleaned EXIF as the very last PNG step, AFTER
            # pngquant --strip / oxipng --strip safe have run (they remove
            # all metadata, which is the point of those flags). The
            # injected chunk carries no Orientation (already baked into
            # pixels) — see _capture_cleaned_exif / _clean_exif.
            if keep_exif and cleaned_exif:
                self._finalize_png_exif(output_path, cleaned_exif)

            if output_path.exists():
                compressed_size = os.path.getsize(output_path)
                result["compressed_size"] = compressed_size
                result["success"] = True
            else:
                result["success"] = False
                result["error"] = "output file not generated"

            self._apply_growth_guard(
                input_path, output_path, "png", result, keep_exif, cleaned_exif, icc_profile
            )

            # After the growth guard: its fallback replaces the file with a
            # stripped copy of the original, so anything attached earlier
            # would be thrown away here. The chunk adds a few hundred bytes,
            # so the reported size is refreshed to stay truthful.
            if icc_profile:
                self._finalize_png_icc(output_path, icc_profile)
            else:
                # No profile to carry (--strip-icc, or the source's profile
                # did not describe RGB pixels): oxipng's "--strip safe" and a
                # PNG that skipped re-encoding both keep an existing iCCP.
                self._remove_png_icc(output_path)
            if result["success"]:
                try:
                    result["compressed_size"] = os.path.getsize(output_path)
                except OSError:
                    pass

        except Exception as e:
            result["success"] = False
            result["error"] = str(e)

        finally:
            # Sweep temps on success AND on the exception path — they live
            # inside ws/output, and /api/download zips that tree with
            # rglob("*"), so leftovers like *.src.png would end up in the
            # user's ZIP.
            for f in temp_files:
                try:
                    if f.exists() and f != output_path:
                        f.unlink()
                except Exception:
                    pass

        return result

    # Historical name, kept for callers that still use it: this method
    # handles every output format (PNG, WebP, JPEG, AVIF), not just PNG.
    optimize_png = optimize_image

    async def _optimize_webp(
        self,
        input_path: Path,
        output_path: Path,
        quality: str,
        max_width: int,
        compression_mode: str,
        progress_callback: Optional[Callable],
        *,
        keep_exif: bool = False,
        cleaned_exif: bytes = b"",
        icc_profile: bytes = b"",
    ) -> dict:
        """WebP goes through Pillow's own encoder rather than pngquant/oxipng
        (which only understand PNG) — there's no external binary dependency
        for this format, so it works even without pngquant/oxipng installed."""
        original_size = os.path.getsize(input_path)
        result = {
            "success": False,
            "original_size": original_size,
            "compressed_size": original_size,
            "error": None,
            "warning": None,
        }
        try:

            def _encode():
                with Image.open(input_path) as opened:
                    # Same EXIF-orientation bake-in as _ensure_png — WebP
                    # output shouldn't come out sideways either.
                    img = ImageOps.exif_transpose(opened)
                    if img.mode == "CMYK":
                        img = cmyk_to_rgb(img)

                    if max_width > 0:
                        new_size = fit_width(img.size, max_width)
                        if new_size:
                            img = img.resize(new_size, Image.Resampling.LANCZOS)

                    # Drop any EXIF carried in info so default (keep_exif=False)
                    # WebP output is metadata-free; attach the cleaned copy
                    # explicitly only when requested (no Orientation → no
                    # double-rotation, same guard as the PNG path).
                    img.info.pop("exif", None)
                    if compression_mode == "lossless" or compression_mode == "resize_only":
                        save_kwargs = {"format": "WEBP", "lossless": True, "method": 6}
                    else:
                        quality_map = {"high": 90, "medium": 75, "low": 55}
                        q = quality_map.get(quality, 75)
                        save_kwargs = {"format": "WEBP", "quality": q, "method": 6}
                    if keep_exif and cleaned_exif:
                        save_kwargs["exif"] = cleaned_exif
                    # ICCP chunk: without it a wide-gamut source's numbers
                    # get reinterpreted as sRGB and the image looks washed
                    # out. Pillow writes the profile straight into the
                    # container, so no post-processing is needed here.
                    if icc_profile:
                        save_kwargs["icc_profile"] = icc_profile
                    img.save(output_path, **save_kwargs)

            if progress_callback:
                mode_desc = "lossless" if compression_mode in ("lossless", "resize_only") else f"quality={quality}"
                await progress_callback(f"encoding WebP ({mode_desc})...")

            async with self._semaphore:
                await self._run_bounded(_encode)

            if output_path.exists():
                result["compressed_size"] = os.path.getsize(output_path)
                result["success"] = True
            else:
                result["error"] = "output file not generated"

            self._apply_growth_guard(
                input_path, output_path, "webp", result, keep_exif, cleaned_exif,
                icc_profile,
            )
        except Exception as e:
            result["success"] = False
            result["error"] = str(e)

        return result

    async def _optimize_jpeg(
        self,
        input_path: Path,
        output_path: Path,
        quality: str,
        max_width: int,
        compression_mode: str,
        progress_callback: Optional[Callable],
        *,
        keep_exif: bool = False,
        cleaned_exif: bytes = b"",
        icc_profile: bytes = b"",
    ) -> dict:
        """Lossy JPEG re-encode through mozjpeg's cjpeg, keeping the JPEG
        format (the output stays a .jpg — no conversion to PNG/WebP).

        Why not pngquant/oxipng? Those only understand PNG input, and the
        whole point of this path is a *genuine JPEG* compression. The
        source is decoded with Pillow (EXIF orientation baked in, optional
        resize, alpha composited onto white since JPEG has no alpha
        channel), written to a PPM intermediate, then re-encoded by cjpeg
        — mozjpeg's encoder produces smaller JPEGs than stock libjpeg at
        equal quality (trellis quantization + tuned progressive output).
        The PPM detour is deliberate: several common mozjpeg Windows
        builds ship a cjpeg without the rdpng/rdjpeg readers, so the only
        input format guaranteed to work on every build is PPM/PGM.

        Mode semantics for JPEG (an inherently lossy codec):
        - standard    -> cjpeg -quality from the {high, medium, low} map
        - lossless    -> no true lossless JPEG pass exists; maps to the
          highest cjpeg quality (95) so the re-encode loses as little as
          possible. Name kept for consistency with the other formats;
          the UI/available_modes docs make clear what it actually does.
        - resize_only -> resize first, then encode at that same high
          quality so the savings come from the smaller dimensions rather
          than from dropping image quality.
        """
        original_size = os.path.getsize(input_path)
        result = {
            "success": False,
            "original_size": original_size,
            "compressed_size": original_size,
            "error": None,
            "warning": None,
        }
        if self.cjpeg_path is None:
            result["error"] = "cjpeg (mozjpeg) not found — cannot produce JPEG output"
            return result

        quality_map = {"high": 85, "medium": 75, "low": 60}
        if compression_mode in ("lossless", "resize_only"):
            q = 95
        else:
            q = quality_map.get(quality, 75)

        temp_files: list = []
        try:
            ppm_path = output_path.with_suffix(".src.ppm")
            tmp_jpg = output_path.with_suffix(".cjpeg.jpg")
            temp_files = [ppm_path, tmp_jpg]

            def _prepare_ppm():
                with Image.open(input_path) as opened:
                    # Same orientation bake-in as _ensure_png / _optimize_webp
                    # — a portrait phone JPEG must not come out sideways.
                    img = ImageOps.exif_transpose(opened)
                    if img.mode == "CMYK":
                        img = cmyk_to_rgb(img)
                    elif img.mode not in ("RGB", "L", "I;16"):
                        # RGBA/LA/PA/P carry an alpha channel JPEG can't
                        # hold — composite transparency onto white instead
                        # of a bare convert("RGB"), which renders it black.
                        rgba = img.convert("RGBA")
                        bg = Image.new("RGB", rgba.size, (255, 255, 255))
                        bg.paste(rgba, mask=rgba.getchannel("A"))
                        img = bg

                    if max_width > 0:
                        new_size = fit_width(img.size, max_width)
                        if new_size:
                            img = img.resize(new_size, Image.Resampling.LANCZOS)

                    # cjpeg starts fresh from the PPM so metadata can't
                    # survive (matches the keep_exif=False strip-everything
                    # default). When keep_exif=True the cleaned copy is
                    # re-attached after encoding via _finalize_jpeg_exif.
                    img.info.pop("exif", None)
                    img.save(ppm_path, format="PPM")

            if progress_callback:
                mode_desc = "near-lossless" if compression_mode in ("lossless", "resize_only") else f"quality={q}"
                await progress_callback(f"encoding JPEG via mozjpeg ({mode_desc})...")
            await self._run_bounded(_prepare_ppm)

            async with self._semaphore:
                proc = await asyncio.create_subprocess_exec(
                    str(self.cjpeg_path),
                    "-quality", str(q),
                    "-progressive",
                    "-optimize",
                    "-outfile", str(tmp_jpg),
                    str(ppm_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr, timed_out = await self._communicate_with_timeout(proc, timeout=await asyncio.to_thread(self._timeout_for, input_path))

            if timed_out:
                # Killed cjpeg can leave a partial file at -outfile.
                try:
                    tmp_jpg.unlink(missing_ok=True)
                except Exception:
                    pass

            if tmp_jpg.exists():
                # The PPM intermediate carries pixels only, so cjpeg's output
                # is metadata-free. Both segments are re-attached here, in the
                # order a camera writes them (APP2 profile, then APP1 Exif).
                if icc_profile:
                    self._embed_jpeg_icc(tmp_jpg, icc_profile)
                if keep_exif and cleaned_exif:
                    self._finalize_jpeg_exif(tmp_jpg, cleaned_exif)
                tmp_jpg.replace(output_path)
            else:
                msg = stderr.decode().strip()
                if progress_callback:
                    await progress_callback(f"cjpeg: {msg} (skipped)")
                result["warning"] = msg or "cjpeg produced no output"
                result["error"] = "cjpeg did not produce an output file"
                return result

            if output_path.exists():
                result["compressed_size"] = os.path.getsize(output_path)
                result["success"] = True
            else:
                result["error"] = "output file not generated"

            self._apply_growth_guard(
                input_path, output_path, "jpg", result, keep_exif, cleaned_exif,
                icc_profile,
            )
        except Exception as e:
            result["success"] = False
            result["error"] = str(e)
        finally:
            for f in temp_files:
                try:
                    if f.exists() and f != output_path:
                        f.unlink()
                except Exception:
                    pass

        return result

    @staticmethod
    def _finalize_jpeg_exif(jpg_path: Path, exif_bytes: bytes) -> None:
        """Attach cleaned EXIF to a finished JPEG by inserting a single APP1
        'Exif' segment right after the SOI marker.

        cjpeg writes metadata-free output from the PPM intermediate, so the
        finished .jpg only ever carries the EXIF we inject here (preserving
        the keep_exif=False strip-everything default). JPEG APP1 Exif
        payloads are `Exif\\x00\\x00` followed by raw TIFF; cleaned_exif
        from _capture_cleaned_exif is already the raw TIFF (prefix
        stripped), so the prefix is re-added here. Best-effort — never
        raises: a failed metadata attach must not turn a successful
        compression into a failure."""
        if not exif_bytes:
            return
        if exif_bytes[:1] not in (b"I", b"M"):
            return
        try:
            data = jpg_path.read_bytes()
            if data[:2] != b"\xff\xd8":  # not a JPEG (no SOI marker) — bail
                return
            payload = b"Exif\x00\x00" + exif_bytes
            segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
            jpg_path.write_bytes(data[:2] + segment + data[2:])
        except Exception:
            pass

    @staticmethod
    def _embed_jpeg_icc(jpg_path: Path, icc_bytes: bytes) -> None:
        """Attach an ICC profile to a finished JPEG as APP2 'ICC_PROFILE'
        segment(s) right after SOI.

        An ICC segment's payload is limited to 65535 bytes including its own
        length field, so a real profile (Adobe RGB is ~560 KB) is split into
        chunks of at most 65519 bytes, each tagged with a 1-based sequence
        number and the total count — the layout ICC.1:2001 §B.4 specifies and
        every colour-managed reader expects. A profile needing more than 255
        chunks cannot be represented and is skipped rather than written
        wrongly. Best-effort — never raises: losing colour management must not
        turn a successful compression into a failure."""
        if not icc_bytes:
            return
        try:
            data = jpg_path.read_bytes()
            if data[:2] != b"\xff\xd8":  # no SOI — not a JPEG, leave it alone
                return
            step = 65519
            chunks = [icc_bytes[i:i + step] for i in range(0, len(icc_bytes), step)]
            if len(chunks) > 255:
                return
            segments = bytearray()
            for index, chunk in enumerate(chunks, start=1):
                payload = b"ICC_PROFILE\x00" + bytes((index, len(chunks))) + chunk
                segments += b"\xff\xe2" + struct.pack(">H", len(payload) + 2) + payload
            # Keep a leading JFIF APP0 directly after SOI (the JFIF spec
            # requires it there); the profile goes right behind it.
            at = 2
            if data[2:4] == b"\xff\xe0" and len(data) >= 6:
                at = 4 + struct.unpack(">H", data[4:6])[0]
            jpg_path.write_bytes(data[:at] + bytes(segments) + data[at:])
        except Exception:
            pass

    async def _optimize_avif(
        self,
        input_path: Path,
        output_path: Path,
        quality: str,
        max_width: int,
        compression_mode: str,
        progress_callback: Optional[Callable],
        *,
        keep_exif: bool = False,
        cleaned_exif: bytes = b"",
        icc_profile: bytes = b"",
    ) -> dict:
        """AVIF encode through avifenc. Source is decoded with Pillow (EXIF
        orientation baked in, optional resize) and written to a PNG
        intermediate, then re-encoded by avifenc.

        The intermediate used to be a ".ppm" file under the assumption that
        avifenc needed Y4M/PPM for reliability, but real avifenc builds
        (verified against the official libavif CLI, v1.3.0) only accept
        `input.[jpg|jpeg|png|y4m]` — PPM isn't recognized at all, so every
        AVIF encode failed unconditionally. PNG is lossless (so it doesn't
        cost any quality versus the old approach) and, unlike a Y4M/PPM
        intermediate, carries an alpha channel straight through, so alpha
        is now preserved instead of being composited onto white — AVIF
        supports transparency natively, so there's no reason to throw it
        away like the JPEG intermediate has to.

        Mode semantics:
        - standard    -> avifenc -q from the {high, medium, low} map
        - lossless    -> avifenc --lossless (true lossless AVIF)
        - resize_only -> avifenc --lossless after resizing, same as the
          lossless branch — AVIF supports true lossless just like PNG/WebP,
          so (like those two, and unlike JPEG, which has no lossless mode
          and has to settle for its highest quality instead) "Resize Only"
          means exactly that: savings come only from the smaller
          dimensions, never from a quality/palette change.
        """
        original_size = os.path.getsize(input_path)
        result = {
            "success": False,
            "original_size": original_size,
            "compressed_size": original_size,
            "error": None,
            "warning": None,
        }
        if self.avifenc_path is None:
            result["error"] = "avifenc not found — cannot produce AVIF output"
            return result

        quality_map = {"high": 80, "medium": 60, "low": 40}
        # resize_only groups with lossless (not with the quality_map branch
        # below) — see the docstring: AVIF has a real --lossless mode, so
        # unlike JPEG there's no reason for "Resize Only" to cost any
        # quality here.
        is_lossless = compression_mode in ("lossless", "resize_only")
        q = quality_map.get(quality, 60) if not is_lossless else None

        temp_files: list = []
        try:
            intermediate_path = output_path.with_suffix(".src.png")
            tmp_avif = output_path.with_suffix(".avifenc.avif")
            temp_files = [intermediate_path, tmp_avif]

            def _prepare_intermediate():
                with Image.open(input_path) as opened:
                    img = ImageOps.exif_transpose(opened)
                    if img.mode == "CMYK":
                        img = cmyk_to_rgb(img)
                    elif img.mode not in ("RGB", "RGBA", "L"):
                        # Anything else (P/palette, LA, etc.) — normalize to
                        # RGBA so a possible alpha channel survives; avifenc
                        # reads alpha straight from the PNG.
                        img = img.convert("RGBA")
                    elif img.mode == "L":
                        img = img.convert("RGB")

                    if max_width > 0:
                        new_size = fit_width(img.size, max_width)
                        if new_size:
                            img = img.resize(new_size, Image.Resampling.LANCZOS)

                    img.info.pop("exif", None)
                    img.info.pop("icc_profile", None)  # see _ensure_png
                    # The profile rides in the PNG's iCCP chunk; avifenc reads
                    # the embedded profile from its input and copies it into
                    # the AVIF's colr box (that is what its --ignore-icc flag
                    # is there to turn off), so no extra command-line plumbing
                    # is needed — and no risk from flag support in older builds.
                    save_kwargs = {"icc_profile": icc_profile} if icc_profile else {}
                    img.save(intermediate_path, format="PNG", **save_kwargs)

            if progress_callback:
                mode_desc = "lossless" if is_lossless else f"quality={q}"
                await progress_callback(f"encoding AVIF via avifenc ({mode_desc})...")
            await self._run_bounded(_prepare_intermediate)

            cmd = [
                str(self.avifenc_path),
            ]
            if is_lossless:
                cmd += ["--lossless"]
            else:
                # avifenc has no long-form "--quality" flag — it's
                # -q/--qcolor (verified against the official CLI's --help).
                # The old "--quality" flag was simply unrecognized, so every
                # non-lossless AVIF encode failed on top of the PPM issue
                # above.
                cmd += ["-q", str(q)]
            cmd += ["--speed", "6"]
            if keep_exif and cleaned_exif:
                exif_path = output_path.with_suffix(".exif")
                exif_path.write_bytes(cleaned_exif)
                temp_files.append(exif_path)
                cmd += ["--exif", str(exif_path)]
            cmd += ["-o", str(tmp_avif), str(intermediate_path)]

            async with self._semaphore:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr, timed_out = await self._communicate_with_timeout(proc, timeout=await asyncio.to_thread(self._timeout_for, input_path))

            if timed_out:
                # Killed avifenc can leave a partial file at -o; the
                # size>0 check below isn't enough on its own to catch a
                # truncated-but-nonempty AVIF bitstream.
                try:
                    tmp_avif.unlink(missing_ok=True)
                except Exception:
                    pass

            if tmp_avif.exists() and tmp_avif.stat().st_size > 0:
                tmp_avif.replace(output_path)
            else:
                msg = stderr.decode().strip()
                if progress_callback:
                    await progress_callback(f"avifenc: {msg} (skipped)")
                result["warning"] = msg or "avifenc produced no output"
                result["error"] = "avifenc did not produce an output file"
                return result

            if output_path.exists():
                result["compressed_size"] = os.path.getsize(output_path)
                result["success"] = True
            else:
                result["error"] = "output file not generated"
        except Exception as e:
            result["success"] = False
            result["error"] = str(e)
        finally:
            for f in temp_files:
                try:
                    if f.exists() and f != output_path:
                        f.unlink()
                except Exception:
                    pass

        return result
