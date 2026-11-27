"""A planned output path the filesystem cannot hold fails up front with a
clear message instead of an OS error from deep inside the run (found by hand-
testing: a 240-character stem worked at 229 characters and died at 240 under
Windows' MAX_PATH)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from image_optimizer import planning
from .test_cli import cli_args


def test_short_paths_are_fine():
    assert planning.path_problem(Path("/tmp/out/a.png"), windows=False) is None
    assert planning.path_problem(Path("C:/out/a.png"), windows=True, long_paths=False) is None


def test_file_name_over_255_bytes_is_reported():
    msg = planning.path_problem(Path("/tmp/out") / ("a" * 252 + ".png"), windows=False)
    assert msg and "256" in msg and "255" in msg


def test_the_limit_counts_bytes_not_characters():
    name = "截" * 90 + ".png"  # 90 chars, 274 bytes in UTF-8
    assert planning.path_problem(Path("/tmp/out") / name, windows=False)


def test_windows_max_path_is_reported_unless_long_paths_are_enabled():
    long = Path("C:/out") / ("d" * 130) / ("f" * 130 + ".png")
    msg = planning.path_problem(long, windows=True, long_paths=False)
    assert msg and "259" in msg and "LongPathsEnabled" in msg
    assert planning.path_problem(long, windows=True, long_paths=True) is None


def test_staging_overhead_is_counted_on_windows():
    # The final path (250 characters) fits MAX_PATH, but the longer temp-file
    # name next to a short file name does not.
    p = Path("C:/out") / ("a" * 200) / ("b" * 40 + ".png")
    assert len(str(p)) < 260
    assert planning.path_problem(p, windows=True, long_paths=False)


@pytest.mark.skipif(sys.platform == "win32", reason="needs a 255-byte file name")
def test_cli_fails_the_file_cleanly_when_the_converted_name_is_too_long(
    app_module, optimizer, tmp_path, capsys,
):
    src = tmp_path / "src"
    src.mkdir()
    # 255 bytes with .jpg, 256 once converted to .webp
    Image.new("RGB", (16, 16), (1, 2, 3)).save(src / ("n" * 251 + ".jpg"), "JPEG")
    code = asyncio.run(app_module._run_cli(
        cli_args(source=str(src), output=str(tmp_path / "out"), format="webp", json=True),
        optimizer_instance=optimizer,
    ))
    f = json.loads(capsys.readouterr().out)["files"][0]
    assert code == 1 and f["status"] == "failed"
    assert "255" in f["error"] and "File name too long" not in f["error"]


@pytest.mark.skipif(sys.platform == "win32", reason="needs a 255-byte file name")
def test_web_batch_fails_the_file_cleanly_when_the_converted_name_is_too_long(
    client, auth_headers, tmp_path,
):
    from .test_api_optimize import scan_and_wait, optimize_and_wait
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(src / ("n" * 251 + ".jpg"), "JPEG")
    scan_and_wait(client, auth_headers, src, recursive=False)
    optimize_and_wait(
        client, auth_headers, quality="medium", output_format="webp",
        output_dir=str(tmp_path / "out"),
    )
    prog = client.get("/api/progress", headers=auth_headers).json()
    assert any("255" in line and "FAIL" in line.upper() for line in prog["logs"]), prog["logs"]
    assert not list((tmp_path / "out").glob("*"))
