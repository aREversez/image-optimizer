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
    # Retry mode: re-run only the given file_ids (the previously-failed ones)
    # without wiping the output dir or discarding earlier successful results.
    # Default False keeps the normal "fresh full run" behavior (clears output,
    # replaces results) that every existing caller/test relies on.
    retry: bool = False
    # Skip files whose optimized output already exists in output_dir: instead
    # of recompressing, reuse the existing file (copied back into ws/output so
    # Compare/preview and the download ZIP still work). Only meaningful with a
    # persistent output_dir set; a no-op otherwise (the temp workspace starts
    # empty each run). Default False keeps the normal recompress-everything run.
    skip_existing: bool = False
    # Retain a curated EXIF subset (camera make/model, date, exposure) in the
    # output while dropping Orientation (already baked into pixels by
    # exif_transpose — re-writing it would double-rotate), GPS (privacy), and
    # MakerNote (bulk). Default False preserves the historical strip-everything
    # behavior (pngquant --strip / oxipng --strip safe / WebP with no exif=).
    keep_exif: bool = False
    # Per-file parameter override: {file_id: {field: value, ...}}. Only the
    # listed fields override the top-level defaults for that one file; every
    # other field falls back to the top-level value. Only file_ids the user
    # manually expanded "advanced settings" on appear here — the common case
    # (one global setting) sends an empty dict. Field names are fixed (see
    # OVERRIDEABLE_FIELDS in main.py) so the request shape can't drift between
    # frontend and backend; unknown fields are rejected with 400.
    overrides: dict[str, dict] = {}


class FileInfo(BaseModel):
    """Shape of one entry in AppState.files / the /api/scan|upload payloads.
    Documentation-only — those dicts are built by hand for flexibility, but
    this model is the reference for what consumers can rely on."""
    id: str
    name: str
    path: str
    size: int
    thumbnail: str


class ResultInfo(BaseModel):
    """Shape of one entry in AppState.results / the /api/progress payload.
    Documentation-only, same reasoning as FileInfo."""
    id: str
    name: str
    original_path: str
    # Output path relative to the workspace output dir — may differ from
    # name+suffix when a filename collision was disambiguated.
    output_name: Optional[str] = None
    original_size: int
    compressed_size: int
    savings: int
    savings_percent: float
    success: bool
    error: Optional[str] = None
    warning: Optional[str] = None


class RecentRemoveRequest(BaseModel):
    key: str
    value: str


class RecentClearRequest(BaseModel):
    key: str


class PreviewRequest(BaseModel):
    """Single-file pre-compression dry run. Carries the same compression
    parameters as OptimizeRequest for one file, but no batch/output
    controls (no output_dir, skip_existing, retry, file_ids list) — a
    preview never persists anything. See OPTIMIZATION_PLAN.md §2."""
    file_id: str
    quality: str = "medium"
    max_width: int = 0
    output_format: str = "png"
    compression_mode: str = "standard"
    protected_colors: list[str] = []
    dithering: bool = True
    keep_exif: bool = False


class WatchRequest(BaseModel):
    """Parameters for Watch mode: watch a directory and auto-optimize every
    image that appears (or changes) in it, writing results into output_dir.
    Carries the same compression parameters as OptimizeRequest, minus the
    batch-specific controls (file_ids / retry / skip_existing / overrides) —
    Watch uses one global setting set for everything it picks up.

    output_dir is required: Watch has no temp-workspace/ZIP flow, it is
    inherently "optimize into a persistent folder"."""
    directory: str
    recursive: bool = True
    # Optimize files already present when watching starts, not just ones
    # that appear afterwards.
    process_existing: bool = False
    quality: str = "medium"
    max_width: int = 0
    output_format: str = "png"
    compression_mode: str = "standard"
    protected_colors: list[str] = []
    dithering: bool = True
    keep_exif: bool = False
    output_dir: str = ""
