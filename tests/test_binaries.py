"""Tests for the first-run encoder bootstrap (image_optimizer/binaries.py).

Fully offline: a threaded http.server on 127.0.0.1 serves exactly the bytes
each test wants. That also pins down an intended property — the trust anchor
is the pinned digest, not TLS — so a plain-HTTP local server is a valid
stand-in for GitHub.

The downloader is deliberately paranoid (it executes what it fetched), so the
interesting tests here are the negative ones: wrong digest, wrong member name,
hostile archive paths, unreachable host.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import socket
import tarfile
import threading
import zipfile
from functools import partial
from pathlib import Path

import pytest

from image_optimizer import binaries

# Opt out of conftest's no_binary_downloads stub: this module is the one place
# that must exercise the real ensure_binaries() (against a loopback server).
pytestmark = pytest.mark.real_binary_bootstrap


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _tar_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - silence the test output
        pass


@pytest.fixture
def served(tmp_path: Path):
    """(base_url, document_root) for a throwaway local HTTP server."""
    root = tmp_path / "served"
    root.mkdir()
    handler = partial(_QuietHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", root
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def isolated_binary_dir(monkeypatch, tmp_path: Path) -> Path:
    """Never touch the developer's real %LOCALAPPDATA%/ImageOptimizer."""
    target = tmp_path / "binhome"
    monkeypatch.setattr(binaries, "binary_dir", lambda: target)
    return target


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- happy path ----------------------------------------------------------


def test_installs_verified_zip(served, isolated_binary_dir):
    base, root = served
    payload = b"#!fake pngquant payload\n"
    (root / "pkg.zip").write_bytes(_zip_bytes({"pngquant.exe": payload}))
    artifact = binaries.Artifact(
        tool="pngquant",
        version="0.0.1",
        url=f"{base}/pkg.zip",
        member="pngquant.exe",
        member_sha256=_sha(payload),
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert report.ok
    assert report.installed == ["pngquant"]
    assert (isolated_binary_dir / "pngquant.exe").read_bytes() == payload


def test_installs_archive_digest_verified_tarball(served, isolated_binary_dir):
    """POSIX oxipng path: digest covers the archive, binary needs +x."""
    base, root = served
    archive = _tar_bytes({"oxipng-10.1.1-x86_64-unknown-linux-gnu/oxipng": b"ELF-ish"})
    (root / "ox.tar.gz").write_bytes(archive)
    artifact = binaries.Artifact(
        tool="oxipng",
        version="0.0.1",
        url=f"{base}/ox.tar.gz",
        member="oxipng",
        sha256=_sha(archive),
        kind="tar.gz",
        executable=True,
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert report.ok, report.failed
    installed = isolated_binary_dir / "oxipng"
    assert installed.read_bytes() == b"ELF-ish"
    # Path traversal is impossible, so the only risk left is a non-executable
    # bit making the "successful" install useless at exec time.
    if binaries.sys.platform != "win32":
        assert installed.stat().st_mode & 0o111


def test_existing_verified_copy_is_not_downloaded_again(served, isolated_binary_dir):
    base, root = served  # note: nothing published under this root
    payload = b"already here"
    isolated_binary_dir.mkdir(parents=True, exist_ok=True)
    (isolated_binary_dir / "pngquant.exe").write_bytes(payload)
    artifact = binaries.Artifact(
        tool="pngquant",
        version="0.0.1",
        url=f"{base}/never-fetched.zip",
        member="pngquant.exe",
        member_sha256=_sha(payload),
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert report.present == ["pngquant"]
    assert report.installed == []
    assert report.ok


# --- negative paths -----------------------------------------------------


def test_rejects_member_digest_mismatch(served, isolated_binary_dir):
    base, root = served
    (root / "pkg.zip").write_bytes(_zip_bytes({"pngquant.exe": b"tampered"}))
    artifact = binaries.Artifact(
        tool="pngquant",
        version="0.0.1",
        url=f"{base}/pkg.zip",
        member="pngquant.exe",
        member_sha256=_sha(b"the real thing"),
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert "pngquant" in report.failed
    assert "mismatch" in report.failed["pngquant"]
    assert not (isolated_binary_dir / "pngquant.exe").exists()
    # A rejected artifact must not leave a .part file that a later run could
    # mistake for a partial install.
    assert not list(isolated_binary_dir.glob("*.part"))


def test_rejects_archive_digest_mismatch(served, isolated_binary_dir):
    base, root = served
    (root / "pkg.zip").write_bytes(_zip_bytes({"pngquant.exe": b"payload"}))
    artifact = binaries.Artifact(
        tool="pngquant",
        version="0.0.1",
        url=f"{base}/pkg.zip",
        member="pngquant.exe",
        sha256=_sha(b"a different archive entirely"),
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert "archive SHA-256 mismatch" in report.failed["pngquant"]
    assert not (isolated_binary_dir / "pngquant.exe").exists()


def test_refuses_artifact_without_digest(served, isolated_binary_dir):
    base, root = served
    (root / "pkg.zip").write_bytes(_zip_bytes({"pngquant.exe": b"payload"}))
    artifact = binaries.Artifact(
        tool="pngquant", version="0.0.1", url=f"{base}/pkg.zip", member="pngquant.exe"
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert "no pinned digest" in report.failed["pngquant"]


def test_missing_member_is_reported(served, isolated_binary_dir):
    base, root = served
    (root / "pkg.zip").write_bytes(_zip_bytes({"something-else.exe": b"x"}))
    artifact = binaries.Artifact(
        tool="oxipng",
        version="0.0.1",
        url=f"{base}/pkg.zip",
        member="oxipng.exe",
        sha256=_sha((root / "pkg.zip").read_bytes()),
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert "not found inside the archive" in report.failed["oxipng"]


def test_hostile_archive_path_cannot_escape_target_dir(served, isolated_binary_dir, tmp_path):
    """`../../pngquant.exe` inside a zip must never write outside the target."""
    base, root = served
    payload = b"payload"
    (root / "pkg.zip").write_bytes(_zip_bytes({"../../pngquant.exe": payload}))
    artifact = binaries.Artifact(
        tool="pngquant",
        version="0.0.1",
        url=f"{base}/pkg.zip",
        member="pngquant.exe",
        member_sha256=_sha(payload),
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert report.ok
    assert (isolated_binary_dir / "pngquant.exe").read_bytes() == payload
    assert not (tmp_path / "pngquant.exe").exists()


def test_corrupt_existing_copy_is_replaced(served, isolated_binary_dir):
    base, root = served
    payload = b"good bytes"
    (root / "pkg.zip").write_bytes(_zip_bytes({"pngquant.exe": payload}))
    isolated_binary_dir.mkdir(parents=True, exist_ok=True)
    (isolated_binary_dir / "pngquant.exe").write_bytes(b"truncated garbage")
    artifact = binaries.Artifact(
        tool="pngquant",
        version="0.0.1",
        url=f"{base}/pkg.zip",
        member="pngquant.exe",
        member_sha256=_sha(payload),
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert report.installed == ["pngquant"]
    assert (isolated_binary_dir / "pngquant.exe").read_bytes() == payload


def test_offline_failure_prints_manual_instructions(isolated_binary_dir):
    out = io.StringIO()
    artifact = binaries.Artifact(
        tool="pngquant",
        version="0.0.1",
        url=f"http://127.0.0.1:{_closed_port()}/pkg.zip",
        member="pngquant.exe",
        member_sha256="0" * 64,
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=out)

    # Never raises: an offline machine still has to reach the web UI.
    assert "pngquant" in report.failed
    text = out.getvalue()
    assert "Manual install" in text
    assert str(isolated_binary_dir) in text


# --- opt-outs and platform selection ------------------------------------


def test_no_download_skips_everything(served, isolated_binary_dir):
    base, _ = served
    artifact = binaries.Artifact(
        tool="pngquant", version="0.0.1", url=f"{base}/x.zip",
        member="pngquant.exe", member_sha256="0" * 64,
    )

    report = binaries.ensure_binaries(download=False, artifacts=(artifact,), out=io.StringIO())

    assert report.skipped == ["pngquant"]
    assert report.failed == {}


def test_frozen_build_never_downloads(monkeypatch, served, isolated_binary_dir):
    base, _ = served
    monkeypatch.setattr(binaries.sys, "frozen", True, raising=False)
    artifact = binaries.Artifact(
        tool="pngquant", version="0.0.1", url=f"{base}/x.zip",
        member="pngquant.exe", member_sha256="0" * 64,
    )

    report = binaries.ensure_binaries(artifacts=(artifact,), out=io.StringIO())

    assert report.skipped == ["frozen build"]


@pytest.mark.skipif(binaries.sys.platform != "win32", reason="Windows layout test")
def test_binary_dir_windows_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert binaries.binary_dir() == tmp_path / "Local" / "ImageOptimizer" / "bin"


def test_binary_dir_linux_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(binaries.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert binaries.binary_dir() == tmp_path / "share" / "image-optimizer" / "bin"


def test_binary_dir_macos(monkeypatch):
    monkeypatch.setattr(binaries.sys, "platform", "darwin")
    expected = Path.home() / "Library" / "Application Support" / "ImageOptimizer" / "bin"
    assert binaries.binary_dir() == expected


def test_every_pinned_artifact_carries_a_full_sha256():
    artifacts = binaries._WINDOWS_AMD64 + tuple(binaries._POSIX_OXIPNG.values())
    for artifact in artifacts:
        assert artifact.sha256 or artifact.member_sha256, artifact.tool
        for digest in (artifact.sha256, artifact.member_sha256):
            if digest:
                assert len(digest) == 64, f"{artifact.tool} digest is not SHA-256 length"
                int(digest, 16)  # raises on non-hex


def test_windows_table_covers_exactly_the_four_encoders():
    assert {a.tool for a in binaries._WINDOWS_AMD64} == {"pngquant", "oxipng", "cjpeg", "avifenc"}
    assert all(a.url.startswith("https://") for a in binaries._WINDOWS_AMD64)


# --- CLI wiring ---------------------------------------------------------


def test_serve_subcommand_is_an_alias():
    from image_optimizer.main import _strip_serve_subcommand as strip

    assert strip(["image-optimizer", "serve"]) == ["image-optimizer"]
    assert strip(["image-optimizer", "serve", "--port", "9000"]) == [
        "image-optimizer", "--port", "9000",
    ]
    # Only argv[1] counts: a directory literally named "serve" is data, not a
    # subcommand.
    assert strip(["image-optimizer", "--dir", "serve"]) == ["image-optimizer", "--dir", "serve"]
    assert strip(["image-optimizer"]) == ["image-optimizer"]


@pytest.mark.parametrize("raw, expected", [
    ("", True),
    ("0", True),
    ("1", False),
    ("true", False),
    ("YES", False),
    ("  on  ", False),
])
def test_download_enabled_env_opt_out(monkeypatch, raw, expected):
    from image_optimizer.main import _download_enabled

    monkeypatch.setenv("IMAGE_OPTIMIZER_NO_DOWNLOAD", raw)
    assert _download_enabled() is expected
