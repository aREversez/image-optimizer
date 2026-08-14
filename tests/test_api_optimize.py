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


class TestRetryPreservesEarlierResults:
    """The Retry Failed button re-runs only a subset with retry=True. Unlike
    a normal subset run (which wipes ws/output and replaces state.results),
    retry must keep the earlier successes' outputs on disk AND merge the
    re-run entries back into the full results set instead of shrinking it to
    just the retried files."""

    def _output_dir(self, client):
        import app.main as m
        return m.SESSIONS[client.cookies.get("imgopt_session")].workspace / "output"

    def test_retry_subset_keeps_full_result_set_and_outputs(self, client, auth_headers, test_images):
        scanned = scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers)
        assert r.status_code == 200 and len(d["results"]) == 3
        assert len(list(self._output_dir(client).rglob("*.png"))) == 3

        one_id = scanned["files"][0]["id"]
        r, d = optimize_and_wait(client, auth_headers, file_ids=[one_id], retry=True)
        assert r.status_code == 200
        # Merged, not replaced: all three results survive (2 preserved + 1 re-run).
        assert len(d["results"]) == 3
        assert {res["id"] for res in d["results"]} == {f["id"] for f in scanned["files"]}
        # Output dir was not wiped: earlier successes' files are still there.
        assert len(list(self._output_dir(client).rglob("*.png"))) == 3

    def test_non_retry_subset_still_replaces_results(self, client, auth_headers, test_images):
        """Guard the default path: without retry, a subset run replaces the
        result set (existing behavior relied on by the UI's normal Start)."""
        scanned = scan_and_wait(client, auth_headers, test_images)
        optimize_and_wait(client, auth_headers)
        one_id = scanned["files"][0]["id"]
        r, d = optimize_and_wait(client, auth_headers, file_ids=[one_id])
        assert r.status_code == 200
        assert len(d["results"]) == 1


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

    def test_output_saved_log_line_appears_exactly_once(self, client, auth_headers, test_images, tmp_path):
        """Bug: the summary log line was appended twice at the end of
        _process_files (two back-to-back identical `if output_dir:`
        blocks — almost certainly a copy-paste slip), so every run with a
        custom output_dir showed the same "Output saved to: ..." line
        duplicated in the live log."""
        scan_and_wait(client, auth_headers, test_images)
        out_dir = tmp_path / "log_dedup_output"
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200
        matching = [line for line in d["logs"] if "Output saved to" in line]
        assert len(matching) == 1, f"expected exactly one 'Output saved to' line, got {matching}"

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


class TestRevealInExplorer:
    """final_output_path is set on results only when a persistent
    output_dir was used, and /api/reveal only opens a path that matches
    one of the session's own recorded results."""

    def test_final_output_path_set_when_output_dir_given(self, client, auth_headers, test_images, tmp_path):
        scan_and_wait(client, auth_headers, test_images)
        out_dir = tmp_path / "output"
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200
        successes = [res for res in d["results"] if res["success"]]
        assert successes
        for res in successes:
            assert res.get("final_output_path"), res
            assert Path(res["final_output_path"]).exists()
            assert Path(res["final_output_path"]).parent == out_dir or \
                out_dir in Path(res["final_output_path"]).parents

    def test_final_output_path_absent_without_output_dir(self, client, auth_headers, test_images):
        """No persistent output_dir → temp workspace only → nothing to
        reveal, so the field must be absent (frontend uses its presence
        to decide whether to render the Reveal button at all)."""
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers)
        assert r.status_code == 200
        for res in d["results"]:
            assert "final_output_path" not in res or not res["final_output_path"]

    def test_reveal_rejects_path_not_in_this_sessions_results(self, client, auth_headers, tmp_path):
        """A path that was never recorded as one of this session's own
        final_output_path values must be rejected — otherwise this
        endpoint is an arbitrary 'open explorer anywhere' oracle driven
        by client-supplied input."""
        somewhere_else = tmp_path / "not_a_real_output" / "file.png"
        somewhere_else.parent.mkdir(parents=True)
        somewhere_else.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)

        r = client.post("/api/reveal", json={"path": str(somewhere_else)}, headers=auth_headers)
        assert r.status_code == 400
        assert "Not a known output file" in r.json()["error"]

    def test_reveal_known_output_path_succeeds(self, client, auth_headers, test_images, tmp_path, monkeypatch):
        """A path that genuinely is one of this session's final_output_path
        values should be accepted and attempt to launch the OS file
        explorer — the subprocess call itself is mocked out since there's
        no real desktop environment in CI."""
        scan_and_wait(client, auth_headers, test_images)
        out_dir = tmp_path / "output"
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200
        target = next(res["final_output_path"] for res in d["results"] if res["success"])

        calls = []
        import app.main as m
        monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))

        r2 = client.post("/api/reveal", json={"path": target}, headers=auth_headers)
        assert r2.status_code == 200
        assert r2.json()["success"] is True
        assert len(calls) == 1

    def test_reveal_requires_auth(self, client, auth_headers, test_images, tmp_path):
        scan_and_wait(client, auth_headers, test_images)
        out_dir = tmp_path / "output"
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        target = next(res["final_output_path"] for res in d["results"] if res["success"])

        r2 = client.post("/api/reveal", json={"path": target})  # no auth header
        assert r2.status_code in (401, 403)

    def test_reveal_missing_file_returns_404(self, client, auth_headers, test_images, tmp_path):
        scan_and_wait(client, auth_headers, test_images)
        out_dir = tmp_path / "output"
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        target = next(res["final_output_path"] for res in d["results"] if res["success"])

        Path(target).unlink()  # simulate the user having deleted it since

        r2 = client.post("/api/reveal", json={"path": target}, headers=auth_headers)
        assert r2.status_code == 404

    def test_reveal_empty_path_opens_output_folder(self, client, auth_headers, test_images, tmp_path, monkeypatch):
        """The UI shows one "Reveal Output Folder" button instead of a
        per-image Reveal link: it posts an empty path, and the backend
        opens the output_dir it recorded for this run itself
        (state.output_dir) rather than anything client-supplied."""
        scan_and_wait(client, auth_headers, test_images)
        out_dir = tmp_path / "output"
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200

        calls = []
        import app.main as m
        monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
        # The win32 focus helper would otherwise poll for a real Explorer
        # window for up to 3s in CI (and never find one — Popen is mocked).
        if hasattr(m, "_focus_new_explorer_window"):
            monkeypatch.setattr(m, "_focus_new_explorer_window", lambda timeout=3.0: None)

        r2 = client.post("/api/reveal", json={"path": ""}, headers=auth_headers)
        assert r2.status_code == 200
        assert r2.json()["success"] is True
        assert len(calls) == 1
        # The launched target must be the run's output folder itself —
        # last argv entry on every platform branch (win32 folder mode
        # wraps it in "/e,/root,<path>" to force a fresh window).
        last = calls[0][0][0][-1]
        if last.startswith("/e,/root,"):
            last = last.split("/e,/root,", 1)[1]
        assert Path(last).resolve() == out_dir.resolve()

    def test_reveal_empty_path_without_output_dir_rejected(self, client, auth_headers, test_images):
        """A temp-workspace-only run has no persistent folder to reveal."""
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers)
        assert r.status_code == 200
        r2 = client.post("/api/reveal", json={"path": ""}, headers=auth_headers)
        assert r2.status_code == 400
        assert "No output folder" in r2.json()["error"]

    def test_reveal_empty_path_missing_folder_returns_404(self, client, auth_headers, test_images, tmp_path):
        scan_and_wait(client, auth_headers, test_images)
        out_dir = tmp_path / "output"
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200

        import shutil
        shutil.rmtree(out_dir)  # simulate the user having deleted it since

        r2 = client.post("/api/reveal", json={"path": ""}, headers=auth_headers)
        assert r2.status_code == 404


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
        """Bug: output_format had no whitelist, so requesting e.g. 'gif'
        produced a file literally named .gif containing real PNG bytes —
        a mislabeled, broken file reported as a success."""
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, output_format="gif")
        assert r.status_code == 400

    def test_png_webp_jpg_output_formats_accepted(self, client, auth_headers, test_images):
        for fmt in ("png", "webp", "jpg"):
            scan_and_wait(client, auth_headers, test_images)
            r, d = optimize_and_wait(client, auth_headers, output_format=fmt)
            assert r.status_code == 200, fmt
            assert all(res["success"] for res in d["results"]), fmt

    def test_jpg_output_keeps_jpeg_format_end_to_end(self, client, auth_headers, test_images):
        """The headline JPEG behavior through the full API: a batch of JPGs
        compressed with output_format='jpg' comes back as *real* JPEGs
        (re-encoded by cjpeg), not PNG-converted files wearing .jpg names."""
        from PIL import Image

        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, output_format="jpg")
        assert r.status_code == 200
        jpg_result = next(res for res in d["results"] if res["name"].endswith(".jpg"))
        assert jpg_result["success"] is True

        import app.main as m
        ws = m.SESSIONS[client.cookies.get("imgopt_session")].workspace
        out_file = ws / "output" / "photo.jpg"
        assert out_file.exists()
        with Image.open(out_file) as img:
            assert img.format == "JPEG"


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


def _session(client):
    import app.main as m
    return m.SESSIONS[client.cookies.get("imgopt_session")]


class TestProgressIncrementalCursor:
    """/api/progress supports an incremental cursor so a long batch doesn't
    re-ship the whole results array every poll. since_result slices the tail;
    result_total reports the full count. Omitting the params keeps the
    original full-payload behavior every existing caller/test relies on."""

    def test_since_result_returns_only_the_tail(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers)
        assert r.status_code == 200 and len(d["results"]) == 3

        tail = client.get("/api/progress?since_result=2", headers=auth_headers).json()
        assert len(tail["results"]) == 1
        assert tail["result_total"] == 3
        assert tail["log_total"] >= 1

    def test_omitting_cursor_still_returns_full_set(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        optimize_and_wait(client, auth_headers)
        full = client.get("/api/progress", headers=auth_headers).json()
        assert len(full["results"]) == 3
        assert full["result_total"] == 3


class TestZipDownloadCache:
    """The download ZIP is rebuilt only when outputs changed since it was last
    built (output_version bumps once per completed run). Repeated downloads of
    an unchanged batch serve the cached archive without re-globbing/re-zipping;
    a fresh run invalidates it."""

    def test_zip_cached_until_next_run(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        optimize_and_wait(client, auth_headers)
        st = _session(client)
        ws_name = st.workspace.name
        zip_path = st.workspace / "optimized.zip"

        r1 = client.get(f"/api/download/{ws_name}")
        assert r1.status_code == 200
        assert st.zip_built_version == st.output_version
        mtime1 = zip_path.stat().st_mtime_ns

        # No new run -> cache hit -> the archive file is left untouched.
        r2 = client.get(f"/api/download/{ws_name}")
        assert r2.status_code == 200
        assert zip_path.stat().st_mtime_ns == mtime1

        # A fresh run bumps output_version, so the next download rebuilds.
        prev_version = st.output_version
        optimize_and_wait(client, auth_headers)
        assert st.output_version == prev_version + 1
        r3 = client.get(f"/api/download/{ws_name}")
        assert r3.status_code == 200
        assert st.zip_built_version == st.output_version


class TestSkipExisting:
    """skip_existing reuses an already-optimized file in the output folder
    instead of recompressing it, while still copying it back into ws/output
    so Compare/preview and the download ZIP keep working."""

    def test_skip_reuses_existing_outputs_without_recompressing(self, client, auth_headers, test_images, tmp_path):
        out_dir = tmp_path / "out"
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200 and all(res["success"] for res in d["results"])
        produced = [p for p in out_dir.rglob("*") if p.is_file()]
        assert produced, "first run should populate the output folder"
        mtimes = {p: p.stat().st_mtime_ns for p in produced}

        # Re-scan the same source (new workspace) and re-run with skip_existing.
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir), skip_existing=True)
        assert r.status_code == 200
        assert all(res.get("skipped") for res in d["results"])
        assert all(res["success"] for res in d["results"])

        # Destination files were reused, not rewritten (mtimes unchanged).
        for p, mt in mtimes.items():
            assert p.stat().st_mtime_ns == mt, f"{p} was recompressed on a skip run"
        # ws/output was repopulated so Compare/ZIP still work.
        ws_output = _session(client).workspace / "output"
        assert len([p for p in ws_output.rglob("*") if p.is_file()]) == len(produced)

    def test_skip_existing_without_output_dir_is_noop(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, skip_existing=True)
        assert r.status_code == 200
        # No persistent output folder -> nothing to reuse -> normal compression.
        assert not any(res.get("skipped") for res in d["results"])

    def test_skip_existing_recompresses_when_source_changed(self, client, auth_headers, tmp_path):
        """Regression: same filename, different content (e.g. a screenshot
        re-taken under the same name) must NOT be silently reused just
        because the output folder already has a file at that path."""
        from PIL import Image

        src = tmp_path / "src"
        src.mkdir()
        out_dir = tmp_path / "out"
        img_path = src / "a.png"
        Image.new("RGB", (10, 10), (255, 0, 0)).save(img_path)

        scan_and_wait(client, auth_headers, src)
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200 and not d["results"][0].get("skipped")
        out_file = out_dir / "a.png"

        # Re-save under the same name with different content, mtime
        # strictly newer (some filesystems have 1s mtime granularity).
        import time
        time.sleep(1.1)
        Image.new("RGB", (10, 10), (0, 255, 0)).save(img_path)

        scan_and_wait(client, auth_headers, src)
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir), skip_existing=True)
        assert r.status_code == 200
        assert not d["results"][0].get("skipped"), "changed source was wrongly reused instead of recompressed"

        with Image.open(out_file) as final:
            assert final.convert("RGB").getpixel((0, 0)) == (0, 255, 0), (
                "output still holds the stale pre-change pixel data"
            )

    def test_skip_existing_recompresses_when_settings_changed(self, client, auth_headers, test_images, tmp_path):
        """Regression test: skip_existing used to key its reuse decision
        purely on the *source*'s mtime vs the existing output's mtime —
        confirmed empirically that changing max_width between runs with
        an untouched source silently kept the old, un-resized output,
        with no error or warning telling the user their new setting had
        no effect. Any settings change (quality/compression_mode/
        max_width/output_format/protected_colors/dithering/keep_exif)
        must now trigger a real recompress instead."""
        out_dir = tmp_path / "out"
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200 and all(res["success"] for res in d["results"])

        first_file = next(f for f in out_dir.iterdir() if f.is_file())
        from PIL import Image
        dims_before = Image.open(first_file).size

        # Same untouched source, skip_existing=True, but this run asks
        # for a resize the previous run never applied.
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(
            client, auth_headers, output_dir=str(out_dir),
            skip_existing=True, max_width=50,
        )
        assert r.status_code == 200
        assert not any(res.get("skipped") for res in d["results"]), (
            "changed max_width was ignored — stale un-resized output was reused"
        )
        dims_after = Image.open(first_file).size
        assert dims_after[0] <= 50, f"resize wasn't applied: {dims_before} -> {dims_after}"

    def test_skip_existing_recompresses_when_no_fingerprint_recorded(
        self, client, auth_headers, test_images, tmp_path
    ):
        """A file already sitting in output_dir with no recorded settings
        fingerprint (e.g. it predates this feature, or was placed there
        by something other than this app) is "unverifiable" — safe
        default is to recompress rather than trust it blindly, same
        conservative direction the mtime-only check already fails in for
        the upload flow."""
        from PIL import Image

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        scanned = scan_and_wait(client, auth_headers, test_images)
        first_name = scanned["files"][0]["name"]
        # Pre-seed output_dir directly (bypassing the app entirely — no
        # fingerprint gets recorded this way) with a *newer* mtime than
        # the source, so the old mtime-only check alone would have
        # reused this.
        seed_path = out_dir / first_name
        seed_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (5, 5), (1, 2, 3)).save(seed_path)
        import time
        time.sleep(1.1)
        seed_path.touch()

        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir), skip_existing=True)
        assert r.status_code == 200
        first_result = next(res for res in d["results"] if res["name"] == first_name)
        assert not first_result.get("skipped"), (
            "a pre-existing file with no recorded fingerprint should be "
            "recompressed, not trusted blindly"
        )

    def test_normal_subset_run_with_skip_existing_wipes_stale_outputs(self, client, auth_headers, test_images, tmp_path):
        """Regression: skip_existing must NOT suppress the ws/output wipe on a
        normal (non-retry) run. The wipe is per-batch and only retry should
        preserve prior outputs; skip_existing is a per-file reuse decision and
        its reuse path copies from output_dir back into ws/output, so it never
        needs ws/output to survive. Coupling the wipe to it left a deselected
        file's earlier output on disk, and the whole-dir ZIP build then leaked
        that stale file into the download even though it wasn't in this run's
        results or selection."""
        import io
        from zipfile import ZipFile

        out_dir = tmp_path / "out"
        scanned = scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200 and len(d["results"]) == 3

        st = _session(client)
        ws_name = st.workspace.name
        ws_output = st.workspace / "output"
        assert len([p for p in ws_output.rglob("*") if p.is_file()]) == 3

        # Same workspace, no re-scan: run only ONE file with skip_existing on,
        # as a normal Start (retry defaults False), deselecting the other two.
        keep_id = scanned["files"][0]["id"]
        keep_name = next(f["name"] for f in scanned["files"] if f["id"] == keep_id).replace("\\", "/")
        r, d = optimize_and_wait(client, auth_headers, file_ids=[keep_id], skip_existing=True)
        assert r.status_code == 200
        assert [res["id"] for res in d["results"]] == [keep_id]

        # ws/output now holds only this run's file; the two deselected files'
        # stale outputs were wiped.
        ws_files = {str(p.relative_to(ws_output)).replace("\\", "/")
                    for p in ws_output.rglob("*") if p.is_file()}
        assert ws_files == {keep_name}

        # And the download ZIP contains exactly that one file, no stale leak.
        zr = client.get(f"/api/download/{ws_name}")
        assert zr.status_code == 200
        names = {n.replace("\\", "/") for n in ZipFile(io.BytesIO(zr.content)).namelist()}
        assert names == {keep_name}