"""FastMCP middleware stack installation.

Installs the conforming request-logging middleware on a FastMCP
instance. The rich-vs-structured output mode is controlled by the
``FASTMCP_ENABLE_RICH_LOGGING`` environment variable.
"""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

from ._env import parse_bool
from ._logging_middleware import RequestLoggingMiddleware


def wire_middleware_stack(mcp: FastMCP) -> None:
    """Install the standard logging middleware on a FastMCP instance.

    Installs a single :class:`RequestLoggingMiddleware`, which emits
    family-conforming, tool-aware log lines — a bare event name as the
    first token, then ``key=value`` pairs — with request timing carried
    inline on the terminal line.

    Output mode is selected by ``FASTMCP_ENABLE_RICH_LOGGING`` (default
    ``true``): rich mode emits ``key=value`` text; structured mode emits
    one JSON object per record for log aggregators.

    Traceback inclusion on failure records is inferred from the root
    logger — tracebacks are emitted when it is at ``DEBUG`` or below.
    Call this *after* CLI / log-level setup so the inference sees the
    right level.

    Args:
        mcp: The :class:`FastMCP` instance to install middleware on.
    """
    include_traceback = logging.getLogger().isEnabledFor(logging.DEBUG)
    rich_raw = os.environ.get("FASTMCP_ENABLE_RICH_LOGGING", "true")
    mcp.add_middleware(
        RequestLoggingMiddleware(
            structured=not parse_bool(rich_raw),
            include_traceback=include_traceback,
        )
    )
