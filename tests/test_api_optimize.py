"""Regression tests for /api/optimize request validation and behavior.

Each test here corresponds to a specific real bug found during manual
testing — see the commit that introduced the test for the original
reproduction, if you need the full story.
"""
from __future__ import annotations

from pathlib import Path

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


class TestFileIdsSemantics:
    """Bug: `if data.file_ids:` treated an explicitly-empty selection
    (user deselected every file) the same as "no filter provided", so
    deselecting everything and clicking Start silently processed
    everything instead of nothing."""

    def test_empty_file_ids_processes_nothing(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, file_ids=[])
        assert r.status_code == 400
        assert "No files" in r.json()["error"]

    def test_omitted_file_ids_processes_everything(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers)
        assert r.status_code == 200
        assert len(d["results"]) == len(scanned["files"]) == 3

    def test_explicit_subset_processes_only_that_subset(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        one_id = scanned["files"][0]["id"]
        r, d = optimize_and_wait(client, auth_headers, file_ids=[one_id])
        assert r.status_code == 200
        assert len(d["results"]) == 1
        assert d["results"][0]["id"] == one_id


class TestOutputDirStructure:
    """Bug: `dest = user_output / f.name` flattened the output directory,
    so two files with the same basename in different subfolders silently
    overwrote each other."""

    def test_same_basename_in_different_subfolders_both_survive(self, client, auth_headers, test_images, tmp_path):
        scan_and_wait(client, auth_headers, test_images)
        out_dir = tmp_path / "custom_output"
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200
        assert all(res["success"] for res in d["results"])

        files = sorted(f.relative_to(out_dir) for f in out_dir.rglob("*") if f.is_file())
        assert len(files) == 3, f"expected 3 files, got {[str(f) for f in files]} — overwrite bug regressed"
        assert Path("2023/vacation/IMG_0001.png") in files
        assert Path("2024/vacation/IMG_0001.png") in files

    def test_output_appears_incrementally_not_only_at_the_end(self, client, auth_headers, tmp_path, fake_bin_dir):
        """Bug: output_dir was only populated once in a batch copy at the
        very end of the run, so the folder looked empty (and wrong) for
        the entire duration of a long batch."""
        import asyncio
        import shutil
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.main import AppState, _process_files
        from app.optimizer import Optimizer

        async def slow_optimize(input_path, output_path, **kwargs):
            await asyncio.sleep(0.25)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5, "error": None, "warning": None}

        opt = Optimizer(bin_dir=fake_bin_dir)
        opt.optimize_png = slow_optimize

        import app.main as m
        m.optimizer = opt

        src = tmp_path / "src.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)

        state = AppState()
        state.workspace = tmp_path / "ws"
        state.workspace.mkdir()
        files = [
            {"id": str(i), "name": f"img{i}.png", "path": str(src)}
            for i in range(3)
        ]
        state.total = 3
        out_dir = tmp_path / "incremental_output"

        async def run_and_check():
            task = asyncio.create_task(_process_files(state, files, "medium", 0, "png", str(out_dir)))
            await asyncio.sleep(0.3)  # let ~1 file finish, batch still running
            mid_run_files = list(out_dir.rglob("*.png")) if out_dir.exists() else []
            await task
            return mid_run_files

        mid_run_files = asyncio.run(run_and_check())
        assert len(mid_run_files) >= 1, "output_dir should already have partial contents mid-run"
        final_files = list(out_dir.rglob("*.png"))
        assert len(final_files) == 3


class TestValidation:
    def test_resize_only_requires_max_width(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, max_width=0, compression_mode="resize_only")
        assert r.status_code == 400

    def test_resize_only_with_max_width_succeeds(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, max_width=50, compression_mode="resize_only")
        assert r.status_code == 200
        assert all(res["success"] for res in d["results"])

    def test_invalid_compression_mode_rejected(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, compression_mode="turbo")
        assert r.status_code == 400

    def test_invalid_protected_color_rejected(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, protected_colors=["not-a-color"])
        assert r.status_code == 400
        assert "not-a-color" in r.json()["error"]

    def test_valid_protected_colors_accepted(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, protected_colors=["#2ecc71", "#FF0000"])
        assert r.status_code == 200

    def test_unsupported_output_format_rejected(self, client, auth_headers, test_images):
        """Bug: output_format had no whitelist, so requesting e.g. 'jpg'
        produced a file literally named .jpg containing real PNG bytes —
        a mislabeled, broken file reported as a success."""
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, output_format="jpg")
        assert r.status_code == 400

    def test_png_and_webp_output_formats_accepted(self, client, auth_headers, test_images):
        for fmt in ("png", "webp"):
            scan_and_wait(client, auth_headers, test_images)
            r, d = optimize_and_wait(client, auth_headers, output_format=fmt)
            assert r.status_code == 200, fmt
            assert all(res["success"] for res in d["results"]), fmt


class TestNonPngInputNormalization:
    """Bug: a non-PNG source (jpg/bmp/tiff) that skipped the resize step
    (no Max Width, or already smaller than the target) got copied through
    completely unmodified but renamed to .png — a file whose extension
    lies about its content, reported as a successful compression."""

    def test_jpeg_output_is_real_png_bytes(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, max_width=0)
        assert r.status_code == 200
        jpg_result = next(res for res in d["results"] if res["name"] == "photo.jpg")
        assert jpg_result["success"] is True

        import app.main as m
        ws = m.SESSIONS[client.cookies.get("imgopt_session")].workspace
        out_file = ws / "output" / "photo.png"
        assert out_file.exists()
        assert out_file.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", (
            "output claims .png but contains non-PNG bytes"
        )
