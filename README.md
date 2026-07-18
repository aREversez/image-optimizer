# Image Optimizer

Batch PNG compression with a local web UI. Uses **pngquant** (lossy quantization) and **oxipng** (lossless strip/optimization) behind a clean interface.

## Features

- **Scan a folder** or **drag & drop files** to queue images
- **Side-by-side** and **overlay** preview before/after compression
- **Three compression modes:**
  - `standard` — pngquant + oxipng, best size reduction
  - `lossless` — oxipng only, no color loss
  - `resize only` — scale down, no quantization
- **Color protection** — list hex colors to preserve in the palette
- **Dithering toggle** — smoother gradients (standard mode)
- **Output directory** — save results to any folder (or use the built-in ZIP download)
- **Cross-platform** — Windows, macOS, Linux

## Quick Start

```bash
pip install -r requirements.txt
start.bat          # or: python -m app
```

Open http://127.0.0.1:8090 in your browser.

The first run will auto-detect `pngquant` and `oxipng` in the `bin/` folder and system PATH. See `bin/README.md` if a binary is missing.

## Usage

1. **Select input** — scan a local folder (click *Select Folder* or drag a folder onto the drop zone)
2. **Review files** — deselect any you want to skip, adjust quality/width/mode
3. **Protect colors** (optional) — add hex colors like `#2ecc71` to preserve
4. **Start** — click *Optimize* and watch the live log
5. **Compare & download** — click any result to see a before/after preview, then download individual files or a ZIP

## Requirements

- Python 3.10+
- Dependencies: `fastapi`, `uvicorn`, `Pillow`, `jinja2`, `python-multipart`
- Optional system tools: `pngquant`, `oxipng` (bundled for Windows in `bin/`)

## Project Structure

```
image-optimizer/
├── app/              # Python web application
│   ├── main.py       # FastAPI server & routes
│   ├── optimizer.py  # pngquant/oxipng orchestration
│   ├── models.py     # Pydantic request models
│   └── templates/    # HTML/CSS/JS frontend
├── bin/              # Binary dependencies (pngquant, oxipng)
├── images/           # Default scan directory (gitignored)
├── requirements.txt
└── start.bat
```

## License

MIT
