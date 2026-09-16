"""Logging setup — delegates to FastMCP's ``configure_logging``.

The ``-v`` CLI flag forces ``DEBUG``; otherwise ``<PREFIX>_LOG_LEVEL``
wins, with the legacy ``FASTMCP_LOG_LEVEL`` honoured as a deprecated
fallback for one release; otherwise ``INFO``.

Three mechanisms keep the operator stream readable at both ends:
``_NOISY_THIRD_PARTY_LOGGERS`` (loud at ``INFO``, demoted),
``_DEBUG_FLOOD_LOGGERS`` (loud at ``DEBUG``, capped), and
``_AccessLogFilter`` (``uvicorn.access``, pinned at ``INFO`` and filtered
down to failures only, since a level cannot express "failures only").

This module also exposes :class:`SecretMaskFilter`, a reusable
``logging.Filter`` that redacts ``Authorization: Bearer/Token/Basic``
values in formatted log messages before they reach handlers.
"""

from __future__ import annotations

import logging
import os
import re
import sys

import fastmcp
from rich.console import Console
from rich.logging import RichHandler

from ._env import env

logger = logging.getLogger(__name__)

_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

_NOISY_THIRD_PARTY_LOGGERS = (
    "mcp.server.lowlevel.server",
    "httpx",
    "httpcore",
)
"""Loggers that are loud at ``INFO`` and quiet at ``WARNING``.

``httpx``/``httpcore`` are here because pvl-core pulls them for the whole
family through fastmcp, exactly like ``docket.worker`` below — the same
position, so the same owner. Three downstream servers had each quieted them
locally, two of them by contradictory rules that fought inside one process.

``uvicorn.access`` is deliberately absent: a level cannot express "failures
only", so it gets a filter instead. ``uvicorn.error`` is also absent: it
carries genuine bind / startup failures and is never demoted.
"""

# Third-party loggers with the opposite shape: near-silent at INFO, but a
# per-iteration firehose at DEBUG driven by a poll loop that runs whether or
# not there is any work. ``docket.worker`` (pydocket, which every consumer
# inherits through the ``fastmcp[tasks]`` base dependency) logs two records
# per poll at its 250 ms default check interval — ~2500 lines/minute on a
# permanently idle queue, which buries every first-party DEBUG line. Capped
# at INFO when the root level is DEBUG, so the worker's own lifecycle records
# still come through while the poll trace does not.
_DEBUG_FLOOD_LOGGERS = ("docket.worker",)

_ACCESS_LOGGER = "uvicorn.access"

_QUERY_RE = re.compile(r"[?#]")
_TRANSFER_TOKEN_RE = re.compile(r"(^|/)transfer/[^/]+")

_ACCESS_ARG_COUNT = 5
_ACCESS_STATUS_INDEX = 4
_ACCESS_PATH_INDEX = 2


class _AccessLogFilter(logging.Filter):
    """Keep failing requests only, and never log a credential.

    uvicorn emits every access record at ``INFO`` regardless of status, so
    at the default level the successful ones are pure duplication: the
    request-logging middleware already reports each MCP call with its method
    and duration. What the middleware cannot report is what never reached it
    — a 401 refused by auth, a 404, a 413 — so those stay, and a readiness
    probe becomes visible exactly when it starts failing.

    Both redactions are about credentials in the request line, which uvicorn
    logs as ``path?query``:

    * the query string goes entirely. No route in this family carries
      diagnostic query parameters; the ones that carry anything are the
      OAuth routes, where it is an authorization code or PKCE material.
    * the segment after ``/transfer/`` is masked, because the transfer token
      is in the *path* — an expired link produces exactly the 4xx this
      filter keeps.

    A record of any other shape passes untouched: this filter judges
    uvicorn's access line, and anything else on that logger is not its
    business.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != _ACCESS_ARG_COUNT:
            return True
        status = args[_ACCESS_STATUS_INDEX]
        if not isinstance(status, int):
            return True
        if status < 400:
            return False
        path = _QUERY_RE.split(str(args[_ACCESS_PATH_INDEX]), maxsplit=1)[0]
        path = _TRANSFER_TOKEN_RE.sub(r"\1transfer/<redacted>", path)
        record.args = (
            args[:_ACCESS_PATH_INDEX] + (path,) + args[_ACCESS_PATH_INDEX + 1 :]
        )
        return True


def _apply_access_policy(level: int) -> None:
    """Install or remove the access filter, leaving exactly one either way."""
    access = logging.getLogger(_ACCESS_LOGGER)
    for existing in [f for f in access.filters if isinstance(f, _AccessLogFilter)]:
        access.removeFilter(existing)
    # Pinned, not demoted: uvicorn's own dictConfig sets this logger to INFO
    # at server start, so anything else here would mean the policy depended
    # on whether that had run yet.
    access.setLevel(logging.INFO)
    if level != logging.DEBUG:
        access.addFilter(_AccessLogFilter())


_OWNED_ATTR = "_pvl_core_owned"
"""Marks the handlers this module installed.

Idempotence needs it: a ``RichHandler`` is not a ``StreamHandler`` and
exposes no ``.stream``, so the console rule below cannot recognise our own
Rich pair on a second call. Marking is what makes repeated configuration —
and a mode change in a later PR — leave exactly one chain at root.
"""


def _console_stream_ids() -> set[int]:
    """Identity of every stream that means "the operator's console"."""
    streams = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    return {id(stream) for stream in streams if stream is not None}


def _is_console_handler(handler: logging.Handler) -> bool:
    """Whether *handler* writes to the console.

    Keys on stream identity, never on handler type. pytest's
    ``LogCaptureHandler`` is a ``StreamHandler`` subclass over a
    ``StringIO``; a type-based rule would detach ``caplog`` from every test
    in the suite. ``RichHandler`` is the mirror case — not a
    ``StreamHandler`` at all, with its stream at ``console.file``.
    """
    stream = getattr(handler, "stream", None)
    if stream is None:
        stream = getattr(getattr(handler, "console", None), "file", None)
    return stream is not None and id(stream) in _console_stream_ids()


def _neutralise_fastmcp() -> None:
    """Stop FastMCP owning its own logger tree.

    ``log_enabled = False`` makes its ``configure_logging`` return before it
    touches handlers or ``propagate`` — including the call inside
    ``temporary_log_level``, which is what silently reverted the first
    attempt at this (issue #323). Turning the library off is durable where
    fighting it was not.

    The level reset is load-bearing rather than tidiness: FastMCP pins the
    ``fastmcp`` logger to ``INFO`` at import time, and left in place that
    blocks every ``fastmcp.*`` DEBUG record — the request middleware's
    included — before it can reach root.
    """
    fastmcp.settings.log_enabled = False
    fastmcp_logger = logging.getLogger("fastmcp")
    for handler in fastmcp_logger.handlers[:]:
        fastmcp_logger.removeHandler(handler)
    fastmcp_logger.propagate = True
    fastmcp_logger.setLevel(logging.NOTSET)


def _install_root_handlers(level: int) -> None:
    """Make pvl-core the sole owner of the root logger's console output.

    Removes our own handlers and any pre-existing console handler — the
    double-render source when ``opentelemetry-instrument`` has installed
    one — and leaves every other handler untouched, so an operator's OTLP,
    file or syslog handler survives. That tolerance is what closes #323:
    with ``fastmcp.*`` propagating again, a handler attached at root now
    receives every record in the process.

    Two handlers, not one, reproducing the pair FastMCP installs: tracebacks
    render compressed (no path or level column, framework frames suppressed,
    three frames) so a stack trace does not drown the line that caused it.
    """
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if getattr(handler, _OWNED_ATTR, False) or _is_console_handler(handler):
            root.removeHandler(handler)

    console = Console(stderr=True)
    formatter = logging.Formatter("%(message)s")

    main = RichHandler(console=console)
    main.setFormatter(formatter)
    main.addFilter(lambda record: record.exc_info is None)

    tracebacks = RichHandler(
        console=console,
        show_path=False,
        show_level=False,
        rich_tracebacks=True,
        tracebacks_max_frames=3,
    )
    tracebacks.setFormatter(formatter)
    tracebacks.addFilter(lambda record: record.exc_info is not None)

    for handler in (main, tracebacks):
        setattr(handler, _OWNED_ATTR, True)
        root.addHandler(handler)
    root.setLevel(level)


def _resolve_level(env_prefix: str, *, verbose: bool) -> tuple[int, bool]:
    """Resolve the level, and say whether the legacy variable supplied it.

    Order: ``verbose`` wins, then ``{PREFIX}_LOG_LEVEL``, then the legacy
    ``FASTMCP_LOG_LEVEL``, then ``INFO``. An unrecognised name falls back to
    ``INFO`` rather than raising — a typo in a log level should not stop a
    server from starting.
    """
    if verbose:
        return logging.DEBUG, False

    raw = env(env_prefix, "LOG_LEVEL")
    legacy = os.environ.get("FASTMCP_LOG_LEVEL")
    bridged = raw is None and legacy is not None
    name = (raw if raw is not None else legacy or "INFO").strip().upper()
    if name not in _VALID_LEVELS:
        name = "INFO"
    return getattr(logging, name, logging.INFO), bridged


def configure_logging_from_env(env_prefix: str, *, verbose: bool = False) -> None:
    """Configure logging globally based on environment and verbose flag.

    Level resolution order:

    1. If *verbose* is ``True``: force ``DEBUG``.
    2. Otherwise, use ``{env_prefix}_LOG_LEVEL`` if set (case-insensitive).
    3. Otherwise, fall back to the legacy ``FASTMCP_LOG_LEVEL`` if set —
       deprecated for one release; a single warning names the replacement.
    4. Otherwise, default to ``INFO``.

    Unknown level names fall back to ``INFO``. pvl-core owns the root
    logger outright: FastMCP's own ``configure_logging`` is neutralised
    and a single Rich handler pair is installed at root instead, so every
    namespace in the process — FastMCP's included — renders through one
    handler chain.

    Three noisy third-party loggers — ``mcp.server.lowlevel.server`` (the MCP
    SDK request line), ``httpx``, and ``httpcore`` — are demoted to
    ``WARNING`` whenever the resolved level is above ``DEBUG``, so their
    per-request chatter stays out of the default ``INFO`` stream. At
    ``DEBUG`` they are reset to ``NOTSET`` and reappear. ``uvicorn.error``
    is never demoted.

    ``uvicorn.access`` (the HTTP access log) is handled differently: a
    level cannot express "failures only", so instead it is pinned to
    ``INFO`` and, whenever the resolved level is above ``DEBUG``, given a
    filter that keeps failing requests only and redacts the query string
    and any ``/transfer/<token>`` segment from the ones it keeps. At
    ``DEBUG`` the filter is removed and every request line — success or
    failure — passes through.

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
        env_prefix: The server's own env var prefix (e.g. ``"MY_APP"``);
            used to read ``{env_prefix}_LOG_LEVEL``. Required.
        verbose: If ``True``, force ``DEBUG`` (overrides both
            ``{env_prefix}_LOG_LEVEL`` and ``FASTMCP_LOG_LEVEL``).
    """
    level, bridged = _resolve_level(env_prefix, verbose=verbose)
    _neutralise_fastmcp()
    _install_root_handlers(level)

    noisy_level = logging.NOTSET if level == logging.DEBUG else logging.WARNING
    for name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)

    # Mirror image of the demotion above: pin at INFO exactly where the root
    # would otherwise let the poll trace through, and hand the logger back to
    # inheritance everywhere else so repeated calls leave no residue.
    flood_level = logging.INFO if level == logging.DEBUG else logging.NOTSET
    for name in _DEBUG_FLOOD_LOGGERS:
        logging.getLogger(name).setLevel(flood_level)

    _apply_access_policy(level)

    if bridged:
        # Emitted last, deliberately: the handlers that carry it are
        # installed above. A deprecation notice nobody can see is worse
        # than none, because it reads as if the migration were silent.
        logger.warning(
            "log_level_env_deprecated old=FASTMCP_LOG_LEVEL new=%s_LOG_LEVEL",
            env_prefix.rstrip("_"),
        )


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
