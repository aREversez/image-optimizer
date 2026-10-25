# Image Optimizer - Binary Dependencies

**A pip install does not use this folder.** `image_optimizer/binaries.py`
downloads the pinned upstream release for the current platform on first run
(verified against a SHA-256) into the per-user data directory:

- Windows: `%LOCALAPPDATA%\ImageOptimizer\bin`
- macOS: `~/Library/Application Support/ImageOptimizer/bin`
- Linux: `$XDG_DATA_HOME/image-optimizer/bin` (default `~/.local/share/image-optimizer/bin`)

This folder is the drop-in spot for a **source checkout** or the PyInstaller
build, and it is searched last: PATH wins over both.

Pinned versions fetched automatically: pngquant 2.17.0, oxipng 10.1.1,
mozjpeg 4.0.3 (`cjpeg-static.exe`), libavif 1.4.2 (`avifenc.exe`).

Place the following executables in this directory (or add to system PATH).

## pngquant

pngquant is licensed under the GNU General Public License, version 3 or later
(GPLv3+). A copy of the GPLv3 is available at https://www.gnu.org/licenses/gpl-3.0.html.
The source code for pngquant is available at https://github.com/kornelski/pngquant.

- Download: https://pngquant.org/
- Windows: `pngquant.exe`
- macOS: `pngquant`
- Linux: `pngquant`

## oxipng

- Download: https://github.com/shssoichiro/oxipng/releases
- Windows: `oxipng.exe`
- macOS: `oxipng`
- Linux: `oxipng`

## cjpeg (mozjpeg)

Required only if you want **JPG output** — true lossy JPEG re-compression
that keeps the JPEG format (rather than converting to PNG/WebP). This is
the Image Optimizer itself running against your images with mozjpeg's
encoder. mozjpeg licenses its code under the BSD 3-Clause license (the
encoder) plus a copy of the IJG reference license.

- Source & releases: https://github.com/mozilla/mozjpeg/releases
- Windows: `cjpeg.exe` (or `cjpeg-static.exe` — both spellings are
  auto-detected)
- macOS `brew install mozjpeg` → `cjpeg`
- Linux: `sudo apt install mozjpeg` / `dnf install mozjpeg` → `cjpeg`

> Note: several prebuilt mozjpeg Windows zips ship a `cjpeg-static.exe`
> without the libpng/libjpeg reader modules. That's fine — the app feeds
> cjpeg a PPM intermediate (Pillow decodes first), so no build flags beyond
> PPM/PGM support are ever needed.

After placing the binaries, restart the tool and they will be auto-detected.

## avifenc (libavif)

Required only if you want **AVIF output** — the next-gen image format with
superior compression. The app feeds avifenc a lossless PNG intermediate
(Pillow decodes first, preserving alpha), so avifenc only needs to handle
PNG input.

- Source & releases: https://github.com/AOMediaCodec/libavif/releases
- Windows: `avifenc.exe`
- macOS: `brew install libavif` → `avifenc`
- Linux: `sudo apt install libavif-bin` / `dnf install libavif-tools` → `avifenc`

After placing the binaries, restart the tool and they will be auto-detected.
