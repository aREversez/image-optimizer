"""A failed copy into the output folder is a failed file (review finding F2).

Before the fix the copy's exception only appended "Output copy failed" to the
log: the result stayed success=True, the batch entry became "done" and the
batch state was cleared, so the green run left nothing in the output folder
and nothing to resume."""
from __future__ import annotations

import json
from pathlib import Path

from .test_api_optimize import scan_and_wait, optimize_and_wait
from .test_batch_resume import _batch_state_path


def _blocked_output(tmp_path: Path) -> Path:
    """An output folder that cannot be created: its parent is a regular file."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    return blocker / "out"


class TestFailedCopyIsReportedAsFailure:
    def test_result_is_failed_with_the_os_error(self, client, auth_headers, test_images, tmp_path):
        out = _blocked_output(tmp_path)
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, quality="medium", output_format="png", output_dir=str(out))
        assert r.status_code == 200
        assert d["results"], "expected per-file results"
        for res in d["results"]:
            assert res["success"] is False
            assert "Output copy failed" in res["error"]
            assert "final_output_path" not in res
        assert any("FAILED" in line for line in d["logs"])
        assert not any(line.strip().startswith("OK ") for line in d["logs"])

    def test_batch_entries_stay_failed_so_resume_and_retry_work(
        self, client, auth_headers, test_images, tmp_path,
    ):
        out = _blocked_output(tmp_path)
        scan_and_wait(client, auth_headers, test_images)
        optimize_and_wait(client, auth_headers, quality="medium", output_format="png", output_dir=str(out))
        p = _batch_state_path(out)
        assert p.exists(), "batch state must survive a run whose copies all failed"
        bs = json.loads(p.read_text(encoding="utf-8"))
        assert bs["files"] and all(f["status"] == "failed" for f in bs["files"])

    def test_retry_succeeds_once_the_output_folder_is_usable(
        self, client, auth_headers, test_images, tmp_path,
    ):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x")
        out = blocker / "out"
        scan_and_wait(client, auth_headers, test_images)
        optimize_and_wait(client, auth_headers, quality="medium", output_format="png", output_dir=str(out))
        blocker.unlink()  # the problem goes away
        r, d = optimize_and_wait(
            client, auth_headers, quality="medium", output_format="png",
            output_dir=str(out), retry=True,
        )
        assert r.status_code == 200
        assert d["results"] and all(res["success"] for res in d["results"])
        assert list(out.rglob("*.png"))

    def test_copy_error_from_the_helper_is_reported_too(
        self, client, auth_headers, test_images, tmp_path, app_module, monkeypatch,
    ):
        """Disk full / permission error raised by the copy itself."""
        def boom(src, dest):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(app_module.persistence, "atomic_copy", boom)
        out = tmp_path / "out"
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, quality="medium", output_format="png", output_dir=str(out))
        assert d["results"] and all(
            (not res["success"]) and "No space left" in res["error"] for res in d["results"]
        )
        assert not list(out.rglob("*.png"))

    def test_successful_copy_is_unchanged(self, client, auth_headers, test_images, tmp_path):
        out = tmp_path / "out"
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, quality="medium", output_format="png", output_dir=str(out))
        assert d["results"] and all(res["success"] and res["final_output_path"] for res in d["results"])
        assert not list(out.rglob(".*")), "no staging leftovers next to the outputs"
