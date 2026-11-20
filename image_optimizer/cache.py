"""Shared helpers for reusing previously produced output files.

`settings_fingerprint` and `is_valid_reusable_output` started life in main.py
for the GUI's skip_existing feature; they live here so the CLI content-hash
cache can use the exact same definitions (one source of truth for "which
settings change the pixels" and "is this file safe to hand back").
main.py re-imports them under their old private names.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

import PIL
from PIL import Image


def settings_fingerprint(
    quality: str, compression_mode: str, max_width: int,
    protected_colors: Optional[list], dithering: bool, keep_exif: bool, output_format: str,
) -> str:
    """Fingerprints the effective per-file compression settings that
    produced (or would produce) a given output. Used by skip_existing to
    tell "the source hasn't changed AND these are the same settings that
    made this file last time" apart from "the source happens to be
    unchanged but the user asked for something different this run" — the
    two look identical if you only check the source's mtime, which is all
    the reuse check used to do.

    Order-independent on protected_colors (case/whitespace-normalized) so
    ["#FF0000", "#00ff00"] and ["#00FF00", "#ff0000"] fingerprint the same,
    matching how the color-map step itself treats them."""
    colors = sorted((c or "").strip().lower() for c in (protected_colors or []))
    payload = json.dumps({
        "quality": quality,
        "compression_mode": compression_mode,
        "max_width": max_width,
        "protected_colors": colors,
        "dithering": bool(dithering),
        "keep_exif": bool(keep_exif),
        "output_format": output_format,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def is_valid_reusable_output(path: Path, expected_format: str) -> bool:
    """Best-effort corruption check for a skip_existing reuse candidate.

    A non-empty file isn't strong enough evidence that it's safe to reuse:
    if pngquant/oxipng was killed mid-write on a previous run (crash, OOM
    kill, power loss), it can leave behind a file that's non-empty but
    truncated or structurally broken. Reusing that file would silently
    hand the user a corrupt "optimized" image instead of recompressing it.

    Uses Pillow's verify() (structural/header check, doesn't decode full
    pixel data — cheap) and confirms the format actually matches what
    this run expects, so e.g. a stray same-named file from a different
    output_format never gets reused by mistake."""
    try:
        with Image.open(path) as img:
            img.verify()
            fmt = (img.format or "").upper()
    except Exception:
        return False
    expected = {
        "png": "PNG",
        "webp": "WEBP",
        "jpg": "JPEG",
        "jpeg": "JPEG",
        "avif": "AVIF",
    }.get(expected_format, "PNG")
    return fmt == expected


# Bump when pipeline logic changes in a way that alters output bytes for the
# same input + settings + encoders. Folded into every cache key, so a bump
# orphans (and LRU eventually reclaims) all older entries.
CACHE_SCHEMA_VERSION = 1

# Output formats the CLI cache may serve. tests/test_determinism.py is the
# gate: a format whose real encoder is found non-deterministic there must be
# removed from this set (and noted in the README).
CACHEABLE_FORMATS = frozenset({"png", "jpg", "webp", "avif"})

# Which external encoder binaries (Optimizer attribute names) feed each
# output format. WebP is Pillow-only, so it is covered by the Pillow version
# in the encoder fingerprint instead.
_FORMAT_ENCODERS = {
    "png": ("pngquant_path", "oxipng_path"),
    "jpg": ("cjpeg_path",),
    "webp": (),
    "avif": ("avifenc_path",),
}

# Stale temp files from a crashed writer are swept at the end of every CLI
# run after this long (ContentCache.sweep_stale_tmp), independent of the
# size cap.
_STALE_TMP_SECONDS = 3600


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def encoder_fingerprint(optimizer, output_format: str) -> str:
    """Identifies the encoders that will produce this run's output: the
    content hash of each external binary the format uses, plus the Pillow
    version (it does the WebP encode and every resize/convert step). Swapping
    a bundled or PATH binary for another build therefore invalidates the cache
    on its own. Computed once per run by the caller."""
    parts = [f"pillow={PIL.__version__}"]
    for attr in _FORMAT_ENCODERS.get(output_format, ()):
        path = getattr(optimizer, attr, None)
        if path is None:
            parts.append(f"{attr}=none")
            continue
        try:
            parts.append(f"{attr}={file_sha256(Path(path))}")
        except OSError:
            # Unreadable binary: a unique value per run means nothing is
            # ever served from or trusted against a fingerprint we could
            # not compute.
            parts.append(f"{attr}=unreadable-{uuid.uuid4().hex}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class CacheProbe(NamedTuple):
    key: str
    original_size: int
    # Size of the file copied to the output path, or None on a miss.
    hit_size: Optional[int]


class ContentCache:
    """Content-addressed store for finished compression results, independent
    of any output directory (so a caller that uses a fresh temp output dir on
    every run still gets hits).

    Layout: <cache_dir>/<key[:2]>/<key>.<ext>
    Key:    sha256(input bytes sha256 + settings fingerprint + encoder
            fingerprint + CACHE_SCHEMA_VERSION)

    All methods are blocking file I/O; call them via asyncio.to_thread.
    Concurrent writers to the same key are fine: entries are written to a
    temp file and moved into place with os.replace, so last write wins and a
    reader never observes a partial file.
    """

    def __init__(self, cache_dir: Path, output_format: str, settings_fp: str, encoder_fp: str):
        self.root = Path(cache_dir)
        self.output_format = output_format
        self._ext = "." + output_format
        self._settings_fp = settings_fp
        self._encoder_fp = encoder_fp

    def key_for_digest(self, input_sha256: str) -> str:
        payload = "|".join(
            [input_sha256, self._settings_fp, self._encoder_fp, str(CACHE_SCHEMA_VERSION)]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def entry_path(self, key: str) -> Path:
        return self.root / key[:2] / (key + self._ext)

    def probe(self, input_path: Path, out_path: Path) -> CacheProbe:
        """Hash the input, and on a valid cache entry copy it to out_path.
        An entry that fails structural validation (truncated, wrong format)
        counts as a miss; the caller recompresses and store() overwrites it."""
        original_size = os.path.getsize(input_path)
        key = self.key_for_digest(file_sha256(input_path))
        entry = self.entry_path(key)
        if not entry.is_file() or not is_valid_reusable_output(entry, self.output_format):
            return CacheProbe(key, original_size, None)
        shutil.copy2(entry, out_path)
        try:
            # Recency for LRU eviction.
            os.utime(entry, None)
        except OSError:
            pass
        return CacheProbe(key, original_size, os.path.getsize(out_path))

    def store(self, key: str, produced: Path) -> Path:
        """Atomically publish a successful result under `key`. Never call
        for a failed result. Returns the entry path, so the caller can hand
        this run's fresh entries to evict(protect=...) instead of having
        them deleted in the very run that wrote them."""
        entry = self.entry_path(key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        tmp = entry.with_name(f"{entry.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            shutil.copyfile(produced, tmp)
            os.replace(tmp, entry)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        return entry

    def sweep_stale_tmp(self) -> int:
        """Delete .tmp-* leftovers from crashed writers older than
        _STALE_TMP_SECONDS. Called at the end of every CLI run regardless of
        the size cap: with --cache-max-size-mb 0 evict() never runs, and
        without this sweep those files would accumulate forever. Returns the
        number of files removed."""
        if not self.root.is_dir():
            return 0
        now = time.time()
        removed = 0
        for shard in self._shards():
            for f in shard.iterdir():
                try:
                    st = f.stat()
                except OSError:
                    continue
                if not f.is_file() or ".tmp-" not in f.name:
                    continue
                if now - st.st_mtime > _STALE_TMP_SECONDS:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed

    def evict(
        self,
        max_bytes: int,
        protect: Optional[set] = None,
        stats: Optional[dict] = None,
    ) -> int:
        """Delete least-recently-used entries (oldest mtime first) until the
        total is at or under max_bytes. max_bytes <= 0 means unlimited.
        Returns the number of entries removed. Entries whose source image is
        gone are never touched by hits, so they age out here naturally.

        `protect` lists entry paths that must not be chosen as victims: the
        entries written during the current run. Without this, a cap smaller
        than one entry (or already-full cache plus a fresh write) would
        delete the just-written entry in the very run that produced it, so
        the cache could never serve a single hit; the cap may transiently be
        exceeded because of them, and they are the oldest-next-run victims
        if nothing hits them meanwhile.

        `stats`, when given a dict, receives {"total_after": bytes} so the
        caller can warn when the cap is still exceeded (e.g. a single entry
        larger than the cap, or everything left protected)."""
        if max_bytes <= 0 or not self.root.is_dir():
            return 0
        protected = protect or set()
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for shard in self._shards():
            for f in shard.iterdir():
                try:
                    st = f.stat()
                except OSError:
                    continue
                if not f.is_file():
                    continue
                if ".tmp-" in f.name:
                    # Crash leftovers are sweep_stale_tmp()'s job; never
                    # counted toward the cap, never chosen as victims.
                    continue
                entries.append((st.st_mtime, st.st_size, f))
                total += st.st_size
        removed = 0
        for _mtime, size, f in sorted(entries, key=lambda e: e[0]):
            if total <= max_bytes:
                break
            if f in protected:
                continue
            try:
                f.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        if stats is not None:
            stats["total_after"] = total
        return removed

    def _shards(self) -> Iterable[Path]:
        for d in self.root.iterdir():
            if d.is_dir() and len(d.name) == 2:
                yield d
