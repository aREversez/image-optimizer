"""Shared pytest fixtures.

pngquant.exe/oxipng.exe are Windows binaries and won't run in CI on Linux/
macOS runners, so every test uses small stand-in scripts that mimic just
enough of their real behavior (reject non-PNG input with a non-zero exit
and no output file; otherwise copy bytes through) to exercise the actual
async subprocess-calling code in app/optimizer.py. This is the same
technique used throughout manual testing during development — see
git log for the reasoning captured in commit messages.
"""
from __future__ import annotations

import re
import stat
import sys
from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PNGQUANT_IMPL = '''#!/usr/bin/env python3
import sys
PNG_MAGIC = b"\\x89PNG\\r\\n\\x1a\\n"
args = sys.argv[1:]
out_path = args[args.index("--output") + 1]
in_path = args[-1]
data = open(in_path, "rb").read()
if data[:8] == PNG_MAGIC:
    open(out_path, "wb").write(data)
    sys.exit(0)
else:
    sys.stderr.write("pngquant: cannot decode input file: not a PNG file\\n")
    sys.exit(1)
'''

OXIPNG_IMPL = '''#!/usr/bin/env python3
import sys
PNG_MAGIC = b"\\x89PNG\\r\\n\\x1a\\n"
args = sys.argv[1:]
out_path = args[args.index("--out") + 1]
in_path = args[-1]
data = open(in_path, "rb").read()
if data[:8] == PNG_MAGIC:
    open(out_path, "wb").write(data)
    sys.exit(0)
else:
    sys.stderr.write("oxipng: invalid PNG header\\n")
    sys.exit(1)
'''


@pytest.fixture
def fake_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    pngquant_impl = bin_dir / "pngquant_impl.py"
    oxipng_impl = bin_dir / "oxipng_impl.py"
    pngquant_impl.write_text(PNGQUANT_IMPL)
    oxipng_impl.write_text(OXIPNG_IMPL)

    if sys.platform == "win32":
        # Optimizer._find_binary only looks for a literal *.exe on
        # Windows, and a shebang script can't pass as one — a real PE
        # binary would be needed for auto-detection to find it. Instead
        # of fighting that, tests bypass auto-detection entirely (see
        # `optimizer`/`app_module` fixtures below) and use a .bat wrapper,
        # which Windows can execute directly via CreateProcess without
        # needing shell=True, forwarding straight through to the same
        # Python logic used on every other platform.
        pngquant = bin_dir / "pngquant.bat"
        oxipng = bin_dir / "oxipng.bat"
        pngquant.write_text(f'@echo off\r\n"{sys.executable}" "{pngquant_impl}" %*\r\n')
        oxipng.write_text(f'@echo off\r\n"{sys.executable}" "{oxipng_impl}" %*\r\n')
    else:
        pngquant = bin_dir / "pngquant"
        oxipng = bin_dir / "oxipng"
        pngquant.write_text(f'#!/usr/bin/env python3\nimport subprocess, sys\nsys.exit(subprocess.call([{sys.executable!r}, {str(pngquant_impl)!r}, *sys.argv[1:]]))\n')
        oxipng.write_text(f'#!/usr/bin/env python3\nimport subprocess, sys\nsys.exit(subprocess.call([{sys.executable!r}, {str(oxipng_impl)!r}, *sys.argv[1:]]))\n')
        pngquant.chmod(pngquant.stat().st_mode | stat.S_IEXEC)
        oxipng.chmod(oxipng.stat().st_mode | stat.S_IEXEC)
    return bin_dir


@pytest.fixture
def optimizer(fake_bin_dir: Path):
    from app.optimizer import Optimizer
    opt = Optimizer(bin_dir=fake_bin_dir)
    # Bypass auto-detection's OS-specific naming assumptions entirely —
    # the fixture already knows exactly which file to point at for this
    # platform (see fake_bin_dir above).
    ext = ".bat" if sys.platform == "win32" else ""
    opt.pngquant_path = fake_bin_dir / f"pngquant{ext}"
    opt.oxipng_path = fake_bin_dir / f"oxipng{ext}"
    return opt


@pytest.fixture
def test_images(tmp_path: Path) -> Path:
    """A directory of test images deliberately shaped to exercise the bugs
    this project has actually hit: same filename in two different
    subfolders (output-directory overwrite bug), and a non-PNG source
    (format-normalization bug)."""
    root = tmp_path / "images"
    (root / "2023" / "vacation").mkdir(parents=True)
    (root / "2024" / "vacation").mkdir(parents=True)
    Image.new("RGB", (64, 64), (46, 204, 113)).save(root / "2023" / "vacation" / "IMG_0001.png")
    Image.new("RGB", (64, 64), (200, 50, 50)).save(root / "2024" / "vacation" / "IMG_0001.png")
    Image.new("RGB", (80, 80), (10, 10, 200)).save(root / "photo.jpg", format="JPEG")
    return root


@pytest.fixture
def app_module(optimizer):
    """Points app.main's global `optimizer` at the fake binaries, so tests
    never depend on real pngquant/oxipng being installed. app.main is only
    imported once per pytest process (module caching), which is fine —
    SESSIONS/APP_TOKEN staying shared across tests doesn't leak state
    between tests since every test gets its own TestClient (see `client`
    below) and therefore its own session cookie / AppState."""
    import app.main as m
    m.optimizer = optimizer
    return m


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def token(client) -> str:
    r = client.get("/")
    m = re.search(r'window\.APP_TOKEN = "([^"]+)"', r.text)
    assert m, "APP_TOKEN not found in served page — auth setup broken"
    return m.group(1)


@pytest.fixture
def auth_headers(token) -> dict:
    return {"X-App-Token": token}


@pytest.fixture(autouse=True)
def isolate_user_config_dir(monkeypatch, tmp_path_factory):
    """Every test gets its own fake home directory for
    app.main._config_dir() (~/.image-optimizer — recent.json and
    config.json both live there). Without this, running the test suite
    silently writes real recent.json entries (full of throwaway pytest
    tmp paths) into the actual developer's home directory on every run,
    since most tests trigger a scan and _scan_and_thumbnail() always
    calls _push_recent() at the end. autouse=True so no test has to
    remember to opt in — forgetting isn't just untidy here, it pollutes
    a real user directory outside the test sandbox entirely."""
    from pathlib import Path
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", lambda: fake_home)


def wait_for(predicate, timeout=10.0, interval=0.02):
    """Poll `predicate()` until it returns a truthy value or timeout."""
    import time
    deadline = time.time() + timeout
    result = predicate()
    while not result and time.time() < deadline:
        time.sleep(interval)
        result = predicate()
    assert result, f"condition not met within {timeout}s"
    return result
