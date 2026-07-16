from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
HEX_COLOR_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")


class Optimizer:
    def __init__(self, bin_dir: Optional[Path] = None):
        self.bin_dir = Path(bin_dir) if bin_dir else Path(__file__).resolve().parent.parent / "bin"
        self.pngquant_path: Optional[Path] = None
        self.oxipng_path: Optional[Path] = None
        self._detect_binaries()
        self._semaphore = asyncio.Semaphore(4)

    @property
    def ready(self) -> bool:
        return self.pngquant_path is not None

    def _find_binary(self, name: str) -> Optional[Path]:
        try:
            cmd = "where" if sys.platform == "win32" else "which"
            result = subprocess.run([cmd, name], capture_output=True, text=True)
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
        img = Image.open(src)
        if img.mode == "CMYK":
            img = img.convert("RGB")
        img.save(dst, format="PNG")

    @staticmethod
    def _resize_image(src: Path, dst: Path, max_width: int):
        img = Image.open(src)
        w, h = img.size
        if w <= max_width:
            return
        ratio = max_width / w
        new_size = (max_width, int(h * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
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

    async def optimize_png(
        self,
        input_path: Path,
        output_path: Path,
        quality: str = "medium",
        max_width: int = 0,
        compression_mode: str = "standard",
        protected_colors: Optional[list[str]] = None,
        dithering: bool = True,
        progress_callback: Optional[Callable] = None,
    ) -> dict:
        if compression_mode not in ("standard", "lossless", "resize_only"):
            compression_mode = "standard"

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
            loop = asyncio.get_event_loop()

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
            elif compression_mode != "standard" and progress_callback:
                await progress_callback(f"skipping color quantization ({compression_mode} mode)")

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
                    oxipng_tmp.replace(output_path)
                else:
                    shutil.copy2(working_path, output_path)
            else:
                if working_path != output_path:
                    shutil.copy2(working_path, output_path)

            for f in temp_files:
                try:
                    if f.exists() and f != output_path:
                        f.unlink()
                except Exception:
                    pass

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

        return result
