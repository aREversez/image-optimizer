"""CLI content-hash cache (--cache-dir / --cache-max-size-mb)."""
from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path

import pytest
from PIL import Image

from image_optimizer.cache import CACHE_SCHEMA_VERSION, ContentCache, file_sha256
from tests.test_cli import cli_args


@pytest.fixture
def counting(optimizer, monkeypatch):
    """Counts how many files actually go through the compression pipeline."""
    calls = {"n": 0}
    real = optimizer.optimize_png

    async def wrapper(*a, **kw):
        calls["n"] += 1
        return await real(*a, **kw)

    monkeypatch.setattr(optimizer, "optimize_png", wrapper)
    return calls


def _make_src(tmp_path: Path, n: int = 2) -> Path:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    for i in range(n):
        rnd = random.Random(i)
        img = Image.new("RGB", (48, 48))
        img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                     for _ in range(48 * 48)])
        img.save((src / "sub" if i % 2 else src) / f"img{i}.png")
    return src


def _run(app_module, optimizer, src, out, cache, **kw):
    return asyncio.run(app_module._run_cli(
        cli_args(source=str(src), output=str(out), cache_dir=str(cache), **kw),
        optimizer_instance=optimizer,
    ))


def _entries(cache: Path) -> list[Path]:
    return [p for p in cache.rglob("*") if p.is_file()]


class TestHitAndMiss:
    def test_first_run_misses_second_run_hits(self, app_module, optimizer, counting, tmp_path, capsys):
        src, cache = _make_src(tmp_path), tmp_path / "cache"
        assert _run(app_module, optimizer, src, tmp_path / "o1", cache) == 0
        assert counting["n"] == 2
        assert len(_entries(cache)) == 2
        assert "Cache: 0 hit, 2 compressed" in capsys.readouterr().out

        assert _run(app_module, optimizer, src, tmp_path / "o2", cache) == 0
        assert counting["n"] == 2  # nothing recompressed
        assert "Cache: 2 hit, 0 compressed" in capsys.readouterr().out

    def test_hit_output_is_byte_identical_and_in_fresh_output_dir(
        self, app_module, optimizer, tmp_path
    ):
        src, cache = _make_src(tmp_path), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o1", cache)
        _run(app_module, optimizer, src, tmp_path / "o2", cache)
        for rel in ("img0.png", "sub/img1.png"):
            assert (tmp_path / "o2" / rel).read_bytes() == (tmp_path / "o1" / rel).read_bytes()

    @pytest.mark.parametrize("change", [
        dict(quality="high"),
        dict(format="webp"),
        dict(max_width=24),
        dict(mode="lossless"),
        dict(dithering=False),
        dict(keep_exif=True),
        dict(protect_colors="#ff0000"),
    ])
    def test_pixel_affecting_setting_change_is_a_miss(
        self, app_module, optimizer, counting, tmp_path, change
    ):
        src, cache = _make_src(tmp_path, 1), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o1", cache)
        assert counting["n"] == 1
        _run(app_module, optimizer, src, tmp_path / "o2", cache, **change)
        assert counting["n"] == 2

    @pytest.mark.parametrize("change", [
        dict(workers=1), dict(recursive=False), dict(exclude=["img1.png"]), dict(include=["img0.png"]),
    ])
    def test_non_pixel_setting_change_still_hits(
        self, app_module, optimizer, counting, tmp_path, change
    ):
        # recursive=False drops sub/img1.png from the scan; img0.png is top-level.
        src, cache = _make_src(tmp_path, 1), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o1", cache)
        _run(app_module, optimizer, src, tmp_path / "o2", cache, **change)
        assert counting["n"] == 1

    def test_cache_is_content_addressed_not_path_addressed(
        self, app_module, optimizer, counting, tmp_path
    ):
        src, cache = _make_src(tmp_path, 1), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o1", cache)
        (src / "img0.png").rename(src / "renamed.png")
        _run(app_module, optimizer, src, tmp_path / "o2", cache)
        assert counting["n"] == 1
        assert (tmp_path / "o2" / "renamed.png").exists()

    def test_changed_input_bytes_is_a_miss(self, app_module, optimizer, counting, tmp_path):
        src, cache = _make_src(tmp_path, 1), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o1", cache)
        Image.new("RGB", (48, 48), (1, 2, 3)).save(src / "img0.png")
        _run(app_module, optimizer, src, tmp_path / "o2", cache)
        assert counting["n"] == 2

    def test_encoder_binary_change_invalidates(
        self, app_module, optimizer, counting, fake_bin_dir, tmp_path
    ):
        src, cache = _make_src(tmp_path, 1), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o1", cache)
        with open(optimizer.pngquant_path, "a", encoding="utf-8") as f:
            f.write("\n# different build\n")
        _run(app_module, optimizer, src, tmp_path / "o2", cache)
        assert counting["n"] == 2

    def test_kept_original_result_is_cached(self, app_module, optimizer, counting, tmp_path):
        """The growth-guard fallback is a successful result and is cached."""
        src, cache = tmp_path / "src", tmp_path / "cache"
        src.mkdir()
        Image.new("RGB", (4, 4), (10, 20, 30)).save(src / "tiny.png")
        _run(app_module, optimizer, src, tmp_path / "o1", cache)
        _run(app_module, optimizer, src, tmp_path / "o2", cache)
        assert counting["n"] == 1
        assert (tmp_path / "o2" / "tiny.png").read_bytes() == (tmp_path / "o1" / "tiny.png").read_bytes()


class TestRobustness:
    def test_without_cache_dir_nothing_changes(self, app_module, optimizer, counting, tmp_path, capsys):
        src = _make_src(tmp_path)
        for i in (1, 2):
            code = asyncio.run(app_module._run_cli(
                cli_args(source=str(src), output=str(tmp_path / f"o{i}")),
                optimizer_instance=optimizer,
            ))
            assert code == 0
        assert counting["n"] == 4
        assert "Cache:" not in capsys.readouterr().out

    def test_truncated_entry_is_recompressed_and_overwritten(
        self, app_module, optimizer, counting, tmp_path
    ):
        src, cache = _make_src(tmp_path, 1), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o1", cache)
        (entry,) = _entries(cache)
        good = entry.read_bytes()
        entry.write_bytes(good[:20])

        assert _run(app_module, optimizer, src, tmp_path / "o2", cache) == 0
        assert counting["n"] == 2  # corrupt entry treated as a miss
        assert entry.read_bytes() == good  # overwritten with a fresh result
        assert (tmp_path / "o2" / "img0.png").read_bytes() == good

    def test_failed_result_is_never_cached(
        self, app_module, optimizer, counting, tmp_path, monkeypatch, capsys
    ):
        stored = []
        real_store = ContentCache.store
        monkeypatch.setattr(
            ContentCache, "store", lambda self, key, produced: (stored.append(key), real_store(self, key, produced))[1]
        )
        src, cache = tmp_path / "src", tmp_path / "cache"
        src.mkdir()
        (src / "bad.png").write_bytes(b"not actually a png")
        assert _run(app_module, optimizer, src, tmp_path / "o1", cache) == 1
        # store() must not even be attempted: relying on it to raise for a
        # missing output file would be an accident, not a guarantee.
        assert stored == []
        assert _entries(cache) == []
        assert "cache write failed" not in capsys.readouterr().err
        assert _run(app_module, optimizer, src, tmp_path / "o2", cache) == 1
        assert counting["n"] == 2

    def test_cache_dir_inside_source_is_rejected(self, app_module, optimizer, tmp_path, capsys):
        src = _make_src(tmp_path, 1)
        code = _run(app_module, optimizer, src, tmp_path / "o", src / ".cache")
        assert code == 1
        assert "must not be inside --source" in capsys.readouterr().err

    def test_negative_cache_size_is_rejected(self, app_module, optimizer, tmp_path, capsys):
        src = _make_src(tmp_path, 1)
        code = _run(app_module, optimizer, src, tmp_path / "o", tmp_path / "c", cache_max_size_mb=-1)
        assert code == 1
        assert "--cache-max-size-mb" in capsys.readouterr().err


def _big_src(tmp_path: Path, n: int) -> Path:
    """n incompressible ~480 KB PNGs (the fake pngquant copies bytes through)."""
    src = tmp_path / "big"
    src.mkdir()
    for i in range(n):
        raw = random.Random(100 + i).randbytes(400 * 400 * 3)
        Image.frombytes("RGB", (400, 400), raw).save(src / f"big{i}.png")
    return src


def _total(cache: Path) -> int:
    return sum(p.stat().st_size for p in _entries(cache))


class TestEvictionViaCli:
    def test_over_cap_evicts_down_to_cap(self, app_module, optimizer, tmp_path, capsys):
        src, cache = _big_src(tmp_path, 4), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o1", cache, cache_max_size_mb=0)
        assert len(_entries(cache)) == 4 and _total(cache) > 1024 * 1024
        capsys.readouterr()

        _run(app_module, optimizer, src, tmp_path / "o2", cache, cache_max_size_mb=1)
        assert _total(cache) <= 1024 * 1024
        assert 0 < len(_entries(cache)) < 4
        assert "evicted" in capsys.readouterr().out

    def test_zero_means_unlimited(self, app_module, optimizer, tmp_path):
        src, cache = _big_src(tmp_path, 4), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o1", cache, cache_max_size_mb=0)
        _run(app_module, optimizer, src, tmp_path / "o2", cache, cache_max_size_mb=0)
        assert len(_entries(cache)) == 4


class TestEvictOrder:
    def _cache(self, tmp_path):
        return ContentCache(tmp_path / "c", "png", "s", "e")

    def _put(self, cache, key, size, mtime):
        p = cache.entry_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        os.utime(p, (mtime, mtime))
        return p

    def test_oldest_mtime_goes_first(self, tmp_path):
        c = self._cache(tmp_path)
        old = self._put(c, "aa" + "0" * 62, 100, 1_000)
        mid = self._put(c, "bb" + "0" * 62, 100, 2_000)
        new = self._put(c, "cc" + "0" * 62, 100, 3_000)
        assert c.evict(250) == 1
        assert not old.exists() and mid.exists() and new.exists()
        assert c.evict(100) == 1
        assert not mid.exists() and new.exists()

    def test_under_cap_and_unlimited_remove_nothing(self, tmp_path):
        c = self._cache(tmp_path)
        a = self._put(c, "aa" + "0" * 62, 100, 1_000)
        assert c.evict(10_000) == 0
        assert c.evict(0) == 0
        assert a.exists()

    def test_hit_refreshes_recency(self, tmp_path):
        """A hit must protect an entry from eviction (LRU, not FIFO)."""
        src = tmp_path / "in.png"
        Image.new("RGB", (8, 8), (5, 5, 5)).save(src)
        c = self._cache(tmp_path)
        key = c.key_for_digest(file_sha256(src))
        entry = self._put(c, key, 0, 1_000)
        entry.write_bytes(src.read_bytes())
        os.utime(entry, (1_000, 1_000))
        probe = c.probe(src, tmp_path / "out.png")
        assert probe.hit_size is not None
        assert entry.stat().st_mtime > 1_000

    def test_schema_version_is_part_of_the_key(self, tmp_path, monkeypatch):
        import image_optimizer.cache as cache_mod
        c = self._cache(tmp_path)
        k1 = c.key_for_digest("d")
        monkeypatch.setattr(cache_mod, "CACHE_SCHEMA_VERSION", CACHE_SCHEMA_VERSION + 1)
        assert c.key_for_digest("d") != k1
