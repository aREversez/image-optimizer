from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ScanRequest(BaseModel):
    directory: str
    recursive: bool = True


class OptimizeRequest(BaseModel):
    file_ids: list[str] = []
    quality: str = "medium"
    max_width: int = 0
    output_format: str = "png"
    output_dir: str = ""


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
