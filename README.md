<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/banner.png">
  <img alt="Image Optimizer" src="assets/banner.png" width="100%">
</picture>

# Image Optimizer

Batch image compression with a local web UI. Uses **pngquant** (lossy PNG
quantization) and **oxipng** (lossless strip/optimization) behind a clean
interface, plus **WebP via Pillow**, **AVIF via avifenc**, and — with
**mozjpeg's cjpeg** — true lossy **JPEG** re-compression that keeps the
JPEG format. Includes a dedicated **Screenshot mode** for near-lossless
compression of UI/software screenshots.

## Features

- **Local folder scanning** — select a directory, scan recursively (optional), and pick which files to optimize
- **File upload** — drag or browse to upload individual images (PNG, JPG, BMP, TIFF, WebP)
- **File sorting** — sort scanned files by modified/created time (newest/oldest) or size (large/small), handy for grabbing the latest screenshots or targeting the biggest files
- **Watch Mode** — monitor a folder and auto-optimize new or changed images as they appear, with live SSE-streamed logs and rename detection
- **Batch resume** — progress is persisted per output folder; an interrupted batch (crash, cancel, server restart) can be resumed where it left off
- **Pause / Resume / Cancel** mid-batch, plus per-file compression parameter overrides and single-file pre-compression preview
- **Before/after preview** — overlay and side-by-side compare views
- **Four compression modes:**
  - `standard` — pngquant + oxipng, best size reduction
  - `lossless` — oxipng only, no color loss
  - `resize only` — scale down, no quantization
  - `screenshot` — near-lossless pngquant pass tuned for UI/software screenshots (PNG-only, hardcoded dithering off)
- **Output format** — PNG (via pngquant/oxipng), WebP (via Pillow's built-in encoder, no extra binary needed), JPG (via mozjpeg's `cjpeg`, keeping JPEG rather than converting), or AVIF (via `avifenc`)
- **Keep-format JPEG optimization** — select the JPG output format on a batch of `.jpg` files and they're genuinely re-compressed lossily by mozjpeg while staying JPEG (requires `cjpeg` in `bin/`, see `bin/README.md`)
- **EXIF retention** (opt-in) — keep a curated EXIF subset (camera, date, exposure; GPS stripped for privacy)
- **Color protection** — list hex colors to preserve in the palette
- **Dithering toggle** — smoother gradients (standard mode)
- **Results sorting & filtering** — sort results by compression %, compressed size, or bytes saved; filter to show all/succeeded/failed
- **Dark mode** — manual theme toggle or auto-follow OS preference
- **Internationalization** — English and 中文 (Chinese) built-in, extensible to more languages
- **SSE progress transport** — real-time server-sent events for progress/logs with automatic polling fallback
- **Output directory** — save results to any folder (or use the built-in ZIP download), with a Reveal Output Folder button to jump straight to it in the OS file explorer
- **Skip already optimized** — re-running against the same output folder reuses existing outputs instead of recompressing unchanged files (settings-aware: changing quality/mode/format forces a real recompress)
- **Recent folder history** — quick-access chips with per-item removal and clear all
- **In-app settings** — gear icon opens a settings panel (host, port, workers, timeouts, language) persisted to `config.json`
- **Session state restoration** — page refresh restores files/results and re-attaches to a still-running batch
- **Auto-detect binaries** — finds `pngquant`/`oxipng`/`cjpeg`/`avifenc` in `bin/` folder or system PATH
- **CLI mode** — `--source`/`--output` runs a batch headlessly (no browser, no server) and exits with a process exit code, for scripts and CI; shares the exact same compression pipeline and settings validation as the web UI
- **Cross-platform** — Windows, macOS, Linux (pre-built exe for Windows; macOS/Linux users need `pngquant`/`oxipng`/`cjpeg`/`avifenc` on PATH)

## Quick Start

### Option 1: Standalone EXE (Windows, no Python required)

Download the latest release from the [Releases page](https://github.com/aREversez/image-optimizer/releases) and run `ImageOptimizer.exe`.

> **Windows SmartScreen:** The exe is unsigned (code signing certificates cost $200+/year for open-source projects). On first run, SmartScreen may show "Windows protected your PC" — click **More info** → **Run anyway**. This is normal for unsigned open-source software.

### Option 2: From source

```bash
git clone https://github.com/aREversez/image-optimizer.git
cd image-optimizer
pip install -r requirements.txt
python -m app          # or: start.bat
```

Open http://127.0.0.1:8090 in your browser.

The first run will auto-detect `pngquant` and `oxipng` in the `bin/` folder and system PATH. See `bin/README.md` if a binary is missing.

## Usage

1. **Enter a folder path** — type or browse to select a directory, then click *Scan Folder* (or drag & drop / upload individual files)
2. **Review files** — deselect any you want to skip, sort by date/size, adjust quality/width/mode (per-file overrides via each card's gear icon, or click *Preview* for a single-file dry run)
3. **Protect colors** (optional) — add hex colors like `#2ecc71` to preserve
4. **Start** — click *Start* and watch the live progress log (SSE-streamed)
5. **Compare & download** — click any result's *Compare* button for before/after preview, then download the ZIP or click *Reveal Output Folder* (when an output folder is set). Sort/filter results by compression ratio or size.

Alternatively, set up **Watch Mode** (sidebar card) to auto-compress new images dropped into a folder, and use *Resume* on page load if a previous batch was interrupted.

## CLI Mode

For scripting, batch jobs, or anything that shouldn't need a browser: pass
`--source` and the app runs headlessly (no web server, no browser) and
exits with a process exit code (`0` if everything succeeded, `1` if
anything failed — safe to check in a script or CI step).

```
python -m app --source "D:\screenshots" --output "D:\screenshots-compressed"
```

```
python -m app --source ./raw --output ./compressed --quality high --max-width 1920 --mode standard --no-dithering
```

CLI mode runs every image through the exact same compression pipeline and
settings validation as the web UI and Watch Mode — no separate "CLI
version" of the logic to drift out of sync, so the same `--quality`/
`--mode`/etc. combination produces the same result either way.

- `--source` — directory to compress (required to trigger CLI mode)
- `--output` — output directory (required with `--source`)
- `--quality {high,medium,low}` — Standard/Screenshot mode color-quality floor (default `medium`)
- `--format {png,jpg,webp,avif}` — output format (default `png`)
- `--mode {standard,lossless,resize_only,screenshot}` — compression mode (default `standard`; `screenshot` is PNG-only)
- `--max-width <int>` — resize so the longest side is at most this many pixels (default `0` = no resize; required `> 0` with `--mode resize_only`)
- `--dithering` / `--no-dithering` — dithering for Standard-mode PNG quantization (default enabled)
- `--protect-colors "#2ecc71,#ff0000"` — hex colors to prioritize keeping exact in Standard mode's palette
- `--keep-exif` — keep a curated subset of EXIF (camera make/model, date, exposure); GPS and orientation are always stripped regardless (default: strip all EXIF)
- `--recursive` / `--no-recursive` — scan subfolders too (default `--recursive`; shared with `--dir`, see Configuration below)
- `--workers` — concurrent compression workers (shared with the web server's `--workers`, see Configuration below)

Output mirrors the source folder's directory structure. Name collisions
after a format conversion (e.g. `photo.png` and `photo.jpg` both becoming
`photo.png`) get a numbered suffix (`photo_2.png`) rather than silently
overwriting one another.

Not yet supported in CLI mode: `--skip-existing`'s settings-aware caching
(every CLI run recompresses everything under `--source`) and per-file
overrides (every file in the batch gets the same settings).

## Configuration

Command-line flags:

```
python -m app --host 127.0.0.1 --port 8090 --workers 4 --dir "D:\screenshots"
```

- `--host` — listen address (default `127.0.0.1`, see Security below)
- `--port` — listen port (default `8090`; if taken, the next free port is used automatically)
- `--workers` — how many images to compress concurrently (default `4`).
  Lower this if pngquant/oxipng are already maxing out your CPU; raise it on a
  many-core machine for faster batches.
- `--thumbnail-workers` — how many images to thumbnail concurrently during a
  scan (default `4`). Independent from `--workers`, so a scan's I/O-bound
  thumbnailing can be tuned separately from CPU-bound compression.
- `--dir` — auto-scan this folder on startup (opens the web UI with results pre-loaded)
- `--recursive` / `--no-recursive` — when used with `--dir` or `--source`,
  whether to scan subfolders too (default `--recursive`, the historical
  behavior). Pass `--no-recursive` to scan only the top level.

For settings you don't want to type every time, either use the **in-app
settings panel** (gear icon in the header — changes persist to
`config.json`; host/port/workers changes require a restart) or create a
`config.json` by hand in `~/.image-optimizer/` (that's
`%USERPROFILE%\.image-optimizer\config.json` on Windows, or
`~/.image-optimizer/config.json` on macOS/Linux — the same folder on every
OS, and the same one `recent.json` already lives in). Copy
[`config.example.json`](config.example.json) there and edit it, or start
from scratch with:

```json
{
  "host": "127.0.0.1",
  "port": 8090,
  "concurrent_workers": 4,
  "thumbnail_workers": 4,
  "workspace_cleanup_delay": 10.0,
  "session_idle_timeout_hours": 4
}
```

Other flags:

- `workspace_cleanup_delay` — seconds to wait before deleting a workspace after
  it's replaced by a new scan/upload (default `10.0`). Files aren't deleted
  immediately so an in-flight request — e.g. a ZIP download or an image still
  loading in another tab — has time to finish before they disappear out from
  under it. Raise this if you routinely have slow downloads running when you
  start a new scan; lower it to free disk space sooner.
- `session_idle_timeout_hours` — how long a browser session (and its scan
  results, in-progress state, and temp workspace) is kept after you stop
  using it before being cleaned up automatically (default `4`). Each browser
  tab/session is tracked separately so multiple tabs don't interfere with
  each other; this controls how long an abandoned one hangs around before
  its temp files are freed. Also sets how long the session cookie itself is
  valid for.

All keys are optional — only include the ones you want to change from the
default (an empty `{}` or a missing file both just mean "use every
default"). Command-line flags always override `config.json`, which
overrides the built-in defaults. Invalid values (e.g. a negative worker
count) are ignored with a startup warning rather than crashing.

## Security

This app has no authentication and is meant to run on `127.0.0.1` only (the
default). It can read any directory you point it at and write to any path
you specify as the output folder — fine for local, single-user use, but
**don't** start it with `--host 0.0.0.0` or a LAN address unless you fully
trust everyone on that network, since there's nothing stopping them from
reading or writing files on your machine through it.

## Requirements

- **Source install:** Python 3.10+ with `fastapi`, `uvicorn`, `Pillow`, `jinja2`, `python-multipart`
  (`pip install -r requirements.txt`, or `pip install -r requirements.lock` for the exact
  versions this was tested against)
- **PNG compression:** `pngquant` and `oxipng` (PNG-only modes; JPG/WebP output and lossless
  fallbacks don't need them)
- **JPG output:** `cjpeg` from mozjpeg (keeps JPEG format instead of converting; see `bin/README.md`)
- **AVIF output:** `avifenc` from libavif (optional — without it the AVIF format simply isn't offered; see `bin/README.md`)
- **Standalone EXE:** Windows 10/11 64-bit, no Python required (all four encoders bundled)

## Project Structure

```
image-optimizer/
├── app/              # Python web application
│   ├── main.py       # FastAPI server & routes
│   ├── optimizer.py  # pngquant/oxipng/cjpeg/avifenc orchestration
│   ├── watcher.py    # Watch Mode directory poller
│   ├── models.py     # Pydantic request models
│   └── templates/    # HTML/CSS/JS frontend (single-page app with i18n)
├── assets/           # Logo, icons, banner images
├── bin/              # Binary dependencies (pngquant, oxipng, cjpeg, avifenc)
├── tests/            # pytest test suite (249 tests)
├── build_exe.py      # PyInstaller build script (onefile mode)
├── pyproject.toml    # pip-installable package config
├── requirements.txt
└── start.bat
```

## License

MIT
