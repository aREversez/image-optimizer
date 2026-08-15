"""Regression tests for per-file parameter override (optimization item #3).

`OptimizeRequest.overrides` is `{file_id: {field: value, ...}}` — only the
listed fields override the top-level defaults for that one file; everything
else falls back. The schema is fixed (no agent free-design) and the same
validation the top-level fields get applies to each override entry.

The cross-cutting test pins the interaction the review flags: overrides on
a subset + skip_existing + a narrowed file_ids selection — skipped files
are reused (override ignored, no recompress), applied files use their
effective params, and the ZIP equals exactly the selected files.
"""
from __future__ import annotations

import shutil
from io import BytesIO
from zipfile import ZipFile

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


def _session(client):
    import app.main as m
    return m.SESSIONS[client.cookies.get("imgopt_session")]


class TestOverridesApplied:
    """A recording optimizer captures the effective parameter set each file
    was actually compressed with, proving overrides flow through to
    optimizer.optimize_png on a per-file basis (not just accepted by the
    API and dropped)."""

    def test_each_file_gets_its_effective_params(self, client, auth_headers, test_images, fake_bin_dir):
        import app.main as m
        from app.optimizer import Optimizer

        scanned = scan_and_wait(client, auth_headers, test_images)
        path_to_id = {f["path"]: f["id"] for f in scanned["files"]}

        recorded: dict[str, dict] = {}

        opt = Optimizer(bin_dir=fake_bin_dir)

        async def rec(input_path, output_path, **kwargs):
            recorded[path_to_id[str(input_path)]] = dict(kwargs)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5,
                    "error": None, "warning": None}

        opt.optimize_png = rec
        m.optimizer = opt
        try:
            r, d = optimize_and_wait(
                client, auth_headers,
                file_ids=["0", "1", "2"],
                overrides={
                    "1": {"quality": "low", "compression_mode": "lossless"},
                    "2": {"max_width": 50, "keep_exif": True},
                },
            )
            assert r.status_code == 200, r.json()
            assert len(d["results"]) == 3

            # File 0: pure defaults.
            assert recorded["0"]["quality"] == "medium"
            assert recorded["0"]["compression_mode"] == "standard"
            assert recorded["0"]["max_width"] == 0
            assert recorded["0"]["keep_exif"] is False
            # File 1: quality + mode overridden, rest default.
            assert recorded["1"]["quality"] == "low"
            assert recorded["1"]["compression_mode"] == "lossless"
            assert recorded["1"]["max_width"] == 0  # not overridden → default
            # File 2: max_width + keep_exif overridden.
            assert recorded["2"]["max_width"] == 50
            assert recorded["2"]["keep_exif"] is True
            assert recorded["2"]["quality"] == "medium"  # not overridden → default
        finally:
            # Restore the fixture's optimizer so later tests aren't affected.
            m.optimizer = opt  # fixtures re-bind per test anyway, but be safe

    def test_override_only_affects_listed_files(self, client, auth_headers, test_images, fake_bin_dir):
        import app.main as m
        from app.optimizer import Optimizer

        scanned = scan_and_wait(client, auth_headers, test_images)
        path_to_id = {f["path"]: f["id"] for f in scanned["files"]}
        recorded: dict[str, dict] = {}
        opt = Optimizer(bin_dir=fake_bin_dir)

        async def rec(input_path, output_path, **kwargs):
            recorded[path_to_id[str(input_path)]] = dict(kwargs)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5,
                    "error": None, "warning": None}

        opt.optimize_png = rec
        m.optimizer = opt
        r, d = optimize_and_wait(
            client, auth_headers,
            overrides={"0": {"dithering": False}},
        )
        assert r.status_code == 200
        assert recorded["0"]["dithering"] is False
        # Files 1 and 2 keep the default dithering=True.
        assert recorded["1"]["dithering"] is True
        assert recorded["2"]["dithering"] is True


class TestOverridesValidation:
    def test_invalid_override_quality_rejected(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, overrides={"0": {"quality": "ultra"}})
        assert r.status_code == 400
        assert "0" in r.json()["error"]

    def test_invalid_override_color_rejected(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, overrides={"0": {"protected_colors": ["nope"]}})
        assert r.status_code == 400

    def test_unknown_override_field_rejected(self, client, auth_headers, test_images):
        """A typo'd field name (e.g. 'qualitiy') must 400, not be silently
        ignored — otherwise the user's per-file setting silently does
        nothing."""
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, overrides={"0": {"qualitiy": "low"}})
        assert r.status_code == 400

    def test_override_for_unselected_id_is_ignored_not_error(self, client, auth_headers, test_images):
        """An override entry for a file_id that isn't in the selection is
        silently ignored (not a 400) — the UI may send overrides for files
        the user configured then deselected; rejecting would be annoying."""
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(
            client, auth_headers,
            file_ids=["0"],
            overrides={"99": {"quality": "low"}},  # 99 not selected
        )
        assert r.status_code == 200
        assert len(d["results"]) == 1

    def test_resize_only_override_requires_max_width(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(client, auth_headers, overrides={"0": {"compression_mode": "resize_only"}})
        assert r.status_code == 400

    def test_screenshot_override_requires_png_output(self, client, auth_headers, test_images):
        """A per-file override switching one file to screenshot mode while
        the top-level output_format is webp must be rejected the same way
        the top-level combination is — screenshot mode is PNG-only."""
        scan_and_wait(client, auth_headers, test_images)
        r, _ = optimize_and_wait(
            client, auth_headers,
            output_format="webp",
            overrides={"0": {"compression_mode": "screenshot"}},
        )
        assert r.status_code == 400
        assert "PNG-only" in r.json()["error"]

    def test_screenshot_override_with_matching_png_format_succeeds(self, client, auth_headers, test_images):
        """The reverse: an override to screenshot mode is fine as long as
        the *effective* output_format for that file is png — whether from
        the top-level setting or from the same override."""
        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(
            client, auth_headers,
            output_format="png",
            overrides={"0": {"compression_mode": "screenshot"}},
        )
        assert r.status_code == 200
        assert all(res["success"] for res in d["results"])


class TestOverridesWithSkipExisting:
    """The cross-cutting case: overrides + skip_existing + a narrowed
    file_ids selection. A skipped file needs BOTH an unchanged source AND
    matching effective settings (including any override) to be reused —
    an override that changes the effective settings for that specific
    file must trigger a genuine recompress with the override applied,
    not a silent skip of stale output. Applied/recompressed files use
    their effective params, and the ZIP equals exactly the selected
    files (no stale leak, no double entry)."""

    def test_skip_reuses_when_override_matches_existing_settings(
        self, client, auth_headers, test_images, tmp_path
    ):
        out_dir = tmp_path / "out"
        scanned = scan_and_wait(client, auth_headers, test_images)
        # First run populates output_dir with all 3 files at the default
        # quality ("medium").
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200 and len(d["results"]) == 3

        # Re-scan (fresh workspace), then run a NARROWED selection of 2
        # files with skip_existing + a same-value override on one of
        # them (quality: "medium" is already the effective default, so
        # this override doesn't actually change anything).
        scan_and_wait(client, auth_headers, test_images)
        keep_ids = [scanned["files"][0]["id"], scanned["files"][1]["id"]]
        keep_names = {
            next(f for f in scanned["files"] if f["id"] == keep_ids[0])["name"].replace("\\", "/"),
            next(f for f in scanned["files"] if f["id"] == keep_ids[1])["name"].replace("\\", "/"),
        }
        r, d = optimize_and_wait(
            client, auth_headers,
            file_ids=keep_ids,
            output_dir=str(out_dir),
            skip_existing=True,
            overrides={keep_ids[0]: {"quality": "medium"}},  # matches run 1's effective quality
        )
        assert r.status_code == 200
        assert len(d["results"]) == 2
        # Both were reused (already in out_dir, and the override didn't
        # actually change the effective settings for either file).
        assert all(res.get("skipped") for res in d["results"])
        assert {res["id"] for res in d["results"]} == set(keep_ids)

        # ZIP contains exactly the 2 selected files — no stale third file
        # leaks in (the skip_existing × subset × wipe regression guard).
        st = _session(client)
        zr = client.get(f"/api/download/{st.workspace.name}")
        assert zr.status_code == 200
        names = {n.replace("\\", "/") for n in ZipFile(BytesIO(zr.content)).namelist()}
        expected = {n.rsplit(".", 1)[0] + ".png" for n in keep_names}
        assert names == expected, (names, expected)

    def test_skip_recompresses_when_override_changes_effective_settings(
        self, client, auth_headers, test_images, tmp_path
    ):
        """Regression test: skip_existing used to key its reuse decision
        purely on the source file's mtime, so a per-file override that
        genuinely changes what should be produced (e.g. quality: "low"
        instead of the original run's "medium") was silently dropped —
        the stale medium-quality output got reused and the override had
        no effect at all. It must now be honored: mismatched effective
        settings mean a real recompress, not a silent skip."""
        out_dir = tmp_path / "out"
        scanned = scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(client, auth_headers, output_dir=str(out_dir))
        assert r.status_code == 200 and len(d["results"]) == 3
        target_id = scanned["files"][0]["id"]
        target_name = next(f for f in scanned["files"] if f["id"] == target_id)["name"]
        stale_mtime_ns = (out_dir / target_name).stat().st_mtime_ns

        import time
        time.sleep(0.05)  # ensure a genuinely later mtime if recompressed

        scan_and_wait(client, auth_headers, test_images)
        r, d = optimize_and_wait(
            client, auth_headers,
            file_ids=[target_id],
            output_dir=str(out_dir),
            skip_existing=True,
            overrides={target_id: {"quality": "low"}},  # differs from run 1's "medium"
        )
        assert r.status_code == 200
        assert len(d["results"]) == 1
        assert d["results"][0].get("skipped") is not True, (
            "an override that changes effective settings must trigger a "
            "recompress, not a silent reuse of stale output"
        )
        assert d["results"][0]["success"] is True
        # The file was actually rewritten (fresh mtime), not left as the
        # stale run-1 copy — the fake pngquant test double copies bytes
        # through unchanged regardless of quality, so mtime is what proves
        # a real write happened here rather than a skip.
        new_mtime_ns = (out_dir / target_name).stat().st_mtime_ns
        assert new_mtime_ns > stale_mtime_ns, (
            "output file's mtime didn't change — looks like it was reused, "
            "not recompressed with the override applied"
        )
