"""Fetch or build the encoder binaries (pngquant, oxipng, cjpeg, avifenc) into bin/.

Used by .github/workflows/release.yml, and handy for a local build_exe.py run.
Tools already present in bin/ are left alone, so running it twice is cheap.

Usage: python scripts/fetch_tools.py [--bin-dir DIR] [--only NAME ...]

Sources (all produce binaries with no dependency on libraries from the
machine that built them, apart from the C runtime):
  pngquant  the `pngquant-cli` wheel on PyPI (needs pip)
  oxipng    the prebuilt archive from the oxipng GitHub release, verified
            against the pinned SHA-256 below (no Rust toolchain needed)
  cjpeg     mozjpeg built from source with cmake (needs git, cmake and, on
            x86-64, nasm — the build fails rather than going without SIMD)
  avifenc   the static build libavif publishes with each release (needs the
            gh CLI and a GH_TOKEN/GITHUB_TOKEN in the environment)

Bump the versions below deliberately: the release workflow caches bin/ keyed
on this file's hash.

The oxipng switch from `cargo install` to the prebuilt archive is not just
speed: the v1.1.0 release's Windows and macOS builds died inside
`cargo install oxipng`, so depending on whatever Rust toolchain the runner
images happen to ship had already bitten this project once.
"""

import argparse
import hashlib
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PNGQUANT_CLI_VERSION = "3.0.3"
OXIPNG_VERSION = "10.2.1"
MOZJPEG_REF = "v4.1.5"
LIBAVIF_VERSION = "v1.4.2"

# asset name in the oxipng release -> SHA-256 of that asset. Verified after
# download, exactly like the runtime bootstrap (image_optimizer/binaries.py)
# verifies what it fetches.
OXIPNG_ASSETS = {
    ("win32", "x64"): (
        "oxipng-10.2.1-x86_64-pc-windows-msvc.zip",
        "7e940f83ee46874b73f53031f96a15834cb70b220af27391fb06fe7b4dd798e1",
    ),
    ("darwin", "arm64"): (
        "oxipng-10.2.1-aarch64-apple-darwin.tar.gz",
        "7039fcfc78e8aa1ed2b57d848057a0296f082e92b3e1807ac65402d10d926764",
    ),
    ("linux", "x64"): (
        "oxipng-10.2.1-x86_64-unknown-linux-gnu.tar.gz",
        "46e3c4beb9aae57290ad809dd3374b07153579d3322a3778c53900633618b7c6",
    ),
}

IS_WINDOWS = sys.platform == "win32"
EXE = ".exe" if IS_WINDOWS else ""


def run(cmd, **kwargs):
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def install(src: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"-> {dest}")


def fetch_pngquant(bin_dir: Path, tmp: Path):
    run([sys.executable, "-m", "pip", "download", f"pngquant-cli=={PNGQUANT_CLI_VERSION}",
         "--no-deps", "--only-binary=:all:", "-d", tmp / "wheel"])
    wheel = next((tmp / "wheel").glob("pngquant_cli-*.whl"))
    with zipfile.ZipFile(wheel) as z:
        z.extractall(tmp / "unpacked")
    exe = next(p for p in (tmp / "unpacked").rglob("*")
               if p.is_file() and p.name in ("pngquant", "pngquant.exe"))
    install(exe, bin_dir / f"pngquant{EXE}")


def fetch_oxipng(bin_dir: Path, tmp: Path):
    # libavif-style asset picking: the platform key is (sys.platform family,
    # platform.machine), normalised.
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        arch = "x64"
    elif machine == "arm64":
        arch = "arm64"
    else:
        arch = machine
    platform_key = ("win32" if IS_WINDOWS else sys.platform, arch)
    try:
        asset, digest = OXIPNG_ASSETS[platform_key]
    except KeyError:
        sys.exit(f"No prebuilt oxipng {OXIPNG_VERSION} for {sys.platform}/{machine}")
    url = (f"https://github.com/oxipng/oxipng/releases/download/"
           f"v{OXIPNG_VERSION}/{asset}")
    print(f"downloading {url}")
    archive = tmp / asset
    with urllib.request.urlopen(url) as r, open(archive, "wb") as f:
        shutil.copyfileobj(r, f)
    got = hashlib.sha256(archive.read_bytes()).hexdigest()
    if got != digest:
        sys.exit(f"{asset}: sha256 {got} does not match the pinned {digest}")
    unpacked = tmp / "unpacked"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(unpacked)
    else:
        with tarfile.open(archive) as t:
            t.extractall(unpacked, filter="data")
    exe = next(p for p in unpacked.rglob(f"oxipng{EXE}") if p.is_file())
    install(exe, bin_dir / f"oxipng{EXE}")


def build_cjpeg(bin_dir: Path, tmp: Path):
    src, build = tmp / "mozjpeg", tmp / "mozjpeg-build"
    run(["git", "clone", "--depth", "1", "--branch", MOZJPEG_REF,
         "https://github.com/mozilla/mozjpeg.git", src])
    # The app feeds cjpeg a PPM intermediate (Pillow decodes first), so PNG
    # support (and with it a libpng dependency) is not needed.
    configure = ["cmake", "-S", src, "-B", build, "-DCMAKE_BUILD_TYPE=Release",
                 "-DBUILD_SHARED_LIBS=OFF", "-DPNG_SUPPORTED=OFF",
                 "-DWITH_TURBOJPEG=OFF", "-DREQUIRE_SIMD=ON"]
    if IS_WINDOWS:
        configure += ["-A", "x64"]
    elif sys.platform == "darwin":
        configure += ["-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0"]
    run(configure)
    run(["cmake", "--build", build, "--config", "Release", "--target", "cjpeg-static",
         "--parallel", "4"])
    # Multi-config generators (Visual Studio) put it under build/Release/.
    exe = next(p for p in build.rglob(f"cjpeg-static{EXE}") if p.is_file())
    install(exe, bin_dir / f"cjpeg-static{EXE}")


def fetch_avifenc(bin_dir: Path, tmp: Path):
    # libavif only publishes one architecture per OS: x86-64 for Windows and
    # Linux, arm64 for macOS.
    machine = platform.machine().lower()
    if IS_WINDOWS and machine in ("amd64", "x86_64"):
        asset = "windows-artifacts.zip"
    elif sys.platform == "darwin" and machine == "arm64":
        asset = "macOS-artifacts.zip"
    elif sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        asset = "linux-artifacts.zip"
    else:
        sys.exit(f"No prebuilt avifenc from libavif for {sys.platform}/{machine}")
    run(["gh", "release", "download", LIBAVIF_VERSION, "--repo", "AOMediaCodec/libavif",
         "--pattern", asset, "--dir", tmp])
    with zipfile.ZipFile(tmp / asset) as z:
        z.extract(f"avifenc{EXE}", tmp / "avif")
    install(tmp / "avif" / f"avifenc{EXE}", bin_dir / f"avifenc{EXE}")


# name -> (file in bin/ that means "already there", fetch function, version flag)
TOOLS = {
    "pngquant": (f"pngquant{EXE}", fetch_pngquant, "--version"),
    "oxipng": (f"oxipng{EXE}", fetch_oxipng, "--version"),
    "cjpeg": (f"cjpeg-static{EXE}", build_cjpeg, "-version"),
    "avifenc": (f"avifenc{EXE}", fetch_avifenc, "--version"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bin-dir", type=Path, default=ROOT / "bin")
    parser.add_argument("--only", nargs="+", choices=TOOLS, metavar="NAME",
                        help=f"Only these tools (default: all of {', '.join(TOOLS)})")
    args = parser.parse_args()

    for name in args.only or TOOLS:
        filename, fetch, version_flag = TOOLS[name]
        target = args.bin_dir / filename
        if target.exists():
            print(f"[{name}] already in {args.bin_dir}, skipping")
            continue
        print(f"[{name}] fetching")
        with tempfile.TemporaryDirectory() as tmp:
            fetch(args.bin_dir, Path(tmp))
        # Smoke-test the result so a binary that cannot run on this machine
        # fails here rather than inside the packaged app.
        result = subprocess.run([str(target), version_flag], capture_output=True, text=True, timeout=60)
        print(f"[{name}] {(result.stdout or result.stderr).strip().splitlines()[0]}")
        if result.returncode != 0:
            sys.exit(f"{target} exited with {result.returncode}")


if __name__ == "__main__":
    main()
