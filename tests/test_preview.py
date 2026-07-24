"""Regression tests for GET /api/preview (the Compare page)."""
from __future__ import annotations

import re

from .conftest import wait_for


def scan_and_wait(client, headers, directory, recursive=True):
    client.post("/api/scan", json={"directory": str(directory), "recursive": recursive}, headers=headers)
    return wait_for(lambda: (lambda d: not d["running"] and d)(
        client.get("/api/scan-progress", headers=headers).json()
    ))


def optimize_and_wait(client, headers, **body):
    client.post("/api/optimize", json=body, headers=headers)
    return wait_for(lambda: (lambda p: not p["running"] and p)(
        client.get("/api/progress", headers=headers).json()
    ))


def get_preview(client, auth_headers, test_images, **optimize_kwargs):
    scanned = scan_and_wait(client, auth_headers, test_images)
    ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
    result = optimize_and_wait(client, auth_headers, **optimize_kwargs)
    file_id = result["results"][0]["id"]
    r = client.get(f"/api/preview/{ws_name}/{file_id}")
    return r


class TestPreviewPage:
    def test_title_uses_filename_not_raw_path(self, client, auth_headers, test_images):
        r = get_preview(client, auth_headers, test_images)
        assert r.status_code == 200
        assert "<title>Compare -" in r.text
        assert "/api/preview/" not in r.text.split("<title>")[1].split("</title>")[0]

    def test_overlay_is_the_default_view(self, client, auth_headers, test_images):
        r = get_preview(client, auth_headers, test_images)
        assert 'id="btn-overlay">Overlay</button>' in r.text
        # The overlay button should carry the active class by default, and
        # the side-by-side container should start hidden.
        assert re.search(r'class="view-btn active"[^>]*id="btn-overlay"', r.text)
        assert re.search(r'id="side-view"[^>]*style="display:none"', r.text)

    def test_zoom_controls_present(self, client, auth_headers, test_images):
        r = get_preview(client, auth_headers, test_images)
        assert "btn-fit" in r.text
        assert "btn-zoom100" in r.text

    def test_webp_result_has_correct_extension_and_content_type(self, client, auth_headers, test_images):
        r = get_preview(client, auth_headers, test_images, output_format="webp")
        assert r.status_code == 200
        comp_match = re.search(r'src="([^"]+)"[^>]*id="overlay-comp"', r.text)
        assert comp_match, "compressed image URL not found in preview page"
        assert comp_match.group(1).endswith(".webp")

        rc = client.get(comp_match.group(1))
        assert rc.status_code == 200
        assert rc.headers["content-type"] == "image/webp"
