"""skip_existing must reuse an output only for the source that made it
(review finding F3).

Reuse used to be keyed on output name + settings fingerprint + mtime. When a
new file shifted the collision numbering (photo.bmp next to an already
processed photo.jpg both map to photo.webp), the new file inherited the old
one's output and was reported as "skipped": the user silently got another
picture."""
from __future__ import annotations

import os
import time
from pathlib import Path

from PIL import Image

from .test_api_optimize import scan_and_wait, optimize_and_wait


def _centre(path: Path):
    with Image.open(path) as im:
        im = im.convert("RGB")
        return im.getpixel((im.width // 2, im.height // 2))


def _run(client, headers, src, out, **extra):
    scan_and_wait(client, headers, src, recursive=False)
    r, d = optimize_and_wait(
        client, headers, quality="high", output_format="webp",
        output_dir=str(out), skip_existing=True, **extra,
    )
    assert r.status_code == 200
    return d["results"]


class TestSkipExistingBindsToSource:
    def test_new_file_that_shifts_collision_numbering_is_not_served_the_old_output(
        self, client, auth_headers, tmp_path,
    ):
        src, out = tmp_path / "src", tmp_path / "out"
        src.mkdir()
        Image.new("RGB", (64, 64), (255, 0, 0)).save(src / "photo.jpg", quality=100)
        _run(client, auth_headers, src, out)
        assert _centre(out / "photo.webp")[0] > 200  # red

        blue = src / "photo.bmp"
        Image.new("RGB", (64, 64), (0, 0, 255)).save(blue)
        old = time.time() - 3600  # older than the existing output, like a copied-in file
        os.utime(blue, (old, old))
        results = _run(client, auth_headers, src, out)

        by_name = {r["name"]: r for r in results}
        assert by_name["photo.bmp"]["success"] and not by_name["photo.bmp"].get("skipped")
        bmp_out = out / by_name["photo.bmp"]["output_name"]
        jpg_out = out / by_name["photo.jpg"]["output_name"]
        assert _centre(bmp_out)[2] > 200 and _centre(bmp_out)[0] < 60, "bmp must produce its own (blue) output"
        assert _centre(jpg_out)[0] > 200 and _centre(jpg_out)[2] < 60, "jpg must keep its own (red) output"

    def test_unchanged_run_still_skips(self, client, auth_headers, tmp_path):
        """The fix must not turn skip_existing into a no-op."""
        src, out = tmp_path / "src", tmp_path / "out"
        src.mkdir()
        Image.new("RGB", (64, 64), (255, 0, 0)).save(src / "a.png")
        first = _run(client, auth_headers, src, out)
        assert first and not first[0].get("skipped")
        second = _run(client, auth_headers, src, out)
        assert second and all(r.get("skipped") for r in second)

    def test_record_in_the_old_format_is_unverifiable_and_recompressed(self, client, auth_headers, tmp_path):
        """A record written before source identity existed (a bare
        fingerprint string) says nothing about which source made the file:
        recompress, never trust."""
        src, out = tmp_path / "src", tmp_path / "out"
        src.mkdir()
        Image.new("RGB", (64, 64), (255, 0, 0)).save(src / "a.png")
        _run(client, auth_headers, src, out)
        from image_optimizer import main
        legacy_fp = main._settings_fingerprint("high", "standard", 0, [], True, False, "webp")
        main._save_skip_fingerprints(str(out), {"a.webp": legacy_fp})
        again = _run(client, auth_headers, src, out)
        assert again and not any(r.get("skipped") for r in again)


def test_reuse_validation_runs_off_the_event_loop(client, auth_headers, tmp_path, app_module, monkeypatch):
    """is_valid_reusable_output now fully decodes JPEG/WebP (about 0.3 s for a
    24 MP photo); called on the loop it would stall every worker, /api/progress
    and the SSE stream once per reused file."""
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    Image.new("RGB", (64, 64), (255, 0, 0)).save(src / "a.png")
    _run(client, auth_headers, src, out)
    seen = []
    real = app_module._is_valid_reusable_output

    def spy(path, fmt):
        import asyncio
        seen.append(asyncio._get_running_loop() is not None)  # True = called on the loop thread
        return real(path, fmt)

    monkeypatch.setattr(app_module, "_is_valid_reusable_output", spy)
    second = _run(client, auth_headers, src, out)
    assert second and all(r.get("skipped") for r in second)
    assert seen == [False], seen
