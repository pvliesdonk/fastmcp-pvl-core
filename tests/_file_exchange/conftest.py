"""Shared fixtures for file-exchange test modules."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

CONFORMANCE_ROOT = Path(__file__).parent / "conformance"


def discover_fixtures(subpath: str) -> list[Path]:
    """Return ``*.json`` files under ``conformance/<subpath>``, sorted."""
    base = CONFORMANCE_ROOT / subpath
    if not base.is_dir():
        return []
    return sorted(base.glob("*.json"))


def fixture_ids(paths: Iterable[Path]) -> list[str]:
    """Render fixture paths as compact pytest ids (file stem only)."""
    return [p.stem for p in paths]
