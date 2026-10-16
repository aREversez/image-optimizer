"""Fetch or build the encoder binaries (pngquant, oxipng, cjpeg, avifenc) into bin/.

Used by .github/workflows/release.yml, and handy for a local build_exe.py run.
Tools already present in bin/ are left alone, so running it twice is cheap.

Usage: python scripts/fetch_tools.py [--bin-dir DIR] [--only NAME ...]

Sources (all produce binaries with no dependency on libraries from the
machine that built them, apart from the C runtime):
  pngquant  the `pngquant-cli` wheel on PyPI (needs pip)
  oxipng    `cargo install oxipng` (needs a Rust toolchain)
  cjpeg     mozjpeg built from source with cmake (needs git, cmake and, on
            x86-64, nasm — the build fails rather than going without SIMD)
  avifenc   the static build libavif publishes with each release (needs the
            gh CLI and a GH_TOKEN/GITHUB_TOKEN in the environment)

Bump the versions below deliberately: the release workflow caches bin/ keyed
on this file's hash.
"""

import argparse
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PNGQUANT_CLI_VERSION = "3.0.3"
OXIPNG_VERSION = "10.2.1"
MOZJPEG_REF = "v4.1.5"
LIBAVIF_VERSION = "v1.4.2"

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


def build_oxipng(bin_dir: Path, tmp: Path):
    run(["cargo", "install", "oxipng", "--locked", "--version", OXIPNG_VERSION,
         "--root", tmp / "oxipng"])
    install(tmp / "oxipng" / "bin" / f"oxipng{EXE}", bin_dir / f"oxipng{EXE}")


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
    "oxipng": (f"oxipng{EXE}", build_oxipng, "--version"),
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
