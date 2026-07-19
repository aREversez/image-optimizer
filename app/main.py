from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from zipfile import ZipFile

from fastapi import FastAPI, File, UploadFile, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import OptimizeRequest, ScanRequest
from app.optimizer import HEX_COLOR_RE, Optimizer

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS) / "app"
else:
    BASE_DIR = Path(__file__).resolve().parent


def _find_free_port(host: str, start: int, max_attempts: int = 10) -> int:
    for port in range(start, start + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}-{start + max_attempts - 1}")


class AppState:
    def __init__(self):
        self.files: list = []
        self.results: list = []
        self.input_dir: Optional[str] = None
        self.workspace: Optional[Path] = None
        self.is_running = False
        self.current = 0
        self.total = 0
        self.logs: list = []
        self.cancelled = False

    def reset(self):
        self.files = []
        self.results = []
        self.input_dir = None
        self.is_running = False
        self.current = 0
        self.total = 0
        self.logs = []
        self.cancelled = False

    def new_workspace(self):
        if self.workspace:
            try:
                shutil.rmtree(self.workspace, ignore_errors=True)
            except Exception:
                pass
        self.workspace = Path(tempfile.mkdtemp(prefix="imgopt_"))
        return self.workspace


state = AppState()
optimizer: Optional[Optimizer] = None


def _ensure_workspace() -> Path:
    if state.workspace is None:
        state.new_workspace()
    return state.workspace


@asynccontextmanager
async def lifespan(app: FastAPI):
    global optimizer, state
    optimizer = Optimizer()
    tools = []
    if optimizer.pngquant_path:
        tools.append(f"pngquant: {optimizer.pngquant_path}")
    if optimizer.oxipng_path:
        tools.append(f"oxipng: {optimizer.oxipng_path}")
    if tools:
        print(f"[Startup] Detected: {', '.join(tools)}")
    else:
        print("[Startup] pngquant/oxipng not found — place binaries in the bin/ directory")
    yield
    if state.workspace:
        try:
            shutil.rmtree(state.workspace, ignore_errors=True)
        except Exception:
            pass


app = FastAPI(title="Image Optimizer", lifespan=lifespan)


def _gen_thumbnail(src: Path, dst: Path, max_size: int = 200):
    try:
        img = Image.open(src)
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, "JPEG", quality=70)
    except Exception as e:
        print(f"[Warning] Thumbnail failed for {src.name}: {e}")


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024 * 1024):.2f} MB"


def _scan_images(directory: Path, recursive: bool = True) -> list:
    patterns = ["*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tiff", "*.tif",
                "*.PNG", "*.JPG", "*.JPEG", "*.BMP", "*.TIFF", "*.TIF"]
    images = []
    iter_method = directory.rglob if recursive else directory.glob
    for pat in patterns:
        images.extend(iter_method(pat))
    return sorted(set(images))


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    tb = traceback.format_exc()
    print(f"[ERROR] {request.method} {request.url.path}: {exc}")
    print(tb)
    return JSONResponse(
        {
            "error": "Internal server error",
            "detail": str(exc),
            "path": request.url.path,
            "method": request.method,
        },
        status_code=500,
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/health")
async def health():
    if optimizer is None:
        return JSONResponse({"error": "Optimizer not initialized"}, status_code=503)
    return JSONResponse({
        "pngquant": optimizer.pngquant_path is not None,
        "oxipng": optimizer.oxipng_path is not None,
        "pngquant_path": str(optimizer.pngquant_path) if optimizer.pngquant_path else None,
        "oxipng_path": str(optimizer.oxipng_path) if optimizer.oxipng_path else None,
    })


@app.post("/api/scan")
async def scan_directory(data: ScanRequest):
    if state.is_running:
        return JSONResponse({"error": "Optimization in progress, please wait"}, status_code=400)
    directory = Path(data.directory)
    if not directory.exists() or not directory.is_dir():
        return JSONResponse({"error": "Directory does not exist"}, status_code=400)

    state.reset()
    state.input_dir = str(directory.resolve())
    ws = state.new_workspace()
    images = _scan_images(directory, data.recursive)

    files = []
    for idx, img_path in enumerate(images):
        rel = str(img_path.relative_to(directory))
        thumb_rel = f"thumb/{idx}_{img_path.stem}.jpg"
        thumb_path = ws / thumb_rel
        if not thumb_path.exists():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _gen_thumbnail, img_path, thumb_path)

        files.append({
            "id": str(idx),
            "name": rel,
            "path": str(img_path),
            "size": img_path.stat().st_size,
            "thumbnail": f"/api/thumb/{state.workspace.name}/{thumb_rel}",
        })

    state.files = files
    state.total = len(files)
    return JSONResponse({"files": files, "count": len(files)})


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    if state.is_running:
        return JSONResponse({"error": "Optimization in progress, please wait"}, status_code=400)
    state.reset()
    ws = state.new_workspace()
    upload_dir = ws / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    state.input_dir = str(upload_dir.resolve())

    result_files = []
    for idx, f in enumerate(files):
        if not f.filename:
            continue
        name = Path(f.filename).name
        unique_name = f"{int(time.time())}_{idx}_{name}"
        file_path = upload_dir / unique_name
        content = await f.read()
        file_path.write_bytes(content)

        thumb_rel = f"thumb/upload_{idx}_{name}.jpg"
        thumb_path = ws / thumb_rel
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _gen_thumbnail, file_path, thumb_path)

        result_files.append({
            "id": str(idx),
            "name": f.filename,
            "path": str(file_path),
            "size": len(content),
            "thumbnail": f"/api/thumb/{state.workspace.name}/{thumb_rel}",
        })

    state.files = result_files
    state.total = len(result_files)
    return JSONResponse({"files": result_files, "count": len(result_files)})


@app.get("/api/thumb/{ws_name}/{thumb_rel:path}")
async def get_thumbnail(ws_name: str, thumb_rel: str):
    if state.workspace and state.workspace.name == ws_name:
        p = (state.workspace / thumb_rel).resolve()
        base = str(state.workspace.resolve())
        if p.exists() and (str(p) == base or str(p).startswith(base + os.sep)):
            return FileResponse(str(p), media_type="image/jpeg")
    return Response(status_code=404)


@app.post("/api/optimize")
async def start_optimization(data: OptimizeRequest):
    if state.is_running:
        return JSONResponse({"error": "Optimization in progress, please wait"}, status_code=400)

    if data.compression_mode not in ("standard", "lossless", "resize_only"):
        return JSONResponse({"error": "Invalid compression_mode"}, status_code=400)
    if data.compression_mode == "resize_only" and data.max_width <= 0:
        return JSONResponse(
            {"error": "Resize Only mode requires Max Width to be set"}, status_code=400
        )
    bad_colors = [c for c in data.protected_colors if not HEX_COLOR_RE.match(c.strip())]
    if bad_colors:
        return JSONResponse(
            {"error": f"Invalid color(s) in Protect Colors: {', '.join(bad_colors)}. Use hex format like #2ecc71."},
            status_code=400,
        )

    files_to_process = state.files
    if data.file_ids is not None:
        ids = set(data.file_ids)
        files_to_process = [f for f in state.files if f["id"] in ids]

    if not files_to_process:
        return JSONResponse({"error": "No files to process"}, status_code=400)

    state.is_running = True
    state.current = 0
    state.total = len(files_to_process)
    state.logs = []
    state.results = []
    state.cancelled = False

    asyncio.create_task(
        _process_files(
            files_to_process,
            data.quality,
            data.max_width,
            data.output_format,
            data.output_dir,
            data.compression_mode,
            data.protected_colors,
            data.dithering,
        )
    )

    return JSONResponse({"ok": True, "total": len(files_to_process)})


async def _process_files(
    files: list,
    quality: str,
    max_width: int,
    output_format: str,
    output_dir: str = "",
    compression_mode: str = "standard",
    protected_colors: Optional[list] = None,
    dithering: bool = True,
):
    if optimizer is None:
        state.logs.append("Optimizer not initialized")
        state.is_running = False
        return

    ws = state.workspace
    opt_output_dir = ws / "output"
    if opt_output_dir.exists():
        shutil.rmtree(opt_output_dir, ignore_errors=True)
    opt_output_dir.mkdir(parents=True, exist_ok=True)

    async def log(msg):
        state.logs.append(f"  {msg}")

    try:
        for i, file_info in enumerate(files):
            if state.cancelled:
                state.logs.append("Cancelled by user")
                break

            input_path = Path(file_info["path"])

            if state.input_dir:
                try:
                    rel = Path(input_path).relative_to(Path(state.input_dir))
                    out_path = opt_output_dir / rel
                except ValueError:
                    out_path = opt_output_dir / input_path.name
            else:
                out_path = opt_output_dir / file_info["name"]

            out_path = out_path.with_suffix(f".{output_format}")
            out_path.parent.mkdir(parents=True, exist_ok=True)

            state.logs.append(f"[{i + 1}/{state.total}] {file_info['name']}")

            result = await optimizer.optimize_png(
                input_path=input_path,
                output_path=out_path,
                quality=quality,
                max_width=max_width,
                compression_mode=compression_mode,
                protected_colors=protected_colors,
                dithering=dithering,
                progress_callback=log,
            )

            result["id"] = file_info["id"]
            result["name"] = file_info["name"]
            result["original_path"] = file_info["path"]

            if result["success"]:
                result["savings"] = result["original_size"] - result["compressed_size"]
                result["savings_percent"] = round(
                    result["savings"] / result["original_size"] * 100, 1
                ) if result["original_size"] > 0 else 0
                state.logs.append(
                    f"  OK {_fmt_size(result['original_size'])} -> {_fmt_size(result['compressed_size'])} "
                    f"(saved {result['savings_percent']}%)"
                )
            else:
                state.logs.append(f"  FAILED: {result.get('error', 'unknown')}")

            state.results.append(result)
            state.current = i + 1

    except Exception as e:
        state.logs.append(f"  UNEXPECTED ERROR: {e}")
        import traceback
        state.logs.append(f"  {traceback.format_exc()}")

    finally:
        state.is_running = False
        total_orig = sum(r["original_size"] for r in state.results if r["success"])
        total_comp = sum(r["compressed_size"] for r in state.results if r["success"])
        saved = total_orig - total_comp
        pct = round(saved / total_orig * 100, 1) if total_orig > 0 else 0
        state.logs.append(
            f"\nDone! {state.current}/{state.total} files, "
            f"saved {_fmt_size(saved)} ({pct}%)"
        )

        if output_dir:
            user_output = Path(output_dir)
            try:
                user_output.mkdir(parents=True, exist_ok=True)
                for f in opt_output_dir.rglob("*"):
                    if f.is_file():
                        dest = user_output / f.relative_to(opt_output_dir)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dest)
                state.logs.append(f"  Output saved to: {user_output.resolve()}")
            except Exception as e:
                state.logs.append(f"  Output copy failed: {e}")

        uploads_dir = ws / "uploads"
        if uploads_dir.exists():
            try:
                shutil.rmtree(uploads_dir, ignore_errors=True)
            except Exception:
                pass


@app.get("/api/progress")
async def get_progress():
    return JSONResponse({
        "running": state.is_running,
        "current": state.current,
        "total": state.total,
        "logs": state.logs[-100:],
        "results": state.results,
    })


@app.post("/api/cancel")
async def cancel():
    state.cancelled = True
    return JSONResponse({"ok": True})


@app.get("/api/source-file/{ws_name}/{file_id}")
async def get_source_file(ws_name: str, file_id: str):
    if state.workspace and state.workspace.name == ws_name:
        for f in state.files:
            if f["id"] == file_id:
                p = Path(f["path"])
                if p.exists():
                    return FileResponse(str(p))
        for r in state.results:
            if r.get("id") == file_id:
                p = Path(r["original_path"])
                if p.exists():
                    return FileResponse(str(p))
    return Response(status_code=404)


@app.get("/api/result/{ws_name}/{result_path:path}")
async def get_result(ws_name: str, result_path: str):
    if state.workspace and state.workspace.name == ws_name:
        p = (state.workspace / "output" / result_path).resolve()
        base = str((state.workspace / "output").resolve())
        if p.exists() and (str(p) == base or str(p).startswith(base + os.sep)):
            return FileResponse(str(p))
    return Response(status_code=404)


@app.get("/api/preview/{ws_name}/{file_id}")
async def preview(ws_name: str, file_id: str):
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#f5f5f0;color:#333;font-family:system-ui,sans-serif;display:flex;flex-direction:column;height:100vh}
.bar{background:#fff;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #ddd;font-size:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.bar .title{font-weight:600;color:#555}
.bar .stats{color:#888}
.view-toggle{display:flex;gap:4px}
.view-btn{padding:4px 10px;font-size:12px;border:1px solid #ddd;background:#fff;border-radius:4px;cursor:pointer;color:#666}
.view-btn.active{background:#333;color:#fff;border-color:#333}
.container{flex:1;display:flex}
.panel{flex:1;display:flex;flex-direction:column;overflow:hidden}
.panel:first-child{border-right:1px solid #ddd}
.label{padding:8px 16px;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#888;background:#fafaf5}
.image-wrap{flex:1;display:flex;align-items:center;justify-content:center;overflow:auto;padding:16px;background:#f0f0eb}
.image-wrap img{max-width:100%;max-height:100%;object-fit:contain}
.footer{padding:10px 20px;text-align:center;font-size:13px;border-top:1px solid #ddd;color:#999}
.overlay-view{flex:1;display:flex;flex-direction:column}
.overlay-label{padding:8px 16px;font-size:12px;text-align:center;color:#888;background:#fafaf5;user-select:none}
.overlay-wrap{flex:1;display:flex;align-items:center;justify-content:center;overflow:auto;padding:16px;background:#f0f0eb;cursor:pointer}
.overlay-img{width:100%;height:100%;object-fit:contain}
</style>
</head><body>"""
    result = None
    for r in state.results:
        if r.get("id") == file_id:
            result = r
            break

    if not result:
        return HTMLResponse("<h2>Not found</h2>")

    orig_url = f"/api/source-file/{ws_name}/{file_id}"

    if state.input_dir:
        orig_path = Path(result["original_path"])
        try:
            rel = orig_path.relative_to(Path(state.input_dir))
        except ValueError:
            rel = Path(orig_path.name)
        comp_rel = str(rel.with_suffix(".png"))
    else:
        comp_rel = Path(result["original_path"]).stem + ".png"

    comp_url = f"/api/result/{ws_name}/{comp_rel}" if comp_rel else ""

    import html as _html
    safe_name = _html.escape(str(result['name']))
    html += f"""
<div class="bar">
  <span class="title">{safe_name}</span>
  <span class="stats">{_fmt_size(result['original_size'])} -> {_fmt_size(result['compressed_size'])} | saved {result.get('savings_percent', 0)}%</span>
  <div class="view-toggle">
    <button class="view-btn active" id="btn-side">Side by Side</button>
    <button class="view-btn" id="btn-overlay">Overlay</button>
  </div>
</div>
<div class="container" id="side-view">
  <div class="panel">
    <div class="label">Original ({_fmt_size(result['original_size'])})</div>
    <div class="image-wrap"><img src="{orig_url}" alt="original"/></div>
  </div>
  <div class="panel">
    <div class="label">Compressed ({_fmt_size(result['compressed_size'])})</div>
    <div class="image-wrap"><img src="{comp_url}" alt="compressed"/></div>
  </div>
</div>
<div class="overlay-view" id="overlay-view" style="display:none">
  <div class="overlay-label" id="overlay-label">Compressed &mdash; click image to toggle (or press space)</div>
  <div class="overlay-wrap" id="overlay-wrap">
    <img src="{orig_url}" class="overlay-img" id="overlay-orig" alt="original" style="display:none"/>
    <img src="{comp_url}" class="overlay-img" id="overlay-comp" alt="compressed"/>
  </div>
</div>
<div class="footer" id="footer-hint">Original (left) vs Compressed (right)</div>
<script>
(function(){{
  var sideBtn = document.getElementById('btn-side');
  var overlayBtn = document.getElementById('btn-overlay');
  var sideView = document.getElementById('side-view');
  var overlayView = document.getElementById('overlay-view');
  var orig = document.getElementById('overlay-orig');
  var comp = document.getElementById('overlay-comp');
  var label = document.getElementById('overlay-label');
  var wrap = document.getElementById('overlay-wrap');
  var footer = document.getElementById('footer-hint');
  var showingOriginal = false;

  function setMode(mode){{
    var isOverlay = mode === 'overlay';
    sideView.style.display = isOverlay ? 'none' : 'flex';
    overlayView.style.display = isOverlay ? 'flex' : 'none';
    sideBtn.classList.toggle('active', !isOverlay);
    overlayBtn.classList.toggle('active', isOverlay);
    footer.textContent = isOverlay
      ? 'Click the image (or press space) to flip between original and compressed'
      : 'Original (left) vs Compressed (right)';
  }}
  sideBtn.addEventListener('click', function(){{ setMode('side'); }});
  overlayBtn.addEventListener('click', function(){{ setMode('overlay'); }});

  function toggleImage(){{
    showingOriginal = !showingOriginal;
    orig.style.display = showingOriginal ? 'block' : 'none';
    comp.style.display = showingOriginal ? 'none' : 'block';
    label.textContent = (showingOriginal ? 'Original' : 'Compressed') + ' \\u2014 click image to toggle (or press space)';
  }}
  wrap.addEventListener('click', toggleImage);
  document.addEventListener('keydown', function(e){{
    if (overlayView.style.display !== 'none' && e.code === 'Space') {{
      e.preventDefault();
      toggleImage();
    }}
  }});
}})();
</script>
</body></html>"""
    return HTMLResponse(html)


@app.get("/favicon.ico")
async def favicon():
    fav = BASE_DIR / "templates" / "favicon.ico"
    if fav.exists():
        return FileResponse(str(fav), media_type="image/x-icon")
    return Response(status_code=404)


@app.get("/api/download/{ws_name}")
async def download_zip(ws_name: str):
    if not (state.workspace and state.workspace.name == ws_name):
        return JSONResponse({"error": "Invalid workspace"}, status_code=404)

    output_dir = state.workspace / "output"
    if not output_dir.exists():
        return JSONResponse({"error": "No files to download"}, status_code=404)

    zip_path = state.workspace / "optimized.zip"
    with ZipFile(zip_path, "w") as zf:
        for f in output_dir.rglob("*"):
            if f.is_file():
                arcname = str(f.relative_to(output_dir))
                zf.write(f, arcname)

    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename="optimized_images.zip",
    )


@app.get("/api/browse-folder")
async def browse_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return JSONResponse({"path": "", "error": "tkinter not available"})
    try:
        def _open():
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.update()
            path = filedialog.askdirectory(title="Select Output Directory")
            root.destroy()
            return path
        loop = asyncio.get_event_loop()
        path = await loop.run_in_executor(None, _open)
        return JSONResponse({"path": path or ""})
    except Exception as e:
        return JSONResponse({"path": "", "error": str(e)})


@app.get("/api/state")
async def get_state():
    return JSONResponse({
        "files": state.files,
        "results": state.results,
        "input_dir": state.input_dir,
        "is_running": state.is_running,
        "current": state.current,
        "total": state.total,
    })


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Image Optimizer Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Listen address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8090, help="Listen port (default 8090)")
    parser.add_argument("--dir", help="Directory to auto-scan on startup")
    args = parser.parse_args()

    if args.dir:
        d = Path(args.dir).resolve()
        if d.exists() and d.is_dir():
            state.input_dir = str(d)
            ws = state.new_workspace()
            images = _scan_images(d)
            for idx, img_path in enumerate(images):
                rel = str(img_path.relative_to(d))
                thumb_rel = f"thumb/{idx}_{img_path.stem}.jpg"
                thumb_path = ws / thumb_rel
                if not thumb_path.exists():
                    _gen_thumbnail(img_path, thumb_path)
                state.files.append({
                    "id": str(idx),
                    "name": rel,
                    "path": str(img_path),
                    "size": img_path.stat().st_size,
                    "thumbnail": f"/api/thumb/{state.workspace.name}/{thumb_rel}",
                })
            state.total = len(state.files)
            print(f"[Startup] Scanned {len(state.files)} images: {d}")
        else:
            print(f"[Warning] Directory does not exist: {args.dir}")

    port = _find_free_port(args.host, args.port)
    if port != args.port:
        print(f"[Startup] Port {args.port} in use, using port {port} instead")

    import uvicorn
    uvicorn.run(app, host=args.host, port=port, reload=False)


if __name__ == "__main__":
    main()