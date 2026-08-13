"""Polling folder watcher for Watch mode.

Zero-dependency alternative to `watchdog`: rather than subscribing to
filesystem events (which adds a native dependency, needs to keep up with
per-OS backends, and must survive the binary-bundle story this app has),
we re-scan the directory tree on a timer and diff against the last
snapshot. Files that are new (or whose mtime/size changed since last seen)
are emitted to the consumer.

Design points:
- Change detection is snapshot-diff on (mtime, size), the same signal every
  incremental build tool uses (make, rsync -u, the app's own skip_existing
  logic). Writing a file atomically (write tmp + rename) produces exactly
  one stat change, so Watch picks it up exactly once.
- Rename detection: if a path disappears and a different path appears in
  the *same* poll cycle with an identical (mtime, size), that's treated as
  a rename of the same content rather than an unrelated delete+create, and
  the new-path event carries `renamed_from` so the caller can clean up
  whatever it produced for the old name (e.g. a stale optimized output)
  instead of leaving an orphan behind. A lone disappearance — nothing
  reappearing with a matching stat — is never reported at all: the watcher
  only emits new/changed files, so a plain delete (no matching reappearance)
  can't be mistaken for something requiring cleanup. This matters because
  many people delete the original after keeping the compressed output, and
  that must never cascade into deleting the output too.
- The scan itself is blocking I/O and runs on a thread executor; new events
  are pushed onto an asyncio.Queue so a slow consumer (lossy encoding of a
  large batch) never stalls the polling loop or misses files arriving
  mid-encode.
- `stop()` is cooperative: the poll loop notices _stop after its current
  sleep, drains the queue, and exits.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

# Same extensions the scan flow accepts (see app/main.py SCAN_EXTS) — case-
# folded so "photo.PNG" is caught on case-sensitive filesystems too.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


class FolderWatcher:
    def __init__(
        self,
        directory: Path,
        recursive: bool = True,
        interval: float = 2.0,
        process_existing: bool = False,
        on_change: Optional[Callable[[str, Path, Optional[str]], Awaitable[None]]] = None,
    ):
        self.directory = directory
        self.recursive = recursive
        self.interval = max(0.5, interval)
        self.process_existing = process_existing
        self.on_change = on_change
        self._known: dict[str, tuple[float, int]] = {}
        self._stop = False
        self._queue: Optional[asyncio.Queue] = None
        self._started = False
        self.files_seen = 0

    def _scan(self) -> list[tuple[str, Path, tuple[float, int]]]:
        """One blocking directory walk. Returns (rel_path, abs_path,
        (mtime, size)) for every image file under self.directory, sorted by
        rel_path so events arrive in deterministic order."""
        iter_method = self.directory.rglob if self.recursive else self.directory.glob
        out: list[tuple[str, Path, tuple[float, int]]] = []
        for p in iter_method("*"):
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
                continue
            try:
                rel = str(p.relative_to(self.directory))
            except ValueError:
                continue
            try:
                s = p.stat()
            except OSError:
                continue  # disappeared mid-walk — skip it this round
            out.append((rel, p, (s.st_mtime, s.st_size)))
        return sorted(out, key=lambda item: item[0])

    @staticmethod
    async def _enqueue(queue: asyncio.Queue, item):
        await queue.put(item)

    async def _once(self) -> None:
        """Scan, fire on_change for every new/changed file, update snapshot.

        First call (baseline): record everything as already-known so nothing
        pre-existing is treated as new — unless process_existing is set, in
        which case the existing files are emitted once up front.

        Subsequent calls: a brand-new path whose (mtime, size) exactly
        matches a path that just disappeared is treated as a rename — the
        event's renamed_from carries the old relative path so the caller
        can clean up what it produced for that name. Every other
        disappearance (no matching reappearance in this same cycle) is
        simply dropped from the snapshot and never reported — deletions on
        their own are not the watcher's concern."""
        items = await asyncio.to_thread(self._scan)
        queue = self._queue
        snapshot = {rel: stat for rel, _, stat in items}
        if not self._started:
            self._started = True
            self._known = snapshot
            if self.process_existing:
                for rel, _path, _stat in items:
                    self.files_seen += 1
                    await self._enqueue(queue, (rel, _stat, None))
            return

        # Candidates for "renamed from": paths present last snapshot, gone
        # from this one. Grouped by stat so a same-cycle reappearance with
        # an identical (mtime, size) can be matched back to the old name.
        disappeared_by_stat: dict[tuple[float, int], list[str]] = {}
        for rel, stat in self._known.items():
            if rel not in snapshot:
                disappeared_by_stat.setdefault(stat, []).append(rel)

        for rel, _path, stat in items:
            prev = self._known.get(rel)
            if prev is None:
                renamed_from = None
                candidates = disappeared_by_stat.get(stat)
                if candidates:
                    # FIFO match — good enough for a same-size/same-mtime
                    # tie-break among multiple simultaneous renames; the
                    # caller only uses this to clean up an old output, not
                    # for anything correctness-critical.
                    renamed_from = candidates.pop(0)
                    if not candidates:
                        del disappeared_by_stat[stat]
                self.files_seen += 1
                await self._enqueue(queue, (rel, stat, renamed_from))
            elif prev != stat:
                self.files_seen += 1
                await self._enqueue(queue, (rel, stat, None))
        self._known = snapshot

    async def _consume(self) -> None:
        queue = self._queue
        while True:
            rel, stat, renamed_from = await queue.get()
            try:
                await self.on_change(rel, Path(self.directory) / rel, renamed_from)
            finally:
                queue.task_done()

    async def run(self) -> None:
        """Start the poll loop. Emits are delivered serially to on_change.

        Normal exit (stop() called): drains the queue — already-discovered
        files finish processing — then shuts the consumer down. External
        cancellation (e.g. session sweeps / server shutdown calling
        task.cancel()): the consumer is dropped immediately so shutdown
        doesn't stall on a long encode."""
        if self._stop or self._queue is not None:
            return
        self._queue = asyncio.Queue()
        consumer = asyncio.create_task(self._consume())
        try:
            await self._once()
            while not self._stop:
                await asyncio.sleep(self.interval)
                await self._once()
        except asyncio.CancelledError:
            consumer.cancel()
            self._queue = None
            raise
        else:
            await self._queue.join()
        finally:
            consumer.cancel()
            self._queue = None

    def stop(self) -> None:
        """Ask the poll loop to shut down at its next opportunity."""
        self._stop = True