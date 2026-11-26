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
import re
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

import PIL
from PIL import Image


def settings_fingerprint(
    quality: str, compression_mode: str, max_width: int,
    protected_colors: Optional[list], dithering: bool, keep_exif: bool, output_format: str,
    keep_icc: bool = True,
) -> str:
    """Fingerprints the effective per-file compression settings that
    produced (or would produce) a given output. Used by skip_existing to
    tell "the source hasn't changed AND these are the same settings that
    made this file last time" apart from "the source happens to be
    unchanged but the user asked for something different this run" - the
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
        "keep_icc": bool(keep_icc),
        "output_format": output_format,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _jpeg_has_end_marker(path: Path) -> bool:
    """A finished JPEG ends with the EOI marker (FF D9); trailing zero
    padding some writers add is tolerated."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - 64))
        tail = f.read().rstrip(b"\x00")
    return tail.endswith(b"\xff\xd9")


def _webp_riff_size_ok(path: Path) -> bool:
    """The RIFF header declares the payload size; a file shorter than that
    was cut off. Trailing bytes beyond it are tolerated."""
    with open(path, "rb") as f:
        head = f.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return False
    declared = int.from_bytes(head[4:8], "little") + 8
    return os.path.getsize(path) >= declared


def is_valid_reusable_output(path: Path, expected_format: str) -> bool:
    """Best-effort corruption check for a skip_existing reuse candidate.

    A non-empty file isn't strong enough evidence that it's safe to reuse:
    if pngquant/oxipng was killed mid-write on a previous run (crash, OOM
    kill, power loss), it can leave behind a file that's non-empty but
    truncated or structurally broken. Reusing that file would silently
    hand the user a corrupt "optimized" image instead of recompressing it.

    Uses Pillow's verify() (structural/header check, doesn't decode full
    pixel data - cheap) and confirms the format actually matches what
    this run expects, so e.g. a stray same-named file from a different
    output_format never gets reused by mistake."""
    try:
        with Image.open(path) as img:
            img.verify()
            fmt = (img.format or "").upper()
        # verify() is a no-op for JPEG and only checks headers for WebP, so
        # add the cheap structural checks it misses, then a full decode.
        # A half-written file is the main thing this guards against.
        if fmt == "JPEG" and not _jpeg_has_end_marker(path):
            return False
        if fmt == "WEBP" and not _webp_riff_size_ok(path):
            return False
        if fmt in ("JPEG", "WEBP"):
            with Image.open(path) as img:
                img.load()
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
#
# v2: animated WebP/APNG and multi-page TIFF are refused instead of being
#     flattened to their first frame. Entries from v1 may hold that
#     flattened output, and a probe happens before optimize_image() gets to
#     the frame check, so an animated source would otherwise still be served
#     a one-frame result — exactly the silent animation loss that fix
#     removed. The settings fingerprint cannot express "this policy
#     changed", so the whole cache is invalidated once.
CACHE_SCHEMA_VERSION = 2

# Output formats the CLI cache may serve. tests/test_determinism.py is the
# gate: a format whose real encoder is found non-deterministic there must be
# removed from this set (and noted in the README).
CACHEABLE_FORMATS = frozenset({"png", "jpg", "webp", "avif"})

# CLI default for --cache-max-size-mb (0 = unlimited). Shared with argparse so
# the "flag without --cache-dir" hint and the post-run warning cannot drift
# from the flag's own default.
DEFAULT_CACHE_MAX_MB = 1024

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

# `--cache-dir` is user-supplied, so it may point at an existing folder full
# of unrelated files. Only paths shaped exactly like the ones this module
# writes are ever counted, swept or evicted: a shard is a two-hex-digit
# directory, an entry is "<64 hex key>.<format>", and a temp file is an entry
# name plus ".tmp-<pid>-<8 hex>". Everything else is left alone. The format
# list comes from CACHEABLE_FORMATS so adding a cacheable format cannot leave
# its entries unevictable or unsweepable.
_CACHE_EXTENSIONS = "|".join(sorted(CACHEABLE_FORMATS))
_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")
_ENTRY_RE = re.compile(rf"^[0-9a-f]{{64}}\.(?:{_CACHE_EXTENSIONS})$")
_TMP_RE = re.compile(
    rf"^[0-9a-f]{{64}}\.(?:{_CACHE_EXTENSIONS})\.tmp-\d+-[0-9a-f]{{8}}$"
)

# Retries for os.replace() on Windows, where replacing a destination that is
# momentarily open for reading (probe() validating or copying an entry) or
# being replaced by another writer fails with PermissionError instead of
# waiting. Backoff is short: every retry is already an unusual collision.
_REPLACE_ATTEMPTS = 6
_REPLACE_BACKOFF_SECONDS = 0.005


def _publish(tmp: Path, entry: Path, output_format: str) -> None:
    """Atomically move a finished temp file into place.

    os.replace() is atomic on every platform, but on Windows it can fail
    with PermissionError while the destination is open elsewhere: a reader
    (probe) has the entry open, or another writer is replacing the same key
    at the same moment. Retry briefly; when the final attempt still fails
    but the entry now exists AND passes the same structural check a hit
    would, another writer has already published a complete file for this
    key, which is just as valid a result as ours, so there is nothing left
    to do. An entry that exists but fails the check (a corrupt leftover that
    probe() just treated as a miss) is not a result: the PermissionError is
    re-raised so the caller reports the failed write instead of silently
    leaving the bad entry in place to miss again on every run."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, entry)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                if entry.is_file() and is_valid_reusable_output(entry, output_format):
                    return
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (2 ** attempt))


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
    temp file and moved into place with os.replace, so a reader never
    observes a partial file. On Windows a replace can transiently fail while
    the destination is open (a reader copying it, or another writer); store()
    retries and treats "another writer already published an entry" as
    success, so last write wins whenever both succeed and any complete entry
    is a valid result for the key.
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
        them deleted in the very run that wrote them (the returned path also
        exists when another writer published the entry first)."""
        entry = self.entry_path(key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        tmp = entry.with_name(f"{entry.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            shutil.copyfile(produced, tmp)
            _publish(tmp, entry, self.output_format)
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
        number of files removed.

        Only names this module writes are considered (see _TMP_RE): the cache
        directory is user-supplied and may contain unrelated files."""
        if not self.root.is_dir():
            return 0
        now = time.time()
        removed = 0
        for shard in self._shards():
            for f in shard.iterdir():
                if not _TMP_RE.match(f.name) or not f.name.startswith(shard.name):
                    continue
                try:
                    st = f.stat()
                except OSError:
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
        exceeded because of them. They are ordinary candidates again on the
        next run, where LRU order decides (they are the newest, so they go
        last).

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
                # Only real cache entries count and only they may be
                # deleted: the directory is user-supplied and may hold
                # unrelated files. Temp files are sweep_stale_tmp()'s job;
                # they are never counted toward the cap nor chosen as
                # victims.
                if not _ENTRY_RE.match(f.name) or not f.name.startswith(shard.name):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
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
            if _SHARD_RE.match(d.name) and d.is_dir():
                yield d
