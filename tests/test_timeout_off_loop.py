"""The encoder-timeout header read must not block the event loop."""
from __future__ import annotations

import asyncio
import threading

from PIL import Image

from image_optimizer.optimizer import Optimizer


def test_timeout_header_read_runs_off_the_event_loop(optimizer, tmp_path, monkeypatch):
    src = tmp_path / "a.png"
    Image.new("RGB", (32, 32), (1, 2, 3)).save(src)
    threads = []
    real = Optimizer._timeout_for

    def spy(self, p):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(self, p)

    monkeypatch.setattr(Optimizer, "_timeout_for", spy)
    res = asyncio.run(optimizer.optimize_image(src, tmp_path / "o.png", quality="medium",
                                               output_format="png", compression_mode="standard"))
    assert res["success"] and threads, "pngquant path should have asked for a timeout"
    assert not any(threads), "Image.open ran on the event-loop thread"
