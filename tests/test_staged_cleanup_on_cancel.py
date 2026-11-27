"""A cancelled or crashing encode must not leave its staging file."""
from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image

from .test_cli import cli_args


class Crashing:
    pngquant_path = oxipng_path = cjpeg_path = avifenc_path = None

    def available_modes(self):
        return {("png", "standard"), ("png", "lossless")}

    async def optimize_image(self, input_path, output_path, **kw):
        Path(output_path).write_bytes(b"half")
        raise asyncio.CancelledError()


def test_cli_discards_the_staged_file_when_the_encode_is_cancelled(app_module, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (16, 16)).save(src / "a.png")
    out = tmp_path / "out"
    try:
        asyncio.run(app_module._run_cli(cli_args(source=str(src), output=str(out)),
                                        optimizer_instance=Crashing()))
    except BaseException:
        pass
    assert [p.name for p in out.rglob("*") if p.is_file()] == []
