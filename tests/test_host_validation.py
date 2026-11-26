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


class TestReverseProxyAndNormalisation:
    def test_proxy_that_rewrites_host_works_when_the_public_name_is_allowed(self, client, monkeypatch):
        """nginx's default sends Host: 127.0.0.1:PORT while the browser's
        Origin is the public name. Allowed only because the operator listed
        that name; an unlisted origin stays refused."""
        tok = _token(client)
        hdr = {"X-App-Token": tok, "Host": "127.0.0.1:8000", "Origin": "https://tools.example"}
        body = {"directory": "/nonexistent"}
        assert client.post("/api/scan", json=body, headers=hdr).status_code == 403
        monkeypatch.setenv("IMAGE_OPTIMIZER_ALLOWED_HOSTS", "tools.example")
        assert client.post("/api/scan", json=body, headers=hdr).status_code == 400  # reached the handler
        hdr["Origin"] = "https://attacker.example"
        assert client.post("/api/scan", json=body, headers=hdr).status_code == 403

    def test_ip_literal_origin_is_not_a_blanket_pass_for_a_different_host(self, client):
        tok = _token(client)
        r = client.post("/api/scan", json={"directory": "/nonexistent"},
                        headers={"X-App-Token": tok, "Origin": "http://192.168.1.50:8080"})
        assert r.status_code == 403

    def test_trailing_dot_and_case_are_normalised(self, client):
        assert client.get("/", headers={"Host": "LocalHost.:8090"}).status_code == 200

    def test_refusals_name_the_setting_that_fixes_them(self, client):
        r = client.get("/", headers={"Host": "mybox.lan"})
        assert r.status_code == 400 and "IMAGE_OPTIMIZER_ALLOWED_HOSTS" in r.json()["error"]
        tok = _token(client)
        r = client.post("/api/scan", json={"directory": "/x"},
                        headers={"X-App-Token": tok, "Origin": "https://tools.example", "Host": "127.0.0.1:8000"})
        assert r.status_code == 403 and "IMAGE_OPTIMIZER_ALLOWED_HOSTS" in r.json()["error"]
