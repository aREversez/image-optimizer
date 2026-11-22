"""CLI content-hash cache (--cache-dir / --cache-max-size-mb)."""
from __future__ import annotations

import asyncio
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image, features as pil_features

import image_optimizer.cache as _cache_mod
from image_optimizer.cache import (
    CACHE_SCHEMA_VERSION,
    ContentCache,
    file_sha256,
    is_valid_reusable_output,
)
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

    def test_cache_dir_inside_output_is_rejected(self, app_module, optimizer, tmp_path, capsys):
        """Same reasoning as --source: key-named entries would land in the
        deliverable output directory (and vanish when LRU evicts them)."""
        src = _make_src(tmp_path, 1)
        out = tmp_path / "out"
        assert _run(app_module, optimizer, src, out, out / ".cache") == 1
        assert "must not be inside --output" in capsys.readouterr().err
        # Equal to --output is rejected too.
        assert _run(app_module, optimizer, src, tmp_path / "out2", tmp_path / "out2") == 1
        assert "must not be inside --output" in capsys.readouterr().err

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="case-insensitive path containment is Windows-only",
    )
    def test_cache_dir_case_variant_of_output_is_rejected(
        self, app_module, optimizer, tmp_path, capsys
    ):
        """Windows compares paths case-insensitively on disk; before
        os.path.normcase, 'Out' vs 'out' slipped past the containment check
        and the cache landed inside the deliverable directory."""
        src = _make_src(tmp_path, 1)
        out = tmp_path / "Out"
        assert _run(app_module, optimizer, src, out, tmp_path / "out") == 1
        assert "must not be inside --output" in capsys.readouterr().err

    def test_cache_dir_ancestor_of_output_is_allowed(self, app_module, optimizer, tmp_path):
        """Only containment is forbidden: a cache dir that merely *contains*
        --output does not pollute anything (same asymmetry as --source)."""
        src = _make_src(tmp_path, 1)
        parent = tmp_path / "work"
        code = _run(app_module, optimizer, src, parent / "out", parent)
        assert code == 0


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


class TestAvifEntries:
    """AVIF cache hits depend on Pillow being able to open AVIF files:
    is_valid_reusable_output() validates an entry by reading it, so a Pillow
    without AVIF support would fail every AVIF entry (fail-closed) and the
    cache would silently never hit for that format. pyproject pins
    Pillow>=11.3 for exactly this reason; the run also warns on stderr when
    the feature is missing."""

    def test_real_avif_bytes_are_served(self, tmp_path):
        if not pil_features.check("avif"):
            pytest.skip("this Pillow build cannot read AVIF")
        src = tmp_path / "in.png"
        Image.new("RGB", (16, 16), (30, 90, 120)).save(src)
        c = ContentCache(tmp_path / "c", "avif", "s", "e")
        key = c.key_for_digest(file_sha256(src))
        entry = c.entry_path(key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        # Real AVIF bytes (what a real avifenc run produces), not the fake
        # pipeline's PNG-with-.avif-extension stand-in.
        Image.new("RGB", (16, 16), (30, 90, 120)).save(entry, format="AVIF")
        probe = c.probe(src, tmp_path / "out.avif")
        assert probe.hit_size == entry.stat().st_size

    def test_non_avif_bytes_in_avif_entry_are_rejected(self, tmp_path):
        """A file that is not really AVIF must not be served, whatever wrote
        it (this is what keeps the fake-binary test env from false-hitting)."""
        src = tmp_path / "in.png"
        Image.new("RGB", (16, 16)).save(src)
        c = ContentCache(tmp_path / "c", "avif", "s", "e")
        key = c.key_for_digest(file_sha256(src))
        entry = c.entry_path(key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16)).save(entry, format="PNG")
        assert c.probe(src, tmp_path / "out.avif").hit_size is None

    def test_run_warns_when_pillow_cannot_read_avif(
        self, app_module, optimizer, monkeypatch, tmp_path, capsys
    ):
        if pil_features.check("avif"):
            pytest.skip("this Pillow build can read AVIF; warning path not reachable")
        src = _make_src(tmp_path, 1)
        _run(app_module, optimizer, src, tmp_path / "o", tmp_path / "cache",
             format="avif")
        assert "will never hit for AVIF" in capsys.readouterr().err


class TestStaleTmpSweep:
    """Crash leftovers (.tmp-* files) must be swept even when no size cap is
    set: with --cache-max-size-mb 0 evict() never runs, and before the
    dedicated sweep those files accumulated forever."""

    @staticmethod
    def _stale_tmp(cache: Path) -> Path:
        p = cache / "ab" / ("ab" + "0" * 62 + ".png.tmp-123-deadbeef")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"partial write")
        old = time.time() - 2 * _cache_mod._STALE_TMP_SECONDS
        os.utime(p, (old, old))
        return p

    def test_sweep_removes_only_stale_tmp(self, tmp_path):
        cache = tmp_path / "c"
        stale = self._stale_tmp(cache)
        fresh = cache / "cd" / ("cd" + "0" * 62 + ".png.tmp-123-cafebabe")
        fresh.parent.mkdir(parents=True, exist_ok=True)
        fresh.write_bytes(b"just written")
        entry = cache / "cd" / ("cd" + "0" * 62 + ".png")
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b"real entry")
        c = ContentCache(cache, "png", "s", "e")
        assert c.sweep_stale_tmp() == 1
        assert not stale.exists()
        assert fresh.exists()  # younger than the threshold
        assert entry.exists()  # real entries are never swept

    def test_unlimited_run_still_sweeps(self, app_module, optimizer, tmp_path):
        src, cache = _make_src(tmp_path, 1), tmp_path / "cache"
        stale = self._stale_tmp(cache)
        assert _run(app_module, optimizer, src, tmp_path / "o", cache,
                    cache_max_size_mb=0) == 0
        assert not stale.exists()


class TestEvictGrace:
    """Entries written during the current run must survive that run's own
    eviction: with a cap smaller than one entry, every run would otherwise
    store a result and immediately delete it, so the cache could never serve
    a single hit."""

    def test_evict_never_picks_protected_entries(self, tmp_path):
        c = ContentCache(tmp_path / "c", "png", "s", "e")
        old = c.entry_path("aa" + "0" * 62)
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"x" * 100)
        os.utime(old, (1_000, 1_000))
        fresh = c.entry_path("bb" + "0" * 62)
        fresh.parent.mkdir(parents=True, exist_ok=True)
        fresh.write_bytes(b"x" * 100)
        os.utime(fresh, (2_000, 2_000))
        stats: dict = {}
        assert c.evict(90, {fresh}, stats) == 1  # only the old entry goes
        assert not old.exists()
        assert fresh.exists()
        # The caller learns the cap is still exceeded (fresh 100 B > 90 B).
        assert stats["total_after"] == 100

    def test_run_warns_when_still_over_cap(self, app_module, optimizer, tmp_path, capsys):
        # One incompressible PNG larger than a 1 MB cap: the fresh entry is
        # protected from same-run eviction, so the cap stays exceeded and the
        # operator gets a warning instead of silent thrash on every run.
        raw = random.Random(7).randbytes(600 * 600 * 3)
        src = tmp_path / "src"
        src.mkdir()
        Image.frombytes("RGB", (600, 600), raw).save(src / "big.png")
        cache = tmp_path / "cache"
        assert _run(app_module, optimizer, src, tmp_path / "o", cache,
                    cache_max_size_mb=1) == 0
        assert "still over --cache-max-size-mb" in capsys.readouterr().err
        # The just-written entry survived its own run's eviction.
        assert len(_entries(cache)) == 1

    def test_under_cap_run_does_not_warn(self, app_module, optimizer, tmp_path, capsys):
        src, cache = _make_src(tmp_path, 1), tmp_path / "cache"
        _run(app_module, optimizer, src, tmp_path / "o", cache, cache_max_size_mb=1024)
        assert "still over" not in capsys.readouterr().err


class TestConcurrency:
    """The cache is lock-free by design (atomic os.replace, last write
    wins); these smoke tests hammer the paths a reviewer would worry about:
    concurrent same-key writers and probes landing while writers replace
    entries. Every served file must be a complete, valid image."""

    def test_same_key_writers_last_write_wins(self, tmp_path):
        c = ContentCache(tmp_path / "c", "png", "s", "e")
        src = tmp_path / "in.png"
        Image.new("RGB", (8, 8)).save(src)
        key = c.key_for_digest(file_sha256(src))
        produced = tmp_path / "produced"
        produced.mkdir()

        def write(i: int) -> None:
            p = produced / f"v{i}.png"
            Image.new("RGB", (8, 8), (i, i, i)).save(p, format="PNG")
            c.store(key, p)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(8)))

        probe = c.probe(src, tmp_path / "out.png")
        assert probe.hit_size is not None
        assert is_valid_reusable_output(c.entry_path(key), "png")

    def test_probe_while_writing_never_serves_partial_bytes(self, tmp_path):
        c = ContentCache(tmp_path / "c", "png", "s", "e")
        src = tmp_path / "in.png"
        Image.new("RGB", (8, 8)).save(src)
        key = c.key_for_digest(file_sha256(src))
        big = tmp_path / "big.png"
        # A few MB so the copy takes long enough to overlap with probes.
        raw = random.Random(3).randbytes(1024 * 1024 * 3)
        Image.frombytes("RGB", (1024, 1024), raw).save(big, format="PNG")
        stop = threading.Event()
        errors: list = []

        def writer(writer_id: int) -> None:
            while not stop.is_set():
                try:
                    c.store(key, big)
                except Exception as e:  # pragma: no cover - failure IS the bug
                    errors.append(e)
                    return

        def reader(reader_id: int) -> None:
            # Distinct out paths: in real runs every file gets its own output
            # path; two writers to the SAME out path would clobber each other
            # with or without the cache (pre-existing, out of scope here).
            out = tmp_path / f"out{reader_id}.png"
            while not stop.is_set():
                probe = c.probe(src, out)
                if probe.hit_size is not None:
                    try:
                        with Image.open(out) as img:
                            img.verify()
                    except Exception:
                        errors.append("probe served a partial/invalid file")
                        return

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(writer, 0), pool.submit(reader, 1), pool.submit(reader, 2)]
            time.sleep(1.0)
            stop.set()
            for fut in futures:
                fut.result()

        assert errors == []

    def test_probe_failure_falls_through_to_recompression(
        self, app_module, optimizer, counting, tmp_path, monkeypatch
    ):
        """An unreadable input (probe raises OSError) must degrade into a
        normal recompression, not a crash: the runcli wrapper catches the
        error and the regular pipeline reports a real input error itself."""
        src, cache = _make_src(tmp_path, 1), tmp_path / "cache"

        def boom(path):
            raise OSError("simulated unreadable input")

        monkeypatch.setattr(_cache_mod, "file_sha256", boom)
        assert _run(app_module, optimizer, src, tmp_path / "o", cache) == 0
        assert counting["n"] == 1  # fell through to the pipeline
        # Nothing stored, by design: the cache key IS the input hash, so a
        # run that could not hash the input has no key to store under. The
        # file is still optimized and the run still exits 0.
        assert len(_entries(cache)) == 0


class TestForeignFilesInCacheDir:
    """--cache-dir is user-supplied, so it can point at a directory that
    already holds unrelated files. Eviction and the temp sweep must never
    count, delete or overwrite anything that is not shaped like a cache
    entry - otherwise a typo'd path turns LRU into a file shredder."""

    def test_evict_ignores_foreign_files(self, tmp_path):
        cache = tmp_path / "c"
        victim = cache / "20" / "holiday.jpg"
        victim.parent.mkdir(parents=True)
        victim.write_bytes(b"x" * 500)
        # A valid entry name, but in the wrong shard (keys live under
        # their own first two hex digits), so it is not one of ours either.
        mismatched = cache / "ab" / ("cd" + "0" * 62 + ".png")
        mismatched.parent.mkdir(parents=True, exist_ok=True)
        mismatched.write_bytes(b"y" * 500)
        real = ContentCache(cache, "png", "s", "e")
        # A real entry above the cap still gets evicted, proving the safety
        # check narrowed the victims rather than disabling eviction.
        real_entry = real.entry_path("ef" + "0" * 62)
        real_entry.parent.mkdir(parents=True, exist_ok=True)
        real_entry.write_bytes(b"z" * 500)
        assert real.evict(100) == 1
        assert not real_entry.exists()
        assert victim.exists()
        assert mismatched.exists()

    def test_sweep_ignores_foreign_tmp_files(self, tmp_path):
        cache = tmp_path / "c"
        foreign = cache / "ab" / "notes.tmp-1-deadbeef"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"unrelated")
        old = time.time() - 2 * _cache_mod._STALE_TMP_SECONDS
        os.utime(foreign, (old, old))
        assert ContentCache(cache, "png", "s", "e").sweep_stale_tmp() == 0
        assert foreign.exists()


class TestReplaceRetry:
    """os.replace() fails with PermissionError on Windows while the
    destination is open elsewhere (a probe copying it, or another writer
    replacing the same key). store() retries, and when the entry exists by
    the last attempt another writer already published a complete file."""

    def test_store_retries_transient_permission_errors(self, tmp_path, monkeypatch):
        c = ContentCache(tmp_path / "c", "png", "s", "e")
        src = tmp_path / "in.png"
        Image.new("RGB", (8, 8)).save(src)
        key = c.key_for_digest(file_sha256(src))
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(a, b):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("simulated Windows reader holding the entry open")
            return real_replace(a, b)

        monkeypatch.setattr(_cache_mod.os, "replace", flaky_replace)
        entry = c.store(key, src)
        assert calls["n"] == 3
        assert is_valid_reusable_output(entry, "png")
        # No temp leftovers, even for the failed attempts.
        assert [p.name for p in entry.parent.iterdir() if ".tmp-" in p.name] == []

    def test_store_accepts_entry_published_by_another_writer(self, tmp_path, monkeypatch):
        c = ContentCache(tmp_path / "c", "png", "s", "e")
        src = tmp_path / "in.png"
        Image.new("RGB", (8, 8)).save(src)
        key = c.key_for_digest(file_sha256(src))
        entry = c.entry_path(key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        # The winning writer published a complete, valid entry (different
        # pixels from ours, so the assertion below can tell them apart).
        Image.new("RGB", (8, 8), (9, 9, 9)).save(entry)
        winner = entry.read_bytes()

        def always_denied(a, b):
            raise PermissionError("destination stays open for the whole run")

        monkeypatch.setattr(_cache_mod.os, "replace", always_denied)
        assert c.store(key, src) == entry
        assert entry.read_bytes() == winner
        assert [p.name for p in entry.parent.iterdir() if ".tmp-" in p.name] == []

    @pytest.mark.parametrize("bad", [b"", b"already published by the winning writer", b"\x89PNG\r\n\x1a\n"])
    def test_store_does_not_accept_a_corrupt_existing_entry(self, tmp_path, monkeypatch, bad):
        """A corrupt leftover is what probe() just treated as a miss. If the
        replace keeps failing, store() must not report success over it: the
        caller would never learn the bad entry was not repaired."""
        c = ContentCache(tmp_path / "c", "png", "s", "e")
        src = tmp_path / "in.png"
        Image.new("RGB", (8, 8)).save(src)
        key = c.key_for_digest(file_sha256(src))
        entry = c.entry_path(key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(bad)

        def always_denied(a, b):
            raise PermissionError("destination stays open for the whole run")

        monkeypatch.setattr(_cache_mod.os, "replace", always_denied)
        with pytest.raises(PermissionError):
            c.store(key, src)
        assert [p.name for p in entry.parent.iterdir() if ".tmp-" in p.name] == []

    def test_store_rejects_an_entry_of_the_wrong_format(self, tmp_path, monkeypatch):
        """A valid image in the wrong format is not a valid entry for this
        cache either (same rule as a hit)."""
        c = ContentCache(tmp_path / "c", "png", "s", "e")
        src = tmp_path / "in.png"
        Image.new("RGB", (8, 8)).save(src)
        key = c.key_for_digest(file_sha256(src))
        entry = c.entry_path(key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8)).save(entry, format="JPEG")

        monkeypatch.setattr(
            _cache_mod.os, "replace",
            lambda a, b: (_ for _ in ()).throw(PermissionError("locked")),
        )
        with pytest.raises(PermissionError):
            c.store(key, src)

    def test_cli_reports_a_failed_cache_write_and_still_succeeds(
        self, app_module, optimizer, tmp_path, monkeypatch, capsys
    ):
        """End to end: an unrepairable corrupt entry surfaces as the existing
        '[Warning] cache write failed' on stderr, the file itself is still
        compressed and delivered, and the exit code stays 0."""
        src_dir = _make_src(tmp_path, 1)
        cache = tmp_path / "cache"
        assert _run(app_module, optimizer, src_dir, tmp_path / "o1", cache) == 0
        (entry,) = _entries(cache)
        entry.write_bytes(entry.read_bytes()[:20])  # corrupt it
        capsys.readouterr()

        # os is a process-wide module: only deny replaces INTO the cache, so
        # the compression pipeline's own os.replace calls keep working.
        real_replace = os.replace

        def deny_cache_replace(a, b):
            if cache in Path(b).parents:
                raise PermissionError("locked")
            return real_replace(a, b)

        monkeypatch.setattr(_cache_mod.os, "replace", deny_cache_replace)
        assert _run(app_module, optimizer, src_dir, tmp_path / "o2", cache) == 0
        assert "cache write failed" in capsys.readouterr().err
        assert (tmp_path / "o2" / "img0.png").exists()
