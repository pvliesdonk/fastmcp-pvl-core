"""Logging setup — delegates to FastMCP's ``configure_logging``.

The ``-v`` CLI flag forces ``DEBUG``; otherwise ``FASTMCP_LOG_LEVEL``
wins; otherwise ``INFO``.

Two module constants keep the operator stream readable at both ends:
``_NOISY_THIRD_PARTY_LOGGERS`` (loud at ``INFO``, demoted) and
``_DEBUG_FLOOD_LOGGERS`` (loud at ``DEBUG``, capped).

This module also exposes :class:`SecretMaskFilter`, a reusable
``logging.Filter`` that redacts ``Authorization: Bearer/Token/Basic``
values in formatted log messages before they reach handlers.
"""

from __future__ import annotations

import logging
import os
import re

from fastmcp.utilities.logging import configure_logging

_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Third-party transport / SDK loggers that emit non-conforming INFO-level
# chatter — one or two lines per request. Demoted to WARNING unless the
# operator opts into DEBUG. ``uvicorn.error`` is deliberately excluded: it
# carries genuine bind / startup failures.
_NOISY_THIRD_PARTY_LOGGERS = ("uvicorn.access", "mcp.server.lowlevel.server")

# Third-party loggers with the opposite shape: near-silent at INFO, but a
# per-iteration firehose at DEBUG driven by a poll loop that runs whether or
# not there is any work. ``docket.worker`` (pydocket, which every consumer
# inherits through the ``fastmcp[tasks]`` base dependency) logs two records
# per poll at its 250 ms default check interval — ~2500 lines/minute on a
# permanently idle queue, which buries every first-party DEBUG line. Capped
# at INFO when the root level is DEBUG, so the worker's own lifecycle records
# still come through while the poll trace does not.
_DEBUG_FLOOD_LOGGERS = ("docket.worker",)


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

    Two noisy third-party loggers — ``uvicorn.access`` (the HTTP access
    log) and ``mcp.server.lowlevel.server`` (the MCP SDK request line) —
    are demoted to ``WARNING`` whenever the resolved level is above
    ``DEBUG``, so their per-request chatter stays out of the default
    ``INFO`` stream. At ``DEBUG`` they are reset to ``NOTSET`` and
    reappear. ``uvicorn.error`` is never demoted.

    One third-party logger is capped in the other direction:
    ``docket.worker`` is pinned to ``INFO`` when the resolved level is
    ``DEBUG``, because its poll loop emits a record per iteration even on
    an idle queue and would otherwise dominate the ``DEBUG`` stream. Its
    ``INFO`` lifecycle records are unaffected, and at every other level it
    is left at ``NOTSET`` — the cap only ever removes the poll trace. An
    operator debugging the task queue itself restores the full stream
    after this call with
    ``logging.getLogger("docket.worker").setLevel(logging.DEBUG)``.

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

    noisy_level = logging.NOTSET if level == logging.DEBUG else logging.WARNING
    for name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)

    # Mirror image of the demotion above: pin at INFO exactly where the root
    # would otherwise let the poll trace through, and hand the logger back to
    # inheritance everywhere else so repeated calls leave no residue.
    flood_level = logging.INFO if level == logging.DEBUG else logging.NOTSET
    for name in _DEBUG_FLOOD_LOGGERS:
        logging.getLogger(name).setLevel(flood_level)


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
