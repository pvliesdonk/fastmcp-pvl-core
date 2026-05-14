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


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip all env vars whose name starts with a common test prefix."""
    prefixes = ("TEST_", "PVLCORE_TEST_")
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def reset_artifact_store_test_seam() -> Iterator[None]:
    """Reset the file-exchange artifact-store test seam on teardown.

    Autouse so a test that calls ``_set_artifact_store_for_test`` and
    forgets to request the fixture explicitly cannot poison subsequent
    tests in the same session.  Zero cost in the common case where the
    seam is ``None`` (the reset is a no-op).  Tests may still request
    the fixture explicitly when they want to read its name in their
    own signature — autouse and explicit-request do not double-run.
    """
    yield
    from fastmcp_pvl_core.file_exchange import _set_artifact_store_for_test

    _set_artifact_store_for_test(None)
