from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from PIL import Image


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
    def _resize_image(src: Path, dst: Path, max_width: int):
        img = Image.open(src)
        w, h = img.size
        if w <= max_width:
            return
        ratio = max_width / w
        new_size = (max_width, int(h * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
        img.save(dst, format="PNG")

    async def optimize_png(
        self,
        input_path: Path,
        output_path: Path,
        quality: str = "medium",
        max_width: int = 0,
        progress_callback: Optional[Callable] = None,
    ) -> dict:
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

            if max_width > 0:
                resized = output_path.with_suffix(f".resized{input_path.suffix}")
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._resize_image, input_path, resized, max_width)
                if resized.exists():
                    if progress_callback:
                        await progress_callback(f"resized to <= {max_width}px")
                    working_path = resized
                    temp_files.append(resized)

            if self.pngquant_path:
                pngquant_tmp = output_path.with_suffix(".pngquant.png")
                quality_map = {"high": "80-100", "medium": "65-80", "low": "50-65"}
                q_range = quality_map.get(quality, "65-80")

                if progress_callback:
                    await progress_callback("color quantization...")

                async with self._semaphore:
                    proc = await asyncio.create_subprocess_exec(
                        str(self.pngquant_path),
                        "--quality", q_range,
                        "--speed", "1",
                        "--strip",
                        "--skip-if-larger",
                        "--output", str(pngquant_tmp),
                        str(working_path),
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
