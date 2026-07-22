from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ScanRequest(BaseModel):
    directory: str
    recursive: bool = True


class OptimizeRequest(BaseModel):
    # None = "no filter, process everything"; [] = "process nothing" (all files
    # were deselected). These used to be conflated because Python treats an
    # empty list as falsy — keep them distinct.
    file_ids: Optional[list[str]] = None
    quality: str = "medium"
    max_width: int = 0
    output_format: str = "png"
    output_dir: str = ""
    # "standard" = pngquant + oxipng (current default behavior)
    # "lossless" = oxipng only, no color quantization
    # "resize_only" = no color quantization, relies on max_width for savings
    compression_mode: str = "standard"
    # Hex colors (e.g. "#2ecc71") to prioritize keeping in the palette when
    # compression_mode == "standard". Best-effort, not a hard guarantee.
    protected_colors: list[str] = []
    # Whether pngquant should dither (Floyd-Steinberg). Only relevant when
    # compression_mode == "standard".
    dithering: bool = True


class FileInfo(BaseModel):
    id: str
    name: str
    path: str
    size: int
    thumbnail: str


class ResultInfo(BaseModel):
    id: str
    name: str
    original_path: str
    original_size: int
    compressed_size: int
    savings: int
    savings_percent: float
    success: bool
    error: Optional[str] = None
    warning: Optional[str] = None
    result_url: Optional[str] = None


class RecentRemoveRequest(BaseModel):
    key: str
    value: str


class RecentClearRequest(BaseModel):
    key: str
