"""Tests for wire_middleware_stack."""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import (
    LoggingMiddleware,
    StructuredLoggingMiddleware,
)
from fastmcp.server.middleware.timing import TimingMiddleware

from fastmcp_pvl_core import wire_middleware_stack

# Middleware types that wire_middleware_stack installs — used to filter
# out any built-in middlewares FastMCP installs by default.
_INSTALLED_BY_HELPER = (
    ErrorHandlingMiddleware,
    TimingMiddleware,
    LoggingMiddleware,
    StructuredLoggingMiddleware,
)


def _installed_types(mcp: FastMCP) -> list[type]:
    """Return the ordered types of middlewares added by wire_middleware_stack.

    FastMCP may pre-install its own middlewares (e.g. DereferenceRefsMiddleware);
    this filters to only those wire_middleware_stack is responsible for so
    tests assert our installation order, not FastMCP internals.
    """
    return [type(m) for m in mcp.middleware if isinstance(m, _INSTALLED_BY_HELPER)]


def test_installs_three_middlewares_in_order():
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    types = _installed_types(mcp)
    assert len(types) == 3
    assert types[0] is ErrorHandlingMiddleware
    assert types[1] is TimingMiddleware
    assert types[2] in (LoggingMiddleware, StructuredLoggingMiddleware)


def test_structured_when_rich_disabled(monkeypatch):
    monkeypatch.setenv("FASTMCP_ENABLE_RICH_LOGGING", "false")
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    types = _installed_types(mcp)
    assert types[2] is StructuredLoggingMiddleware


def test_rich_when_rich_unset(monkeypatch):
    monkeypatch.delenv("FASTMCP_ENABLE_RICH_LOGGING", raising=False)
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    types = _installed_types(mcp)
    assert types[2] is LoggingMiddleware


def test_rich_when_rich_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("FASTMCP_ENABLE_RICH_LOGGING", "true")
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    types = _installed_types(mcp)
    assert types[2] is LoggingMiddleware


def _error_mw(mcp: FastMCP) -> ErrorHandlingMiddleware:
    return next(m for m in mcp.middleware if isinstance(m, ErrorHandlingMiddleware))


def test_include_traceback_inferred_from_debug_log_level(caplog):
    """Default (None) infers include_traceback from root logger DEBUG-enabled."""
    with caplog.at_level("DEBUG"):
        mcp = FastMCP(name="t")
        wire_middleware_stack(mcp)
    assert _error_mw(mcp).include_traceback is True


def test_include_traceback_inferred_off_when_root_above_debug():
    """Default (None) yields False when root logger sits above DEBUG."""
    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.WARNING)
    try:
        mcp = FastMCP(name="t")
        wire_middleware_stack(mcp)
        assert _error_mw(mcp).include_traceback is False
    finally:
        root.setLevel(prev)


def test_include_traceback_explicit_override():
    """Explicit include_traceback wins over log-level inference."""
    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.WARNING)
    try:
        mcp = FastMCP(name="t")
        wire_middleware_stack(mcp, include_traceback=True)
        assert _error_mw(mcp).include_traceback is True
    finally:
        root.setLevel(prev)


def test_transform_errors_default_false():
    """Default transform_errors is False (matches MV behavior)."""
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    assert _error_mw(mcp).transform_errors is False


def test_transform_errors_explicit_true():
    """transform_errors=True is honored."""
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp, transform_errors=True)
    assert _error_mw(mcp).transform_errors is True
