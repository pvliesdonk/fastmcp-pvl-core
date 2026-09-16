"""Shared pytest fixtures for fastmcp-pvl-core tests."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import pytest

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
def _fastmcp_logger_propagates() -> Iterator[None]:
    """Allow pytest's caplog to capture records from the ``fastmcp.*`` hierarchy.

    FastMCP installs a RichHandler on the ``fastmcp`` root logger and sets
    ``propagate=False``, which prevents records from reaching the stdlib root
    logger that pytest's caplog handler is attached to.  Any test that uses
    ``caplog`` with a logger under ``fastmcp.*`` would silently capture nothing
    without this fixture.

    The fixture temporarily re-enables propagation and yields; teardown
    restores the original state so other tests (and the RichHandler output
    stream) are not affected.
    """
    fastmcp_logger = logging.getLogger("fastmcp")
    original_propagate = fastmcp_logger.propagate
    fastmcp_logger.propagate = True
    yield
    fastmcp_logger.propagate = original_propagate


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip all env vars whose name starts with a common test prefix."""
    prefixes = ("TEST_", "PVLCORE_TEST_")
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_legacy_url_warning_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level one-shot warning flag between tests.

    ``build_kv_store`` suppresses the legacy ``event_store_url`` warning
    after its first emission per-process. Without this reset, any test
    (here or in another file — e.g. ``test_factory.py``) that exercises
    the legacy fallback after another test already triggered the
    warning would see an unexpectedly silent path. Lifted suite-wide
    so the fixture protects every test that touches ``build_kv_store``.
    """
    monkeypatch.setattr("fastmcp_pvl_core._kv_store._legacy_url_warned", False)


@pytest.fixture(autouse=True)
def _reset_default_fallback_warning_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the one-shot memory-fallback warning flag between tests.

    Same reasoning as ``_reset_legacy_url_warning_flag``: the flag that
    keeps the unusable-default-directory warning to one line per process
    would otherwise silence it for whichever test happens to run second.
    """
    monkeypatch.setattr("fastmcp_pvl_core._kv_store._default_fallback_warned", False)


MANAGED_LOGGERS = (
    "fastmcp",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "mcp.server.lowlevel.server",
    "httpx",
    "httpcore",
    "docket.worker",
)
"""Every logger ``configure_logging_from_env`` mutates.

One list, in one place: two modules snapshot this topology, and a copy in
each would drift the moment the policy gained a logger — which is how a
test module last left ``httpx`` demoted for the rest of the suite.
"""


@pytest.fixture
def restore_logging_topology() -> Iterator[None]:
    """Snapshot the whole logging topology and put it back afterwards.

    Root handlers included: tests that call ``configure_logging_from_env``
    install and remove handlers at root, and without this the first one to
    run leaves the rest of the suite — and pytest's own ``caplog`` — on a
    tree it did not expect.

    Not autouse: only the modules that reconfigure logging pay for it.
    """
    import fastmcp

    root = logging.getLogger()
    saved_root = (root.handlers[:], root.level)
    saved = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).filters[:],
        )
        for name in MANAGED_LOGGERS
    }
    saved_log_enabled = fastmcp.settings.log_enabled
    try:
        yield
    finally:
        root.handlers[:] = saved_root[0]
        root.setLevel(saved_root[1])
        for name, (handlers, level, propagate, filters) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.filters[:] = filters
        fastmcp.settings.log_enabled = saved_log_enabled
