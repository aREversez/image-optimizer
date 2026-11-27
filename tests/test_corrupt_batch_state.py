"""A corrupt batch file is reported and kept, not silently discarded
(review finding F9).

_load_batch_state() returned None on any JSONDecodeError, so the resume data
just vanished: the file stayed on disk, every later page load found it again
and listed nothing, and the user could not tell a broken batch from a
finished one.
"""
from __future__ import annotations

from .test_batch_resume import _batch_state_path


def _corrupt(out):
    path = _batch_state_path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    return path


def test_batch_state_endpoint_moves_a_corrupt_file_aside(client, auth_headers, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    path = _corrupt(out)

    r = client.get("/api/batch-state", params={"output_dir": str(out)}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["has_batch"] is False

    assert not path.exists(), "the unreadable file must not be found again on every load"
    kept = list(path.parent.glob(path.name + ".corrupt-*"))
    assert len(kept) == 1, "the file is kept for inspection, never deleted"
    assert kept[0].read_text(encoding="utf-8") == "{not json"


def test_page_load_listing_also_quarantines_corrupt_state(client, auth_headers, tmp_path):
    """The banner's own call (no output_dir) must not keep tripping over it."""
    out = tmp_path / "out"
    out.mkdir()
    path = _corrupt(out)

    assert client.get("/api/batch-state", headers=auth_headers).json()["has_batch"] is False
    assert not path.exists()
    assert len(list(path.parent.glob(path.name + ".corrupt-*"))) == 1


def test_resume_on_a_corrupt_batch_says_there_is_nothing_to_resume(client, auth_headers, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _corrupt(out)

    r = client.post("/api/optimize", json={
        "resume": True, "output_dir": str(out),
        "output_format": "png", "compression_mode": "lossless",
    }, headers=auth_headers)
    assert r.status_code == 400
    assert "No resumable" in r.json()["error"]
