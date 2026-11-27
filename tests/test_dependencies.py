"""Declared dependencies match what the package imports."""
from __future__ import annotations

from pathlib import Path


def test_unused_jinja2_is_not_declared():
    root = Path(__file__).resolve().parents[1]
    for name in ("pyproject.toml", "requirements.txt"):
        assert "jinja2" not in (root / name).read_text(encoding="utf-8").lower(), name
