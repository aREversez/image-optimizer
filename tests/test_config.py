"""Regression tests for app.main._load_app_config() (config.json support)."""
from __future__ import annotations

import json


def isolate_config_dir(monkeypatch, tmp_path):
    """_config_dir() now always resolves to Path.home() / '.image-optimizer'
    — patch Path.home() directly rather than guessing at platform-specific
    env vars, so the test is deterministic on every OS and doesn't
    accidentally touch the real per-user config dir."""
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


class TestLoadAppConfig:
    def test_defaults_when_no_config_file(self, monkeypatch, tmp_path):
        import app.main as m
        isolate_config_dir(monkeypatch, tmp_path)
        assert m._load_app_config() == m.DEFAULT_CONFIG

    def test_valid_overrides_are_applied(self, monkeypatch, tmp_path):
        import app.main as m
        isolate_config_dir(monkeypatch, tmp_path)
        m._config_file().write_text(json.dumps({"port": 9999, "concurrent_workers": 8}))

        cfg = m._load_app_config()
        assert cfg["port"] == 9999
        assert cfg["concurrent_workers"] == 8
        assert cfg["host"] == m.DEFAULT_CONFIG["host"], "unset keys should keep their default"

    def test_invalid_values_fall_back_to_defaults_without_crashing(self, monkeypatch, tmp_path):
        import app.main as m
        isolate_config_dir(monkeypatch, tmp_path)
        m._config_file().write_text(json.dumps({"port": "not-a-number", "concurrent_workers": -5}))

        cfg = m._load_app_config()
        assert cfg["port"] == m.DEFAULT_CONFIG["port"]
        assert cfg["concurrent_workers"] == m.DEFAULT_CONFIG["concurrent_workers"]

    def test_unknown_keys_are_ignored_not_errors(self, monkeypatch, tmp_path):
        import app.main as m
        isolate_config_dir(monkeypatch, tmp_path)
        m._config_file().write_text(json.dumps({"some_future_option": 123, "port": 8888}))

        cfg = m._load_app_config()
        assert "some_future_option" not in cfg
        assert cfg["port"] == 8888

    def test_malformed_json_falls_back_to_defaults(self, monkeypatch, tmp_path):
        import app.main as m
        isolate_config_dir(monkeypatch, tmp_path)
        m._config_file().write_text("{not valid json")

        assert m._load_app_config() == m.DEFAULT_CONFIG

    def test_zero_workers_clamped_to_at_least_one(self, monkeypatch, tmp_path):
        """A worker count of 0 would make the processing queue never
        drain (no worker tasks spun up at all) — must be rejected/clamped,
        not accepted as a valid-looking but broken configuration."""
        import app.main as m
        isolate_config_dir(monkeypatch, tmp_path)
        m._config_file().write_text(json.dumps({"concurrent_workers": 0}))

        cfg = m._load_app_config()
        assert cfg["concurrent_workers"] >= 1


class TestOptimizerConcurrencyIsConfigurable:
    """Reads asyncio.Semaphore's private _value attribute rather than
    testing through actual concurrent acquisition — a pragmatic shortcut
    for something this simple. _value has been stable across CPython 3.x
    for a long time, but if this ever breaks on a Python version bump,
    replace with a real acquire-N-times-successfully-then-block test."""

    def test_semaphore_size_follows_max_concurrency(self, fake_bin_dir):
        from app.optimizer import Optimizer
        opt = Optimizer(bin_dir=fake_bin_dir, max_concurrency=2)
        assert opt._semaphore._value == 2

    def test_zero_or_negative_concurrency_clamped_to_one(self, fake_bin_dir):
        from app.optimizer import Optimizer
        opt = Optimizer(bin_dir=fake_bin_dir, max_concurrency=0)
        assert opt._semaphore._value == 1


class TestConfigExampleFile:
    """config.example.json is a copy-paste template referenced from
    README — if it ever drifts out of sync with the real DEFAULT_CONFIG
    (e.g. a new config key gets added to the code but not the example
    file, or vice versa), the template silently becomes wrong/incomplete
    without anyone noticing until a user hits it."""

    def test_example_file_matches_default_config_exactly(self):
        import json
        from pathlib import Path
        import app.main as m

        example_path = Path(__file__).resolve().parent.parent / "config.example.json"
        assert example_path.exists(), "config.example.json is missing from the repo root"
        example = json.loads(example_path.read_text(encoding="utf-8"))
        assert example == m.DEFAULT_CONFIG, (
            "config.example.json has drifted from app.main.DEFAULT_CONFIG — "
            "update one to match the other"
        )
