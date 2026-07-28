"""Regression tests for the issues found in the July 2026 project review:

1. Output filename collision — "photo.png" and "photo.jpg" both mapped to
   "photo.png" after the suffix swap and silently overwrote each other.
2. Upload path traversal — /api/upload reported the raw (unsanitized)
   f.filename as "name", which _process_files joined onto the output dir.
3. /api/preview reflected an arbitrary ws_name into HTML without either
   the workspace-ownership check every other endpoint has or escaping.
4. quality had no server-side whitelist (silently fell back to "medium").
5. Standard-mode PNG without pngquant silently degraded — /api/optimize
   now surfaces a warning field.
6. _scan_images missed .webp and mixed-case extensions.
7. /api/reset clears server-side session state (new endpoint).
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from .conftest import wait_for


def scan_and_wait(client, headers, directory, recursive=True):
    client.post("/api/scan", json={"directory": str(directory), "recursive": recursive}, headers=headers)
    return wait_for(lambda: (lambda d: not d["running"] and d)(
        client.get("/api/scan-progress", headers=headers).json()
    ))


def optimize_and_wait(client, headers, **body):
    r = client.post("/api/optimize", json=body, headers=headers)
    if r.status_code != 200:
        return r, None
    d = wait_for(lambda: (lambda p: not p["running"] and p)(
        client.get("/api/progress", headers=headers).json()
    ))
    return r, d


def session_workspace(client):
    import app.main as m
    return m.SESSIONS[client.cookies.get("imgopt_session")].workspace


class TestOutputNameCollision:
    def test_same_stem_different_extension_both_survive(self, client, auth_headers, tmp_path):
        d = tmp_path / "collide"
        d.mkdir()
        Image.new("RGB", (40, 40), (255, 0, 0)).save(d / "photo.png")
        Image.new("RGB", (40, 40), (0, 255, 0)).save(d / "photo.jpg", format="JPEG")

        scan_and_wait(client, auth_headers, d)
        r, res = optimize_and_wait(client, auth_headers)
        assert r.status_code == 200
        assert all(x["success"] for x in res["results"]), res["results"]

        out = session_workspace(client) / "output"
        files = sorted(f.name for f in out.rglob("*") if f.is_file())
        assert len(files) == 2, f"collision regressed — one file overwrote the other: {files}"
        assert "photo.png" in files and "photo_2.png" in files

        # Each result records exactly where its bytes actually landed.
        names = sorted(x["output_name"] for x in res["results"])
        assert names == ["photo.png", "photo_2.png"]

    def test_preview_uses_disambiguated_output_name(self, client, auth_headers, tmp_path):
        d = tmp_path / "collide2"
        d.mkdir()
        Image.new("RGB", (40, 40), (255, 0, 0)).save(d / "photo.png")
        Image.new("RGB", (40, 40), (0, 255, 0)).save(d / "photo.jpg", format="JPEG")

        scanned = scan_and_wait(client, auth_headers, d)
        ws_name = scanned["files"][0]["thumbnail"].split("/")[3]
        _, res = optimize_and_wait(client, auth_headers)

        for result in res["results"]:
            page = client.get(f"/api/preview/{ws_name}/{result['id']}")
            assert page.status_code == 200
            assert f"/api/result/{ws_name}/{result['output_name']}" in page.text
            # And the URL it embeds must actually serve the file.
            rc = client.get(f"/api/result/{ws_name}/{result['output_name']}")
            assert rc.status_code == 200


class TestUploadFilenameSanitization:
    def _upload(self, client, headers, filename):
        buf = BytesIO()
        Image.new("RGB", (16, 16)).save(buf, format="PNG")
        buf.seek(0)
        return client.post(
            "/api/upload", files={"files": (filename, buf, "image/png")}, headers=headers
        )

    def test_traversal_filename_is_reduced_to_basename(self, client, auth_headers):
        r = self._upload(client, auth_headers, "../../evil.png")
        assert r.status_code == 200
        files = r.json()["files"]
        assert len(files) == 1
        assert files[0]["name"] == "evil.png", (
            "raw f.filename leaked into 'name' — _process_files would join "
            "it onto the output dir and write outside the workspace"
        )

    def test_optimized_output_stays_inside_workspace(self, client, auth_headers, tmp_path):
        r = self._upload(client, auth_headers, "..\\..\\escape.png")
        assert r.status_code == 200
        _, res = optimize_and_wait(client, auth_headers)
        assert all(x["success"] for x in res["results"])

        out = (session_workspace(client) / "output").resolve()
        for f in out.rglob("*"):
            assert str(f.resolve()).startswith(str(out)), f


class TestPreviewWorkspaceCheck:
    def test_foreign_ws_name_is_404(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        _, res = optimize_and_wait(client, auth_headers)
        file_id = res["results"][0]["id"]
        r = client.get(f"/api/preview/imgopt_someoneelse/{file_id}")
        assert r.status_code == 404

    def test_ws_name_is_not_reflected_unescaped(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        _, res = optimize_and_wait(client, auth_headers)
        file_id = res["results"][0]["id"]
        payload = '"><script>alert(1)</script>'
        r = client.get(f"/api/preview/{payload}/{file_id}")
        assert r.status_code == 404
        assert "<script>alert(1)</script>" not in r.text, "reflected XSS regressed"


class TestOptimizeValidationAndWarning:
    def test_invalid_quality_rejected(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r = client.post("/api/optimize", json={"quality": "ultra"}, headers=auth_headers)
        assert r.status_code == 400
        assert "quality" in r.json()["error"].lower()

    def test_png_standard_without_pngquant_returns_warning(self, client, auth_headers, test_images, app_module):
        scan_and_wait(client, auth_headers, test_images)
        app_module.optimizer.pngquant_path = None  # simulate missing binary
        r, res = optimize_and_wait(client, auth_headers)
        assert r.status_code == 200
        assert r.json()["warning"], "degraded capability should be surfaced, not silent"
        assert all(x["success"] for x in res["results"])  # still degrades gracefully

    def test_webp_never_warns_about_pngquant(self, client, auth_headers, test_images, app_module):
        scan_and_wait(client, auth_headers, test_images)
        app_module.optimizer.pngquant_path = None
        r, _ = optimize_and_wait(client, auth_headers, output_format="webp")
        assert r.status_code == 200
        assert r.json()["warning"] is None


class TestScanExtensions:
    def test_webp_and_mixed_case_are_found(self, tmp_path):
        from app.main import _scan_images
        d = tmp_path / "exts"
        d.mkdir()
        Image.new("RGB", (10, 10)).save(d / "a.webp", format="WEBP")
        Image.new("RGB", (10, 10)).save(d / "b.PNG")
        Image.new("RGB", (10, 10)).save(d / "c.Png")
        (d / "notes.txt").write_text("not an image")
        found = {p.name for p in _scan_images(d, recursive=False)}
        assert found == {"a.webp", "b.PNG", "c.Png"}

    def test_scanned_webp_optimizes_successfully(self, client, auth_headers, tmp_path):
        d = tmp_path / "webpsrc"
        d.mkdir()
        Image.new("RGB", (30, 30), (1, 2, 3)).save(d / "pic.webp", format="WEBP")
        scan_and_wait(client, auth_headers, d)
        r, res = optimize_and_wait(client, auth_headers)
        assert r.status_code == 200
        assert res["results"][0]["success"] is True
        out_file = session_workspace(client) / "output" / "pic.png"
        assert out_file.exists()
        assert out_file.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


class TestResetEndpoint:
    def test_reset_requires_token(self, client):
        assert client.post("/api/reset").status_code == 403

    def test_reset_clears_server_state(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        assert client.get("/api/state").json()["files"]
        r = client.post("/api/reset", headers=auth_headers)
        assert r.status_code == 200
        d = client.get("/api/state").json()
        assert d["files"] == [] and d["results"] == [] and d["input_dir"] is None
