# Changelog

All notable changes to this project will be documented in this file.

## [1.0.1] - 2026-07-22

### Changed
- Two-column layout: left panel (paths + settings) sticky, right panel (files + progress + results)
- Overlay compare view is now the default
- "Include subfolders" unchecked by default
- Recent folders show per-item × button and "Clear all" button
- Quality controls disabled in lossless/resize-only modes
- Max Width disabled in standard mode
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
