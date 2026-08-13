"""Build standalone Windows executable with PyInstaller.

Usage: python build_exe.py

Requires PyInstaller: pip install pyinstaller
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
BIN = ROOT / "bin"
ASSETS = ROOT / "assets"
OUT = ROOT / "dist"

PYINSTALLER_ARGS = [
    sys.executable, "-m", "PyInstaller",
    # --onefile: one self-contained exe for release uploads. Trades a few
    # seconds of cold start (the bundle is extracted to a temp dir on
    # every launch) for a single-file distribution — resources resolve
    # through sys._MEIPASS exactly like the old --onedir builds.
    "--onefile",
    "--name", "ImageOptimizer",
    "--noconfirm",
    "--clean",
    f"--icon={ASSETS / 'app_icon.ico'}",
    f"--distpath={OUT}",
    "--add-data", f"{APP / 'templates' / 'index.html'}{';'}app/templates/",
    "--add-data", f"{APP / 'templates' / 'favicon.ico'}{';'}app/templates/",
    "--add-data", f"{BIN / 'pngquant.exe'}{';'}bin/",
    "--add-data", f"{BIN / 'oxipng.exe'}{';'}bin/",
    # JPEG (mozjpeg) and AVIF encoders — without these the frozen build
    # silently lacks both output formats (see CHANGELOG 1.0.3).
    "--add-data", f"{BIN / 'cjpeg-static.exe'}{';'}bin/",
    "--add-data", f"{BIN / 'avifenc.exe'}{';'}bin/",
    str(APP / "__main__.py"),
]

def main():
    print("Building ImageOptimizer standalone exe...")
    print(f"PyInstaller args:\n  {' '.join(PYINSTALLER_ARGS[2:])}\n")
    result = subprocess.run(PYINSTALLER_ARGS, cwd=ROOT)
    if result.returncode != 0:
        print(f"Build failed with return code {result.returncode}")
        sys.exit(result.returncode)

    exe = OUT / "ImageOptimizer.exe"
    print(f"\nDone! Standalone exe at: {exe}")
    print(f"Size: {exe.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"\nTo distribute: upload {exe.name} directly to the release (single self-contained file).")


if __name__ == "__main__":
    main()
