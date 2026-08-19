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

    def test_source_file_content_types_are_explicit_not_os_guessed(self, client, auth_headers, test_images):
        """Bug (found via Windows CI, not visible on Linux): FileResponse
        without an explicit media_type falls back to Python's `mimetypes`
        module, which on Windows reads from the registry rather than a
        built-in table. .webp (and potentially others) often isn't
        registered there, silently serving as application/octet-stream
        instead of the real image type. /api/source-file and /api/result
        must set media_type explicitly rather than relying on this
        OS-dependent guess — this test asserts the exact content-type for
        every format this app actually serves, so it fails identically on
        every platform instead of only on whichever OS's registry happens
        to be missing an entry."""
        scanned = scan_and_wait(client, auth_headers, test_images)
        expected = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        checked_extensions = set()
        for f in scanned["files"]:
            ext = "." + f["name"].rsplit(".", 1)[-1].lower()
            if ext not in expected:
                continue
            r = client.get(f"/api/source-file/{scanned['files'][0]['thumbnail'].split('/')[3]}/{f['id']}")
            assert r.status_code == 200
            assert r.headers["content-type"] == expected[ext], f["name"]
            checked_extensions.add(ext)
        assert checked_extensions == {".png", ".jpg"}, "test_images fixture should cover both — update this test if it changes"


class TestPreviewPageI18n:
    """The Compare page is server-rendered and opened in its own tab, so it
    can't read index.html's localStorage — the caller passes its current
    language as ?lang=, and the page picks strings from PREVIEW_I18N."""

    def test_lang_zh_renders_chinese_strings(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
        result = optimize_and_wait(client, auth_headers)
        file_id = result["results"][0]["id"]
        r = client.get(f"/api/preview/{ws_name}/{file_id}?lang=zh")
        assert r.status_code == 200
        assert "<title>对比 -" in r.text
        assert "并排对比" in r.text  # Side by Side
        assert "叠加对比" in r.text  # Overlay
        assert "Side by Side" not in r.text
        assert "Overlay</button>" not in r.text

    def test_unknown_lang_falls_back_to_english(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
        result = optimize_and_wait(client, auth_headers)
        file_id = result["results"][0]["id"]
        r = client.get(f"/api/preview/{ws_name}/{file_id}?lang=fr")
        assert r.status_code == 200
        assert "<title>Compare -" in r.text
        assert 'id="btn-overlay">Overlay</button>' in r.text

    def test_no_lang_param_defaults_to_english(self, client, auth_headers, test_images):
        r = get_preview(client, auth_headers, test_images)
        assert r.status_code == 200
        assert "<title>Compare -" in r.text