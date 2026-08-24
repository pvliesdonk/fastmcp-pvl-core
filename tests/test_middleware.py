"""Tests for wire_middleware_stack."""

from __future__ import annotations

import logging

from fastmcp import Client, FastMCP

from fastmcp_pvl_core import wire_middleware_stack
from fastmcp_pvl_core._logging_middleware import RequestLoggingMiddleware


def _request_logging_mws(mcp: FastMCP) -> list[RequestLoggingMiddleware]:
    """Return the RequestLoggingMiddleware instances wire_middleware_stack added.

    FastMCP may pre-install its own middlewares (e.g. DereferenceRefsMiddleware);
    this filters to only the one wire_middleware_stack is responsible for.
    """
    return [m for m in mcp.middleware if isinstance(m, RequestLoggingMiddleware)]


def test_installs_single_request_logging_middleware():
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    assert len(_request_logging_mws(mcp)) == 1


def test_rich_mode_when_rich_unset(monkeypatch):
    monkeypatch.delenv("FASTMCP_ENABLE_RICH_LOGGING", raising=False)
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    assert _request_logging_mws(mcp)[0].structured is False


def test_rich_mode_when_rich_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("FASTMCP_ENABLE_RICH_LOGGING", "true")
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    assert _request_logging_mws(mcp)[0].structured is False


def test_structured_mode_when_rich_disabled(monkeypatch):
    monkeypatch.setenv("FASTMCP_ENABLE_RICH_LOGGING", "false")
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    assert _request_logging_mws(mcp)[0].structured is True


def test_include_traceback_inferred_from_debug_log_level(caplog):
    """include_traceback is inferred True when the root logger is at DEBUG."""
    with caplog.at_level(logging.DEBUG):
        mcp = FastMCP(name="t")
        wire_middleware_stack(mcp)
    assert _request_logging_mws(mcp)[0].include_traceback is True


def test_include_traceback_inferred_off_when_root_above_debug(caplog):
    """include_traceback is inferred False when the root logger sits above DEBUG."""
    with caplog.at_level(logging.WARNING):
        mcp = FastMCP(name="t")
        wire_middleware_stack(mcp)
    assert _request_logging_mws(mcp)[0].include_traceback is False


async def test_real_tool_dispatch_logs_conforming_tool_pair(caplog, monkeypatch):
    monkeypatch.delenv("FASTMCP_ENABLE_RICH_LOGGING", raising=False)
    mcp = FastMCP(name="t")

    @mcp.tool
    def echo(value: str) -> str:
        return value

    wire_middleware_stack(mcp)
    logger_name = "fastmcp.middleware.requests"
    with caplog.at_level(logging.INFO, logger=logger_name):
        async with Client(mcp) as client:
            await client.call_tool("echo", {"value": "hello"})

    tool_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name and record.getMessage().startswith("tool_call_")
    ]
    assert len(tool_messages) == 2
    assert tool_messages[0].startswith(
        "tool_call_started tool=echo method=tools/call source=client"
    )
    assert tool_messages[1].startswith("tool_call_completed tool=echo duration_ms=")
