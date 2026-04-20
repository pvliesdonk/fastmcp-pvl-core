"""Tests for wire_middleware_stack."""

from __future__ import annotations

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


def test_error_handling_middleware_includes_traceback():
    """Verify ErrorHandlingMiddleware is configured with include_traceback=True."""
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    error_mw = next(m for m in mcp.middleware if isinstance(m, ErrorHandlingMiddleware))
    assert error_mw.include_traceback is True
