"""Shared pytest fixtures for fastmcp-pvl-core tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from fastmcp_pvl_core._authorization import set_current_authorizer
from fastmcp_pvl_core._subject import set_current_auth_mode


@pytest.fixture(autouse=True)
def _reset_auth_mode() -> Iterator[None]:
    """Reset the auth-mode contextvar between tests.

    ``set_current_auth_mode`` writes to a :class:`ContextVar` whose
    visible value crosses test boundaries when tests share the same
    asyncio task / module run.  Lifted suite-wide here so that any
    test calling ``build_auth`` (which mutates the var as a startup
    side effect) does not leak the resolved mode into the next test
    that reads via ``get_subject``.
    """
    set_current_auth_mode(None)
    yield
    set_current_auth_mode(None)


@pytest.fixture(autouse=True)
def _reset_authorizer() -> Iterator[None]:
    """Reset the authorizer contextvar between tests.

    Mirrors ``_reset_auth_mode``; same rationale.
    """
    set_current_authorizer(None)
    yield
    set_current_authorizer(None)


@pytest.fixture(autouse=True)
def _reset_capability_builders() -> Iterator[None]:
    """Clear the per-FastMCP capability-builder registry between tests.

    Builders are keyed by ``id(mcp)`` (see
    ``fastmcp_pvl_core.file_exchange._capability_builders``); pytest
    fixtures may recycle FastMCP instances across the process. Without
    this reset, capability state from one test can leak into the next
    — e.g. an upload-direction contribution showing up in a
    download-only test's capability dict.
    """
    from fastmcp_pvl_core.file_exchange import (
        reset_capability_builders_for_test,
    )

    reset_capability_builders_for_test()
    yield
    reset_capability_builders_for_test()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip all env vars whose name starts with a common test prefix."""
    prefixes = ("TEST_", "PVLCORE_TEST_")
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    yield
