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
from typing import Any, Awaitable, Callable, List, Optional
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
    # NOTE: this is inherently a probe — there's a small TOCTOU window
    # between closing the probe socket and uvicorn binding the real one.
    # SO_REUSEADDR (non-Windows only: on Windows it means "steal the port",
    # not "ignore TIME_WAIT") keeps a lingering TIME_WAIT from a previous
    # run from making the probe reject a port uvicorn could bind just fine.
    for port in range(start, start + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if sys.platform != "win32":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}-{start + max_attempts - 1}")


WORKSPACE_CLEANUP_DELAY = 10.0  # seconds


async def _async_rmtree(path: Path):
    """shutil.rmtree off the event loop. Recursive directory delete is a
    synchronous syscall that scales with the number of files in the tree —
    a workspace with thousands of thumbnails can take long enough to stall
    every other async co-routine in the process (progress polls, thumbnail
    workers, other sessions' scans). Anything the server itself triggers
    in an async context should go through here rather than calling
    shutil.rmtree directly.

    The AppState.new_workspace no-event-loop fallback (synchronous path
    during CLI startup) deliberately does NOT use this — there's no
    running loop to `to_thread` onto, and nothing else is concurrent to
    be stalled by it anyway.
    """
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


async def _delayed_rmtree(path: Path):
    """Give any in-flight requests reading from `path` a chance to finish
    before actually deleting it. Not a hard guarantee (an unusually slow
    download could still lose a race), but turns a zero-grace-period bug
    into one that needs a multi-second coincidence to hit."""
    await asyncio.sleep(WORKSPACE_CLEANUP_DELAY)
    await _async_rmtree(path)


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
        self.paused = False
        self.last_seen = time.time()
        self.scan_running = False
        self.scan_current = 0
        self.scan_total = 0
        self.scan_error: Optional[str] = None
        # ZIP download cache: output_version bumps once per completed run
        # (outputs changed); zip_built_version records which version the
        # on-disk optimized.zip reflects, so download_zip can skip a rebuild
        # when nothing changed since the last archive.
        self.output_version = 0
        self.zip_built_version = -1

    def reset(self):
        self.files = []
        self.results = []
        self.input_dir = None
        self.is_running = False
        self.current = 0
        self.total = 0
        self.logs = []
        self.cancelled = False
        self.paused = False
        self.scan_running = False
        self.scan_current = 0
        self.scan_total = 0
        self.scan_error = None
        self.output_version = 0
        self.zip_built_version = -1

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
# two different browsers/profiles — or two people on the same LAN if this
# were ever pointed at 0.0.0.0 — no longer share scan results, progress, or
# a workspace and can't clobber each other's in-flight work. Note the
# cookie is browser-wide, NOT per-tab: two tabs in the same browser still
# share one AppState, which is why the scan/optimize endpoints reject
# concurrent work with a 400 instead of silently swapping state.
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
                # Each stale workspace gets deleted off the event loop — a
                # sweep that grabs several oversized sessions in one pass
                # could otherwise freeze the server for the combined delete
                # duration, right when a still-active session's progress poll
                # is exactly the request we'd want not to stall.
                await _async_rmtree(st.workspace)


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
    # A single, predictable location on every OS (like ~/.ssh, ~/.npm,
    # ~/.docker) rather than following each platform's own convention
    # (APPDATA on Windows, XDG_CONFIG_HOME on Linux/macOS) — easier to
    # find and document with one path instead of three.
    d = Path.home() / ".image-optimizer"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _recent_file() -> Path:
    return _config_dir() / "recent.json"


# Defaults for everything a config.json can override. CLI flags (where they
# exist) take priority over the config file, which takes priority over
# these. Keep this in sync with README's config.json documentation.
DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8090,
    "concurrent_workers": 4,   # how many images to compress at once (pngquant/oxipng/Pillow)
    "thumbnail_workers": 4,    # how many images to thumbnail at once during a scan
    "workspace_cleanup_delay": 10.0,  # seconds to wait before deleting a replaced workspace
    "session_idle_timeout_hours": 4,  # how long an inactive browser session is kept
}


def _config_file() -> Path:
    return _config_dir() / "config.json"


def _load_app_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(_config_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except Exception as e:
        print(f"[Warning] Ignoring unreadable config.json ({_config_file()}): {e}")
        return cfg

    for key, value in raw.items():
        if key not in cfg:
            print(f"[Warning] Ignoring unknown config.json key: {key!r}")
            continue
        cfg[key] = value

    # Validate rather than trust blindly — a hand-edited config file with a
    # typo'd value shouldn't crash startup or silently misbehave (e.g. a
    # negative/zero worker count would make the scan/optimize queues never
    # drain). Uses an explicit ValueError, NOT `assert`, because `python -O`
    # strips assertions out entirely — running the app with `-O` would then
    # silently accept e.g. ..."port": 99999} (out of valid port range) and
    # bind to a garbage port, or accept a negative cleanup delay, defeating
    # the whole point of the guard.
    try:
        cfg["port"] = int(cfg["port"])
        if not (1 <= cfg["port"] <= 65535):
            raise ValueError("port out of range 1-65535")
    except Exception:
        print(f"[Warning] Invalid config.json 'port' ({cfg['port']!r}), using default {DEFAULT_CONFIG['port']}")
        cfg["port"] = DEFAULT_CONFIG["port"]
    try:
        cfg["concurrent_workers"] = int(cfg["concurrent_workers"])
        if cfg["concurrent_workers"] < 1:
            raise ValueError("concurrent_workers must be >= 1")
    except Exception:
        print(
            f"[Warning] Invalid config.json 'concurrent_workers' ({cfg['concurrent_workers']!r}), "
            f"using default {DEFAULT_CONFIG['concurrent_workers']}"
        )
        cfg["concurrent_workers"] = DEFAULT_CONFIG["concurrent_workers"]
    try:
        cfg["thumbnail_workers"] = int(cfg["thumbnail_workers"])
        if cfg["thumbnail_workers"] < 1:
            raise ValueError("thumbnail_workers must be >= 1")
    except Exception:
        print(
            f"[Warning] Invalid config.json 'thumbnail_workers' ({cfg['thumbnail_workers']!r}), "
            f"using default {DEFAULT_CONFIG['thumbnail_workers']}"
        )
        cfg["thumbnail_workers"] = DEFAULT_CONFIG["thumbnail_workers"]
    try:
        cfg["workspace_cleanup_delay"] = float(cfg["workspace_cleanup_delay"])
        if cfg["workspace_cleanup_delay"] < 0:
            raise ValueError("workspace_cleanup_delay must be >= 0")
    except Exception:
        cfg["workspace_cleanup_delay"] = DEFAULT_CONFIG["workspace_cleanup_delay"]
    try:
        cfg["session_idle_timeout_hours"] = float(cfg["session_idle_timeout_hours"])
        if cfg["session_idle_timeout_hours"] <= 0:
            raise ValueError("session_idle_timeout_hours must be > 0")
    except Exception:
        cfg["session_idle_timeout_hours"] = DEFAULT_CONFIG["session_idle_timeout_hours"]

    return cfg


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
    optimizer = Optimizer(max_concurrency=CONCURRENT_WORKERS)
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
                await _async_rmtree(st.workspace)
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
        # Context manager so the source file handle is released even on the
        # error path (matters on Windows, where a lingering handle can keep
        # the user's file locked until GC gets around to it).
        with Image.open(src) as img:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "LA", "PA", "P"):
                # Composite transparency onto white instead of a bare
                # convert("RGB"), which renders transparent areas black.
                # Also covers LA/PA, whose alpha made the JPEG save fail
                # outright (no thumbnail at all).
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            dst.parent.mkdir(parents=True, exist_ok=True)
            img.save(dst, "JPEG", quality=70)
    except Exception as e:
        print(f"[Warning] Thumbnail failed for {src.name}: {e}")


# Starlette's FileResponse falls back to Python's `mimetypes` module when
# no media_type is given, which on Windows reads from the registry rather
# than a built-in table — .webp (and sometimes others) frequently isn't
# registered there, silently serving image/webp files as
# application/octet-stream instead. Serving these explicitly sidesteps
# that OS-dependent guesswork entirely. Caught by Windows CI, not visible
# on Linux where Python's built-in mimetypes table already has .webp.
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


def _media_type_for(path: Path) -> Optional[str]:
    return _MEDIA_TYPES.get(path.suffix.lower())


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024 * 1024):.2f} MB"


async def _run_worker_pool(
    items: list,
    process_item: Callable[[Any], Awaitable[None]],
    *,
    n_workers: Optional[int] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    pause_check: Optional[Callable[[], bool]] = None,
    on_item_error: Optional[Callable[[Any, Exception], None]] = None,
    on_item_done: Optional[Callable[[Any], None]] = None,
):
    """Shared queue + worker pool used by both the scan flow
    (`_scan_and_thumbnail`) and the compression flow (`_process_files`).

    What's shared here is exactly the part that was duplicated:
      - pre-fill an asyncio.Queue with the work items
      - spin up N workers, each draining the queue with get_nowait()
      - await asyncio.gather() on the pool
      - call `on_item_done(item)` once per processed item (success or
        per-item exception) so a progress counter is bumped exactly once
        — both scan and optimize had the same `finally: state.X += 1`
        line before, so promoting it here. Cancelled-but-not-started
        items do NOT call it (see the cancel note below).
      - make sure an unhandled exception raised by `process_item` degrades
        to `on_item_error(item, e)` and never propagates out of the worker
        (an uncaught exception makes gather() return immediately while the
        other workers keep running orphaned — see CHANGELOG "[Unreleased]"
        and test_concurrency.test_one_file_raising_does_not_orphan_the_others
        for the bug this guard prevents).

    What is NOT shared, on purpose:
      - how a result is stored: scan assigns into a preallocated list by
        index (deterministic sorted order, regression-tested by
        test_files_are_in_sorted_order_despite_concurrency); optimize
        appends to state.results. Each side keeps its own storage logic
        inside its `process_item` closure.
      - cancel behavior: optimize drains the queue without starting new
        work once cancelled; scan has no cancel path. Kept in `cancel_check`
        so it's only checked when the caller opts in. A cancelled item is
        drained without calling `on_item_done` — that matches the old
        optimize behavior, where cancelled-but-not-started files did NOT
        bump the progress counter, so `state.current < state.total`
        after a cancel is the signal that the run was partial (see
        test_cancel_stops_new_work_but_finishes_in_flight).

    `n_workers` reads the module global `CONCURRENT_WORKERS` lazily at call
    time rather than as a default argument: `main()` rebinds that global
    from config.json/`--workers` after the module body has finished running,
    so capturing it as a default would freeze it at the startup default of 4
    and ignore the configured value.
    """
    if n_workers is None:
        n_workers = CONCURRENT_WORKERS

    queue: asyncio.Queue = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)

    async def worker():
        while True:
            # Cancel takes precedence over everything (including pause): a
            # paused-then-cancelled run drains the queue and ends. We check
            # it first so a worker stuck in the pause loop below still
            # observes a cancel and exits. See test_cancel_during_pause_ends_run.
            cancelled = cancel_check and cancel_check()
            if cancelled:
                # Drain one item without processing. A cancelled item
                # intentionally does NOT call on_item_done() — the original
                # optimize flow didn't bump the progress counter for
                # cancelled items (only files that actually started
                # processing counted toward state.current), and that
                # distinction is how /api/progress reports "current < total"
                # after a cancel. Bumping here would erase that signal.
                # See test_cancel_stops_new_work_but_finishes_in_flight.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                queue.task_done()
                continue
            # Soft pause (optimize only): stop SCHEDULING new items, but
            # anything already in flight (past this gate on another worker)
            # runs to completion — no subprocess kill, no signal handling.
            # Sleep briefly rather than busy-spinning; resume latency is one
            # sleep interval (~50ms). If the queue is already empty there's
            # nothing held back, so the worker exits (prevents a deadlock
            # where a paused run with no queued work never completes).
            if pause_check and pause_check():
                if queue.empty():
                    return
                await asyncio.sleep(0.05)
                continue
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await process_item(item)
            except Exception as e:
                # Any exception process_item doesn't already catch must be
                # caught here, per-item — see the comment block above.
                if on_item_error is not None:
                    on_item_error(item, e)
            finally:
                if on_item_done is not None:
                    on_item_done(item)
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(max(1, n_workers))]
    await asyncio.gather(*workers)


SCAN_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}


def _scan_images(directory: Path, recursive: bool = True) -> list:
    # One directory walk with a case-folded suffix check, instead of one
    # glob pass per extension×case pattern. Catches mixed-case names like
    # "photo.Png" on case-sensitive filesystems (the old explicit
    # upper/lower pattern list missed those), and includes .webp — the
    # optimizer reads it like any other Pillow-supported input.
    iter_method = directory.rglob if recursive else directory.glob
    return sorted(p for p in iter_method("*") if p.is_file() and p.suffix.lower() in SCAN_EXTS)


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
    # Re-detect every health check so that placing pngquant/oxipng in the
    # bin/ directory (or PATH) after startup is reflected on the next page
    # refresh — no restart needed. Detection shells out to `where`/`which`,
    # which is blocking I/O — run it off the event loop so a health check
    # never stalls other sessions' in-flight requests.
    await asyncio.to_thread(optimizer._detect_binaries)
    return JSONResponse({
        "pngquant": optimizer.pngquant_path is not None,
        "oxipng": optimizer.oxipng_path is not None,
        "pngquant_path": str(optimizer.pngquant_path) if optimizer.pngquant_path else None,
        "oxipng_path": str(optimizer.oxipng_path) if optimizer.oxipng_path else None,
    })


async def _scan_and_thumbnail(state: AppState, directory: Path, recursive: bool):
    ws = state.workspace
    try:
        # `asyncio.get_event_loop()` inside a coroutine is the deprecated way
        # of grabbing the loop — on Python 3.10+ `get_running_loop()` is
        # the explicit counterpart, and is always safe here because we're in
        # an async context. (See also upload_files/browse_folder below,
        # where `asyncio.to_thread()` is the natural fit instead of going
        # through an explicit loop reference.)
        loop = asyncio.get_running_loop()
        images = await loop.run_in_executor(None, _scan_images, directory, recursive)
    except Exception as e:
        state.scan_error = str(e)
        state.scan_running = False
        return

    state.scan_total = len(images)
    files: list = [None] * len(images)  # preallocated so results land in a
    # stable, deterministic order (matching the sorted scan order) even
    # though workers finish in whatever order thumbnailing completes.

    async def process_item(item):
        idx, img_path = item
        rel = str(img_path.relative_to(directory))
        thumb_rel = f"thumb/{idx}_{img_path.stem}.jpg"
        thumb_path = ws / thumb_rel
        if not thumb_path.exists():
            await asyncio.to_thread(_gen_thumbnail, img_path, thumb_path)
        files[idx] = {
            "id": str(idx),
            "name": rel,
            "path": str(img_path),
            "size": img_path.stat().st_size,
            "thumbnail": f"/api/thumb/{ws.name}/{thumb_rel}",
        }

    def on_item_error(item, e):
        # One unreadable/corrupt file shouldn't stop the rest of the scan —
        # same reasoning as the optimize worker pool.
        idx, img_path = item
        files[idx] = {
            "id": str(idx),
            "name": str(img_path),
            "path": str(img_path),
            "size": 0,
            "thumbnail": "",
            "error": str(e),
        }

    def on_item_done(_item):
        state.scan_current += 1

    await _run_worker_pool(
        [(idx, img_path) for idx, img_path in enumerate(images)],
        process_item,
        n_workers=THUMBNAIL_WORKERS,
        on_item_error=on_item_error,
        on_item_done=on_item_done,
    )

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
    # Mirror the /api/scan guard: scanning runs in a fire-and-forget
    # background task writing thumbnails into state.workspace. Replacing
    # the workspace now would queue a _delayed_rmtree on it while those
    # workers are still writing — picture a worker finishing its
    # thumbnail write to a path in a directory the OS is about to nuke,
    # or a mid-scan tab being silently replaced because the user dropped
    # new files in another. Reject and let the in-flight scan finish.
    if state.scan_running:
        return JSONResponse({"error": "A scan is already in progress, please wait"}, status_code=400)
    state.reset()
    ws = state.new_workspace()
    upload_dir = ws / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    result_files = []
    for idx, f in enumerate(files):
        if not f.filename:
            continue
        # Path(...).name strips any directory components a non-browser
        # client could smuggle into the filename (browsers only ever send
        # a basename). The sanitized name must be used for BOTH the stored
        # file and the reported "name" field — file_info["name"] is later
        # joined onto the output directory in _process_files, so a raw
        # "../"-carrying filename would write outside the workspace.
        name = Path(f.filename.replace("\\", "/")).name
        if not name:
            continue
        unique_name = f"{int(time.time())}_{idx}_{name}"
        file_path = upload_dir / unique_name
        # Stream to disk in 1 MiB chunks instead of buffering each whole
        # file in memory — a batch of large images would otherwise spike
        # memory by the combined upload size.
        size = 0
        with open(file_path, "wb") as out:
            while chunk := await f.read(1024 * 1024):
                out.write(chunk)
                size += len(chunk)

        thumb_rel = f"thumb/upload_{idx}_{name}.jpg"
        thumb_path = ws / thumb_rel
        await asyncio.to_thread(_gen_thumbnail, file_path, thumb_path)

        result_files.append({
            "id": str(idx),
            "name": name,
            "path": str(file_path),
            "size": size,
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
    if data.quality not in ("high", "medium", "low"):
        # Previously an unknown quality silently fell back to "medium" deep
        # inside the optimizer — reject it here like the other enum fields
        # so a typo'd client request fails loudly instead.
        return JSONResponse(
            {"error": f"Invalid quality: {data.quality!r}. Supported: 'high', 'medium', 'low'."},
            status_code=400,
        )
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
    state.cancelled = False
    state.paused = False
    if data.retry:
        # Retry re-runs only the previously-failed files. Keep every result
        # that isn't being retried (the earlier successes and their outputs
        # on disk stay intact) and drop just the stale entries for the ids
        # about to be re-run, so they don't appear twice once the fresh
        # results are appended.
        retry_ids = {f["id"] for f in files_to_process}
        state.results = [r for r in state.results if r.get("id") not in retry_ids]
    else:
        state.results = []

    # Surface degraded capability up front instead of only as a per-file
    # log line: PNG+standard without pngquant still runs, but silently
    # downgrades to lossless-only compression — the user should know why
    # their files barely shrank. available_modes() is the source of truth.
    warning = None
    if optimizer is not None and (data.output_format, data.compression_mode) not in optimizer.available_modes():
        warning = (
            "pngquant not found — Standard mode PNG output falls back to lossless-only "
            "compression (larger files). Place pngquant in the bin/ directory for full compression."
        )
        state.logs.append(f"[Warning] {warning}")

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
            retry=data.retry,
            skip_existing=data.skip_existing,
            keep_exif=data.keep_exif,
        )
    )

    return JSONResponse({"ok": True, "total": len(files_to_process), "warning": warning})


CONCURRENT_WORKERS = 4  # default; overridden by main() from config.json / --workers
# Compression concurrency knob (alias of CONCURRENT_WORKERS for readability at
# the call site). Kept as the same name the tests monkeypatch
# (CONCURRENT_WORKERS) so the existing pause/concurrency tests keep working
# when they force a single compress worker.
# THUMBNAIL_WORKERS is the independent scan-thumbnail concurrency knob —
# splitting the two lets a scan that's I/O-bound on thumbnail generation not
# starve (or be starved by) a concurrent compression run in another session,
# and lets each be tuned to the machine. See OPTIMIZATION_PLAN.md §6.
THUMBNAIL_WORKERS = 4  # default; overridden by main() from config.json / --thumbnail-workers


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
    retry: bool = False,
    skip_existing: bool = False,
    keep_exif: bool = False,
):
    if optimizer is None:
        state.logs.append("Optimizer not initialized")
        state.is_running = False
        return

    ws = state.workspace
    opt_output_dir = ws / "output"
    if opt_output_dir.exists() and not retry:
        # Off the event loop: a leftover output dir from a previous run could
        # hold thousands of files; deleting them synchronously here would
        # block the worker spawn below (and any other concurrent async work
        # in this process) for the duration of the unlink.
        #
        # Skipped ONLY on a retry: the earlier run's successful outputs live
        # in here, and retry only re-runs the previously-failed files —
        # wiping the dir would delete good results (breaking their Compare
        # links and the download ZIP) just to regenerate a few failures.
        #
        # skip_existing does NOT suppress the wipe. Its purpose is purely
        # per-file ("can this file be reused instead of recompressed?"), and
        # the reuse path copies from output_dir back into ws/output anyway —
        # it never relies on ws/output surviving. Coupling the wipe to it
        # meant a normal subset run with skip_existing on would leave last
        # run's now-unselected outputs in ws/output, and the whole-dir ZIP
        # build would then leak those stale files into the download even
        # though they aren't in this run's results/selection.
        await _async_rmtree(opt_output_dir)
    opt_output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-assign every file's output path so name collisions are resolved
    # deterministically: "photo.png" and "photo.jpg" both map to
    # "photo.png" after the suffix swap and would silently overwrite each
    # other mid-batch (last worker to finish wins, both reported as
    # successes). First occurrence keeps the plain name; later collisions
    # get a numbered stem ("photo_2.png"). Keys are case-folded because
    # Windows/macOS filesystems collide case-insensitively. A None entry
    # marks a name whose resolved path escapes the output dir (should be
    # impossible after upload-side sanitization — defense in depth) and is
    # turned into a per-file failure inside process_one.
    base_resolved = str(opt_output_dir.resolve())
    out_paths: dict[str, Optional[Path]] = {}
    used_names: set[str] = set()
    for file_info in files:
        base = (opt_output_dir / file_info["name"]).with_suffix(f".{output_format}")
        candidate = base
        n = 2
        while str(candidate).lower() in used_names:
            candidate = base.with_name(f"{base.stem}_{n}{base.suffix}")
            n += 1
        used_names.add(str(candidate).lower())
        resolved = str(candidate.resolve())
        inside = resolved == base_resolved or resolved.startswith(base_resolved + os.sep)
        out_paths[file_info["id"]] = candidate if inside else None

    async def log(msg):
        state.logs.append(f"  {msg}")

    async def process_one(file_info: dict):
        input_path = Path(file_info["path"])

        # file_info["name"] is always the clean, structure-preserving
        # relative name (e.g. "2023/vacation/IMG_0001.png") for both the
        # scan and upload flows — out_paths derives from it directly rather
        # than from the physical storage path, which for uploads is
        # intentionally flattened/deduplicated and does NOT mirror the
        # original folder structure.
        out_path = out_paths[file_info["id"]]
        if out_path is None:
            raise ValueError("unsafe file name escapes the output directory")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Skip-already-optimized: if the user re-runs against the same output
        # folder and this file's target is already there, reuse it instead of
        # paying pngquant/oxipng again. We still copy it back into ws/output so
        # Compare/preview (/api/result) and the download ZIP keep working — the
        # copy is cheap next to a re-compress. No-op without output_dir (the
        # temp workspace is wiped/empty each fresh run, nothing to reuse).
        if skip_existing and output_dir:
            rel = out_path.relative_to(opt_output_dir)
            dest = Path(output_dir) / rel
            if dest.exists() and dest.stat().st_size > 0:
                shutil.copy2(dest, out_path)
                orig_size = input_path.stat().st_size if input_path.exists() else 0
                comp_size = dest.stat().st_size
                savings = orig_size - comp_size
                skipped_result = {
                    "id": file_info["id"],
                    "name": file_info["name"],
                    "original_path": file_info["path"],
                    "output_format": output_format,
                    "output_name": str(rel),
                    "success": True,
                    "skipped": True,
                    "original_size": orig_size,
                    "compressed_size": comp_size,
                    "savings": savings,
                    "savings_percent": round(savings / orig_size * 100, 1) if orig_size > 0 else 0,
                }
                state.logs.append(f"  SKIP {file_info['name']} (already optimized)")
                state.results.append(skipped_result)
                return

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
            keep_exif=keep_exif,
        )

        result["id"] = file_info["id"]
        result["name"] = file_info["name"]
        result["original_path"] = file_info["path"]
        result["output_format"] = output_format
        # The actual output location relative to ws/output — may differ
        # from name+suffix when a collision was disambiguated above, so
        # consumers (e.g. /api/preview) must use this instead of
        # re-deriving it from "name".
        result["output_name"] = str(out_path.relative_to(opt_output_dir))

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

        # The progress bump moved to _run_worker_pool's on_item_done — see
        # the No-lock-note there (same single-threaded-cooperative reasoning
        # applies: state.results.append + on_item_done's state.current += 1
        # have no `await` between them, so no other worker interleaves).
        state.results.append(result)

    def on_item_error(file_info, e):
        # Anything process_one doesn't already catch internally (optimize_png
        # has its own try/except, but e.g. mkdir permission errors or a
        # malformed file_info wouldn't be) lands here. Degrading to a
        # normal failed-file result keeps one bad file from taking down the
        # whole batch — see test_concurrency.test_one_file_raising_does_not_orphan_the_others.
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

    def on_item_done(_file_info):
        state.current += 1

    try:
        await _run_worker_pool(
            files,
            process_one,
            n_workers=CONCURRENT_WORKERS,
            cancel_check=lambda: state.cancelled,
            pause_check=lambda: state.paused,
            on_item_error=on_item_error,
            on_item_done=on_item_done,
        )
        if state.cancelled:
            state.logs.append("Cancelled by user")

    except Exception as e:
        state.logs.append(f"  UNEXPECTED ERROR: {e}")
        import traceback
        state.logs.append(f"  {traceback.format_exc()}")

    finally:
        state.is_running = False
        # Outputs for this run are now final — invalidate any cached ZIP so the
        # next download rebuilds (see download_zip). Bumped once per run (fresh
        # or retry), which is exactly when ws/output contents change.
        state.output_version += 1
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


@app.get("/api/progress")
async def get_progress(
    state: AppState = Depends(get_session),
    since_result: int = 0,
    since_log: Optional[int] = None,
):
    # Incremental cursor: the frontend passes how many results/log lines it
    # has already received so we only send the tail. For a few-thousand-file
    # batch, re-sending the whole results array every 400ms poll was O(n) per
    # poll / O(n^2) over the run — the dominant client-side cost. Slicing from
    # a cursor makes each result/log line travel exactly once (O(n) total).
    #
    # Backward compatible: since_result defaults to 0 (results[0:] == full) and
    # since_log defaults to None (last-100 lines, the original behavior), so
    # callers/tests that omit the params see the pre-existing full payload.
    return JSONResponse({
        "running": state.is_running,
        "paused": state.paused,
        "current": state.current,
        "total": state.total,
        "logs": state.logs[since_log:] if since_log is not None else state.logs[-100:],
        "results": state.results[since_result:],
        "result_total": len(state.results),
        "log_total": len(state.logs),
    })


@app.post("/api/cancel")
async def cancel(state: AppState = Depends(get_session), _auth: None = Depends(require_token)):
    state.cancelled = True
    return JSONResponse({"ok": True})


@app.post("/api/pause")
async def pause(state: AppState = Depends(get_session), _auth: None = Depends(require_token)):
    """Soft pause: the worker pool stops scheduling NEW files, but any file
    already mid-compression (its pngquant/oxipng subprocess running) finishes
    naturally — no process kill. Resume drains the rest. Only valid while a
    run is actually in progress; pausing an idle session is a no-op error."""
    if not state.is_running:
        return JSONResponse({"error": "No optimization in progress"}, status_code=400)
    state.paused = True
    return JSONResponse({"ok": True})


@app.post("/api/resume")
async def resume(state: AppState = Depends(get_session), _auth: None = Depends(require_token)):
    if not state.paused:
        return JSONResponse({"error": "Not paused"}, status_code=400)
    state.paused = False
    return JSONResponse({"ok": True})


@app.get("/api/source-file/{ws_name}/{file_id}")
async def get_source_file(ws_name: str, file_id: str, state: AppState = Depends(get_session)):
    if state.workspace and state.workspace.name == ws_name:
        for f in state.files:
            if f["id"] == file_id:
                p = Path(f["path"])
                if p.exists():
                    return FileResponse(str(p), media_type=_media_type_for(p))
        for r in state.results:
            if r.get("id") == file_id:
                p = Path(r["original_path"])
                if p.exists():
                    return FileResponse(str(p), media_type=_media_type_for(p))
    return Response(status_code=404)


@app.get("/api/result/{ws_name}/{result_path:path}")
async def get_result(ws_name: str, result_path: str, state: AppState = Depends(get_session)):
    if state.workspace and state.workspace.name == ws_name:
        p = (state.workspace / "output" / result_path).resolve()
        base = str((state.workspace / "output").resolve())
        if p.exists() and (str(p) == base or str(p).startswith(base + os.sep)):
            return FileResponse(str(p), media_type=_media_type_for(p))
    return Response(status_code=404)


@app.get("/api/preview/{ws_name}/{file_id}")
async def preview(ws_name: str, file_id: str, state: AppState = Depends(get_session)):
    # Same ownership check as every other workspace-scoped endpoint. Doing
    # it FIRST also closes a reflected-XSS hole: ws_name is an arbitrary
    # URL path segment that gets embedded in the HTML below — without this
    # gate (plus the html.escape on the URLs), a crafted ws_name like
    # `"><script>...` would execute in this app's origin and could then
    # harvest APP_TOKEN from the index page, defeating the CSRF defense.
    if not (state.workspace and state.workspace.name == ws_name):
        return HTMLResponse("<h2>Not found</h2>", status_code=404)
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
        return HTMLResponse("<h2>Not found</h2>", status_code=404)

    import html as _html

    orig_url = _html.escape(f"/api/source-file/{ws_name}/{file_id}", quote=True)

    # Use the exact output path recorded by _process_files — it may carry a
    # collision-disambiguated name ("photo_2.png") that can't be re-derived
    # from result["name"]. Fallback keeps old sessions/results working.
    out_fmt = result.get("output_format", "png")
    comp_rel = result.get("output_name") or str(Path(result["name"]).with_suffix(f".{out_fmt}"))

    comp_url = _html.escape(f"/api/result/{ws_name}/{comp_rel}", quote=True) if comp_rel else ""

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

    def _build_zip():
        # ZipFile + glob + write are all synchronous blocking I/O — running
        # them inline in an async endpoint would freeze the whole event
        # loop for the duration of the archive build (progress polls for
        # this same session, plus every other session's image thumbnails
        # and concurrent scans, all stall out). Hand the synchronous work
        # to a thread executor and await the result.
        with ZipFile(zip_path, "w") as zf:
            for f in output_dir.rglob("*"):
                if f.is_file():
                    arcname = str(f.relative_to(output_dir))
                    zf.write(f, arcname)

    # Cache: rebuild only when the archive is missing or the outputs changed
    # since it was built (output_version bumps once per completed run). A
    # repeated download of an unchanged batch — or a second click after the
    # browser's first request — then serves the existing file without paying
    # the rglob + re-compress cost again.
    if not (zip_path.exists() and state.zip_built_version == state.output_version):
        await asyncio.to_thread(_build_zip)
        state.zip_built_version = state.output_version

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
            # Without DPI awareness, Windows upscales the native dialog using
            # bitmap stretching on high-DPI displays — the whole window looks
            # noticeably blurry. Declare per-monitor awareness before creating
            # any Tk widgets so the dialog renders at the native resolution.
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
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
        path = await asyncio.to_thread(_open)
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


@app.post("/api/reset")
async def reset_session(state: AppState = Depends(get_session), _auth: None = Depends(require_token)):
    """Clear this session's server-side state (the UI Reset button used to
    only wipe the page, leaving files/results alive server-side — a page
    refresh with /api/state restore would resurrect them). The workspace is
    deleted with the same grace period as a workspace swap."""
    if state.is_running:
        return JSONResponse({"error": "Optimization in progress, please wait"}, status_code=400)
    if state.scan_running:
        return JSONResponse({"error": "A scan is already in progress, please wait"}, status_code=400)
    if state.workspace:
        asyncio.create_task(_delayed_rmtree(state.workspace))
        state.workspace = None
    state.reset()
    return JSONResponse({"ok": True})


def main():
    import argparse

    config = _load_app_config()

    parser = argparse.ArgumentParser(description="Image Optimizer Web UI")
    parser.add_argument(
        "--host", default=config["host"],
        help=f"Listen address (default {config['host']!r} / localhost-only, or set 'host' in "
             f"{_config_file()}). WARNING: this app has no authentication — binding to 0.0.0.0 "
             f"or a LAN address exposes local file read (scan any directory) and write "
             f"(output_dir) to anyone on the network.",
    )
    def _port_int(raw: str) -> int:
        # argparse `type` callback: rejects non-integer or out-of-range
        # BEFORE main() even runs, so `--port notanumber` / `--port 0` /
        # `--port 99999` exit with the standard argparse usage error rather
        # than getting handed to the socket layer (where `--port 0` would
        # bind to an arbitrary OS-assigned port without any user-facing
        # signal, and out-of-range ints would just be silently truncated).
        try:
            v = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}")
        if not (1 <= v <= 65535):
            raise argparse.ArgumentTypeError(f"port must be in 1..65535, got {v}")
        return v

    parser.add_argument(
        "--port", type=_port_int, default=config["port"],
        help=f"Listen port (default {config['port']}, or set 'port' in {_config_file()})",
    )
    parser.add_argument(
        "--workers", type=int, default=config["concurrent_workers"],
        help=f"How many images to compress concurrently (default {config['concurrent_workers']}, "
             f"or set 'concurrent_workers' in {_config_file()}). Lower this if pngquant/oxipng "
             f"are already maxing out your CPU; raise it on a many-core machine.",
    )
    parser.add_argument(
        "--thumbnail-workers", type=int, default=config["thumbnail_workers"],
        help=f"How many images to thumbnail concurrently during a scan (default "
             f"{config['thumbnail_workers']}, or set 'thumbnail_workers' in {_config_file()}). "
             f"Independent from --workers so a scan's I/O-bound thumbnailing can be tuned "
             f"separately from CPU-bound compression.",
    )
    parser.add_argument("--dir", help="Directory to auto-scan on startup")
    # Defaults to True to match the historical CLI behavior — the UI checkbox
    # defaults to False, but `--dir` has always scanned recursively since the
    # feature shipped. --no-recursive lets an operator override that (e.g. a
    # folder with thousands of files nested under deep subfolder trees where
    # only the top-level files are wanted) without breaking existing
    # invocations that never passed this flag at all.
    parser.add_argument(
        "--recursive", action=argparse.BooleanOptionalAction, default=True,
        help="When used with --dir, scan subfolders too (default True, the "
             "historical behavior; pass --no-recursive to scan only the top "
             "level).",
    )
    args = parser.parse_args()

    global CONCURRENT_WORKERS, THUMBNAIL_WORKERS, WORKSPACE_CLEANUP_DELAY, SESSION_IDLE_TIMEOUT
    CONCURRENT_WORKERS = max(1, args.workers)
    THUMBNAIL_WORKERS = max(1, args.thumbnail_workers)
    WORKSPACE_CLEANUP_DELAY = config["workspace_cleanup_delay"]
    SESSION_IDLE_TIMEOUT = config["session_idle_timeout_hours"] * 3600

    if args.dir:
        d = Path(args.dir).resolve()
        if d.exists() and d.is_dir():
            global _cli_prescan_state
            prescan = AppState()
            prescan.input_dir = str(d)
            ws = prescan.new_workspace()
            images = _scan_images(d, recursive=args.recursive)
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