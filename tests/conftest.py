"""Shared pytest fixtures for fastmcp-pvl-core tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip all env vars whose name starts with a common test prefix."""
    prefixes = ("TEST_", "PVLCORE_TEST_")
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    yield
