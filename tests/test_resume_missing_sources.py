"""Resume marks vanished sources "missing" so the banner clears (review F13).

Resume skipped sources that no longer exist but left their batch entries
"pending", so the batch file was never cleared and the Resume banner came
back forever."""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from .conftest import wait_for
from .test_batch_resume import _batch_state_path, _save_batch_state


def _batch(tmp_path, names, existing):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    out.mkdir()
    files = []
    for i, n in enumerate(names):
        p = src / n
        if n in existing:
            Image.new("RGB", (32, 32), (i * 40, 5, 5)).save(p)
        files.append({"id": str(i), "path": str(p), "name": n, "size": 100, "status": "pending"})
    _save_batch_state({
        "output_dir": str(out), "input_dir": str(src), "files": files, "results": [],
        "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
    })
    return out


def _resume(client, headers, out):
    return client.post("/api/optimize", json={
        "resume": True, "output_dir": str(out), "output_format": "png", "compression_mode": "lossless",
    }, headers=headers)


def test_resume_with_some_vanished_sources_finishes_and_clears_the_batch(client, auth_headers, tmp_path):
    out = _batch(tmp_path, ["a.png", "gone.png"], existing={"a.png"})
    r = _resume(client, auth_headers, out)
    assert r.status_code == 200 and r.json()["total"] == 1
    wait_for(lambda: (lambda p: not p["running"] and p)(client.get("/api/progress", headers=auth_headers).json()), timeout=30.0)
    assert (out / "a.png").exists()
    assert not _batch_state_path(out).exists(), "vanished source must not keep the batch alive"
    assert client.get("/api/batch-state", headers=auth_headers).json()["has_batch"] is False


def test_resume_with_every_source_vanished_clears_the_batch_and_says_why(client, auth_headers, tmp_path):
    out = _batch(tmp_path, ["gone1.png", "gone2.png"], existing=set())
    r = _resume(client, auth_headers, out)
    assert r.status_code == 400 and "no longer available" in r.json()["error"]
    assert not _batch_state_path(out).exists()
    assert client.get("/api/batch-state", headers=auth_headers).json()["has_batch"] is False


def test_vanished_entries_are_marked_missing_while_other_work_remains(client, auth_headers, tmp_path, app_module, monkeypatch):
    """If the resumed run cannot finish (here: cancelled before it starts),
    the persisted file must say which entries are gone, not leave them pending."""
    out = _batch(tmp_path, ["a.png", "gone.png"], existing={"a.png"})
    # Make the run end without completing "a.png": the copy step fails.
    monkeypatch.setattr(app_module.persistence, "atomic_copy", lambda s, d: (_ for _ in ()).throw(OSError("disk full")))
    r = _resume(client, auth_headers, out)
    assert r.status_code == 200
    wait_for(lambda: (lambda p: not p["running"] and p)(client.get("/api/progress", headers=auth_headers).json()), timeout=30.0)
    bs = json.loads(_batch_state_path(out).read_text(encoding="utf-8"))
    status = {f["name"]: f["status"] for f in bs["files"]}
    assert status == {"a.png": "failed", "gone.png": "missing"}
