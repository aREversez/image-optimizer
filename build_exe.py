"""Build a standalone executable with PyInstaller (Windows, macOS or Linux).

Usage: python build_exe.py [--allow-missing]

Requires PyInstaller: pip install pyinstaller

PyInstaller cannot cross-compile: run this on the platform you are targeting.
The encoder binaries (pngquant, oxipng, cjpeg, avifenc) must already be in
bin/ — see bin/README.md, or run scripts/fetch_tools.py to download/build
them for the current platform.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "image_optimizer"
BIN = ROOT / "bin"
ASSETS = ROOT / "assets"
OUT = ROOT / "dist"

IS_WINDOWS = sys.platform == "win32"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""
# --add-data separates source and destination with os.pathsep: ";" on
# Windows, ":" everywhere else.
SEP = os.pathsep

# (name, candidate file names in bin/) — mozjpeg's encoder ships as either
# `cjpeg-static` or `cjpeg`, the app's own lookup accepts both.
TOOLS = [
    ("pngquant", ("pngquant",)),
    ("oxipng", ("oxipng",)),
    ("cjpeg", ("cjpeg-static", "cjpeg")),
    ("avifenc", ("avifenc",)),
]


# Modules PyInstaller would otherwise bundle but the app never uses. Two kinds:
#  * Optional extras that happen to be installed in the build environment.
#    PyInstaller follows imports it finds in any installed package, and e.g.
#    uvicorn imports yaml/dotenv/uvloop/httptools/... lazily if present — so a
#    dev machine with a few extra packages gets a bigger exe than a clean CI
#    runner. Excluding them keeps the build the same wherever it runs (and
#    makes the frozen app use the same h11/asyncio code path as a clean install).
#  * Build/dev tooling and stdlib corners nothing in the app imports.
# Deliberately NOT excluded: tkinter (folder-picker dialog), ssl and
# multiprocessing (imported by uvicorn), and Pillow's AVIF support (Pillow
# opens AVIF results to read their size and to verify them when resuming).
EXCLUDES = [
    # optional extras
    "yaml", "dotenv", "uvloop", "httptools", "websockets", "wsproto", "watchfiles",
    "numpy", "scipy", "pandas", "matplotlib", "IPython", "cryptography",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "pytest", "httpx",
    # build / dev tooling
    "setuptools", "pkg_resources", "_distutils_hack", "distutils", "pip", "wheel",
    "unittest", "doctest", "pdb", "pydoc", "pydoc_data", "test", "idlelib", "lib2to3",
    # stdlib the app never touches
    "smtplib", "ftplib", "xmlrpc", "netrc", "curses", "readline", "rlcompleter",
]


def _find_tools(allow_missing: bool) -> list[Path]:
    found, missing = [], []
    for name, candidates in TOOLS:
        path = next((BIN / f"{c}{EXE_SUFFIX}" for c in candidates
                     if (BIN / f"{c}{EXE_SUFFIX}").exists()), None)
        if path is None:
            missing.append(name)
        else:
            found.append(path)
    if missing:
        msg = f"missing from {BIN}: {', '.join(missing)} (see bin/README.md or scripts/fetch_tools.py)"
        if not allow_missing:
            # Failing loudly beats silently shipping a build without JPEG/AVIF
            # output (see CHANGELOG 1.0.3).
            sys.exit(f"Error: {msg}\nPass --allow-missing to build without them.")
        print(f"Warning: {msg}\n")
    return found


def _pyinstaller_args(tools: list[Path]) -> list[str]:
    return [
        sys.executable, "-m", "PyInstaller",
        # --onefile: one self-contained exe for release uploads. Trades a few
        # seconds of cold start (the bundle is extracted to a temp dir on
        # every launch) for a single-file distribution — resources resolve
        # through sys._MEIPASS exactly like the old --onedir builds.
        "--onefile",
        "--name", "ImageOptimizer",
        "--noconfirm",
        "--clean",
        # The icon only applies to Windows executables; macOS/Linux builds
        # are plain console binaries.
        *([f"--icon={ASSETS / 'app_icon.ico'}"] if IS_WINDOWS else []),
        # Strip debug symbols from the bundled libraries (Linux only: pointless
        # on Windows, and not worth the code-signing risk on macOS).
        *(["--strip"] if sys.platform.startswith("linux") else []),
        *[arg for mod in EXCLUDES for arg in ("--exclude-module", mod)],
        f"--distpath={OUT}",
        "--add-data", f"{APP / 'templates' / 'index.html'}{SEP}image_optimizer/templates/",
        "--add-data", f"{APP / 'templates' / 'favicon.ico'}{SEP}image_optimizer/templates/",
        # pngquant/oxipng plus the JPEG (mozjpeg) and AVIF encoders — without
        # the latter two the frozen build silently lacks both output formats
        # (see CHANGELOG 1.0.3).
        *[arg for tool in tools for arg in ("--add-data", f"{tool}{SEP}bin/")],
        str(APP / "__main__.py"),
    ]


def main():
    parser = argparse.ArgumentParser(description="Build the standalone ImageOptimizer executable.")
    parser.add_argument(
        "--allow-missing", action="store_true",
        help="Build even if some of the encoder binaries are missing from bin/ "
             "(the matching output formats will not be available in that build).",
    )
    args = parser.parse_args()

    pyinstaller_args = _pyinstaller_args(_find_tools(args.allow_missing))
    print("Building ImageOptimizer standalone executable...")
    print(f"PyInstaller args:\n  {' '.join(pyinstaller_args[2:])}\n")
    result = subprocess.run(pyinstaller_args, cwd=ROOT)
    if result.returncode != 0:
        print(f"Build failed with return code {result.returncode}")
        sys.exit(result.returncode)

    exe = OUT / f"ImageOptimizer{EXE_SUFFIX}"
    print(f"\nDone! Standalone exe at: {exe}")
    print(f"Size: {exe.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"\nTo distribute: upload {exe.name} directly to the release (single self-contained file).")


if __name__ == "__main__":
    main()
