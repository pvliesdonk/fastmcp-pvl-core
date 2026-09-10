"""Tests for RequestLoggingMiddleware."""

from __future__ import annotations

import json
import logging
import re

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


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")],
)
async def test_render_value_escapes_control_chars_to_one_line(caplog, raw, escaped):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    err = ValueError("line one" + raw + "line two")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(err))
    msg = caplog.records[-1].getMessage()
    assert raw not in msg
    assert 'error="line one' + escaped + 'line two"' in msg


# --- trace correlation (#319) -------------------------------------------------


def _records(caplog):
    """Only the middleware's own records.

    ``caplog`` installs its handler on the root logger, so a sibling
    logger's record would otherwise shift the indices below — turning an
    assertion failure into a confusing ``AttributeError``.
    """
    return [r for r in caplog.records if r.name == _LOGGER_NAME]


def _tracer():
    """A tracer backed by a real SDK provider, without touching global state.

    ``trace.set_tracer_provider`` is set-once per process, so tests use a
    local provider; ``start_as_current_span`` still attaches to the ambient
    context, which is what the middleware reads.
    """
    from opentelemetry.sdk.trace import TracerProvider

    return TracerProvider().get_tracer("test")


async def test_tool_call_lines_carry_trace_and_span_ids(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with _tracer().start_as_current_span("outer"):
            await mw.on_message(ctx, _ok_call_next)

    started = _records(caplog)[0].getMessage()
    assert re.search(r"\btrace_id=[0-9a-f]{32}\b", started), started
    assert re.search(r"\bspan_id=[0-9a-f]{16}\b", started), started


async def test_started_and_completed_share_the_same_trace(caplog):
    """The whole point: the pair must be joinable to one trace."""
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with _tracer().start_as_current_span("outer"):
            await mw.on_message(ctx, _ok_call_next)

    ids = [
        re.search(r"trace_id=([0-9a-f]{32})", r.getMessage()).group(1)
        for r in _records(caplog)
    ]
    assert len(ids) == 2
    assert ids[0] == ids[1]


async def test_structured_mode_carries_trace_ids_as_json_keys(caplog):
    mw = RequestLoggingMiddleware(structured=True)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with _tracer().start_as_current_span("outer"):
            await mw.on_message(ctx, _ok_call_next)

    payload = json.loads(_records(caplog)[0].getMessage())
    assert re.fullmatch(r"[0-9a-f]{32}", payload["trace_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", payload["span_id"])


async def test_failed_line_carries_trace_ids(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with _tracer().start_as_current_span("outer"):
            with pytest.raises(ValueError):
                await mw.on_message(ctx, _failing_call_next(ValueError("boom")))

    failed = _records(caplog)[-1].getMessage()
    assert failed.startswith("tool_call_failed ")
    assert re.search(r"\btrace_id=[0-9a-f]{32}\b", failed), failed


async def test_no_trace_fields_when_no_span_is_active(caplog):
    """Servers without tracing must see byte-identical output to before."""
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)

    for record in _records(caplog):
        assert "trace_id=" not in record.getMessage()
        assert "span_id=" not in record.getMessage()


async def test_no_trace_fields_in_structured_mode_without_span(caplog):
    mw = RequestLoggingMiddleware(structured=True)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)

    payload = json.loads(_records(caplog)[0].getMessage())
    assert "trace_id" not in payload
    assert "span_id" not in payload


async def test_inbound_traceparent_correlates_without_an_sdk(caplog):
    """A propagated trace must correlate even with no SDK configured.

    FastMCP extracts ``traceparent`` from request ``_meta`` without
    gating on an SDK, so "no SDK" does not imply "no trace ids".
    """
    from opentelemetry import context as otel_context
    from opentelemetry import propagate

    carrier = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
    token = otel_context.attach(propagate.extract(carrier))
    try:
        mw = RequestLoggingMiddleware()
        ctx = _context(method="tools/call", message=_ToolParams("read"))
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            await mw.on_message(ctx, _ok_call_next)
    finally:
        otel_context.detach(token)

    started = _records(caplog)[0].getMessage()
    assert "trace_id=4bf92f3577b34da6a3ce929d0e0e4736" in started
    assert "span_id=00f067aa0ba902b7" in started
