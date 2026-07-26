"""Regression tests for per-session state isolation and app-token auth.

The single biggest architectural change this project went through: a
module-level global `state` (shared by every browser tab / every user on
a LAN) became a cookie-keyed SESSIONS dict. These tests exist specifically
to catch a regression back to shared global state.
"""
from __future__ import annotations

import re

from .conftest import wait_for


def scan_and_wait(client, headers, directory, recursive=True):
    client.post("/api/scan", json={"directory": str(directory), "recursive": recursive}, headers=headers)
    return wait_for(lambda: (lambda d: not d["running"] and d)(
        client.get("/api/scan-progress", headers=headers).json()
    ))


class TestAppToken:
    def test_sensitive_endpoints_require_token(self, client, tmp_path):
        r = client.post("/api/scan", json={"directory": str(tmp_path)})
        assert r.status_code == 403

    def test_wrong_token_rejected(self, client, tmp_path):
        r = client.post(
            "/api/scan", json={"directory": str(tmp_path)}, headers={"X-App-Token": "wrong"}
        )
        assert r.status_code == 403

    def test_correct_token_accepted(self, client, auth_headers, tmp_path):
        r = client.post("/api/scan", json={"directory": str(tmp_path)}, headers=auth_headers)
        assert r.status_code == 200

    def test_image_endpoints_work_without_token(self, client, auth_headers, test_images):
        """Thumbnails/results are loaded via plain <img src>, which can't
        carry a custom header — these must stay accessible without the
        token (session cookie is what scopes access here instead)."""
        result = scan_and_wait(client, auth_headers, test_images)
        thumb_url = result["files"][0]["thumbnail"]
        r = client.get(thumb_url)  # deliberately no auth header
        assert r.status_code == 200


class TestSessionIsolation:
    def test_two_clients_get_different_sessions(self, app_module, fake_bin_dir):
        from fastapi.testclient import TestClient
        with TestClient(app_module.app) as a, TestClient(app_module.app) as b:
            a.get("/")
            b.get("/")
            assert a.cookies.get("imgopt_session") != b.cookies.get("imgopt_session")
            assert len(app_module.SESSIONS) >= 2

    def test_scan_results_do_not_leak_between_sessions(self, app_module, fake_bin_dir, test_images, tmp_path):
        from fastapi.testclient import TestClient
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        with TestClient(app_module.app) as a, TestClient(app_module.app) as b:
            token_a = re.search(r'window\.APP_TOKEN = "([^"]+)"', a.get("/").text).group(1)
            token_b = re.search(r'window\.APP_TOKEN = "([^"]+)"', b.get("/").text).group(1)
            ha, hb = {"X-App-Token": token_a}, {"X-App-Token": token_b}

            result_a = scan_and_wait(a, ha, test_images)
            result_b = scan_and_wait(b, hb, empty_dir)

            assert len(result_a["files"]) == 3
            assert len(result_b["files"]) == 0

    def test_cross_session_thumbnail_access_is_denied(self, app_module, fake_bin_dir, test_images):
        from fastapi.testclient import TestClient
        with TestClient(app_module.app) as a, TestClient(app_module.app) as b:
            token_a = re.search(r'window\.APP_TOKEN = "([^"]+)"', a.get("/").text).group(1)
            b.get("/")
            ha = {"X-App-Token": token_a}

            result_a = scan_and_wait(a, ha, test_images)
            thumb_url = result_a["files"][0]["thumbnail"]

            # b's session cookie doesn't know about a's workspace
            r = b.get(thumb_url)
            assert r.status_code == 404

    def test_concurrent_optimize_runs_do_not_interfere(self, app_module, fake_bin_dir, test_images, tmp_path):
        from fastapi.testclient import TestClient
        empty_dir = tmp_path / "empty2"
        empty_dir.mkdir()

        with TestClient(app_module.app) as a, TestClient(app_module.app) as b:
            token_a = re.search(r'window\.APP_TOKEN = "([^"]+)"', a.get("/").text).group(1)
            token_b = re.search(r'window\.APP_TOKEN = "([^"]+)"', b.get("/").text).group(1)
            ha, hb = {"X-App-Token": token_a}, {"X-App-Token": token_b}

            scan_and_wait(a, ha, test_images)
            scan_and_wait(b, hb, empty_dir)

            ra = a.post("/api/optimize", json={}, headers=ha)
            rb = b.post("/api/optimize", json={}, headers=hb)
            assert ra.status_code == 200
            assert rb.status_code == 400  # b has no files
