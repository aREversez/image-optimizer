# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [1.0.4] - 2026-08-19

### Added
- **Screenshot mode** (`compression_mode: "screenshot"`, PNG only). A
  dedicated one-click preset for software/UI screenshots, which Standard/
  Lossless/Resize Only all handle poorly: Standard's default quality
  ranges visibly shifted colors on gradients/shadows (measured mean
  color error 0.33, gradient-region error up to 48/255, clearly visible
  banding); Lossless alone only cut a synthetic 4K UI screenshot by 43%;
  Resize Only blurs exactly the UI text a screenshot is usually kept
  around to reference. Screenshot mode instead runs pngquant at a much
  tighter quality floor than Standard's 50-65/65-80/80-100, with
  dithering forced off, since screenshots' native color count is
  naturally close to 256 already (flat UI regions + anti-aliased text
  edges). Measured on a synthetic 3840x2160 UI screenshot (flat
  sidebar/toolbar, ~60 lines of anti-aliased text, a gradient panel):
  75.3% size reduction with mean color error 0.007 and zero resolution
  loss. Dithering was measured to make things worse for this content,
  not better, so it's hardcoded off rather than left as a per-run
  choice. If an individual image's true color complexity is too high to
  fit within 256 colors (e.g. a screenshot with an embedded photo),
  pngquant declines and the file falls back to a plain lossless pass
  automatically. Quality/Max Width/Protect Colors/Dithering don't apply
  to this mode and are disabled in the UI when it's selected; Output
  Format is locked to PNG since the whole technique is PNG-specific.
- **Front-end internationalization (English/Chinese), extensible to more
  languages.** A dictionary-based `I18N = { en: {...}, zh: {...} }` system
  replaces ~150 previously hardcoded English strings across the template,
  including the standalone Compare page (which crosses a page-reload
  boundary via a `?lang=` query param, since it can't share the main
  page's client-side dict). Default language auto-detects from
  `navigator.language` (falls back to English for anything non-Chinese);
  an explicit choice in Settings > Language always wins and persists to
  `localStorage`. Adding a language is one new key in `I18N` plus one
  `<option>` in the language `<select>`, no other code changes needed.
  Scoped to front-end UI text only; server-pushed log lines stay in
  English. 161 keys, verified with zero missing/unused entries in either
  direction.
- **Settings panel restructured into a three-column layout** so Settings
  and Watch Mode are visible without scrolling past the file list, and
  Compression Mode's 4 options no longer overflow the sidebar (now a
  self-contained 2x2 grid). Left column holds only Source Path / Target
  Path; container widens to 1680px with a 1300px breakpoint that drops
  the third column to a full-width row, and the existing 1000px
  breakpoint still collapses everything to one column.
- **"Screenshot preset" button** in Settings — one click sets Quality:
  High, Max Width: 1920px, Mode: Standard, Dithering: Off, the combo that
  measured best for compressing software screenshots without the blur
  Screenshot mode's own resize restriction rules out. A short "Settings
  are saved automatically" caption sits above it, since Quality/Max
  Width/Mode/Dithering/Format already persist to `localStorage` across
  reloads but nothing previously told the user that.

### Changed
- **Screenshot mode's pngquant quality floor loosened from 95-100 to
  75-100.** The tighter floor is a pass/fail gate, not an optimization
  target — content that already clears it (flat UI, icons, moderate
  gradients) produces byte-for-byte identical output either way, but
  content that couldn't clear 95 (embedded photos/video thumbnails,
  richer gradient panels) failed outright and fell all the way through
  to a 0%-gain lossless pass. At 75, that harder content now succeeds
  instead, with resulting color error concentrated almost entirely in
  the photo-like regions rather than the text/UI chrome where a
  screenshot's readability actually lives.

### Fixed
- **`skip_existing` ignored settings changes between runs.** Its reuse
  decision was keyed purely on the source file's mtime vs the existing
  output's, so re-running against an untouched source with a different
  `max_width` (or quality/compression_mode/output_format/
  protected_colors/dithering/keep_exif) silently reused the old output
  produced under the *previous* settings, with no warning that the new
  settings had no effect. A settings fingerprint (quality/mode/width/
  colors/dithering/keep_exif/format) is now stored per output_dir
  alongside the file it produced, and skip_existing only reuses when
  both the source is unchanged AND the fingerprint matches this run's
  effective settings. No recorded fingerprint (e.g. a file that predates
  this feature) is treated as unverifiable and recompressed rather than
  trusted.
- **AVIF's "Resize Only" mode was silently lossy**, unlike every other
  format's Resize Only. PNG and WebP both guarantee Resize Only costs no
  quality beyond the resize itself; AVIF silently encoded at `-q 90`
  even though avifenc has a real `--lossless` mode already used
  correctly for AVIF's own "Lossless" mode, and the quality loss
  happened even when no resize occurred at all. resize_only now shares
  the same `--lossless` path as AVIF's lossless mode, confirmed
  pixel-identical to the source against the real libavif CLI.
- **Watch mode didn't validate `resize_only` requires a Max Width** —
  `/api/optimize` and per-file overrides both reject that combination,
  but `/api/watch/start` let it through and ran indefinitely
  "processing" every detected file with no resize ever applied. Watch
  mode now rejects the same combination with the same error the other
  two entry points already use.
- **Same-format optimization (PNG/JPEG/WebP) could ship a result bigger
  than the source** when the encoder couldn't improve on already-
  near-minimal content — nothing checked the output was actually
  smaller before shipping it. A new growth guard falls back to the
  original's pixel data with EXIF/GPS/XMP/text-comment metadata
  stripped (never re-encoded, so the result is provably never bigger
  than the source), preserving the "GPS is gone by default" guarantee
  even on this fallback path. Scoped to same-format results only — a
  genuine format conversion has no valid original-bytes fallback at the
  target extension.
- **Screenshot mode could balloon a non-screenshot source (e.g. a real
  photo) into a PNG many times larger than the original** instead of
  failing. When pngquant can't hit the quality floor on a source that
  isn't already PNG, that's an actual format conversion in progress
  (jpg -> png), and a lossless PNG re-encode of photographic content is
  inherently much bigger than a lossy JPEG source (reproduced at ~14x on
  a synthetic photo). Now fails cleanly with an actionable message
  instead. A source that's already PNG is unaffected — no format
  conversion happens there, so the existing lossless fallback is
  unchanged.
- **Dark theme's selected-state text contrast regressed to failing WCAG
  AA**, and selecting a Compression Mode option could resize the whole
  button row. A color refinement pass had flipped selected-state text
  from black to white globally (not scoped to dark mode, so it also hit
  light mode) and added `font-weight: 500` on selection, which measurably
  widens text and pushed `#mode-group`'s equal-width columns around.
  Reverted selected-state text to black in both themes (measured contrast
  passes WCAG AA; white text against dark mode's lighter accent measured
  as low as 2.88, failing even the lenient large-text threshold) and
  dropped the weight change — background contrast alone reads clearly as
  "selected". Also switched `#mode-group`'s columns to `minmax(0, 1fr)`
  and gave `.config-group` a definite width, so nothing inside it
  (button labels, notes, anything added later) can expand the panel
  again.
- **Source Path / Target Path sidebar overlapped page content between
  1000px and 1300px viewport widths.** The sidebar's `position: sticky`
  was only reset to `static` below 1000px, but the layout already drops
  to a narrower 2-column arrangement at 1300px — the sidebar kept
  scrolling-pinned behavior appropriate to the wider 3-column layout and
  visually covered the row beneath it. Reproduced and confirmed fixed
  with Playwright screenshots at multiple widths; no change to the
  intentional sticky behavior above 1300px or the single-column layout
  below 1000px.
- Renamed "Output Path" to "Target Path" to actually deliver the
  Source/Target pairing "Select Images" was renamed to "Source Path" for
  in an earlier pass, and fixed a leftover grammar mismatch ("Source
  Path and Output paths are both empty") in the same string.
- Removed remaining em-dashes from user-facing tooltip and notice text
  (28 strings across both languages) for more consistent, less "AI-ish"
  copy; a few Chinese strings also switched from straight quotes to
  proper 【】-bracket conventions for naming UI elements.

## [1.0.3] - 2026-08-13

### Fixed
- **Watch mode's settings were silently frozen at "Start Watching" time,
  with no UI indication.** Watch Mode reuses the same shared Quality /
  Output Format / Compression Mode / Max Width / Protect Colors /
  Dithering / Keep EXIF controls as batch optimize, and those stayed
  fully interactive while a watch was running — changing them looked
  like it should apply, but `_watch_loop` captures the request once at
  start and never re-reads live UI state. The freeze-at-start behavior
  itself is reasonable (re-reading settings mid-run risks a file being
  processed under a half-changed config), so it's kept; the fix is
  purely making the UI honest about it: those controls are now disabled
  and a notice explains they're locked until Watch Mode is stopped and
  restarted.
- **Renaming a file being watched reprocessed it under the new name and
  left the old output behind as an orphan**, since change detection is
  purely path+mtime+size — a rename looks identical to "new file
  appeared." Rather than switching to content hashing (a broader, slower
  change this project has consistently avoided elsewhere — see
  `skip_existing`'s mtime-based staleness check), `FolderWatcher` now
  detects same-cycle renames specifically: if a path disappears and a
  different path appears in the very same poll with an identical
  (mtime, size), that's reported as a rename (`renamed_from`) rather
  than an unrelated new file. `_watch_loop` uses this to delete the old
  output once the new one is written — cleanup only ever fires on a
  confirmed rename. A file that's simply deleted (nothing reappearing to
  match it) is deliberately left alone with no cleanup at all: plenty of
  people delete the original after keeping the compressed output, and a
  lone disappearance must never be allowed to delete anything.
- **AVIF output was completely broken against the real avifenc binary**,
  in two independent ways. libavif CLI (avifenc v1.3.0): (1) the intermediate file was written as
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
- **"Reveal Output Folder" opened behind the browser window** (Windows).
  A bare `explorer <dir>` may silently reuse an already-open window, and
  Windows' foreground-lock rules stop a window spawned by this background
  server process from stealing focus — it appeared only as a taskbar
  flash. Folder-mode reveal now launches `explorer /e,/root,<dir>` (which
  always spawns a fresh window) and then brings it to the foreground via
  the documented `AttachThreadInput` + `SetForegroundWindow` workaround
  (best-effort; the window is open regardless).
- **Standalone exe builds shipped without the JPEG and AVIF encoders.**
  `build_exe.py` only bundled `pngquant.exe` and `oxipng.exe`, so a
  frozen build silently lacked both mozjpeg JPG output and AVIF output
  added since. `cjpeg-static.exe` and `avifenc.exe` are now included in
  the PyInstaller data.

### Added
- **Watch Mode: "Use Select Images / Output paths" button.** Watch Mode
  previously required re-picking or re-typing both folder paths even when
  they were the exact same ones already set up in the main Select
  Images / Output panels. One click now copies the current values across;
  if Output path is empty (e.g. the main panel is using the temp-folder
  default), a notice explains that Watch Mode needs a real persistent
  output folder and doesn't have a temp-folder mode of its own.
- **"Reveal Output Folder" on results** (`POST /api/reveal`). When a
  run has a persistent `output_dir` (not the temp workspace), a single
  Reveal Output Folder button in the Results bar opens the run's output
  folder in the OS file explorer (`explorer /e,/root,` on Windows,
  `open` on macOS, `xdg-open` on Linux) — one folder per run, one
  button, instead of one per image. The backend opens the folder it
  recorded for this run itself (`state.output_dir`); the client sends
  no path, so there's nothing to spoof. The endpoint also accepts a
  specific `final_output_path` for file-level reveal
  (`explorer /select,` / `open -R`) and rejects anything that isn't one
  of the current session's own recorded output files.
- **Files grid sorting.** The Files section's header gains a Sort
  dropdown: scan order, modified (newest/oldest), created
  (newest/oldest), size (large/small). Useful without watch mode —
  sort newest-first to grab the latest screenshots for a manual batch,
  or largest-first to target the biggest files. Scan/upload/resume
  file entries now carry `mtime`/`ctime`; sorting is display-only and
  never changes the selection or the order sent to the server.
- **AVIF output** (`output_format: "avif"`). Selecting AVIF encodes through
  `avifenc` (auto-detected in `bin/` or PATH; without it the `(avif, *)`
  modes drop out of `available_modes()` and the UI warns up front).
  Pillow decodes the source (EXIF transpose, optional resize), writes a
  PNG intermediate (alpha preserved end-to-end), then avifenc encodes the
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