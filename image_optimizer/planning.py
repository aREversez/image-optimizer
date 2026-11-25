"""Run-planning checks shared by every entry point (CLI, web batch, Watch).

Before this module existed each entry point decided on its own whether an
output folder was safe; only Watch mode did, so the CLI and the web batch
could overwrite originals or re-compress their own output. Anything that
decides "is this run allowed to write here" lives in this file so the entry
points cannot drift apart again.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional


def _norm(p: Path) -> str:
    # resolve() follows symlinks; normcase makes Windows comparisons
    # case-insensitive (Path equality is not, before Python 3.12).
    return os.path.normcase(str(Path(p).resolve()))


def same_path(a: Path, b: Path) -> bool:
    """True if a and b are the same file or folder: equal after resolving
    symlinks and case, or the same inode (hard links, bind mounts)."""
    if _norm(a) == _norm(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def is_inside(child: Path, parent: Path) -> bool:
    """True if child is strictly inside parent (after resolving symlinks)."""
    c, p = _norm(child), _norm(parent)
    return c != p and c.startswith(p.rstrip(os.sep) + os.sep)


def output_dir_conflict(source: Path, output: Path, recursive: bool) -> Optional[str]:
    """Return an error message if writing into `output` while reading
    `source` is unsafe, else None.

    - output is the source folder: originals get replaced by their lossy
      result (same format) or the next scan re-compresses the output
      (different format). Always rejected.
    - output inside source and the scan is recursive: the next run scans
      its own output (out/out/...), adding generation loss. Rejected. With a
      non-recursive scan only the top level is listed, so a nested output
      folder is never re-read and is allowed.

    A source folder nested inside the output folder is deliberately not
    rejected here: nothing is re-read from the output tree. The one way it
    can still hurt (a planned output file that is an original) is caught per
    file by `planned_outputs_hit_sources`.
    """
    if same_path(source, output):
        return (
            f"output folder must not be the source folder ({Path(output).resolve()}): "
            "it would overwrite or re-compress the originals. Pick a different folder."
        )
    if recursive and is_inside(output, source):
        return (
            f"output folder {Path(output).resolve()} is inside the source folder "
            f"{Path(source).resolve()} and the scan is recursive: the next run would "
            "re-compress its own output. Pick a folder outside the source (or use "
            "--no-recursive / turn off recursive scanning)."
        )
    return None


def planned_outputs_hit_sources(
    planned: Iterable[Optional[Path]], sources: Iterable[Path],
) -> Optional[Path]:
    """Return the first planned output path that is the same file as a
    source file (so writing it would destroy an original), else None.
    Compares resolved paths and, for outputs that already exist, the
    (device, inode) identity, so hard links and case differences are caught
    without an O(sources x outputs) scan."""
    sources = list(sources)
    source_keys = {_norm(s) for s in sources}
    source_ids = set()
    for s in sources:
        try:
            st = os.stat(s)
        except OSError:
            continue
        if st.st_ino:
            source_ids.add((st.st_dev, st.st_ino))
    for p in planned:
        if p is None:
            continue
        if _norm(p) in source_keys:
            return p
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_ino and (st.st_dev, st.st_ino) in source_ids:
            return p
    return None
