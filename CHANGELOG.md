# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed
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