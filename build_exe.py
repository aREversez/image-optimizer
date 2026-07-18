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
    "--onedir",
    "--name", "ImageOptimizer",
    "--noconfirm",
    "--clean",
    f"--icon={ASSETS / 'app_icon.ico'}",
    f"--distpath={OUT}",
    "--add-data", f"{APP / 'templates' / 'index.html'}{';'}app/templates/",
    "--add-data", f"{APP / 'templates' / 'favicon.ico'}{';'}app/templates/",
    "--add-data", f"{BIN / 'pngquant.exe'}{';'}bin/",
    "--add-data", f"{BIN / 'oxipng.exe'}{';'}bin/",
    str(APP / "__main__.py"),
]

def main():
    print("Building ImageOptimizer standalone exe...")
    print(f"PyInstaller args:\n  {' '.join(PYINSTALLER_ARGS[2:])}\n")
    result = subprocess.run(PYINSTALLER_ARGS, cwd=ROOT)
    if result.returncode != 0:
        print(f"Build failed with return code {result.returncode}")
        sys.exit(result.returncode)

    exe_dir = OUT / "ImageOptimizer"
    print(f"\nDone! Standalone exe at: {exe_dir / 'ImageOptimizer.exe'}")
    print(f"Total size: {sum(f.stat().st_size for f in exe_dir.rglob('*') if f.is_file()) / 1024 / 1024:.1f} MB")
    print(f"\nTo distribute: zip the entire '{exe_dir.name}' folder.")


if __name__ == "__main__":
    main()
