"""Regression tests for bugs in the resume-after-restart path that only
show up once the *server process* has restarted (a plain page refresh
reuses the same live workspace/state, so it doesn't reproduce these) — the
tests rebuild session state the way a resumed batch actually looks:
batch_state.json written to disk, then /api/optimize called with
resume=True against a fresh session, no prior /api/scan or /api/optimize
call in this test process.
"""
from __future__ import annotations

from PIL import Image

from .test_batch_resume import _save_batch_state


def _make_batch(test_images, output_dir):
    """A batch with one file already done (before the simulated restart)
    and one still pending — the shape a real interrupted run leaves behind.
    """
    done_src = test_images / "done.png"
    pending_src = test_images / "pending.png"
    Image.new("RGB", (24, 24), (10, 20, 30)).save(done_src)
    Image.new("RGB", (24, 24), (40, 50, 60)).save(pending_src)

    # The file already copied to the user's chosen output folder when the
    # first file finished, before the restart (see _process_files).
    final_output = output_dir / "done.png"
    Image.new("RGB", (24, 24), (10, 20, 30)).save(final_output)

    bs = {
        "session_id": "resume-ui-test",
        "input_dir": str(test_images),
        "output_dir": str(output_dir),
        "output_format": "png",
        "quality": "medium",
        "compression_mode": "lossless",
        "files": [
            {"id": "0", "path": str(done_src), "name": "done.png", "size": 100, "status": "done"},
            {"id": "1", "path": str(pending_src), "name": "pending.png", "size": 100, "status": "pending"},
        ],
        "results": [
            {
                "id": "0",
                "name": "done.png",
                "original_path": str(done_src),
                "success": True,
                "original_size": 100,
                "compressed_size": 80,
                "savings_percent": 20,
                "output_format": "png",
                "output_name": "done.png",
                "final_output_path": str(final_output),
            },
        ],
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    _save_batch_state(bs)
    return bs


class TestResumeRestoresInputDir:
    def test_state_reports_input_dir_after_resume(self, client, auth_headers, test_images, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_batch(test_images, output_dir)

        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.status_code == 200

        d = client.get("/api/state", headers=auth_headers).json()
        assert d["input_dir"] == str(test_images)


class TestResumeCompareAfterRestart:
    def test_state_exposes_ws_name(self, client, auth_headers, test_images, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_batch(test_images, output_dir)

        client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)

        d = client.get("/api/state", headers=auth_headers).json()
        assert d["ws_name"], "ws_name should be non-empty right after resume"
        # ws_name must come from the explicit field, not from parsing
        # files[0].thumbnail — true regardless of whether the concurrent
        # backfill (see TestResumeThumbnailBackfill) has already populated
        # it by the time this request lands or not.

    def test_result_served_for_pre_restart_file(self, client, auth_headers, test_images, tmp_path):
        """The file that finished before the restart must still be
        comparable — served from final_output_path since the old workspace
        that had it is gone."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_batch(test_images, output_dir)

        client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)

        ws_name = client.get("/api/state", headers=auth_headers).json()["ws_name"]
        assert ws_name

        r = client.get(f"/api/result/{ws_name}/done.png", headers=auth_headers)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")

    def test_preview_page_loads_for_pre_restart_file(self, client, auth_headers, test_images, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_batch(test_images, output_dir)

        client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)

        ws_name = client.get("/api/state", headers=auth_headers).json()["ws_name"]
        r = client.get(f"/api/preview/{ws_name}/0", headers=auth_headers)
        assert r.status_code == 200
        assert f"/api/result/{ws_name}/done.png" in r.text

    def test_result_404s_for_unrelated_ws_name(self, client, auth_headers, test_images, tmp_path):
        """Ownership check must still hold — a mismatched ws_name is still
        rejected, fallback or not."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_batch(test_images, output_dir)

        client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)

        r = client.get("/api/result/not-the-real-workspace/done.png", headers=auth_headers)
        assert r.status_code == 404


class TestResumeProgressState:
    """restoreState() (called right after clicking resume, and on a page
    refresh mid-run) reads total/paused straight off /api/state — a client
    fallback of `d.total ?? STATE.total` is only as good as whatever
    STATE.total already is, so /api/state must carry the real values."""

    def test_state_reports_total_and_paused_after_resume(self, client, auth_headers, test_images, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_batch(test_images, output_dir)

        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        assert r.json()["total"] == 1  # only the pending file is resumed

        d = client.get("/api/state", headers=auth_headers).json()
        assert d["total"] == 1
        assert d["paused"] is False


class TestResumeThumbnailBackfill:
    """Thumbnails for files rebuilt on resume are backfilled by a
    concurrent background task (see _backfill_resume_thumbnails), separate
    from the compression task started in the same request."""

    def test_thumbnails_are_backfilled_for_all_resumed_files(self, client, auth_headers, test_images, tmp_path):
        from .conftest import wait_for

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_batch(test_images, output_dir)

        client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)

        def all_have_thumbnails():
            d = client.get("/api/state", headers=auth_headers).json()
            return d["files"] and all(f["thumbnail"] for f in d["files"])

        wait_for(all_have_thumbnails, timeout=10.0)

        d = client.get("/api/state", headers=auth_headers).json()
        # Both the previously-done file and the pending one get a thumbnail
        # — the grid shouldn't have a gap depending on which side of the
        # restart a file happened to finish on.
        assert len(d["files"]) == 2
        for f in d["files"]:
            assert f["thumbnail"].startswith(f"/api/thumb/{d['ws_name']}/")

    def test_backfill_does_not_disturb_total_or_current(self, client, auth_headers, test_images, tmp_path):
        """The whole reason this isn't just a call into _scan_and_thumbnail:
        that function overwrites state.total, which would race with the
        compression task setting state.current as files complete."""
        from .conftest import wait_for

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_batch(test_images, output_dir)

        r = client.post("/api/optimize", json={
            "resume": True,
            "output_dir": str(output_dir),
            "output_format": "png",
            "compression_mode": "lossless",
        }, headers=auth_headers)
        expected_total = r.json()["total"]

        wait_for(lambda: client.get("/api/state", headers=auth_headers).json()["is_running"] is False, timeout=10.0)

        d = client.get("/api/state", headers=auth_headers).json()
        assert d["total"] == expected_total
        assert d["current"] == expected_total
