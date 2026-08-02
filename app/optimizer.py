from __future__ import annotations

import asyncio
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageOps

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
HEX_COLOR_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")

# EXIF tag ids used by the retention logic (see OPTIMIZATION_PLAN.md §1).
# Orientation is stripped because `exif_transpose` already bakes it into
# the pixel grid — re-writing it would double-rotate in any reader that
# honors EXIF orientation. GPSInfo is dropped for privacy (images may be
# publicly shared). MakerNote is vendor-specific bulk with no display value.
EXIF_TAG_ORIENTATION = 0x0112
EXIF_TAG_GPS_INFO = 0x8825
EXIF_TAG_EXIF_IFD = 0x8769
EXIF_TAG_MAKER_NOTE = 0x927C


class Optimizer:
    def __init__(self, bin_dir: Optional[Path] = None, max_concurrency: int = 4):
        if bin_dir:
            self.bin_dir = Path(bin_dir)
        elif getattr(sys, "frozen", False):
            self.bin_dir = Path(sys._MEIPASS) / "bin"
        else:
            self.bin_dir = Path(__file__).resolve().parent.parent / "bin"
        self.pngquant_path: Optional[Path] = None
        self.oxipng_path: Optional[Path] = None
        self._detect_binaries()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

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

        This is the API new code should call instead of `ready` when deciding
        whether to enable a particular (format, mode) option in the UI or
        accept it on the server side.
        """
        if self.pngquant_path is not None:
            return {("png", "standard"), ("png", "lossless"), ("png", "resize_only"),
                    ("webp", "standard"), ("webp", "lossless"), ("webp", "resize_only")}
        return {("png", "lossless"), ("png", "resize_only"),
                ("webp", "standard"), ("webp", "lossless"), ("webp", "resize_only")}

    def _find_binary(self, name: str) -> Optional[Path]:
        try:
            cmd = "where" if sys.platform == "win32" else "which"
            # timeout: a hung `where`/`which` (e.g. a dead network drive on
            # PATH) must not wedge detection — and with it /api/health —
            # forever.
            result = subprocess.run([cmd, name], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return Path(result.stdout.strip().splitlines()[0])
        except Exception:
            pass
        exe_name = f"{name}.exe" if sys.platform == "win32" else name
        local = self.bin_dir / exe_name
        if local.exists():
            return local.resolve()
        return None

    def _detect_binaries(self):
        self.pngquant_path = self._find_binary("pngquant")
        self.oxipng_path = self._find_binary("oxipng")

    @staticmethod
    def _is_png(path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                return f.read(8) == PNG_MAGIC
        except Exception:
            return False

    @staticmethod
    def _ensure_png(src: Path, dst: Path):
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
                img = img.convert("RGB")
            # Drop EXIF at normalization. The compress pipeline
            # (pngquant --strip / oxipng --strip safe) strips it anyway in
            # production, but the test stand-in binaries only copy bytes
            # through — removing it here keeps default (keep_exif=False)
            # behavior deterministic across both. A cleaned copy is
            # re-attached at the very end of the pipeline only when
            # keep_exif=True (see _finalize_png_exif / _optimize_webp).
            img.info.pop("exif", None)
            img.save(dst, format="PNG")

    @staticmethod
    def _resize_image(src: Path, dst: Path, max_width: int):
        with Image.open(src) as img:
            w, h = img.size
            if w <= max_width:
                return
            ratio = max_width / w
            new_size = (max_width, int(h * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            img.info.pop("exif", None)
            img.save(dst, format="PNG")

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

    # --- EXIF retention helpers (see OPTIMIZATION_PLAN.md §1) ---
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
            iend = data.find(b"IEND")
            if iend < 8:
                return
            iend_start = iend - 4  # back up over IEND's 4-byte length field
            png_path.write_bytes(data[:iend_start] + chunk + data[iend_start:])
        except Exception:
            pass

    async def optimize_png(
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
    ) -> dict:
        if compression_mode not in ("standard", "lossless", "resize_only"):
            compression_mode = "standard"

        # Capture a cleaned EXIF copy from the source up front (before any
        # pipeline step that strips/transposes it). Attached to the output
        # at the very end — Orientation is already baked into pixels by
        # exif_transpose, so the retained EXIF never carries Orientation
        # (double-rotation guard).
        cleaned_exif = b""
        if keep_exif:
            cleaned_exif = self._capture_cleaned_exif(input_path)

        if output_format == "webp":
            return await self._optimize_webp(
                input_path, output_path, quality, max_width, compression_mode,
                progress_callback, keep_exif=keep_exif, cleaned_exif=cleaned_exif,
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
            loop = asyncio.get_running_loop()

            # Step 0: guarantee we're working with a real PNG from here on, regardless
            # of the source format and regardless of whether a resize happens next.
            if not self._is_png(working_path):
                normalized = output_path.with_suffix(".src.png")
                await loop.run_in_executor(None, self._ensure_png, working_path, normalized)
                if normalized.exists():
                    if progress_callback:
                        await progress_callback("converted to PNG")
                    working_path = normalized
                    temp_files.append(normalized)

            if max_width > 0:
                resized = output_path.with_suffix(".resized.png")
                await loop.run_in_executor(None, self._resize_image, working_path, resized, max_width)
                if resized.exists():
                    if progress_callback:
                        await progress_callback(f"resized to <= {max_width}px")
                    working_path = resized
                    temp_files.append(resized)

            if compression_mode == "standard" and self.pngquant_path:
                pngquant_tmp = output_path.with_suffix(".pngquant.png")
                quality_map = {"high": "80-100", "medium": "65-80", "low": "50-65"}
                q_range = quality_map.get(quality, "65-80")

                if progress_callback:
                    await progress_callback("color quantization...")

                cmd = [
                    str(self.pngquant_path),
                    "--quality", q_range,
                    "--speed", "1",
                    "--strip",
                    "--skip-if-larger",
                ]
                if not dithering:
                    cmd.append("--nofs")

                map_path: Optional[Path] = None
                if protected_colors:
                    map_path = output_path.with_suffix(".mapcolors.png")
                    made = await loop.run_in_executor(None, self._make_color_map, protected_colors, map_path)
                    if made:
                        cmd += ["--map", str(map_path)]
                        temp_files.append(map_path)
                    else:
                        map_path = None

                cmd += ["--output", str(pngquant_tmp), str(working_path)]

                async with self._semaphore:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()

                if pngquant_tmp.exists():
                    working_path = pngquant_tmp
                    temp_files.append(pngquant_tmp)
                elif proc.returncode != 0:
                    msg = stderr.decode().strip()
                    if progress_callback:
                        await progress_callback(f"pngquant: {msg} (skipped)")
                    result["warning"] = msg
            elif progress_callback:
                if compression_mode != "standard":
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
                    await proc.communicate()

                if oxipng_tmp.exists():
                    # Registered as a temp BEFORE the replace: if replace()
                    # fails (e.g. output_path unexpectedly blocked), the
                    # finally-sweep still removes it. On success the file
                    # has moved, so the sweep's exists() check skips it.
                    temp_files.append(oxipng_tmp)
                    oxipng_tmp.replace(output_path)
                else:
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
            loop = asyncio.get_running_loop()

            def _encode():
                with Image.open(input_path) as opened:
                    # Same EXIF-orientation bake-in as _ensure_png — WebP
                    # output shouldn't come out sideways either.
                    img = ImageOps.exif_transpose(opened)
                    if img.mode == "CMYK":
                        img = img.convert("RGB")

                    if max_width > 0:
                        w, h = img.size
                        if w > max_width:
                            ratio = max_width / w
                            img = img.resize((max_width, int(h * ratio)), Image.Resampling.LANCZOS)

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
                    img.save(output_path, **save_kwargs)

            if progress_callback:
                mode_desc = "lossless" if compression_mode in ("lossless", "resize_only") else f"quality={quality}"
                await progress_callback(f"encoding WebP ({mode_desc})...")

            async with self._semaphore:
                await loop.run_in_executor(None, _encode)

            if output_path.exists():
                result["compressed_size"] = os.path.getsize(output_path)
                result["success"] = True
            else:
                result["error"] = "output file not generated"
        except Exception as e:
            result["success"] = False
            result["error"] = str(e)

        return result