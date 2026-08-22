"""Regression tests for GET /api/preview (the Compare page)."""
from __future__ import annotations

import json
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


class TestGetImageInfo:
    """Unit tests for _get_image_info — in particular the failure path
    (vibe-coding-rules #1: a read failure must return an explicit None the
    caller can render as "unavailable", never a default/placeholder that
    looks like real data)."""

    def test_returns_width_height_depth_for_rgb_png(self, app_module, tmp_path):
        from PIL import Image
        p = tmp_path / "img.png"
        Image.new("RGB", (64, 48), (0, 0, 0)).save(p)
        info = app_module._get_image_info(p)
        assert info == {"width": 64, "height": 48, "depth": "24-bit RGB"}

    def test_returns_depth_for_rgba_png(self, app_module, tmp_path):
        from PIL import Image
        p = tmp_path / "img.png"
        Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(p)
        info = app_module._get_image_info(p)
        assert info["depth"] == "32-bit RGBA"

    def test_returns_depth_for_palette_png(self, app_module, tmp_path):
        from PIL import Image
        p = tmp_path / "img.png"
        Image.new("P", (10, 10)).save(p)
        info = app_module._get_image_info(p)
        assert info["depth"] == "8-bit indexed"

    def test_nonexistent_file_returns_none_not_a_placeholder_dict(self, app_module, tmp_path):
        info = app_module._get_image_info(tmp_path / "does-not-exist.png")
        assert info is None

    def test_corrupt_file_returns_none_not_a_placeholder_dict(self, app_module, tmp_path):
        p = tmp_path / "corrupt.png"
        p.write_bytes(b"not actually a png")
        info = app_module._get_image_info(p)
        assert info is None


class TestPreviewPageImageInfo:
    """The Compare page shows width/height/bit-depth for both images (see
    _get_image_info) — these tests confirm it actually renders, for both
    the side-by-side labels and the overlay label's initial and toggled
    state."""

    def test_side_by_side_labels_show_dimensions_and_depth(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
        png_file = next(f for f in scanned["files"] if f["name"].endswith(".png"))
        result = optimize_and_wait(client, auth_headers, file_ids=[png_file["id"]])
        file_id = result["results"][0]["id"]
        r = client.get(f"/api/preview/{ws_name}/{file_id}")
        assert r.status_code == 200
        # test_images' PNGs are 64x64 RGB; fake pngquant/oxipng copy bytes
        # through unchanged, so the compressed output is byte-identical.
        assert "64\u00d764" in r.text
        assert "24-bit RGB" in r.text

    def test_overlay_label_shows_compressed_image_info_by_default(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
        png_file = next(f for f in scanned["files"] if f["name"].endswith(".png"))
        result = optimize_and_wait(client, auth_headers, file_ids=[png_file["id"]])
        file_id = result["results"][0]["id"]
        r = client.get(f"/api/preview/{ws_name}/{file_id}")
        overlay_label = re.search(r'id="overlay-label">([^<]*)</div>', r.text)
        assert overlay_label, "overlay-label div not found"
        assert "64\u00d764" in overlay_label.group(1)
        assert "24-bit RGB" in overlay_label.group(1)

    def test_jpeg_source_dimensions_and_depth_present(self, client, auth_headers, test_images):
        """photo.jpg in the fixture is 80x80 RGB — a non-PNG source, to
        confirm this isn't PNG-only (unlike Screenshot mode)."""
        scanned = scan_and_wait(client, auth_headers, test_images)
        ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
        jpg_file = next(f for f in scanned["files"] if f["name"].endswith(".jpg"))
        result = optimize_and_wait(client, auth_headers, file_ids=[jpg_file["id"]])
        file_id = result["results"][0]["id"]
        r = client.get(f"/api/preview/{ws_name}/{file_id}")
        assert r.status_code == 200
        assert "80\u00d780" in r.text
        assert "24-bit RGB" in r.text

    def test_missing_original_file_shows_unavailable_not_blank(self, client, auth_headers, test_images):
        """If the source file is gone by the time Compare is opened (moved/
        deleted after the batch ran), the page must say so explicitly
        rather than silently showing nothing or a wrong number."""
        scanned = scan_and_wait(client, auth_headers, test_images)
        ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
        # Explicit file_ids, not a full-directory optimize_and_wait(): the
        # worker pool processes files concurrently, so results[0] isn't
        # guaranteed to be the PNG whose original this test deletes below —
        # it could just as easily land on photo.jpg, whose original is
        # untouched, making "info unavailable" not appear and the test flake
        # depending on completion order. Pin down which file this is.
        png_file = next(f for f in scanned["files"] if f["name"].endswith(".png"))
        result = optimize_and_wait(client, auth_headers, file_ids=[png_file["id"]])
        file_id = result["results"][0]["id"]
        for img in test_images.rglob("*.png"):
            img.unlink()
        r = client.get(f"/api/preview/{ws_name}/{file_id}")
        assert r.status_code == 200
        assert "info unavailable" in r.text


class TestSliderView:
    """The drag-to-compare Slider view: a third mode alongside Side by Side
    and Overlay. slider-top (original) sits over slider-base (compressed)
    and is clipped via CSS to expose the base layer on one side — these
    tests cover what the server actually renders; the clip-path math and
    drag/keyboard behavior themselves are JS/CSS that this Python suite
    can't execute, and were verified separately with a real browser."""

    def test_slider_button_present_and_inactive_by_default(self, client, auth_headers, test_images):
        r = get_preview(client, auth_headers, test_images)
        assert 'id="btn-slider">Slider</button>' in r.text
        assert re.search(r'class="view-btn active"[^>]*id="btn-slider"', r.text) is None

    def test_slider_view_hidden_by_default(self, client, auth_headers, test_images):
        r = get_preview(client, auth_headers, test_images)
        assert re.search(r'id="slider-view"[^>]*style="display:none"', r.text)

    def test_slider_view_has_both_images_and_accessible_handle(self, client, auth_headers, test_images):
        r = get_preview(client, auth_headers, test_images)
        assert 'id="slider-base"' in r.text
        assert 'id="slider-top"' in r.text
        handle = re.search(r'<div class="slider-handle" id="slider-handle"([^>]*)>', r.text)
        assert handle, "slider-handle element not found"
        attrs = handle.group(1)
        assert 'role="slider"' in attrs
        assert 'tabindex="0"' in attrs
        assert 'aria-valuemin="0"' in attrs
        assert 'aria-valuemax="100"' in attrs
        assert 'aria-valuenow="50"' in attrs

    def test_slider_labels_show_dimensions_and_depth_for_both_sides(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
        png_file = next(f for f in scanned["files"] if f["name"].endswith(".png"))
        result = optimize_and_wait(client, auth_headers, file_ids=[png_file["id"]])
        file_id = result["results"][0]["id"]
        r = client.get(f"/api/preview/{ws_name}/{file_id}")
        assert r.status_code == 200
        label_block = re.search(r'<div class="slider-label">(.*?)<div class="slider-wrap"', r.text, re.S)
        assert label_block, "slider-label block not found"
        # test_images' PNGs are 64x64 RGB; fake pngquant/oxipng copy bytes
        # through unchanged, so both sides report the same dimensions/depth.
        assert label_block.group(1).count("64\u00d764") == 2
        assert label_block.group(1).count("24-bit RGB") == 2

    def test_slider_zh_i18n_button_and_footer(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
        result = optimize_and_wait(client, auth_headers)
        file_id = result["results"][0]["id"]
        r = client.get(f"/api/preview/{ws_name}/{file_id}?lang=zh")
        assert r.status_code == 200
        assert "滑动对比" in r.text  # Slider
        assert "Slider</button>" not in r.text
        i18n_match = re.search(r"var I18N = (\{.*?\});", r.text, re.S)
        assert i18n_match, "I18N JS object not found"
        i18n = json.loads(i18n_match.group(1))
        assert "把手" in i18n["footer_slider"]

    def test_slider_en_footer_hint_present(self, client, auth_headers, test_images):
        r = get_preview(client, auth_headers, test_images)
        i18n_match = re.search(r"var I18N = (\{.*?\});", r.text, re.S)
        assert i18n_match, "I18N JS object not found"
        i18n = json.loads(i18n_match.group(1))
        assert "handle" in i18n["footer_slider"]
        assert "arrow keys" in i18n["footer_slider"]