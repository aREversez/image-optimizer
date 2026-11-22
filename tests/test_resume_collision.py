"""Regression test for a resume-specific output-name collision.

_resolve_collision_free_output_paths only sees the items passed to it in
one call, starting used_names fresh each time. On a normal first run that's
fine (one call, one flat namespace). On resume it isn't: previous_results
(already-completed files from before the restart, each with its own
output_name) sit in state.results, but the resume call to
_resolve_collision_free_output_paths only ever sees files_to_process (the
still-pending ones) — so a pending file whose collision-resolved name
happens to match an already-done file's output_name gets assigned that
same name with no bump.

That produces two state.results entries with an identical output_name: the
old one (served from final_output_path, the copy in the user's real output
folder) and the new one (served from the fresh workspace copy, since
_resolve_result_file checks the live workspace first). Concretely: a
source folder with "photo.jpg" (compressed to png and marked done before
the simulated restart) and "photo.png" (still pending, same target name
after the jpg->png conversion). On resume, only "photo.png" reprocesses,
collision-resolves to plain "photo.png" (nothing else in this call's own
item list to bump against), and /api/result/{ws}/photo.png for the OLD
result then resolves to the *new* file's workspace copy instead of the old
result's own final_output_path — silently showing the wrong image on the
old result's compare/preview page.
"""
from __future__ import annotations

import io

from PIL import Image

from .test_batch_resume import _save_batch_state


def _make_collision_batch(test_images, output_dir):
    jpg_src = test_images / "photo.jpg"
    png_src = test_images / "photo.png"
    Image.new("RGB", (24, 24), (10, 20, 30)).save(jpg_src)
    Image.new("RGB", (24, 24), (40, 50, 60)).save(png_src)

    # photo.jpg already compressed to png before the "restart" — lives at
    # output_dir/photo.png, its final_output_path.
    final_output = output_dir / "photo.png"
    Image.new("RGB", (24, 24), (10, 20, 30)).save(final_output)

    bs = {
        "session_id": "collision-test",
        "input_dir": str(test_images),
        "output_dir": str(output_dir),
        "output_format": "png",
        "quality": "medium",
        "compression_mode": "lossless",
        "files": [
            {"id": "0", "path": str(jpg_src), "name": "photo.jpg", "size": 100, "status": "done"},
            {"id": "1", "path": str(png_src), "name": "photo.png", "size": 100, "status": "pending"},
        ],
        "results": [
            {
                "id": "0", "name": "photo.jpg", "original_path": str(jpg_src),
                "success": True, "original_size": 100, "compressed_size": 80,
                "savings_percent": 20, "output_format": "png",
                "output_name": "photo.png",
                "final_output_path": str(final_output),
            },
        ],
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    _save_batch_state(bs)
    return bs


class TestResumeCollisionDoesNotSwapOldResult:
    def test_old_result_output_name_gets_bumped_not_reused(
        self, client, auth_headers, test_images, tmp_path
    ):
        """The new run's collision resolver must see "photo.png" as
        already taken (by the old result) and bump the newly-processed
        file to "photo_2.png" — not silently reuse the old result's name.
        """
        from .conftest import wait_for

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_collision_batch(test_images, output_dir)

        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.status_code == 200, r.text

        wait_for(
            lambda: client.get("/api/state", headers=auth_headers).json()["is_running"] is False,
            timeout=10.0,
        )

        d = client.get("/api/state", headers=auth_headers).json()
        results_by_id = {res["id"]: res for res in d["results"]}
        assert results_by_id["0"]["output_name"] == "photo.png"
        assert results_by_id["1"]["output_name"] != "photo.png", (
            "the newly-resumed 'photo.png' file must not be assigned the "
            "same output_name as the already-done 'photo.jpg' result"
        )

    def test_old_result_compare_still_shows_the_old_image(
        self, client, auth_headers, test_images, tmp_path
    ):
        """/api/result for the old result's output_name must keep serving
        the old result's own bytes (from final_output_path), not whatever
        the newly-processed file produced."""
        from .conftest import wait_for

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_collision_batch(test_images, output_dir)

        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.status_code == 200, r.text

        wait_for(
            lambda: client.get("/api/state", headers=auth_headers).json()["is_running"] is False,
            timeout=10.0,
        )

        d = client.get("/api/state", headers=auth_headers).json()
        ws_name = d["ws_name"]

        old_bytes = client.get(f"/api/result/{ws_name}/photo.png", headers=auth_headers).content
        old_img = Image.open(io.BytesIO(old_bytes)).convert("RGB")
        assert old_img.getpixel((0, 0)) == (10, 20, 30), (
            "the old result's own image (10,20,30) must still be served at "
            "its recorded output_name, not the newly-processed file's output"
        )
