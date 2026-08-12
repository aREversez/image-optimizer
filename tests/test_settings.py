"""Tests for the in-app settings panel (GET/PUT /api/settings)."""
import json
import pytest
from fastapi.testclient import TestClient


class TestGetSettings:
    def test_get_settings_returns_defaults(self, client: TestClient):
        """No config.json → returns DEFAULT_CONFIG values."""
        r = client.get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        assert "settings" in data
        assert "defaults" in data
        assert data["settings"]["port"] == 8090
        assert data["settings"]["concurrent_workers"] == 4
        assert data["settings"]["thumbnail_workers"] == 4
        assert data["settings"]["workspace_cleanup_delay"] == 10.0
        assert data["settings"]["session_idle_timeout_hours"] == 4

    def test_get_settings_returns_saved_config(self, client: TestClient, auth_headers):
        """After PUT, GET returns the persisted values."""
        client.put("/api/settings", json={"port": 9999}, headers=auth_headers)
        r = client.get("/api/settings")
        assert r.status_code == 200
        assert r.json()["settings"]["port"] == 9999


class TestPutSettings:
    def test_put_settings_persists(self, client: TestClient, auth_headers):
        """PUT writes to config.json and the values survive a re-read."""
        r = client.put("/api/settings", json={"port": 7777, "concurrent_workers": 8}, headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["settings"]["port"] == 7777
        assert data["settings"]["concurrent_workers"] == 8

    def test_put_settings_validates_port(self, client: TestClient, auth_headers):
        """Invalid port values are rejected."""
        r = client.put("/api/settings", json={"port": 99999}, headers=auth_headers)
        assert r.status_code == 400
        assert "port" in r.json()["error"].lower() or "Invalid" in r.json()["error"]

        r = client.put("/api/settings", json={"port": -1}, headers=auth_headers)
        assert r.status_code == 400

        r = client.put("/api/settings", json={"port": "abc"}, headers=auth_headers)
        assert r.status_code == 400

    def test_put_settings_validates_workers(self, client: TestClient, auth_headers):
        """Negative worker counts are rejected."""
        r = client.put("/api/settings", json={"concurrent_workers": -1}, headers=auth_headers)
        assert r.status_code == 400
        assert "concurrent_workers" in r.json()["error"]

        r = client.put("/api/settings", json={"thumbnail_workers": 0}, headers=auth_headers)
        assert r.status_code == 400

    def test_put_settings_partial_update(self, client: TestClient, auth_headers):
        """Updating one field doesn't erase the others."""
        # First set a known config
        client.put("/api/settings", json={"port": 5555, "concurrent_workers": 2}, headers=auth_headers)
        # Now only update workspace_cleanup_delay
        r = client.put("/api/settings", json={"workspace_cleanup_delay": 20.0}, headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["settings"]["workspace_cleanup_delay"] == 20.0
        # The previously-set values should still be there
        assert data["settings"]["port"] == 5555
        assert data["settings"]["concurrent_workers"] == 2

    def test_put_settings_rejects_unknown_key(self, client: TestClient, auth_headers):
        """Unknown config keys are rejected."""
        r = client.put("/api/settings", json={"nonexistent_key": 42}, headers=auth_headers)
        assert r.status_code == 400
        assert "Unknown" in r.json()["error"]

    def test_put_settings_requires_restart(self, client: TestClient, auth_headers):
        """Changing restart-required keys returns them in the response."""
        r = client.put("/api/settings", json={"port": 8888, "workspace_cleanup_delay": 5.0}, headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "port" in data["requires_restart"]
        assert "workspace_cleanup_delay" not in data["requires_restart"]

    def test_put_settings_validates_host(self, client: TestClient, auth_headers):
        """Empty host is rejected."""
        r = client.put("/api/settings", json={"host": ""}, headers=auth_headers)
        assert r.status_code == 400
        assert "host" in r.json()["error"].lower() or "Invalid" in r.json()["error"]

    def test_put_settings_validates_cleanup_delay(self, client: TestClient, auth_headers):
        """Negative cleanup delay is rejected."""
        r = client.put("/api/settings", json={"workspace_cleanup_delay": -1}, headers=auth_headers)
        assert r.status_code == 400

    def test_put_settings_validates_idle_timeout(self, client: TestClient, auth_headers):
        """Negative idle timeout is rejected."""
        r = client.put("/api/settings", json={"session_idle_timeout_hours": -2}, headers=auth_headers)
        assert r.status_code == 400
