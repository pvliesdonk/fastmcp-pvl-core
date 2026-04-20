"""Logging setup — delegates to FastMCP's ``configure_logging``.

The ``-v`` CLI flag forces ``DEBUG``; otherwise ``FASTMCP_LOG_LEVEL``
wins; otherwise ``INFO``.
"""

from __future__ import annotations

import logging
import os

from fastmcp.utilities.logging import configure_logging

_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def configure_logging_from_env(*, verbose: bool = False) -> None:
    """Configure logging globally based on environment and verbose flag.

    Level resolution order:

    1. If *verbose* is ``True``: force ``DEBUG`` and also set
       ``FASTMCP_LOG_LEVEL=DEBUG`` in the environment so FastMCP's own
       loggers (which read the env var at import time) pick up the same
       level.
    2. Otherwise, use ``FASTMCP_LOG_LEVEL`` if set (case-insensitive).
    3. Otherwise, default to ``INFO``.

    Unknown level names fall back to ``INFO``.  The root logger is set
    to the resolved level and FastMCP's ``configure_logging`` is called
    so its loggers produce matching output.

    Args:
        verbose: If ``True``, force ``DEBUG`` (overrides
            ``FASTMCP_LOG_LEVEL``).
    """
    if verbose:
        os.environ["FASTMCP_LOG_LEVEL"] = "DEBUG"
        level_name = "DEBUG"
    else:
        level_name = os.environ.get("FASTMCP_LOG_LEVEL", "INFO").strip().upper()
        if level_name not in _VALID_LEVELS:
            level_name = "INFO"

    level = getattr(logging, level_name, logging.INFO)
    logging.getLogger().setLevel(level)
    configure_logging(level)
