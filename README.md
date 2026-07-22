<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/banner.png">
  <img alt="Image Optimizer" src="assets/banner.png" width="100%">
</picture>

# Image Optimizer

Batch PNG compression with a local web UI. Uses **pngquant** (lossy quantization) and **oxipng** (lossless strip/optimization) behind a clean interface.

## Features

- **Local folder scanning** — select a directory, scan recursively (optional), and pick which files to optimize
- **Before/after preview** — overlay and side-by-side compare views
- **Three compression modes:**
  - `standard` — pngquant + oxipng, best size reduction
  - `lossless` — oxipng only, no color loss
  - `resize only` — scale down, no quantization
- **Output format** — PNG (via pngquant/oxipng) or WebP (via Pillow's built-in encoder, no extra binary needed)
- **Color protection** — list hex colors to preserve in the palette
- **Dithering toggle** — smoother gradients (standard mode)
- **Output directory** — save results to any folder (or use the built-in ZIP download)
- **Recent folder history** — quick-access chips with per-item removal and clear all
- **Auto-detect binaries** — finds `pngquant`/`oxipng` in `bin/` folder or system PATH
- **Cross-platform** — Windows, macOS, Linux (pre-built exe for Windows; macOS/Linux users need `pngquant`/`oxipng` on PATH)

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

1. **Enter a folder path** — type or browse to select a directory, then click *Scan Folder*
2. **Review files** — deselect any you want to skip, adjust quality/width/mode
3. **Protect colors** (optional) — add hex colors like `#2ecc71` to preserve
4. **Start** — click *Start* and watch the live progress log
5. **Compare & download** — click any result's *Compare* button for before/after preview, then download individual files or the full ZIP

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
- **Standalone EXE:** Windows 10/11 64-bit, no Python required

## Project Structure

```
image-optimizer/
├── app/              # Python web application
│   ├── main.py       # FastAPI server & routes
│   ├── optimizer.py  # pngquant/oxipng orchestration
│   ├── models.py     # Pydantic request models
│   └── templates/    # HTML/CSS/JS frontend
├── assets/           # Logo, icons, banner images
├── bin/              # Binary dependencies (pngquant, oxipng)
├── images/           # Default scan directory (gitignored)
├── build_exe.py      # PyInstaller build script
├── pyproject.toml    # pip-installable package config
├── requirements.txt
└── start.bat
```

## License

MIT
