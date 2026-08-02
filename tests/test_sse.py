"""Tests for the SSE progress transport (optimization item #5).

`GET /api/events` streams `result` / `log` / `done` events with ids encoding
the cumulative (result, log) cursor — `r{R}:l{L}` — so a client can reattach
after a refresh by sending `Last-Event-ID` and replaying only the tail. The
existing HTTP polling path (/api/progress) stays as the automatic fallback
and must keep working unchanged.

These tests pin the replay/reattach semantics (the review's core ask) and
that polling and SSE agree on the result set.
"""
from __future__ import annotations

import json

from .conftest import wait_for


def scan_and_wait(client, headers, directory, recursive=True):
    client.post("/api/scan", json={"directory": str(directory), "recursive": recursive}, headers=headers)
    return wait_for(lambda: (lambda d: not d["running"] and d)(
        client.get("/api/scan-progress", headers=headers).json()
    ))


def parse_sse(text: str):
    """Parse an SSE text body into a list of {id, event, data} dicts."""
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        ev = {"id": None, "event": None, "data": None}
        for line in block.split("\n"):
            if line.startswith("id:"):
                ev["id"] = line[3:].strip()
            elif line.startswith("event:"):
                ev["event"] = line[6:].strip()
            elif line.startswith("data:"):
                ev["data"] = json.loads(line[5:].strip())
        events.append(ev)
    return events


class TestSseReplay:
    def test_full_replay_when_no_last_event_id(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        client.post("/api/optimize", json={}, headers=auth_headers)
        wait_for(lambda: not client.get("/api/progress", headers=auth_headers).json()["running"])

        r = client.get("/api/events", headers=auth_headers)
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        events = parse_sse(r.text)
        result_events = [e for e in events if e["event"] == "result"]
        log_events = [e for e in events if e["event"] == "log"]
        done = [e for e in events if e["event"] == "done"]
        assert len(result_events) == 3
        assert log_events, "expected at least one log event"
        assert len(done) == 1
        assert done[0]["data"]["result_total"] == 3
        # Every event carries an id encoding the cursor state.
        assert all(e["id"] for e in events if e["event"] is not None)

    def test_reattach_via_last_event_id_replays_only_tail(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        client.post("/api/optimize", json={}, headers=auth_headers)
        wait_for(lambda: not client.get("/api/progress", headers=auth_headers).json()["running"])

        # Pretend the client already saw the first result (r1:l0): only
        # results[1:] and all logs should be replayed.
        r = client.get("/api/events", headers={**auth_headers, "Last-Event-ID": "r1:l0"})
        events = parse_sse(r.text)
        result_events = [e for e in events if e["event"] == "result"]
        done = [e for e in events if e["event"] == "done"]
        assert len(result_events) == 2, "reattach should replay only results[1:]"
        # The first replayed result's id reflects r=2 (one more after r1).
        assert result_events[0]["id"].startswith("r2:")
        assert done and done[-1]["data"]["result_total"] == 3

    def test_reattach_at_end_emits_only_done(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        client.post("/api/optimize", json={}, headers=auth_headers)
        prog = wait_for(lambda: (lambda p: not p["running"] and p)(
            client.get("/api/progress", headers=auth_headers).json()
        ))
        # Client already saw everything (r{all}:l{all}): nothing to replay
        # but a done event so the client knows the run is final.
        r_total = prog["result_total"]
        l_total = prog["log_total"]
        r = client.get("/api/events", headers={**auth_headers, "Last-Event-ID": f"r{r_total}:l{l_total}"})
        events = parse_sse(r.text)
        assert not [e for e in events if e["event"] in ("result", "log")]
        assert [e for e in events if e["event"] == "done"]

    def test_no_run_emits_done_immediately(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        # No optimize started: is_running is False, no results.
        r = client.get("/api/events", headers=auth_headers)
        events = parse_sse(r.text)
        assert not [e for e in events if e["event"] in ("result", "log")]
        assert [e for e in events if e["event"] == "done"]
        assert events[-1]["data"]["result_total"] == 0


class TestSseAndPollingAgree:
    """SSE and the polling fallback read the same state.results, so they
    must report the same result set. (The fallback path itself is
    regression-tested by the existing /api/progress tests.)"""

    def test_sse_result_count_matches_progress(self, client, auth_headers, test_images):
        scan_and_wait(client, auth_headers, test_images)
        client.post("/api/optimize", json={}, headers=auth_headers)
        wait_for(lambda: not client.get("/api/progress", headers=auth_headers).json()["running"])

        prog = client.get("/api/progress", headers=auth_headers).json()
        sse = parse_sse(client.get("/api/events", headers=auth_headers).text)
        sse_results = [e for e in sse if e["event"] == "result"]
        assert len(sse_results) == prog["result_total"] == len(prog["results"]) == 3


class TestSseLiveStream:
    def test_stream_opened_during_run_delivers_events_then_done(
        self, client, auth_headers, tmp_path, fake_bin_dir, monkeypatch
    ):
        """Open the SSE stream while a slow run is in progress: it must stay
        open, deliver result/log events as they happen, and close with a done
        event when the run finishes (not hang)."""
        import asyncio
        import shutil
        import app.main as m
        from app.optimizer import Optimizer
        from PIL import Image

        d = tmp_path / "imgs"
        d.mkdir()
        for i in range(3):
            Image.new("RGB", (32, 32), (i * 50, 100, 150)).save(d / f"img{i}.png")
        scan_and_wait(client, auth_headers, d)

        opt = Optimizer(bin_dir=fake_bin_dir)

        async def slow(input_path, output_path, **kwargs):
            await asyncio.sleep(0.2)
            shutil.copy2(input_path, output_path)
            return {"success": True, "original_size": 10, "compressed_size": 5,
                    "error": None, "warning": None}

        opt.optimize_png = slow
        m.optimizer = opt

        # Start the run, then immediately open the stream.
        client.post("/api/optimize", json={}, headers=auth_headers)
        with client.stream("GET", "/api/events", headers=auth_headers) as r:
            body = b"".join(r.iter_bytes()).decode()
        events = parse_sse(body)
        assert len([e for e in events if e["event"] == "result"]) == 3
        assert [e for e in events if e["event"] == "done"]
        # Stream ended (iter_bytes returned) — didn't hang.
