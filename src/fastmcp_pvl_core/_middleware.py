"""FastMCP middleware stack installation.

Installs the standard three-middleware stack on a FastMCP instance:
ErrorHandling, Timing, then either rich or structured Logging.
The logging flavor is controlled by ``FASTMCP_ENABLE_RICH_LOGGING``.
"""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import (
    LoggingMiddleware,
    StructuredLoggingMiddleware,
)
from fastmcp.server.middleware.timing import TimingMiddleware

from fastmcp_pvl_core._env import parse_bool


def wire_middleware_stack(
    mcp: FastMCP,
    *,
    include_traceback: bool | None = None,
    transform_errors: bool = False,
) -> None:
    """Install the standard middleware stack on a FastMCP instance.

    Installation order (outermost first):

    1. :class:`ErrorHandlingMiddleware` — catches unhandled exceptions and
       (optionally) logs them with a traceback.
    2. :class:`TimingMiddleware` — records tool invocation duration.
    3. :class:`LoggingMiddleware` (rich) or
       :class:`StructuredLoggingMiddleware` (JSON) — selected by the
       ``FASTMCP_ENABLE_RICH_LOGGING`` environment variable (default: rich).

    Args:
        mcp: The :class:`FastMCP` instance to install middleware on.
        include_traceback: Whether ``ErrorHandlingMiddleware`` should log
            tracebacks for unhandled exceptions. ``None`` (default) infers
            from the root logger: tracebacks are enabled when the root
            logger is at ``DEBUG`` or below. Pass ``True``/``False`` to
            override. Call this *after* CLI/log-level setup so inference
            sees the right level.
        transform_errors: Whether ``ErrorHandlingMiddleware`` should
            convert exceptions into MCP error responses. Defaults to
            ``False`` (let exceptions propagate to FastMCP's own handlers).
    """
    if include_traceback is None:
        include_traceback = logging.getLogger().isEnabledFor(logging.DEBUG)
    mcp.add_middleware(
        ErrorHandlingMiddleware(
            include_traceback=include_traceback,
            transform_errors=transform_errors,
        )
    )
    mcp.add_middleware(TimingMiddleware())

    rich_raw = os.environ.get("FASTMCP_ENABLE_RICH_LOGGING", "true")
    if parse_bool(rich_raw):
        mcp.add_middleware(LoggingMiddleware())
    else:
        mcp.add_middleware(StructuredLoggingMiddleware())
