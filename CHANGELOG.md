# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed
- **AVIF output never actually worked.** Verified against the real
  libavif CLI (avifenc v1.3.0): (1) the intermediate file was written as
  `.src.ppm`, but avifenc only recognizes `input.[jpg|jpeg|png|y4m]` — PPM
  is rejected outright ("Unrecognized file format"), so every single AVIF
  encode failed regardless of quality/mode. (2) The quality flag was
  `--quality`, which doesn't exist on avifenc — the real flag is
  `-q`/`--qcolor`; this alone would have broken every non-lossless AVIF
  encode even with (1) fixed. Both are now fixed: the intermediate is a
  PNG (also lossless, so no quality regression versus the old approach),
  and the CLI invocation uses `-q`. As a side benefit, since PNG (unlike
  the old PPM path) carries an alpha channel, AVIF output now preserves
  transparency instead of compositing it onto white — AVIF supports alpha
  natively, so there was no reason to throw it away. The test suite's
  fake avifenc previously accepted any input/flags, so it couldn't catch
  either bug; it now validates the same way the real binary does.
- **Watch mode could get stuck in an infinite self-reprocessing loop** if
  `output_dir` was the watched directory itself, or a subfolder of it
  while watching recursively (e.g. "watch my screenshots folder and
  shrink new screenshots in place" — a natural thing to want). Writing
  optimized output back into the watched tree makes the poller detect
  that output as a new/changed file on the very next scan and reprocess
  it, forever — confirmed empirically: a single dropped-in file was
  reprocessed 7 times in 3 seconds with no upper bound. `POST
  /api/watch/start` now rejects an `output_dir` that would create this
  overlap, with a message explaining why and how to avoid it.
- **Batch resume state was a single global file shared by every browser
  tab/session.** `~/.image-optimizer/batch_state.json` didn't distinguish
  between unrelated batches, even though the rest of the app's state
  (`state.files`, `state.results`, `state.workspace`) is per-session. Two
  tabs running batches into different output folders would silently
  clobber each other's resume data — e.g. tab B finishing a normal batch
  called the same global `_clear_batch_state()`, wiping out tab A's
  still-interrupted batch and its resume banner along with it. Batch
  state is now stored one file per output_dir under
  `~/.image-optimizer/batches/` (filename derived from a hash of the
  resolved output_dir), keyed by output_dir rather than session id so
  resume still survives a server restart (session ids are re-issued on
  restart; output_dir isn't). `GET /api/batch-state` accepts an optional
  `?output_dir=` to check one specific batch, or with no query param
  reports the most recently updated batch that's still unfinished across
  everything on disk. `POST /api/optimize` with `resume: true` now
  requires `output_dir` up front (400 if missing) since that's what
  identifies which saved batch to resume.

### Added
- **AVIF output** (`output_format: "avif"`). Selecting AVIF encodes through
  `avifenc` (auto-detected in `bin/` or PATH; without it the `(avif, *)`
  modes drop out of `available_modes()` and the UI warns up front).
  Pillow decodes the source (EXIF transpose, alpha composited onto white,
  optional resize), writes a PPM intermediate, then avifenc encodes the
  final AVIF. `keep_exif` passes cleaned EXIF via avifenc's `--exif`
  sidecar. Quality map: high=80, medium=60, low=40; lossless uses
  `--lossless`; resize_only encodes at q=90 after downscaling.
- **Batch resume** (`GET /api/batch-state`, `POST /api/optimize` with
  `resume: true`). Progress is persisted (one file per output_dir, under
  `~/.image-optimizer/batches/` — see Fixed below) after each file
  completes. If the server crashes or the user cancels mid-batch, the
  frontend shows a banner on reload ("Found unfinished batch: X/Y
  completed") with a Resume button that re-processes only the
  pending/failed files into the same output directory. Batch state is
  auto-cleared when every file finishes successfully. Runs without a
  persistent output_dir don't create batch state (the temp workspace
  disappears anyway).
- **Watch mode** (`POST /api/watch/start`, `/stop`, `/status`, `/events`).
  Monitor a folder and auto-optimize new or changed images as they appear.
  `FolderWatcher` polls on a timer, diffs by (mtime, size), and fires a
  per-file handler that runs the full optimizer pipeline into a chosen
  output directory. SSE endpoint streams live logs and status updates;
  the frontend falls back to polling when SSE is unavailable. Watch mode
  and batch optimize are mutually exclusive per session (400 guard).
  Options: recursive, process-existing, quality/mode/format reuse the
  global settings card. Errors on individual files are logged but never
  stop the watcher.
- **True JPEG optimization** (`output_format: "jpg"`). Selecting JPG for a
  batch of `.jpg` files now genuinely re-compresses them lossily with
  mozjpeg's `cjpeg` while *keeping the JPEG format* — previously JPEG/BMP/
  TIFF/WebP sources were silently transcoded to PNG or WebP, so "just make
  this batch of JPEGs smaller, same format" wasn't possible. Pillow decodes
  (EXIF orientation baked in, alpha composited onto white, optional resize)
  to a PPM intermediate, then `cjpeg -quality N -progressive -optimize`
  re-encodes. `keep_exif` works too: cleaned EXIF is re-injected as an APP1
  segment after SOI. `cjpeg`/`cjpeg-static` is auto-detected in `bin/` or
  PATH; without it the `(jpg, *)` modes drop out of `available_modes()` and
  Start warns up front. "Lossless"/"Resize Only" for JPEG map to the highest
  cjpeg quality (95) — JPEG is inherently lossy, and the UI says so.
- **In-app settings panel** (`GET /api/settings`, `PUT /api/settings`).
  Gear icon in the header opens a modal with Host, Port, Concurrent
  Workers, Thumbnail Workers, Workspace Cleanup Delay, and Session Idle
  Timeout fields. PUT accepts partial updates, validates each field, and
  persists to `~/.image-optimizer/config.json`. Keys that require a
  server restart (host, port, workers) are flagged in the response so
  the UI can inform the user.

## [August 2026 review batch]

Six features from the August 2026 project review. Each shipped test-first
with a regression test that exposes its cross-cutting interaction with the
existing batch state machine (`skip_existing` / `retry` / narrowed
`file_ids` / `output_version`). Full design notes in `OPTIMIZATION_PLAN.md`.

### Added
- **EXIF metadata retention** (`keep_exif`, default off). When on, a curated
  EXIF subset (camera make/model, date, exposure) is retained in the output;
  Orientation (already baked into pixels by `exif_transpose` — re-writing it
  would double-rotate), GPS (privacy), and MakerNote (bulk) are dropped.
  Works for both PNG (eXIf chunk injected after pngquant/oxipng strip) and
  WebP (Pillow `exif=` arg). UI toggle next to "Skip files already in the
  output folder".
- **Pre-compression preview** (`POST /api/preview-optimize`). Single-file
  dry run that projects the compressed size under the current settings
  without touching `state.results` / `output_version` / the download ZIP
  (writes to `ws/preview`, not `ws/output`, then deletes). Per-file
  "Preview" button in the file grid.
- **Per-file parameter override** (`overrides: {file_id: {field: value}}`).
  A per-file form (gear icon on each file card) overrides quality / mode /
  width / dithering / protected colors / keep_exif / output format for just
  that file; everything else inherits the global setting. Fixed field set
  (unknown fields rejected with 400). `out_paths` collision resolution and
  `skip_existing` reuse both honor the per-file format.
- **Pause / Resume** (soft pause). `POST /api/pause` stops scheduling new
  files; in-flight pngquant/oxipng subprocesses finish naturally (no kill).
  `POST /api/resume` drains the rest. Cancel takes precedence over pause.
  `/api/progress` reports `paused`. Pause/Resume buttons in the progress bar.
- **SSE progress transport** (`GET /api/events`). Streams `result` / `log` /
  `done` events with `id: r{R}:l{L}` cursor ids; a client reconnects with
  `Last-Event-ID` to replay only the tail (reattach after a refresh). The
  HTTP polling path stays as an automatic fallback (SSE connect failure or
  sustained silence → polling). Shared render helpers keep both transports
  visually identical.
- **Split compress vs thumbnail concurrency.** New `thumbnail_workers`
  config key and `--thumbnail-workers` CLI flag size the scan-thumbnail
  pool independently from `concurrent_workers` / `--workers` (compress).
  `tests/bench_workers.py` benchmarks scan+compress across knob combos.

### Tests
- 47 new tests across `test_exif.py`, `test_preview_optimize.py`,
  `test_overrides.py`, `test_pause.py`, `test_worker_split.py`,
  `test_sse.py`. The full suite (147 tests) passes.

## [1.0.2] - 2026-07-28

### Security
- `/api/preview` now enforces the same workspace-ownership check as every
  other workspace-scoped endpoint and HTML-escapes the URLs it embeds.
  Previously an arbitrary `ws_name` path segment was reflected unescaped
  into the page — a crafted URL could execute script in the app's origin
  and harvest `APP_TOKEN`, defeating the CSRF defense.
- `/api/upload` reported the raw client-supplied filename as the file's
  `name`; `_process_files` joins that name onto the output directory, so a
  `../`-carrying filename (from a non-browser client) could write optimized
  output outside the workspace. The name is now reduced to a sanitized
  basename, and `_process_files` additionally refuses any output path that
  resolves outside the output directory (defense in depth).
- Frontend now HTML-escapes log lines and per-file error messages before
  rendering (both embed file names, which may legally contain `<`/`>` on
  non-Windows filesystems).

### Fixed
- Output filename collision: `photo.png` and `photo.jpg` both mapped to
  `photo.png` after the output-format suffix swap and silently overwrote
  each other mid-batch (both reported as successes). Output paths are now
  pre-assigned per batch with deterministic disambiguation (`photo_2.png`),
  case-folded to match Windows/macOS filesystem semantics; results carry
  the actual `output_name`, which `/api/preview` uses instead of
  re-deriving the path. This also removes the concurrent temp-file
  collisions (`photo.resized.png` etc.) that followed from shared stems.
- Optimizer temp files (`*.src.png`, `*.pngquant.png`, ...) are now swept
  in a `finally` block — an exception mid-pipeline used to leave them in
  `ws/output/`, where `/api/download` would zip them into the user's
  archive.
- `/api/health` no longer blocks the event loop: binary re-detection shells
  out to `where`/`which`, which now runs via `asyncio.to_thread` (and with
  a 5 s subprocess timeout so a hung PATH entry can't wedge health checks).
- `checkHealth()` no longer unconditionally disables the Start button —
  button state is owned solely by `updateStartButtonState()`.
- Thumbnails of transparent images (RGBA/LA/PA/P) are composited onto a
  white background instead of going black (`convert("RGB")` drops alpha);
  LA-mode images previously failed the JPEG save outright and got no
  thumbnail at all. Pillow images are opened via context managers so file
  handles are released promptly on error paths (Windows file locking).
- EXIF orientation is baked in when converting non-PNG sources and when
  encoding WebP — portrait phone photos no longer come out sideways.
- `_scan_images` missed `.webp` entirely and mixed-case extensions
  (`photo.Png`) on case-sensitive filesystems; it now does a single
  directory walk with a case-folded suffix check.
- Invalid `quality` values are rejected with a 400 like the other enum
  fields instead of silently falling back to "medium".
- `_find_free_port` sets `SO_REUSEADDR` on non-Windows so a lingering
  TIME_WAIT socket from a previous run doesn't skip a perfectly usable
  port.
- Corrected the session-isolation comment: the cookie is browser-wide, not
  per-tab — two tabs share one session and rely on the 400 guards.
- Concurrent file processing: an unhandled exception from a single file
  (e.g. a permission error writing output) could make `asyncio.gather()`
  return early while the other in-flight workers kept running orphaned in
  the background — their results were silently lost, and a new run
  started too soon could see stale writes from the orphaned tasks. A
  single file's failure now degrades to a normal failed-file result
  instead of taking down the batch.
- Persistent completion notice ("Compression complete") was auto-dismissing
  after 5 seconds, which defeated its purpose — it's meant to stay visible
  until manually dismissed or a new scan/optimize starts, specifically so
  it's still there if you step away for a while. Regular (non-persistent)
  toast notices still auto-dismiss after 5 seconds as intended.
- Files card showed completely blank before any scan (title + "Select
  all" checkbox, nothing else). Added a proper empty-state placeholder.
- Removed leftover `.drop-zone` CSS for drag-and-drop, a feature that was
  already removed from the actual UI in an earlier change.

### Added
- Page refresh now restores the session's files/results from `/api/state`
  (previously unused) and re-attaches to a still-running batch instead of
  coming back blank.
- `POST /api/reset`: the Reset button also clears server-side session
  state (files, results, workspace) — previously it only wiped the page,
  so the server kept everything alive until the idle sweep.
- `/api/optimize` returns a `warning` field (surfaced as a toast) when the
  requested format×mode combination is degraded — e.g. Standard-mode PNG
  without pngquant silently fell back to lossless-only compression with no
  user-visible signal. `Optimizer.available_modes()` is now actually used.
- Uploads stream to disk in 1 MiB chunks instead of buffering whole files
  in memory; the file picker now also accepts TIFF and WebP, matching what
  the scanner and optimizer support.
- Scan progress bar: scanning a large folder now shows live progress
  (`GET /api/scan-progress`) instead of the UI appearing frozen until
  every thumbnail finishes. Thumbnail generation also now runs on the
  same 4-worker concurrent pool as compression, which is a real speedup,
  not just a progress indicator.

## [1.0.1] - 2026-07-22

### Changed
- Two-column layout: left panel (paths + settings) sticky, right panel (files + progress + results)
- Overlay compare view is now the default
- "Include subfolders" unchecked by default
- Recent folders show per-item × button and "Clear all" button
- Quality controls disabled in lossless/resize-only modes
- All notifications auto-dismiss after 5 seconds
- Cancel button hidden until compression starts
- Placeholder text no longer wraps; improved spacing

### Added
- `POST /api/recent/remove` and `POST /api/recent/clear` endpoints
- Persistent completion toast with dismiss

### Fixed
- Duplicate "### Fixed" heading in changelog

## [1.0.0] - 2026-07-18

### Added
- Scan local folders for PNG/JPG/BMP/TIFF images
- Drag & drop or browse to upload individual files
- Three compression modes: standard (lossy), lossless, resize-only
- Color protection: preserve specific hex colors during quantization
- Dithering toggle (Floyd-Steinberg) for pngquant
- Side-by-side and overlay preview before/after compression
- Live log output during optimization
- ZIP download of all results
- Auto-detection of pngquant and oxipng binaries
- Cross-platform support (Windows, macOS, Linux; binaries bundled for Windows)
- `build_exe.py` — PyInstaller script to produce a standalone Windows exe (`--onedir`)
- Favicon, app icon, and README banner images in `assets/`
- PyInstaller frozen-build support (`sys._MEIPASS` path resolution)

### Fixed
- Path traversal vulnerabilities in file-serve endpoints
- XSS in file name display on results and preview pages
- Race condition allowing state reset during active optimization
- Fire-and-forget task crash hiding optimizer errors
- Busy port detection and fallback
- tkinter crash in headless environments
- Preview page broken for uploaded (non-scanned) files
- Log color-coding mismatch between backend and frontend