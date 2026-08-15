"""Regression tests for the single-file pre-compression preview /
dry-run endpoint (optimization item #2).

The preview reuses `optimizer.optimize_png` to project the size of one
file under the current settings, but MUST NOT touch the batch state
machine: no `state.results` entries, no `state.current`/`total` bumps, no
`output_version` bump, no files written into `ws/output` (which the
download ZIP rglobs), and nothing copied into a persistent `output_dir`.
These tests pin that isolation, including the cross-cutting case the
review flags: preview-then-real-batch must leave results/ZIP equal to the
real batch alone.
"""
from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

from PIL import Image

from .conftest import wait_for


def scan_and_wait(client, headers, directory, recursive=True):
    client.post("/api/scan", json={"directory": str(directory), "recursive": recursive}, headers=headers)
    return wait_for(lambda: (lambda d: not d["running"] and d)(
        client.get("/api/scan-progress", headers=headers).json()
    ))


def _session(client):
    import app.main as m
    return m.SESSIONS[client.cookies.get("imgopt_session")]


class TestPreviewOptimize:
    def test_preview_returns_projected_sizes(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        file_id = scanned["files"][0]["id"]
        orig_size = scanned["files"][0]["size"]

        r = client.post("/api/preview-optimize", json={"file_id": file_id}, headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["original_size"] == orig_size
        # Fake binaries copy bytes through, so compressed may equal original;
        # real binaries shrink it. Either way it must be a valid non-negative
        # size no larger than the source.
        assert 0 <= body["compressed_size"] <= orig_size
        assert body["savings_percent"] >= 0

    def test_preview_does_not_pollute_batch_state(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        file_id = scanned["files"][0]["id"]
        st = _session(client)

        r = client.post("/api/preview-optimize", json={"file_id": file_id}, headers=auth_headers)
        assert r.status_code == 200

        # None of the batch-state fields moved.
        assert st.results == []
        assert st.current == 0
        assert st.is_running is False
        assert st.output_version == 0
        # And nothing leaked into ws/output (the ZIP source tree).
        ws_output = st.workspace / "output"
        assert not ws_output.exists() or not list(ws_output.rglob("*"))

    def test_preview_then_real_batch_leaves_clean_results_and_zip(self, client, auth_headers, test_images):
        """The cross-cutting guard: a preview before the real batch must
        not change what the real batch produces — results count, ZIP
        contents, and output_version must reflect ONLY the real batch."""
        scanned = scan_and_wait(client, auth_headers, test_images)
        st = _session(client)
        ws_name = st.workspace.name
        ids = [f["id"] for f in scanned["files"]]

        # Preview every file first.
        for fid in ids:
            r = client.post("/api/preview-optimize", json={"file_id": fid}, headers=auth_headers)
            assert r.status_code == 200

        # Now the real batch.
        r = client.post("/api/optimize", json={}, headers=auth_headers)
        assert r.status_code == 200
        prog = wait_for(lambda: (lambda p: not p["running"] and p)(
            client.get("/api/progress", headers=auth_headers).json()
        ))
        assert len(prog["results"]) == len(ids)
        assert all(res["success"] for res in prog["results"])
        assert st.output_version == 1  # one bump for the real batch, zero for the previews

        zr = client.get(f"/api/download/{ws_name}")
        assert zr.status_code == 200
        names = {n.replace("\\", "/") for n in ZipFile(BytesIO(zr.content)).namelist()}
        assert len(names) == len(ids)

    def test_preview_writes_only_to_preview_dir_not_output(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        file_id = scanned["files"][0]["id"]
        st = _session(client)

        r = client.post("/api/preview-optimize", json={"file_id": file_id}, headers=auth_headers)
        assert r.status_code == 200

        # The preview temp is cleaned up; ws/output never created.
        preview_dir = st.workspace / "preview"
        ws_output = st.workspace / "output"
        assert not ws_output.exists()
        # preview dir may exist but should hold no leftover output file
        leftovers = list(preview_dir.rglob("*")) if preview_dir.exists() else []
        assert not leftovers, f"preview left temp files behind: {leftovers}"

    def test_preview_unknown_file_id_is_404(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r = client.post("/api/preview-optimize", json={"file_id": "nope"}, headers=auth_headers)
        assert r.status_code == 404

    def test_preview_rejects_invalid_params_like_optimize(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        file_id = scanned["files"][0]["id"]
        r = client.post(
            "/api/preview-optimize",
            json={"file_id": file_id, "compression_mode": "turbo"},
            headers=auth_headers,
        )
        assert r.status_code == 400

    def test_preview_screenshot_mode_requires_png(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        file_id = scanned["files"][0]["id"]
        r = client.post(
            "/api/preview-optimize",
            json={"file_id": file_id, "compression_mode": "screenshot", "output_format": "jpg"},
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert "PNG-only" in r.json()["error"]

    def test_preview_screenshot_mode_with_png_succeeds(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        file_id = scanned["files"][0]["id"]
        r = client.post(
            "/api/preview-optimize",
            json={"file_id": file_id, "compression_mode": "screenshot", "output_format": "png"},
            headers=auth_headers,
        )
        assert r.status_code == 200

    def test_preview_supports_keep_exif_and_formats(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        file_id = scanned["files"][0]["id"]
        for fmt in ("png", "webp", "jpg"):
            r = client.post(
                "/api/preview-optimize",
                json={"file_id": file_id, "output_format": fmt, "keep_exif": True},
                headers=auth_headers,
            )
            assert r.status_code == 200, fmt
            assert r.json()["success"] is True, fmt

    def test_preview_requires_token(self, client):
        assert client.post("/api/preview-optimize", json={"file_id": "0"}).status_code == 403
