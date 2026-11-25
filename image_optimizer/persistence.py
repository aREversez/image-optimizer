"""Crash-safe file writes shared by every place that persists something.

A result is only real once the complete file is at its destination. Writing
in place (open(dest, "w"), shutil.copy2(src, dest), an encoder pointed at
the final name) leaves a truncated file behind if the process is killed or
the disk fills mid-write, and a later run may reuse it. Everything here
writes a temp file in the destination folder (same filesystem, so the final
step is a rename) and then os.replace()s it into place.
"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Union

# os.replace() is atomic everywhere, but on Windows it fails with
# PermissionError while the destination is momentarily open elsewhere (an
# antivirus scan, a reader). A few short retries cover that.
_REPLACE_ATTEMPTS = 6
_REPLACE_BACKOFF_SECONDS = 0.005


def staging_path(dest: Path) -> Path:
    """A unique hidden sibling of `dest` that keeps dest's suffix.

    The suffix is kept because encoders and Pillow pick the output format
    from the file extension. The leading dot plus the ".part-" marker keep a
    leftover from a crash out of normal listings and out of "already
    optimized" checks that match the real output name."""
    dest = Path(dest)
    return dest.with_name(f".{dest.stem}.part-{os.getpid()}-{uuid.uuid4().hex[:8]}{dest.suffix}")


def replace_with_retry(src: Path, dest: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (2 ** attempt))


def commit_staged(staged: Path, dest: Path) -> None:
    """Move a finished staged file into place. On failure the staged file is
    removed and the error re-raised, so the caller can report the file as
    failed and no partial file is left at (or next to) the destination."""
    try:
        replace_with_retry(staged, dest)
    except BaseException:
        discard(staged)
        raise


def discard(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def atomic_copy(src: Union[str, Path], dest: Union[str, Path]) -> None:
    """copy2 into a staging file next to dest, then replace dest with it."""
    dest = Path(dest)
    staged = staging_path(dest)
    try:
        shutil.copy2(src, staged)
        replace_with_retry(staged, dest)
    except BaseException:
        discard(staged)
        raise


def atomic_write_text(
    path: Union[str, Path], text: str, encoding: str = "utf-8", durable: bool = False,
) -> None:
    """Write text so that a reader (or a crash) sees either the old file or
    the complete new one, never a truncated mix. `durable` also fsyncs the
    data before the rename (survives power loss, costs a disk flush per
    call, so it is off for state files that are rewritten after every image)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        discard(tmp)
        raise
