"""Folder picker on macOS: osascript instead of Tk on a worker thread (R2).

Tk must run on the main thread on macOS; the old handler called it from a
uvicorn worker thread, where it aborts the process instead of returning a
path. Release builds ship a macOS dmg, so that path was reachable. The
native panel is opened through osascript, which has no thread affinity.
"""
from __future__ import annotations

import subprocess
import sys

import pytest


def test_panel_path_is_returned_without_the_trailing_slash(app_module, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "/Users/eliot/Photos/\n", "")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    path, error = app_module._macos_folder_dialog("Select Folder")
    assert (path, error) == ("/Users/eliot/Photos", "")
    assert captured["argv"][0].endswith("osascript")
    assert "choose folder" in captured["argv"][2]


def test_root_folder_survives_stripping(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "/\n", ""),
    )
    assert app_module._macos_folder_dialog("x") == ("/", "")


def test_user_cancel_is_not_an_error(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "execution error: User canceled. (-128)"),
    )
    assert app_module._macos_folder_dialog("x") == ("", "")


def test_osascript_failure_is_reported(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "execution error: boom (-1728)"),
    )
    path, error = app_module._macos_folder_dialog("x")
    assert path == ""
    assert "boom" in error


def test_missing_osascript_is_reported(app_module, monkeypatch):
    def boom(argv, **kwargs):
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(app_module.subprocess, "run", boom)
    path, error = app_module._macos_folder_dialog("x")
    assert path == ""
    assert "osascript" in error


def test_title_cannot_break_out_of_the_applescript_string(app_module, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["script"] = argv[2]
        return subprocess.CompletedProcess(argv, 0, "/tmp/\n", "")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    app_module._macos_folder_dialog('Pick "this" \\ that')
    script = captured["script"]
    assert script.count('"') % 2 == 0
    assert '\\"this\\"' in script
    assert "\\\\" in script


@pytest.mark.skipif(sys.platform != "darwin", reason="the Tk path is the non-macOS one")
def test_endpoint_delegates_to_osascript(client, auth_headers, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_macos_folder_dialog", lambda title: ("/Users/x/Pics", ""))
    body = client.get("/api/browse-folder", headers=auth_headers).json()
    assert body == {"path": "/Users/x/Pics"}
