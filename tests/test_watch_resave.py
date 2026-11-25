"""Watch mode re-saves overwrite the source's own output (review finding F4).

used_names was never released for the same source, so a changed file found
its own earlier output "taken" and was written as name_2, name_3, ...
Screenshot tools and editors that re-save a file filled the output folder
with stale copies."""
from __future__ import annotations

import time

from PIL import Image

from .conftest import wait_for


def _start(client, headers, watch_dir, output_dir, **extra):
    body = {
        "directory": str(watch_dir), "output_dir": str(output_dir),
        "output_format": "png", "compression_mode": "lossless", **extra,
    }
    r = client.post("/api/watch/start", json=body, headers=headers)
    assert r.status_code == 200
    time.sleep(0.5)


def _processed(client, headers, n):
    return lambda: client.get("/api/watch/status", headers=headers).json()["processed"] >= n


class TestWatchResave:
    def test_resaving_the_same_file_keeps_exactly_one_output(self, client, auth_headers, tmp_path):
        watch, out = tmp_path / "watch", tmp_path / "out"
        watch.mkdir()
        out.mkdir()
        _start(client, auth_headers, watch, out)
        try:
            shot = watch / "shot.png"
            for i, size in enumerate((24, 32, 40), start=1):
                Image.new("RGB", (size, size), (10 * i, 20, 30)).save(shot)
                wait_for(_processed(client, auth_headers, i), timeout=20.0)
            names = sorted(p.name for p in out.iterdir())
            assert names == ["shot.png"], names
            with Image.open(out / "shot.png") as im:
                assert im.size == (40, 40), "the output must be the latest save"
        finally:
            client.post("/api/watch/stop", headers=auth_headers)

    def test_different_sources_with_the_same_output_name_still_get_distinct_files(
        self, client, auth_headers, tmp_path,
    ):
        watch, out = tmp_path / "watch", tmp_path / "out"
        watch.mkdir()
        out.mkdir()
        _start(client, auth_headers, watch, out, recursive=False)
        try:
            # Two names that collide after the suffix swap.
            Image.new("RGB", (24, 24), (200, 0, 0)).save(watch / "shot.png")
            Image.new("RGB", (24, 24), (0, 0, 200)).save(watch / "shot.bmp")
            wait_for(_processed(client, auth_headers, 2), timeout=20.0)
            assert sorted(p.name for p in out.iterdir()) == ["shot.png", "shot_2.png"]
        finally:
            client.post("/api/watch/stop", headers=auth_headers)

    def test_resave_leaves_no_staging_leftovers(self, client, auth_headers, tmp_path):
        watch, out = tmp_path / "watch", tmp_path / "out"
        watch.mkdir()
        out.mkdir()
        _start(client, auth_headers, watch, out)
        try:
            Image.new("RGB", (24, 24), (1, 2, 3)).save(watch / "x.png")
            wait_for(_processed(client, auth_headers, 1), timeout=20.0)
            assert [p.name for p in out.iterdir() if p.name.startswith(".")] == []
        finally:
            client.post("/api/watch/stop", headers=auth_headers)
