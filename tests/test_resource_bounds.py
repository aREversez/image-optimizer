"""Resource bounds: decode concurrency and encoder timeouts (review R1).

Two problems, both about a batch using far more resources than --workers
promises:

- Pillow decode/resize/encode ran in the default thread pool, outside the
  semaphore that only bounds the external encoders. The CLI starts a task per
  file, so every file could be decoded at once.
- The encoder timeout was a flat 120 s regardless of image size: too long
  for an icon, short enough to kill a legitimate pngquant --speed 1 pass over
  a big photo (which then looked like a failure).
"""
from __future__ import annotations

import asyncio
import threading
import time

from PIL import Image

from image_optimizer import optimizer as optimizer_module
from image_optimizer.optimizer import PIXELS_PER_TIMEOUT_UNIT, Optimizer


class _FakeImage:
    """Stands in for Image.open() so a pixel count can be tested without
    allocating (or encoding) a 36 MP bitmap."""

    def __init__(self, size):
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestTimeoutScaling:
    def _stub(self, monkeypatch, size):
        monkeypatch.setattr(optimizer_module.Image, "open", lambda _p: _FakeImage(size))

    def test_small_image_gets_the_base_timeout(self, optimizer, tmp_path, monkeypatch):
        monkeypatch.setattr(optimizer_module, "SUBPROCESS_TIMEOUT", 100)
        self._stub(monkeypatch, (64, 64))
        assert optimizer._timeout_for(tmp_path / "x.png") == 100.0

    def test_timeout_grows_with_pixel_count(self, optimizer, tmp_path, monkeypatch):
        monkeypatch.setattr(optimizer_module, "SUBPROCESS_TIMEOUT", 100)
        self._stub(monkeypatch, (2000, 2000))  # 4 MP = one unit
        assert optimizer._timeout_for(tmp_path / "x.png") == 100.0
        self._stub(monkeypatch, (4000, 4000))  # 16 MP = four units
        assert optimizer._timeout_for(tmp_path / "x.png") == 400.0

    def test_timeout_is_capped_at_eight_times(self, optimizer, tmp_path, monkeypatch):
        monkeypatch.setattr(optimizer_module, "SUBPROCESS_TIMEOUT", 60)
        self._stub(monkeypatch, (12000, 12000))  # 144 MP would be 36 units
        assert optimizer._timeout_for(tmp_path / "x.png") == 480.0

    def test_real_small_image_is_not_scaled(self, optimizer, tmp_path, monkeypatch):
        monkeypatch.setattr(optimizer_module, "SUBPROCESS_TIMEOUT", 45)
        path = tmp_path / "tiny.png"
        Image.new("RGB", (32, 32), (1, 2, 3)).save(path)
        assert optimizer._timeout_for(path) == 45.0

    def test_unreadable_input_falls_back_to_the_base(self, optimizer, tmp_path, monkeypatch):
        monkeypatch.setattr(optimizer_module, "SUBPROCESS_TIMEOUT", 30)
        bogus = tmp_path / "not-an-image.png"
        bogus.write_bytes(b"not an image at all")
        assert optimizer._timeout_for(bogus) == 30.0

    def test_unit_is_four_megapixels(self):
        assert PIXELS_PER_TIMEOUT_UNIT == 4_000_000


class TestBoundedDecode:
    def test_decode_concurrency_is_bounded_by_max_concurrency(self, tmp_path):
        """A batch dispatches one task per file. With concurrency 2, no more
        than 2 blocking decode calls may be inside Pillow at once."""
        opt = Optimizer(bin_dir=tmp_path, max_concurrency=2)

        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def blocking(_n):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.03)
            with lock:
                state["now"] -= 1

        async def run():
            await asyncio.gather(*(opt._run_bounded(blocking, i) for i in range(12)))

        asyncio.run(run())
        assert state["peak"] == 2, state

    def test_slot_is_released_when_the_work_raises(self, tmp_path):
        opt = Optimizer(bin_dir=tmp_path, max_concurrency=1)

        def boom():
            raise RuntimeError("decode failed")

        async def run():
            for _ in range(3):
                try:
                    await opt._run_bounded(boom)
                except RuntimeError:
                    pass
            # A leaked slot would hang here forever.
            await asyncio.wait_for(opt._run_bounded(lambda: "ok"), timeout=2)

        asyncio.run(run())
