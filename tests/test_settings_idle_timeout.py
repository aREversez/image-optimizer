"""PUT /api/settings and the config loader agree on session_idle_timeout_hours."""
from __future__ import annotations



def test_settings_reject_zero_idle_timeout_like_the_loader(client, auth_headers, app_module):
    """PUT accepted 0 but the loader treats <= 0 as invalid and silently
    replaced it with the default on next start: reject it at the door."""
    r = client.put("/api/settings", json={"session_idle_timeout_hours": 0}, headers=auth_headers)
    assert r.status_code == 400
    r = client.put("/api/settings", json={"session_idle_timeout_hours": 2}, headers=auth_headers)
    assert r.status_code == 200
    assert app_module._load_app_config()["session_idle_timeout_hours"] == 2
