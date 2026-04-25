"""Logging setup — delegates to FastMCP's ``configure_logging``.

The ``-v`` CLI flag forces ``DEBUG``; otherwise ``FASTMCP_LOG_LEVEL``
wins; otherwise ``INFO``.

This module also exposes :class:`SecretMaskFilter`, a reusable
``logging.Filter`` that redacts ``Authorization: Bearer/Token`` values
in formatted log messages before they reach handlers.
"""

from __future__ import annotations

import logging
import os
import re

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


class SecretMaskFilter(logging.Filter):
    """Redact ``Authorization: Bearer/Token/Basic`` values in log records.

    Attach to a logger to mask secret credentials in formatted messages
    before they reach handlers — typically wired up by HTTP-client
    modules that log request/response details at ``DEBUG`` level::

        import logging
        from fastmcp_pvl_core import SecretMaskFilter

        logger = logging.getLogger(__name__)
        logger.addFilter(SecretMaskFilter())

    Matches both header-style (``Authorization: Bearer xyz``) and
    dict-repr (``'Authorization': 'Token xyz'``) representations,
    case-insensitive on the ``Authorization`` keyword and the
    ``Bearer`` / ``Token`` / ``Basic`` scheme name. The scheme name's
    original casing is preserved in the redacted output (e.g.
    ``bearer ***``). Records with no match pass through unchanged.

    The filter never suppresses records — it always returns ``True``.
    """

    # ``Authorization`` is the only keyword we recognise — other custom
    # auth headers (e.g. ``X-Api-Key``) are out of scope and need their
    # own filter. ``[^\s'\"]+`` stops the secret capture at whitespace
    # or quote, which preserves the surrounding dict structure.
    _PATTERN = re.compile(
        r"(Authorization['\"]?\s*[:=]\s*['\"]?)(Token|Bearer|Basic)\s+[^\s'\"]+",
        re.IGNORECASE,
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            original = record.getMessage()
        except Exception:
            # A broken format string upstream must not silence the whole
            # log stream; let the producer's TypeError surface elsewhere.
            return True
        masked = self._PATTERN.sub(r"\1\2 ***", original)
        if masked != original:
            # Replace the formatted message and clear args so subsequent
            # ``getMessage()`` calls return the masked text rather than
            # re-expanding the original args.
            record.msg = masked
            record.args = ()
        return True
