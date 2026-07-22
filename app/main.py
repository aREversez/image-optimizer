from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from zipfile import ZipFile

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import OptimizeRequest, RecentClearRequest, RecentRemoveRequest, ScanRequest
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


WORKSPACE_CLEANUP_DELAY = 10.0  # seconds


async def _delayed_rmtree(path: Path):
    """Give any in-flight requests reading from `path` a chance to finish
    before actually deleting it. Not a hard guarantee (an unusually slow
    download could still lose a race), but turns a zero-grace-period bug
    into one that needs a multi-second coincidence to hit."""
    await asyncio.sleep(WORKSPACE_CLEANUP_DELAY)
    shutil.rmtree(path, ignore_errors=True)


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
        self.last_seen = time.time()
        self.scan_running = False
        self.scan_current = 0
        self.scan_total = 0
        self.scan_error: Optional[str] = None

    def reset(self):
        self.files = []
        self.results = []
        self.input_dir = None
        self.is_running = False
        self.current = 0
        self.total = 0
        self.logs = []
        self.cancelled = False
        self.scan_running = False
        self.scan_current = 0
        self.scan_total = 0
        self.scan_error = None

    def new_workspace(self):
        if self.workspace:
            old = self.workspace
            try:
                # Don't delete the old workspace synchronously — another
                # request (e.g. a download or image load in a different
                # tab) may still be reading from it. Give in-flight
                # requests a grace period instead of yanking the directory
                # out from under them.
                asyncio.get_running_loop()
                asyncio.create_task(_delayed_rmtree(old))
            except RuntimeError:
                # No running event loop (e.g. the --dir CLI startup path,
                # which runs before uvicorn starts) — nothing could be
                # reading from it yet, safe to delete immediately.
                shutil.rmtree(old, ignore_errors=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="imgopt_"))
        return self.workspace


# Each browser session (identified by a cookie) gets its own AppState, so
# two tabs — or two people on the same LAN if this were ever pointed at
# 0.0.0.0 — no longer share scan results, progress, or a workspace and
# can't clobber each other's in-flight work.
SESSIONS: dict[str, AppState] = {}
SESSION_COOKIE = "imgopt_session"
SESSION_IDLE_TIMEOUT = 4 * 3600  # sweep sessions untouched for this long
SESSION_SWEEP_INTERVAL = 600

# Populated once by `--dir` at CLI startup (before any HTTP request exists,
# so there's no session yet to attach it to) and handed to whichever
# session is created first, so the browser that opens still sees the
# pre-scanned folder. Cleared after being consumed once.
_cli_prescan_state: Optional[AppState] = None


def get_session(request: Request) -> AppState:
    global _cli_prescan_state
    sid = request.cookies.get(SESSION_COOKIE)
    st = SESSIONS.get(sid) if sid else None
    if st is None:
        sid = secrets.token_urlsafe(24)
        st = _cli_prescan_state if _cli_prescan_state is not None else AppState()
        _cli_prescan_state = None
        SESSIONS[sid] = st
        # Stashed for session_cookie_middleware below to pick up — a
        # dependency mutating an injected Response object only takes effect
        # if the route handler lets FastAPI build the response itself, but
        # nearly every route here returns its own JSONResponse/HTMLResponse
        # explicitly, which would silently discard that. Middleware runs on
        # the actual outgoing response regardless, so it's reliable here.
        request.state.new_session_id = sid
    st.last_seen = time.time()
    return st


async def _sweep_stale_sessions():
    while True:
        await asyncio.sleep(SESSION_SWEEP_INTERVAL)
        now = time.time()
        stale = [sid for sid, st in SESSIONS.items() if now - st.last_seen > SESSION_IDLE_TIMEOUT]
        for sid in stale:
            st = SESSIONS.pop(sid, None)
            if st and st.workspace:
                shutil.rmtree(st.workspace, ignore_errors=True)


optimizer: Optional[Optimizer] = None

# Generated fresh each time the process starts. Injected into the page on
# load and required (as a custom header) on every state-changing API call.
# This isn't about keeping a secret from the person using the app — it's
# about making sure a request actually came from this app's own page:
# adding a custom header forces the browser to send a CORS preflight for
# cross-origin requests, and since this server sends no CORS headers at
# all, that preflight always fails closed. A malicious page open in another
# tab has no way to read this token (same-origin policy) and so can never
# construct a request this server will accept.
APP_TOKEN = secrets.token_urlsafe(32)


async def require_token(x_app_token: str = Header(default="")):
    if not secrets.compare_digest(x_app_token, APP_TOKEN):
        raise HTTPException(status_code=403, detail="Missing or invalid app token")

RECENT_LIMIT = 8


def _config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home())
    d = Path(base) / "image-optimizer"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _recent_file() -> Path:
    return _config_dir() / "recent.json"


def _load_recent() -> dict:
    try:
        data = json.loads(_recent_file().read_text(encoding="utf-8"))
        data.setdefault("scan_dirs", [])
        data.setdefault("output_dirs", [])
        return data
    except Exception:
        return {"scan_dirs": [], "output_dirs": []}


def _save_recent(data: dict):
    try:
        _recent_file().write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass  # best-effort — a read-only config dir shouldn't break the app


def _push_recent(key: str, path: str):
    if not path:
        return
    data = _load_recent()
    lst = [p for p in data.get(key, []) if p != path]
    lst.insert(0, path)
    data[key] = lst[:RECENT_LIMIT]
    _save_recent(data)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global optimizer
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
    sweep_task = asyncio.create_task(_sweep_stale_sessions())
    yield
    sweep_task.cancel()
    for st in SESSIONS.values():
        if st.workspace:
            try:
                shutil.rmtree(st.workspace, ignore_errors=True)
            except Exception:
                pass


app = FastAPI(title="Image Optimizer", lifespan=lifespan)


@app.middleware("http")
async def session_cookie_middleware(request: Request, call_next):
    response = await call_next(request)
    new_sid = getattr(request.state, "new_session_id", None)
    if new_sid:
        response.set_cookie(
            SESSION_COOKIE, new_sid, httponly=True, samesite="lax", max_age=SESSION_IDLE_TIMEOUT
        )
    return response


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
async def index(state: AppState = Depends(get_session)):
    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    token_script = f'<script>window.APP_TOKEN = "{APP_TOKEN}";</script>'
    html = html.replace("</head>", f"{token_script}\n</head>", 1)
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


async def _scan_and_thumbnail(state: AppState, directory: Path, recursive: bool):
    ws = state.workspace
    try:
        images = await asyncio.get_event_loop().run_in_executor(
            None, _scan_images, directory, recursive
        )
    except Exception as e:
        state.scan_error = str(e)
        state.scan_running = False
        return

    state.scan_total = len(images)
    files: list = [None] * len(images)  # preallocated so results land in a
    # stable, deterministic order (matching the sorted scan order) even
    # though workers finish in whatever order thumbnailing completes.

    queue: asyncio.Queue = asyncio.Queue()
    for idx, img_path in enumerate(images):
        queue.put_nowait((idx, img_path))

    loop = asyncio.get_event_loop()

    async def worker():
        while True:
            try:
                idx, img_path = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                rel = str(img_path.relative_to(directory))
                thumb_rel = f"thumb/{idx}_{img_path.stem}.jpg"
                thumb_path = ws / thumb_rel
                if not thumb_path.exists():
                    await loop.run_in_executor(None, _gen_thumbnail, img_path, thumb_path)
                files[idx] = {
                    "id": str(idx),
                    "name": rel,
                    "path": str(img_path),
                    "size": img_path.stat().st_size,
                    "thumbnail": f"/api/thumb/{ws.name}/{thumb_rel}",
                }
            except Exception as e:
                # One unreadable/corrupt file shouldn't stop the rest of
                # the scan — same reasoning as the optimize worker pool.
                files[idx] = {
                    "id": str(idx),
                    "name": str(img_path),
                    "path": str(img_path),
                    "size": 0,
                    "thumbnail": "",
                    "error": str(e),
                }
            finally:
                state.scan_current += 1
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(CONCURRENT_WORKERS)]
    await asyncio.gather(*workers)

    state.files = [f for f in files if f is not None]
    state.total = len(state.files)
    state.scan_running = False
    _push_recent("scan_dirs", str(directory.resolve()))


@app.post("/api/scan")
async def scan_directory(data: ScanRequest, state: AppState = Depends(get_session), _auth: None = Depends(require_token)):
    if state.is_running:
        return JSONResponse({"error": "Optimization in progress, please wait"}, status_code=400)
    if state.scan_running:
        return JSONResponse({"error": "A scan is already in progress, please wait"}, status_code=400)
    directory = Path(data.directory)
    if not directory.exists() or not directory.is_dir():
        return JSONResponse({"error": "Directory does not exist"}, status_code=400)

    state.reset()
    state.input_dir = str(directory.resolve())
    state.new_workspace()
    state.scan_running = True
    state.scan_current = 0
    state.scan_total = 0

    asyncio.create_task(_scan_and_thumbnail(state, directory, data.recursive))
    return JSONResponse({"scanning": True})


@app.get("/api/scan-progress")
async def get_scan_progress(state: AppState = Depends(get_session)):
    return JSONResponse({
        "running": state.scan_running,
        "current": state.scan_current,
        "total": state.scan_total,
        "error": state.scan_error,
        "files": state.files if not state.scan_running else [],
    })


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...), state: AppState = Depends(get_session), _auth: None = Depends(require_token)):
    if state.is_running:
        return JSONResponse({"error": "Optimization in progress, please wait"}, status_code=400)
    state.reset()
    ws = state.new_workspace()
    upload_dir = ws / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

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
async def get_thumbnail(ws_name: str, thumb_rel: str, state: AppState = Depends(get_session)):
    if state.workspace and state.workspace.name == ws_name:
        p = (state.workspace / thumb_rel).resolve()
        base = str(state.workspace.resolve())
        if p.exists() and (str(p) == base or str(p).startswith(base + os.sep)):
            return FileResponse(str(p), media_type="image/jpeg")
    return Response(status_code=404)


@app.post("/api/optimize")
async def start_optimization(data: OptimizeRequest, state: AppState = Depends(get_session), _auth: None = Depends(require_token)):
    if state.is_running:
        return JSONResponse({"error": "Optimization in progress, please wait"}, status_code=400)

    if data.compression_mode not in ("standard", "lossless", "resize_only"):
        return JSONResponse({"error": "Invalid compression_mode"}, status_code=400)
    if data.output_format not in ("png", "webp"):
        # The pipeline only ever produces real PNG or WebP bytes — accepting
        # anything else here would silently produce a file whose extension
        # lies about its actual content.
        return JSONResponse(
            {"error": f"Unsupported output_format: {data.output_format!r}. Supported: 'png', 'webp'."},
            status_code=400,
        )
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

    if data.output_dir:
        _push_recent("output_dirs", str(Path(data.output_dir).resolve()))

    state.is_running = True
    state.current = 0
    state.total = len(files_to_process)
    state.logs = []
    state.results = []
    state.cancelled = False

    asyncio.create_task(
        _process_files(
            state,
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


CONCURRENT_WORKERS = 4  # matches Optimizer's internal subprocess semaphore


async def _process_files(
    state: AppState,
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

    queue: asyncio.Queue = asyncio.Queue()
    for file_info in files:
        queue.put_nowait(file_info)

    async def process_one(file_info: dict):
        input_path = Path(file_info["path"])

        # file_info["name"] is always the clean, structure-preserving
        # relative name (e.g. "2023/vacation/IMG_0001.png") for both the
        # scan and upload flows — use it directly rather than trying to
        # re-derive it from the physical storage path, which for uploads
        # is intentionally flattened/deduplicated and does NOT mirror
        # the original folder structure.
        out_path = opt_output_dir / file_info["name"]
        out_path = out_path.with_suffix(f".{output_format}")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        state.logs.append(f"Processing: {file_info['name']}")

        result = await optimizer.optimize_png(
            input_path=input_path,
            output_path=out_path,
            quality=quality,
            max_width=max_width,
            compression_mode=compression_mode,
            protected_colors=protected_colors,
            dithering=dithering,
            output_format=output_format,
            progress_callback=log,
        )

        result["id"] = file_info["id"]
        result["name"] = file_info["name"]
        result["original_path"] = file_info["path"]
        result["output_format"] = output_format

        if result["success"]:
            result["savings"] = result["original_size"] - result["compressed_size"]
            result["savings_percent"] = round(
                result["savings"] / result["original_size"] * 100, 1
            ) if result["original_size"] > 0 else 0
            state.logs.append(
                f"  OK {file_info['name']}: {_fmt_size(result['original_size'])} -> "
                f"{_fmt_size(result['compressed_size'])} (saved {result['savings_percent']}%)"
            )
            # Copy this file to the user's chosen output folder right away,
            # rather than waiting for the whole batch to finish — otherwise
            # the folder stays empty for the entire run, which looks like
            # the wrong folder was picked.
            if output_dir:
                try:
                    user_output = Path(output_dir)
                    rel = out_path.relative_to(opt_output_dir)
                    dest = user_output / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(out_path, dest)
                except Exception as e:
                    state.logs.append(f"  Output copy failed: {e}")
        else:
            state.logs.append(f"  FAILED {file_info['name']}: {result.get('error', 'unknown')}")

        # No lock needed: asyncio is single-threaded/cooperative and there's
        # no `await` between these two statements, so no other worker can
        # interleave in between.
        state.results.append(result)
        state.current += 1

    async def worker():
        while True:
            try:
                file_info = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if state.cancelled:
                    # Drain the queue without processing once cancelled,
                    # rather than stopping this one worker while others
                    # keep pulling — matches the old behavior of not
                    # starting new files after Cancel, while files already
                    # in flight are left to finish naturally.
                    continue
                try:
                    await process_one(file_info)
                except Exception as e:
                    # Anything process_one doesn't already catch internally
                    # (optimize_png has its own try/except, but e.g. mkdir
                    # permission errors or a malformed file_info wouldn't be)
                    # must be caught HERE, per-file, rather than left to
                    # propagate out of the worker. An uncaught exception
                    # would make asyncio.gather() below return immediately
                    # without waiting for the other 3 workers — they'd keep
                    # running as orphaned background tasks whose results
                    # never get recorded, while the batch already reports
                    # itself done. Degrading to a normal failed-file result
                    # keeps one bad file from taking down the whole batch.
                    state.logs.append(f"  FAILED {file_info.get('name', '?')}: {e}")
                    state.results.append({
                        "id": file_info.get("id"),
                        "name": file_info.get("name"),
                        "original_path": file_info.get("path"),
                        "success": False,
                        "error": str(e),
                        "original_size": 0,
                        "compressed_size": 0,
                    })
                    state.current += 1
            finally:
                queue.task_done()

    try:
        workers = [asyncio.create_task(worker()) for _ in range(CONCURRENT_WORKERS)]
        await asyncio.gather(*workers)
        if state.cancelled:
            state.logs.append("Cancelled by user")

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
            state.logs.append(f"  Output saved to: {Path(output_dir).resolve()}")

        if output_dir:
            state.logs.append(f"  Output saved to: {Path(output_dir).resolve()}")


@app.get("/api/progress")
async def get_progress(state: AppState = Depends(get_session)):
    return JSONResponse({
        "running": state.is_running,
        "current": state.current,
        "total": state.total,
        "logs": state.logs[-100:],
        "results": state.results,
    })


@app.post("/api/cancel")
async def cancel(state: AppState = Depends(get_session), _auth: None = Depends(require_token)):
    state.cancelled = True
    return JSONResponse({"ok": True})


@app.get("/api/source-file/{ws_name}/{file_id}")
async def get_source_file(ws_name: str, file_id: str, state: AppState = Depends(get_session)):
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
async def get_result(ws_name: str, result_path: str, state: AppState = Depends(get_session)):
    if state.workspace and state.workspace.name == ws_name:
        p = (state.workspace / "output" / result_path).resolve()
        base = str((state.workspace / "output").resolve())
        if p.exists() and (str(p) == base or str(p).startswith(base + os.sep)):
            return FileResponse(str(p))
    return Response(status_code=404)


@app.get("/api/preview/{ws_name}/{file_id}")
async def preview(ws_name: str, file_id: str, state: AppState = Depends(get_session)):
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
.image-wrap.zoom-100{align-items:flex-start;justify-content:flex-start}
.image-wrap img.zoom-100{max-width:none;max-height:none;width:auto;height:auto}
.footer{padding:10px 20px;text-align:center;font-size:13px;border-top:1px solid #ddd;color:#999}
.overlay-view{flex:1;display:flex;flex-direction:column}
.overlay-label{padding:8px 16px;font-size:12px;text-align:center;color:#888;background:#fafaf5;user-select:none}
.overlay-wrap{flex:1;display:flex;align-items:center;justify-content:center;overflow:auto;padding:16px;background:#f0f0eb;cursor:pointer}
.overlay-img{width:100%;height:100%;object-fit:contain}
.overlay-wrap.zoom-100{align-items:flex-start;justify-content:flex-start}
.overlay-img.zoom-100{width:auto;height:auto;max-width:none;max-height:none}
.zoom-toggle{display:flex;gap:4px}
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

    # Same reasoning as _process_files: result["name"] is the clean,
    # structure-preserving relative name for both scan and upload flows —
    # use it directly instead of re-deriving from the physical storage path.
    out_fmt = result.get("output_format", "png")
    comp_rel = str(Path(result["name"]).with_suffix(f".{out_fmt}"))

    comp_url = f"/api/result/{ws_name}/{comp_rel}" if comp_rel else ""

    import html as _html
    safe_name = _html.escape(str(result['name']))
    safe_basename = _html.escape(Path(result['name']).name)
    html = html.replace(
        '<head><meta charset="utf-8">',
        f'<head><meta charset="utf-8"><title>Compare - {safe_basename}</title>',
        1,
    )
    html += f"""
<div class="bar">
  <span class="title">{safe_name}</span>
  <span class="stats">{_fmt_size(result['original_size'])} -> {_fmt_size(result['compressed_size'])} | saved {result.get('savings_percent', 0)}%</span>
  <div class="view-toggle">
    <button class="view-btn" id="btn-side">Side by Side</button>
    <button class="view-btn active" id="btn-overlay">Overlay</button>
  </div>
  <div class="zoom-toggle">
    <button class="view-btn active" id="btn-fit" title="Scale images to fit the window">Fit</button>
    <button class="view-btn" id="btn-zoom100" title="Show actual pixels — the real test for whether text stays sharp">100%</button>
  </div>
</div>
<div class="container" id="side-view" style="display:none">
  <div class="panel">
    <div class="label">Original ({_fmt_size(result['original_size'])})</div>
    <div class="image-wrap" id="orig-wrap"><img src="{orig_url}" id="orig-img" alt="original"/></div>
  </div>
  <div class="panel">
    <div class="label">Compressed ({_fmt_size(result['compressed_size'])})</div>
    <div class="image-wrap" id="comp-wrap"><img src="{comp_url}" id="comp-img" alt="compressed"/></div>
  </div>
</div>
<div class="overlay-view" id="overlay-view">
  <div class="overlay-label" id="overlay-label">Compressed &mdash; click image to toggle (or press space)</div>
  <div class="overlay-wrap" id="overlay-wrap">
    <img src="{orig_url}" class="overlay-img" id="overlay-orig" alt="original" style="display:none"/>
    <img src="{comp_url}" class="overlay-img" id="overlay-comp" alt="compressed"/>
  </div>
</div>
<div class="footer" id="footer-hint">Overlay — click the image (or press space) to flip between original and compressed</div>
<script>
(function(){{
  var sideBtn = document.getElementById('btn-side');
  var overlayBtn = document.getElementById('btn-overlay');
  var fitBtn = document.getElementById('btn-fit');
  var zoomBtn = document.getElementById('btn-zoom100');
  var sideView = document.getElementById('side-view');
  var overlayView = document.getElementById('overlay-view');
  var origWrap = document.getElementById('orig-wrap');
  var compWrap = document.getElementById('comp-wrap');
  var origImg = document.getElementById('orig-img');
  var compImg = document.getElementById('comp-img');
  var orig = document.getElementById('overlay-orig');
  var comp = document.getElementById('overlay-comp');
  var label = document.getElementById('overlay-label');
  var wrap = document.getElementById('overlay-wrap');
  var footer = document.getElementById('footer-hint');
  var showingOriginal = false;
  var zoomedIn = false;

  function setMode(mode){{
    var isOverlay = mode === 'overlay';
    sideView.style.display = isOverlay ? 'none' : 'flex';
    overlayView.style.display = isOverlay ? 'flex' : 'none';
    sideBtn.classList.toggle('active', !isOverlay);
    overlayBtn.classList.toggle('active', isOverlay);
    updateFooter();
  }}
  sideBtn.addEventListener('click', function(){{ setMode('side'); }});
  overlayBtn.addEventListener('click', function(){{ setMode('overlay'); }});

  function setZoom(mode){{
    zoomedIn = mode === '100';
    [origWrap, compWrap, wrap].forEach(function(w){{ w.classList.toggle('zoom-100', zoomedIn); }});
    [origImg, compImg, orig, comp].forEach(function(img){{ img.classList.toggle('zoom-100', zoomedIn); }});
    fitBtn.classList.toggle('active', !zoomedIn);
    zoomBtn.classList.toggle('active', zoomedIn);
    updateFooter();
  }}
  fitBtn.addEventListener('click', function(){{ setZoom('fit'); }});
  zoomBtn.addEventListener('click', function(){{ setZoom('100'); }});

  function updateFooter(){{
    var isOverlay = overlayView.style.display !== 'none';
    var zoomHint = zoomedIn ? ' (scroll to pan around at actual size)' : '';
    footer.textContent = isOverlay
      ? 'Click the image (or press space) to flip between original and compressed' + zoomHint
      : 'Original (left) vs Compressed (right)' + zoomHint;
  }}

  function toggleImage(){{
    if (zoomedIn) return; // clicking while panning a zoomed image shouldn't also flip it
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
async def download_zip(ws_name: str, state: AppState = Depends(get_session)):
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


@app.get("/api/recent")
async def get_recent(_auth: None = Depends(require_token)):
    return JSONResponse(_load_recent())


@app.post("/api/recent/remove")
async def recent_remove(data: RecentRemoveRequest, _auth: None = Depends(require_token)):
    recent = _load_recent()
    if data.key in recent and data.value in recent[data.key]:
        recent[data.key] = [p for p in recent[data.key] if p != data.value]
        _save_recent(recent)
    return JSONResponse({"ok": True})


@app.post("/api/recent/clear")
async def recent_clear(data: RecentClearRequest, _auth: None = Depends(require_token)):
    recent = _load_recent()
    if data.key in recent:
        recent[data.key] = []
        _save_recent(recent)
    return JSONResponse({"ok": True})


@app.get("/api/browse-folder")
async def browse_folder(title: str = "Select Folder", _auth: None = Depends(require_token)):
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
            # Can't make this window use Chrome's icon — it's a native Tk
            # window, not something the browser renders — but we can at
            # least use our own icon instead of the generic Tk feather.
            icon = BASE_DIR / "templates" / "favicon.ico"
            if icon.exists():
                try:
                    root.iconbitmap(str(icon))
                except Exception:
                    pass  # e.g. .ico unsupported on non-Windows Tk builds
            root.update()
            path = filedialog.askdirectory(title=title)
            root.destroy()
            return path
        loop = asyncio.get_event_loop()
        path = await loop.run_in_executor(None, _open)
        return JSONResponse({"path": path or ""})
    except Exception as e:
        return JSONResponse({"path": "", "error": str(e)})


@app.get("/api/state")
async def get_state(state: AppState = Depends(get_session)):
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
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Listen address (default 127.0.0.1 / localhost-only). WARNING: this app has no "
             "authentication — binding to 0.0.0.0 or a LAN address exposes local file read "
             "(scan any directory) and write (output_dir) to anyone on the network.",
    )
    parser.add_argument("--port", type=int, default=8090, help="Listen port (default 8090)")
    parser.add_argument("--dir", help="Directory to auto-scan on startup")
    args = parser.parse_args()

    if args.dir:
        d = Path(args.dir).resolve()
        if d.exists() and d.is_dir():
            global _cli_prescan_state
            prescan = AppState()
            prescan.input_dir = str(d)
            ws = prescan.new_workspace()
            images = _scan_images(d)
            for idx, img_path in enumerate(images):
                rel = str(img_path.relative_to(d))
                thumb_rel = f"thumb/{idx}_{img_path.stem}.jpg"
                thumb_path = ws / thumb_rel
                if not thumb_path.exists():
                    _gen_thumbnail(img_path, thumb_path)
                prescan.files.append({
                    "id": str(idx),
                    "name": rel,
                    "path": str(img_path),
                    "size": img_path.stat().st_size,
                    "thumbnail": f"/api/thumb/{prescan.workspace.name}/{thumb_rel}",
                })
            prescan.total = len(prescan.files)
            _cli_prescan_state = prescan
            print(f"[Startup] Scanned {len(prescan.files)} images: {d}")
        else:
            print(f"[Warning] Directory does not exist: {args.dir}")

    port = _find_free_port(args.host, args.port)
    if port != args.port:
        print(f"[Startup] Port {args.port} in use, using port {port} instead")

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[Warning] Binding to {args.host} — this server has NO authentication. "
            f"Anyone who can reach this address can read any directory on this machine "
            f"(/api/scan) and write to any writable path (output_dir). Only do this on a "
            f"network you fully trust."
        )

    import uvicorn
    uvicorn.run(app, host=args.host, port=port, reload=False)


if __name__ == "__main__":
    main()