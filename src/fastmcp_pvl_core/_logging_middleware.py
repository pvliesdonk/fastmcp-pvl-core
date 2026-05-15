"""Conforming, tool-aware request-logging middleware.

Emits family-standard log lines for every MCP message: a bare
snake_case event name as the first token, followed by ``key=value``
pairs. Tool calls surface the tool name via ``tool=<name>``; request
duration is carried inline on the terminal (``*_completed`` /
``*_failed``) line. This middleware replaces FastMCP's
``LoggingMiddleware`` and ``TimingMiddleware`` in
:func:`fastmcp_pvl_core.wire_middleware_stack`.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

_DEFAULT_LOGGER_NAME = "fastmcp.middleware.requests"


def _duration_ms(start: float) -> float:
    """Elapsed wall-clock milliseconds since *start*, rounded to 2 dp."""
    return round((time.perf_counter() - start) * 1000, 2)


def _render_value(value: object) -> str:
    """Render a field value for the rich (text) output mode.

    Strings containing whitespace or a double quote are wrapped in
    double quotes — with embedded backslashes, double quotes, and
    control characters (newline, carriage return, tab) escaped — so the
    record stays on one unambiguous ``key=value`` line; everything else
    renders bare.
    """
    text = str(value)
    if any(char.isspace() for char in text) or '"' in text:
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return '"' + escaped + '"'
    return text


def _render_fields(fields: dict[str, object]) -> str:
    """Join an ordered field mapping into ``key=value`` text."""
    return " ".join(key + "=" + _render_value(value) for key, value in fields.items())


class RequestLoggingMiddleware(Middleware):
    """Logs every MCP message as a conforming, tool-aware event pair.

    Overrides only :meth:`on_message` — the single outermost dispatch
    hook — so each message produces exactly one ``*_started`` line and
    one terminal (``*_completed`` / ``*_failed``) line. Overriding a
    method-specific hook in addition would double-log.

    Tool calls (``tools/call``) use the ``tool_call_*`` event vocabulary
    and carry ``tool=<name>``; every other message uses ``request_*`` or
    ``notification_*`` keyed by ``method=``.

    Args:
        structured: When ``True``, emit one JSON object per record (for
            log aggregators). When ``False`` (default), emit
            bare-event-name-first ``key=value`` text.
        include_traceback: When ``True``, ``*_failed`` records carry
            ``exc_info`` so the log handler renders a traceback.
        logger: Logger to emit through. Defaults to a logger named
            ``fastmcp.middleware.requests``.
    """

    def __init__(
        self,
        *,
        structured: bool = False,
        include_traceback: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.structured = structured
        self.include_traceback = include_traceback
        self.logger = logger or logging.getLogger(_DEFAULT_LOGGER_NAME)

    async def on_message(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        is_tool_call = context.method == "tools/call"
        if is_tool_call:
            event_base = "tool_call"
            id_key = "tool"
            id_val: str = getattr(context.message, "name", None) or "unknown"
        else:
            event_base = context.type
            id_key = "method"
            id_val = context.method or "unknown"

        started_fields: dict[str, object] = {id_key: id_val}
        if is_tool_call:
            started_fields["method"] = "tools/call"
        started_fields["source"] = context.source
        self._emit(event_base + "_started", started_fields, logging.INFO)

        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            self._emit(
                event_base + "_failed",
                {
                    id_key: id_val,
                    "duration_ms": _duration_ms(start),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                logging.ERROR,
                exc_info=self.include_traceback,
            )
            raise
        self._emit(
            event_base + "_completed",
            {id_key: id_val, "duration_ms": _duration_ms(start)},
            logging.INFO,
        )
        return result

    def _emit(
        self,
        event: str,
        fields: dict[str, object],
        level: int,
        *,
        exc_info: bool = False,
    ) -> None:
        """Emit one conforming record in the configured output mode.

        ``exc_info=True`` attaches the current exception triple; ``False``
        (default) passes ``None`` to logging so the record's ``exc_info``
        attribute is ``None`` rather than ``False``.
        """
        # logging stores exc_info=False as False on the record, which breaks
        # ``assert record.exc_info is None`` assertions.  Always pass None for
        # the "no traceback" case; logging treats None identically to False.
        effective_exc_info = sys.exc_info() if exc_info else None
        if self.structured:
            payload: dict[str, object] = {"event": event}
            payload.update(fields)
            self.logger.log(
                level, "%s", json.dumps(payload), exc_info=effective_exc_info
            )
        else:
            self.logger.log(
                level,
                "%s %s",
                event,
                _render_fields(fields),
                exc_info=effective_exc_info,
            )
