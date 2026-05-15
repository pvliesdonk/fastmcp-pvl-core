"""Tests for RequestLoggingMiddleware."""

from __future__ import annotations

import json
import logging

import pytest
from fastmcp.server.middleware.middleware import MiddlewareContext

from fastmcp_pvl_core._logging_middleware import RequestLoggingMiddleware

_LOGGER_NAME = "fastmcp.middleware.requests"


class _ToolParams:
    """Minimal stand-in for CallToolRequestParams — only ``.name`` is read."""

    def __init__(self, name: str) -> None:
        self.name = name


def _context(*, method, message=None, type_="request", source="client"):
    return MiddlewareContext(message=message, method=method, type=type_, source=source)


async def _ok_call_next(context):
    return "result"


def _failing_call_next(exc):
    async def _call_next(context):
        raise exc

    return _call_next


async def test_tool_call_started_and_completed(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = await mw.on_message(ctx, _ok_call_next)
    assert result == "result"
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0].startswith("tool_call_started ")
    assert "tool=read" in messages[0]
    assert "method=tools/call" in messages[0]
    assert "source=client" in messages[0]
    assert messages[1].startswith("tool_call_completed ")
    assert "tool=read" in messages[1]
    assert "duration_ms=" in messages[1]


async def test_request_uses_method_vocabulary(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="initialize")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0].startswith("request_started ")
    assert "method=initialize" in messages[0]
    assert messages[1].startswith("request_completed ")
    assert "method=initialize" in messages[1]
    assert "duration_ms=" in messages[1]


async def test_notification_uses_notification_vocabulary(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="notifications/initialized", type_="notification")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)
    started = caplog.records[0].getMessage()
    assert started.startswith("notification_started ")
    assert "method=notifications/initialized" in started
    assert "source=client" in started
    assert caplog.records[1].getMessage().startswith("notification_completed ")


async def test_tool_call_failed_line(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("bad section here")))
    failed = caplog.records[-1]
    msg = failed.getMessage()
    assert msg.startswith("tool_call_failed ")
    assert "tool=read" in msg
    assert "duration_ms=" in msg
    assert "error_type=ValueError" in msg
    assert 'error="bad section here"' in msg
    assert failed.levelno == logging.ERROR


async def test_failed_error_value_unquoted_when_no_whitespace(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("oneword")))
    assert "error=oneword" in caplog.records[-1].getMessage()


async def test_include_traceback_attaches_exc_info(caplog):
    mw = RequestLoggingMiddleware(include_traceback=True)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("boom")))
    assert caplog.records[-1].exc_info is not None


async def test_no_traceback_when_include_traceback_false(caplog):
    mw = RequestLoggingMiddleware(include_traceback=False)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("boom")))
    assert caplog.records[-1].exc_info is None


async def test_unknown_tool_name_falls_back(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=object())
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)
    assert "tool=unknown" in caplog.records[0].getMessage()


async def test_structured_mode_emits_json(caplog):
    mw = RequestLoggingMiddleware(structured=True)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)
    started = json.loads(caplog.records[0].getMessage())
    assert started["event"] == "tool_call_started"
    assert started["tool"] == "read"
    assert started["method"] == "tools/call"
    assert started["source"] == "client"
    completed = json.loads(caplog.records[1].getMessage())
    assert completed["event"] == "tool_call_completed"
    assert completed["tool"] == "read"
    assert "duration_ms" in completed


async def test_structured_mode_failed_json(caplog):
    mw = RequestLoggingMiddleware(structured=True)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("bad section here")))
    failed = json.loads(caplog.records[-1].getMessage())
    assert failed["event"] == "tool_call_failed"
    assert failed["error_type"] == "ValueError"
    assert failed["error"] == "bad section here"


async def test_exception_is_reraised_unchanged(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    sentinel = ValueError("propagate me")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError) as excinfo:
            await mw.on_message(ctx, _failing_call_next(sentinel))
    assert excinfo.value is sentinel


async def test_custom_logger_is_used(caplog):
    mw = RequestLoggingMiddleware(logger=logging.getLogger("test.custom.requests"))
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger="test.custom.requests"):
        await mw.on_message(ctx, _ok_call_next)
    assert caplog.records
    assert all(record.name == "test.custom.requests" for record in caplog.records)


async def test_tool_name_with_whitespace_is_quoted(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read section"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)
    assert 'tool="read section"' in caplog.records[0].getMessage()


async def test_render_value_escapes_embedded_quotes(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError('say"hi"')))
    assert r'error="say\"hi\""' in caplog.records[-1].getMessage()
