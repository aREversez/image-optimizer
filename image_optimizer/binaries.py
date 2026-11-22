"""Fetch and install the external encoder binaries on first use.

Why this exists
---------------
The wheel deliberately contains Python only. `pngquant` is GPL-3.0-or-later,
and shipping its .exe from PyPI would put GPLv3 §6's "offer the Corresponding
Source" obligation on every downstream user of this package — the same reason
most distro packages keep the tool separate. `oxipng`, `mozjpeg`/`cjpeg` and
`libavif`/`avifenc` are permissively licensed, but they are still
architecture-specific blobs that have no business inside a `py3-none-any`
sdist/wheel. So: download the exact upstream release once, verify it, and
keep it in the per-user data directory.

Trust model
-----------
The pinned SHA-256 is the *only* thing that makes a download executable.
Every artifact either carries the publisher's own archive digest (oxipng,
libavif) or a digest of the one file we pull out of the archive (pngquant,
mozjpeg). A mismatch is discarded — never "warn and continue". The URL scheme
is not load-bearing; a local test server over plain HTTP is fine because the
digest still has to match. Conversely, an artifact with no digest at all is
rejected before a single byte is requested.

Nothing here is allowed to prevent the app from starting: a missing binary
only removes the output formats that need it (see
`Optimizer.available_modes`), and WebP via Pillow always works. Offline or
behind an internal firewall, we print the exact manual instructions instead.
"""

from __future__ import annotations

import hashlib
import os
import platform
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# One socket-level timeout per operation; a hung connection must not wedge
# startup forever (same reasoning as SUBPROCESS_TIMEOUT in optimizer.py).
DOWNLOAD_TIMEOUT = 60

# Ceiling for a single archive. The largest pinned artifact (libavif's
# windows-artifacts.zip) is ~15 MB; a response far beyond this is a wrong URL
# or a hostile redirect, and has no reason to fill the disk.
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_MEMBER_BYTES = 120 * 1024 * 1024

_CHUNK = 256 * 1024
USER_AGENT = "image-optimizer (binary bootstrap)"


class DownloadError(RuntimeError):
    """Raised when an artifact cannot be fetched, verified, or unpacked."""


@dataclass(frozen=True)
class Artifact:
    """One pinnable upstream release.

    `sha256` is the publisher's digest for the whole archive when they
    publish one; `member_sha256` is the digest of the single executable we
    extract, used when they don't. At least one must be set.
    """

    tool: str
    version: str
    url: str
    member: str
    sha256: Optional[str] = None
    member_sha256: Optional[str] = None
    kind: str = "zip"  # "zip" | "tar.gz"
    executable: bool = False

    @property
    def filename(self) -> str:
        return self.member


# --- Windows x86-64 -------------------------------------------------------
# pngquant: pngquant.org serves a version-less URL (pngquant-windows.zip) and
# no checksum, so the digest below pins the executable itself. At the time of
# writing the Windows build is still the 2.17.0 C release even though
# pngquant.org advertises 3.0.3 for macOS/Linux; if upstream ever swaps the
# Windows payload, verification fails loudly and this entry needs a bump
# rather than silently installing an unverified newer binary.
_WINDOWS_AMD64: tuple[Artifact, ...] = (
    Artifact(
        tool="pngquant",
        version="2.17.0",
        url="https://pngquant.org/pngquant-windows.zip",
        member="pngquant.exe",
        member_sha256="7aadad6a50aa5c40cfcb20f9478c92f6030360a3424bc262ce383dc1e97fb86a",
    ),
    Artifact(
        tool="oxipng",
        version="10.1.1",
        url="https://github.com/shssoichiro/oxipng/releases/download/v10.1.1/"
            "oxipng-10.1.1-x86_64-pc-windows-msvc.zip",
        member="oxipng.exe",
        sha256="0f57b33abb46c76258ac8e20be604a48208141d514bc2936b5200ed626976dd8",
    ),
    Artifact(
        tool="cjpeg",
        version="4.0.3",
        url="https://github.com/mozilla/mozjpeg/releases/download/v4.0.3/"
            "mozjpeg-v4.0.3-win-x64.zip",
        member="cjpeg-static.exe",
        member_sha256="dac0b9d64f660a150ef03a6137836592528c30fc3fd5e39511c2cff7a8206ae1",
    ),
    Artifact(
        tool="avifenc",
        version="1.4.2",
        url="https://github.com/AOMediaCodec/libavif/releases/download/v1.4.2/"
            "windows-artifacts.zip",
        member="avifenc.exe",
        sha256="cb2d9fea43dcbab1d0707e3b37eb7b08070ad2fb60a2c188c39ec12382c0484a",
        member_sha256="42312321ed9f715987cb3623292eaedc778970b45efb9b1de62ce795f61991a0",
    ),
)


# --- macOS / Linux --------------------------------------------------------
# oxipng publishes a checksummed static build for every mainstream
# target triple, so it is the one tool that is safe to fetch on POSIX.
# pngquant/mozjpeg only ship Windows binaries officially, and libavif's
# POSIX archives are dynamically linked against a toolchain we cannot
# guarantee — those stay manual (`brew`/`apt`/`dnf`) rather than risking a
# binary that fails at exec time.
_POSIX_OXIPNG: dict[tuple[str, str], Artifact] = {
    ("darwin", "x86_64"): Artifact(
        tool="oxipng",
        version="10.1.1",
        url="https://github.com/shssoichiro/oxipng/releases/download/v10.1.1/"
            "oxipng-10.1.1-x86_64-apple-darwin.tar.gz",
        member="oxipng",
        sha256="8f23d1bc9cd5b3f88e84f1cca77b461f6900aa20f2ecbf84f729cd12530c5a53",
        kind="tar.gz",
        executable=True,
    ),
    ("darwin", "arm64"): Artifact(
        tool="oxipng",
        version="10.1.1",
        url="https://github.com/shssoichiro/oxipng/releases/download/v10.1.1/"
            "oxipng-10.1.1-aarch64-apple-darwin.tar.gz",
        member="oxipng",
        sha256="615a401629b5cc104bdc775ac962abd67ab2f3084f0b3b1ba9c51e9b76c3964f",
        kind="tar.gz",
        executable=True,
    ),
    ("linux", "x86_64"): Artifact(
        tool="oxipng",
        version="10.1.1",
        url="https://github.com/shssoichiro/oxipng/releases/download/v10.1.1/"
            "oxipng-10.1.1-x86_64-unknown-linux-gnu.tar.gz",
        member="oxipng",
        sha256="5988392e88fc3d03f562f2d565e5c12a095223ae8bd983576f2aaff2fadf3738",
        kind="tar.gz",
        executable=True,
    ),
    ("linux", "arm64"): Artifact(
        tool="oxipng",
        version="10.1.1",
        url="https://github.com/shssoichiro/oxipng/releases/download/v10.1.1/"
            "oxipng-10.1.1-aarch64-unknown-linux-gnu.tar.gz",
        member="oxipng",
        sha256="a69c9dbb110463746153bf9b56488a293747e1d90dc1f30d24af3d9615165b98",
        kind="tar.gz",
        executable=True,
    ),
}


MANUAL_HINTS = {
    "win32": "  winget install pngquant  /  choco install pngquant oxipng mozjpeg",
    "darwin": "  brew install pngquant oxipng mozjpeg libavif",
    "linux": "  sudo apt install pngquant oxipng mozjpeg libavif-bin   (Fedora: dnf install "
             "pngquant oxipng mozjpeg-dtools libavif-tools)",
}


def _machine() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine or "unknown"


def binary_dir() -> Path:
    """Per-user install location for the downloaded executables.

    Deliberately outside the package directory: site-packages is frequently
    read-only (system Python, pipx, container images), and a per-user cache is
    what every other self-bootstrapping tool uses.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "ImageOptimizer" / "bin"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ImageOptimizer" / "bin"
    data = os.environ.get("XDG_DATA_HOME")
    root = Path(data) if data else Path.home() / ".local" / "share"
    return root / "image-optimizer" / "bin"


def artifacts_for_current_platform() -> tuple[Artifact, ...]:
    """The artifacts we are willing to fetch here (may legitimately be empty)."""
    if sys.platform == "win32":
        return _WINDOWS_AMD64 if _machine() == "x86_64" else ()
    return (_POSIX_OXIPNG[(_platform_key(), _machine())],) if (
        (_platform_key(), _machine()) in _POSIX_OXIPNG
    ) else ()


def _platform_key() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            with open(dest, "wb") as fh:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise DownloadError(
                            f"response exceeded {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB"
                        )
                    fh.write(chunk)
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DownloadError(f"timed out after {DOWNLOAD_TIMEOUT}s") from exc
    if total == 0:
        raise DownloadError("empty response")
    return total


def _read_member(artifact: Artifact, archive: Path) -> bytes:
    """Pull out the one executable we want, by basename.

    Matching on `Path(name).name` (and never calling extract/extractall) is
    what makes archive traversal impossible: a hostile entry called
    `../../evil.exe` simply never matches the expected filename, and we never
    write outside the target path anyway.
    """
    if artifact.kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir() or Path(info.filename).name != artifact.member:
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise DownloadError(f"{artifact.member} is unreasonably large")
                with zf.open(info) as fh:
                    return fh.read()
        raise DownloadError(f"{artifact.member} not found inside the archive")
    if artifact.kind == "tar.gz":
        with tarfile.open(archive, "r:*") as tf:
            for info in tf.getmembers():
                if not info.isfile() or Path(info.name).name != artifact.member:
                    continue
                if info.size > MAX_MEMBER_BYTES:
                    raise DownloadError(f"{artifact.member} is unreasonably large")
                handle = tf.extractfile(info)
                if handle is None:
                    continue
                with handle:
                    return handle.read()
        raise DownloadError(f"{artifact.member} not found inside the archive")
    raise DownloadError(f"unsupported archive kind {artifact.kind!r}")


def _install_artifact(artifact: Artifact, target_dir: Path, *, force: bool, out) -> str:
    """Return "present" or "installed"; raise DownloadError on any failure."""
    target = target_dir / artifact.filename

    if target.exists() and not force:
        # An existing file is only trusted when we can re-verify it; otherwise
        # (oxipng on POSIX, whose digest covers the archive rather than the
        # binary) we take its presence at face value — re-downloading a 500 KB
        # tarball on every start would be worse than the risk it mitigates.
        if artifact.member_sha256:
            if _sha256_file(target) == artifact.member_sha256:
                print(f"[Binaries] {artifact.tool} {artifact.version} already installed", file=out)
                return "present"
            print(
                f"[Binaries] {artifact.tool} at {target} does not match the pinned "
                f"{artifact.version} digest — reinstalling",
                file=out,
            )
        else:
            print(f"[Binaries] {artifact.tool} {artifact.version} already installed", file=out)
            return "present"

    if not (artifact.sha256 or artifact.member_sha256):
        raise DownloadError("refusing to download an artifact with no pinned digest")

    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="imgopt-dl-") as tmp:
        archive = Path(tmp) / "archive"
        print(f"[Binaries] downloading {artifact.tool} {artifact.version} from {artifact.url}", file=out)
        size = _download(artifact.url, archive)
        if artifact.sha256 and _sha256_file(archive) != artifact.sha256:
            raise DownloadError("archive SHA-256 mismatch — discarded")
        payload = _read_member(artifact, archive)
        if artifact.member_sha256:
            actual = hashlib.sha256(payload).hexdigest()
            if actual != artifact.member_sha256:
                raise DownloadError(
                    f"{artifact.member} SHA-256 mismatch (got {actual}) — discarded"
                )

    # Write-then-rename so a crash or a second instance never leaves a
    # half-written executable where _find_binary would happily exec it.
    staging = target.with_name(target.name + ".part")
    staging.write_bytes(payload)
    if artifact.executable and sys.platform != "win32":
        os.chmod(staging, 0o755)
    os.replace(staging, target)
    print(f"[Binaries] installed {artifact.tool} {artifact.version} ({size // 1024} KB) -> {target}", file=out)
    return "installed"


@dataclass
class EnsureReport:
    installed: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def _print_manual_help(artifact: Artifact, reason: str, out) -> None:
    print(f"[Warning] {artifact.tool} {artifact.version} unavailable: {reason}", file=out)
    print(f"          Manual install: {artifact.url}", file=out)
    print(f"          then put {artifact.member} in: {binary_dir()}", file=out)
    if artifact.member_sha256:
        print(f"          expected SHA-256: {artifact.member_sha256}", file=out)
    elif artifact.sha256:
        print(f"          expected archive SHA-256: {artifact.sha256}", file=out)


def _print_platform_help(platform_key: str, out) -> None:
    hint = MANUAL_HINTS.get(platform_key)
    if hint:
        print("[Binaries] No automatic download for this platform/architecture.", file=out)
        print(f"[Binaries] Install the tools with your package manager instead:\n{hint}", file=out)
    print(f"[Binaries] Binaries are also found on PATH; anything you install there is used as-is.", file=out)


def ensure_binaries(
    *,
    download: bool = True,
    force: bool = False,
    artifacts: Optional[Iterable[Artifact]] = None,
    out=None,
) -> EnsureReport:
    """Make sure the platform's encoders are present, fetching what is pinned.

    Never raises: a failure is reported (with manual instructions) and the
    caller keeps starting, because WebP still works with no external tool at
    all. `artifacts` is injectable for tests.
    """
    out = out if out is not None else sys.stdout
    report = EnsureReport()

    if getattr(sys, "frozen", False):
        # The standalone exe bundles its binaries; nothing to fetch.
        report.skipped.append("frozen build")
        return report

    selected = tuple(artifacts) if artifacts is not None else artifacts_for_current_platform()
    if not selected:
        _print_platform_help(sys.platform if sys.platform in MANUAL_HINTS else "linux", out)
        return report

    if not download:
        report.skipped.extend(a.tool for a in selected)
        print("[Binaries] --no-download given; skipping automatic fetch", file=out)
        return report

    target_dir = binary_dir()
    for artifact in selected:
        try:
            status = _install_artifact(artifact, target_dir, force=force, out=out)
        except DownloadError as exc:
            report.failed[artifact.tool] = str(exc)
            _print_manual_help(artifact, str(exc), out)
        except OSError as exc:
            report.failed[artifact.tool] = str(exc)
            _print_manual_help(artifact, f"filesystem error: {exc}", out)
        else:
            getattr(report, status).append(artifact.tool)
    return report


def missing_tools_hint() -> str:
    """One-line hint used by the startup banner."""
    return (
        "Missing encoders can be fetched automatically on next start "
        f"(into {binary_dir()}), or installed manually."
    )
