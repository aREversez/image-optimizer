"""Unit tests for the _run_worker_pool helper itself.

This helper was extracted from the two worker pools that used to be
duplicated in `_scan_and_thumbnail` and `_process_files`. The extraction
only pays off if the helper's contract (concurrency, error isolation,
cancel vs. on_item_done interactions, and `n_workers` resolution) stays
locked down — these tests deliberately exercise the helper in isolation,
with no FastAPI app, no Optimizer, no temp files, and no session state:
each test passes toy `process_item` / `on_item_error` / `on_item_done`
callbacks that record what the helper actually did, then asserts on
those records. A regression in the helper (e.g. starting to call
on_item_done on a cancelled item, or forgetting to call on_item_error on
an exception) would show up here first, before anyone could re-introduce
the orphaned-worker / lost-result bugs the helper exists to prevent.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def call_helper(items, process_item, **kwargs):
    """Run the helper to completion. Wrapped so tests stay one-liners."""
    from app.main import _run_worker_pool
    await _run_worker_pool(items, process_item, **kwargs)


class TestProcessOnce:
    """Core guarantee: every non-cancelled item reaches process_item
    exactly once, no matter how many workers run concurrently or how the
    queue is drained."""

    def test_all_items_processed_exactly_once(self):
        seen = []
        events = []

        async def p(item):
            events.append(("proc", item))

        items = list(range(20))
        asyncio.run(call_helper(items, p, on_item_done=lambda i: seen.append(i)))

        # on_item_done is called once per processed item, so `seen` is
        # the set of items that actually got processed.
        assert sorted(seen) == items
        # Each item is processed exactly once (process_item received each
        # item exactly once — duplicates would mean a worker pulled an item
        # out and it somehow showed up twice).
        processed = [e[1] for e in events if e[0] == "proc"]
        assert sorted(processed) == items
        assert len(processed) == len(items)

    def test_empty_item_list_is_noop(self):
        called = []

        async def p(item):
            called.append(item)

        asyncio.run(call_helper([], p))
        assert called == []


class TestRealConcurrency:
    """A regression to serial processing is the single fastest way to
    undo the entire point of the helper (the original scan flow went from
    blocking-for-the-total-duration to actually concurrent). If the
    highest-observed concurrency drops back to 1, that's a regression.

    Measured by tracking how many workers are simultaneously inside
    `process_item` at any one instant.
    """

    def test_items_run_in_parallel_up_to_n_workers(self):
        observed_peak = {"n": 0}
        in_flight = {"n": 0}

        async def slow(_item):
            in_flight["n"] += 1
            observed_peak["n"] = max(observed_peak["n"], in_flight["n"])
            await asyncio.sleep(0.05)
            in_flight["n"] -= 1

        # 8 items, 4 workers → peak in-flight should reach 4 if they're
        # really concurrent. Anything strictly less than 4 here means
        # workers are being serialized somehow (e.g. by accident awaiting
        # sequentially within one worker before pulling the next item).
        items = list(range(8))
        asyncio.run(call_helper(items, slow, n_workers=4))
        assert observed_peak["n"] == 4, (
            f"expected peak concurrency == 4, got {observed_peak['n']} — "
            "workers may be serialized"
        )

    def test_n_workers_one_runs_sequentially_and_still_completes(self):
        seen = []

        async def p(item):
            seen.append(item)

        items = list(range(5))
        asyncio.run(call_helper(items, p, n_workers=1))
        assert sorted(seen) == items

    def test_zero_or_negative_n_workers_clamped_to_one(self):
        """max(1, n_workers) guard inside the helper — 0 workers would
        mean no tasks ever started draining the queue, so the helper
        would hang forever."""
        seen = []

        async def p(item):
            seen.append(item)

        items = list(range(5))
        # Run on its own event loop with a watchdog — if the helper hangs
        # (because N=0 spawns no workers), this test fails on timeout
        # rather than hanging the whole pytest process.
        asyncio.run(asyncio.wait_for(call_helper(items, p, n_workers=0), timeout=5))
        assert sorted(seen) == items

    def test_n_workers_none_uses_global_default(self, monkeypatch):
        """The default-argument trap the docstring warns about: passing
        `n_workers=None` must read the module global `CONCURRENT_WORKERS`
        at call time, not freeze the value at module import. This test
        sets the global to something distinctive (7) right before calling
        and verifies the helper actually picked it up."""
        import app.main as m
        monkeypatch.setattr(m, "CONCURRENT_WORKERS", 7)
        observed_peak = {"n": 0}
        in_flight = {"n": 0}

        async def slow(_item):
            in_flight["n"] += 1
            observed_peak["n"] = max(observed_peak["n"], in_flight["n"])
            await asyncio.sleep(0.05)
            in_flight["n"] -= 1

        asyncio.run(call_helper(list(range(14)), slow))
        assert observed_peak["n"] == 7


class TestErrorIsolation:
    """An unhandled exception from one item must not (a) propagate out
    of the helper, (b) stop the other items from running, (c) go
    silently unrecorded if `on_item_error` was provided."""

    def test_exception_does_not_propagate_out(self):
        async def p(item):
            if item == 3:
                raise ValueError("boom")
            await asyncio.sleep(0)

        # No on_item_error provided — the helper still must not raise;
        # it just swallows the exception and moves on.
        asyncio.run(call_helper(list(range(10)), p))

    def test_exception_routes_to_on_item_error_and_does_not_stop_others(self):
        errors = []
        processed = []

        async def p(item):
            if item == 3:
                raise ValueError("boom on 3")
            processed.append(item)

        def on_err(item, e):
            errors.append((item, type(e).__name__, str(e)))

        asyncio.run(call_helper(list(range(10)), p, on_item_error=on_err))
        # The raising item never reached the bottom of process_item, so
        # it shouldn't appear in `processed`.
        assert 3 not in processed
        # All other items still got processed.
        assert sorted(processed) == [i for i in range(10) if i != 3]
        # The exception was routed, with the right item, type, and message.
        assert errors == [(3, "ValueError", "boom on 3")]

    def test_multiple_exceptions_each_isolated(self):
        seen = []

        async def p(item):
            if item % 2 == 0:
                raise RuntimeError(f"even {item}")

        def on_err(item, e):
            seen.append((item, str(e)))

        asyncio.run(call_helper(list(range(8)), p, on_item_error=on_err,
                                on_item_done=lambda _i: None))
        assert sorted(seen) == [(i, f"even {i}") for i in range(0, 8, 2)]


class TestOnItemDoneSemantics:
    """The on_item_done callback is the progress-counter hook. Its
    call discipline is what the cancel signal hinges on — see the
    cancel tests below."""

    def test_called_once_per_processed_item_success(self):
        done = []

        async def p(item):
            pass

        asyncio.run(call_helper(list(range(7)), p, on_item_done=done.append))
        assert sorted(done) == list(range(7))
        assert len(done) == 7  # exactly once — not zero times, not twice

    def test_called_after_per_item_exception_too(self):
        """A failing item still counts as 'done' for progress purposes:
        it was attempted, the failure was handled, the item is no longer
        in the queue. The old optimize flow's `except` branch did
        `state.current += 1` for this reason — bumping the counter so
        the progress bar moved even when a file failed outright, rather
        than sticking on file 3/N forever while the queue kept
        draining behind the scenes."""
        done = []

        async def p(item):
            if item == 2:
                raise ValueError("boom")

        asyncio.run(call_helper(list(range(5)), p,
                                on_item_error=lambda i, e: None,
                                on_item_done=done.append))
        assert sorted(done) == list(range(5))

    def test_on_done_receives_the_item_not_a_something_else(self):
        received = []

        async def p(item):
            pass

        asyncio.run(call_helper(["a", "b", "c"], p, on_item_done=received.append))
        assert sorted(received) == ["a", "b", "c"]


class TestCancel:
    """`cancel_check` returning True tells the helper: stop starting new
    items, but let everything already in flight finish naturally. The
    fine print that's easy to get wrong: a cancelled-but-not-started item
    is NOT counted via on_item_done, because the original optimize flow
    distinguished 'actually processed' from 'skipped' that way (and
    /api/progress's `current < total` after a cancel is precisely the
    'this run was partial' signal — erasing it would make the user think
    everything ran)."""

    def test_cancel_before_start_processes_nothing_and_on_item_done_not_called(self):
        processed = []
        done = []

        async def p(item):
            processed.append(item)

        asyncio.run(call_helper(
            list(range(10)), p,
            cancel_check=lambda: True,           # cancelled from the very start
            on_item_done=done.append,
        ))
        assert processed == []
        assert done == []  # cancelled items do NOT bump the counter.

    def test_cancel_mid_run_finishes_in_flight_skips_remaining(self):
        """Start with N items in flight across 4 workers; flip cancel
        after the first batch starts. Items already running must
        finish; items not yet pulled must not start and must not count
        toward on_item_done."""
        processed = []
        done = []
        cancel_after = 3
        counter = {"started": 0}

        async def p(item):
            # Each item sleeps a tiny bit so the cancel flip can land
            # while a worker is mid-`process_item` (i.e. 'in flight'),
            # rather than the helper draining the whole queue on the
            # main thread before the cancel_check ever gets re-evaluated.
            counter["started"] += 1
            await asyncio.sleep(0.05)
            processed.append(item)

        def cancel_check():
            return counter["started"] >= cancel_after

        asyncio.run(call_helper(
            list(range(20)), p,
            n_workers=4,
            cancel_check=cancel_check,
            on_item_done=done.append,
        ))
        # Items that were actually processed are a subset of all items.
        assert len(processed) < 20, "cancel should have stopped some items"
        # Every processed item also got counted (success path), but no
        # more than what was processed — the cancelled ones did NOT bump
        # the counter.
        assert set(done) == set(processed)
        assert len(done) == len(processed)

    def test_cancel_check_false_some_items_done_full_run(self):
        """The cancel_check returning False for the whole run is the
        no-cancel case — every item gets processed and counted."""
        processed = []
        done = []

        async def p(item):
            processed.append(item)

        asyncio.run(call_helper(
            list(range(5)), p,
            cancel_check=lambda: False,           # checked, but never cancels
            on_item_done=done.append,
        ))
        assert sorted(processed) == list(range(5))
        assert sorted(done) == list(range(5))


class TestCancelSkipsDoNotOrphanWorkers:
    """The original bug the helper prevents: an unhandled exception in one
    worker used to make asyncio.gather return immediately while the
    other workers kept running orphaned (their results silently lost).
    This is also why a cancelled run still has to fully drain the queue
    — workers must all exit cleanly via QueueEmpty, not get blocked or
    abandoned."""

    def test_after_cancel_all_workers_exit_within_a_time_bound(self):
        """If cancelled items don't continue draining the queue (e.g. if
        a refactor made the cancelled branch `return` instead of
        `continue`), one worker hits the cancel early and exits, leaving
        the work to the other N-1 — but a queue that's never drained
        sits forever. Watchdog the whole helper with asyncio.wait_for
        to turn that into a test failure instead of a hang."""
        async def p(_):
            await asyncio.sleep(0.01)

        async def run():
            await asyncio.wait_for(
                call_helper(list(range(50)), p,
                            cancel_check=lambda: True,
                            on_item_done=lambda _: None),
                timeout=2.0,
            )

        # Should complete far under the 2s watchdog — every worker just
        # reads QueueEmpty after the cancel skip and exits. If this ever
        # hits the timeout, a cancel branch is leaving items in the
        # queue no one will ever pull.
        t0 = time.time()
        asyncio.run(run())
        assert time.time() - t0 < 2.0
