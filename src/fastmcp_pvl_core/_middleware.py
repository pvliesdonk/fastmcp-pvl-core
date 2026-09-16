"""FastMCP middleware stack installation.

Installs the conforming request-logging middleware on a FastMCP
instance. The middleware always logs through the shared log-call
grammar; the root handler's formatter (not this module) decides
whether that renders as Rich text or JSON.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from ._logging_middleware import RequestLoggingMiddleware


def wire_middleware_stack(mcp: FastMCP) -> None:
    """Install the standard logging middleware on a FastMCP instance.

    Installs a single :class:`RequestLoggingMiddleware`, which emits
    family-conforming, tool-aware log lines — a bare event name as the
    first token, then ``key=value`` pairs — with request timing carried
    inline on the terminal line. Every record is logged through the
    shared log-call grammar; the process-wide output format (Rich text
    or JSON) is chosen once at the root handler, not here.

    Traceback inclusion on failure records is inferred from the root
    logger — tracebacks are emitted when it is at ``DEBUG`` or below.
    Call this *after* CLI / log-level setup so the inference sees the
    right level.

    Args:
        mcp: The :class:`FastMCP` instance to install middleware on.
    """
    include_traceback = logging.getLogger().isEnabledFor(logging.DEBUG)
    mcp.add_middleware(RequestLoggingMiddleware(include_traceback=include_traceback))
