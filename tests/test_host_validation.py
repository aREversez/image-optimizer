"""Host and Origin validation on the local server (review finding F5).

GET / injects the app token into the page, and the token comment relies on
the same-origin policy. DNS rebinding defeats that: a hostile page whose
hostname is re-pointed at 127.0.0.1 is same-origin with the app and can read
the token. The server therefore refuses any Host header that is not a
loopback name, an IP literal, or one the operator allowed - a rebinding page
always arrives with its own hostname - and refuses state-changing requests
whose Origin / Sec-Fetch-Site says they came from another site."""
from __future__ import annotations

import re

import pytest


def _token(client):
    r = client.get("/")
    return re.search(r'window\.APP_TOKEN = "([^"]+)"', r.text).group(1)


class TestHostHeader:
    @pytest.mark.parametrize("host", [
        "attacker.example", "attacker.example:8090", "127.0.0.1.attacker.example",
        "localhost.attacker.example", "evil.com:80",
    ])
    def test_foreign_host_cannot_read_the_page_or_token(self, client, host):
        r = client.get("/", headers={"Host": host})
        assert r.status_code in (400, 403)
        assert "APP_TOKEN" not in r.text

    def test_foreign_host_is_refused_on_api_routes_too(self, client):
        tok = _token(client)
        r = client.get("/api/health", headers={"Host": "attacker.example"})
        assert r.status_code in (400, 403)
        r = client.post("/api/scan", json={"directory": "/"}, headers={"Host": "attacker.example", "X-App-Token": tok})
        assert r.status_code in (400, 403)

    @pytest.mark.parametrize("host", [
        "127.0.0.1:8090", "127.0.0.1", "localhost:8090", "LOCALHOST:8090",
        "[::1]:8090", "[::1]", "192.168.1.20:8090", "app.localhost:8090",
    ])
    def test_loopback_names_and_ip_literals_work(self, client, host):
        r = client.get("/", headers={"Host": host})
        assert r.status_code == 200
        assert "APP_TOKEN" in r.text

    def test_operator_can_allow_a_hostname_via_env(self, client, monkeypatch):
        monkeypatch.setenv("IMAGE_OPTIMIZER_ALLOWED_HOSTS", "mybox.lan, other.lan")
        assert client.get("/", headers={"Host": "mybox.lan:8090"}).status_code == 200
        assert client.get("/", headers={"Host": "OTHER.lan"}).status_code == 200
        assert client.get("/", headers={"Host": "attacker.example"}).status_code in (400, 403)

    def test_bound_hostname_is_allowed(self, client, app_module, monkeypatch):
        monkeypatch.setattr(app_module, "_BOUND_HOST", "mybox.lan")
        assert client.get("/", headers={"Host": "mybox.lan:8090"}).status_code == 200


class TestOriginCheck:
    def test_cross_site_state_change_is_refused_even_with_the_token(self, client):
        tok = _token(client)
        r = client.post("/api/scan", json={"directory": "/nonexistent"},
                        headers={"X-App-Token": tok, "Origin": "https://attacker.example"})
        assert r.status_code == 403

    def test_sec_fetch_site_cross_site_is_refused(self, client):
        tok = _token(client)
        r = client.post("/api/scan", json={"directory": "/nonexistent"},
                        headers={"X-App-Token": tok, "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403

    def test_same_origin_request_still_works(self, client):
        tok = _token(client)
        r = client.post("/api/scan", json={"directory": "/nonexistent"},
                        headers={"X-App-Token": tok, "Origin": "http://127.0.0.1:8090",
                                 "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 400  # reached the handler: "Directory does not exist"

    def test_requests_without_origin_headers_still_work(self, client):
        """curl, scripts and the bundled CLI tooling send neither header."""
        tok = _token(client)
        r = client.post("/api/scan", json={"directory": "/nonexistent"}, headers={"X-App-Token": tok})
        assert r.status_code == 400

    def test_cross_site_get_is_not_blocked_by_the_origin_rule(self, client):
        """Reads are protected by the Host and token checks, not the Origin rule."""
        assert client.get("/api/health", headers={"Origin": "https://other.example"}).status_code in (200, 503)
