"""Background-task backend wiring (ADR 0002 §4).

fastmcp's SEP-1686 background tasks (Docket, via ``fastmcp[tasks]`` —
a pvl-core **base** dependency, so every consumer has it) select their
backend through the native ``FASTMCP_DOCKET_*`` env surface, outside
pvl-core's unified ``kv_store_url`` contract.
:func:`configure_task_backend` closes that gap: it resolves the task
backend from pvl-core's own operator config and injects the result into
``fastmcp.settings.docket`` before the server starts — fastmcp reads
those settings lazily, at root-lifespan entry, so a pre-``run()``
assignment is all the integration required.

Docket supports exactly two backends: ``memory://`` (in-process, single
process only) and ``redis://`` (distributed). It is a task queue, not an
``AsyncKeyValue`` — the kv-store backends cannot literally back it; what
this module unifies is the *operator configuration surface*.

Worker tunables (``FASTMCP_DOCKET_CONCURRENCY``, ``…_WORKER_NAME``,
``…_REDELIVERY_TIMEOUT``, ``…_RECONNECTION_DELAY``,
``…_MINIMUM_CHECK_INTERVAL``) are deliberately not wrapped: they are
worker tuning with sensible upstream defaults, part of the acknowledged
native ``FASTMCP_*`` axis (ADR 0002 §4.3).
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from ._config import ServerConfig
from ._env import _resolve_key
from ._errors import ConfigurationError

logger = logging.getLogger(__name__)

_DOCKET_SCHEMES = frozenset({"memory", "redis"})
"""URL schemes Docket accepts — the validation set for ``tasks_url``."""


def _derive_queue_name(env_prefix: str) -> str:
    """Derive the Docket queue name from the server's env-prefix identity.

    Two family servers sharing one Redis must not share a task queue by
    default, exactly as kv namespaces must not collide — so the queue name
    replaces fastmcp's global ``"fastmcp"`` default with a per-server
    identity. Parameterized on the caller's prefix (never hard-coded to
    pvl-core's own name, per the foldability rule).
    """
    return env_prefix.rstrip("_").lower().replace("_", "-")


def configure_task_backend(env_prefix: str, config: ServerConfig) -> None:
    """Resolve the background-task backend and inject it into fastmcp.

    Mutates the process-global ``fastmcp.settings.docket`` (fastmcp offers
    no constructor-level injection point); call once, before
    ``mcp.run(...)``. Argument categories per the ``CLAUDE.md`` axis:
    *env_prefix* is caller identity (parameterized, like the sibling
    ``build_*`` helpers), *config* carries operator configuration. There
    are no hook or shape kwargs — backend selection is operator config,
    and every naming/derivation decision here is pvl-core-owned shape.

    URL resolution (ADR 0002 §4.2):

    1. ``config.tasks_url`` set → validated against Docket's schemes
       (``memory://``, ``redis://``) and written to
       ``fastmcp.settings.docket.url``.
    2. Unset, and ``config.kv_store_url`` (or the legacy
       ``event_store_url``) has scheme ``redis://`` → that same URL is
       reused, so one ``<PREFIX>_KV_STORE_URL=redis://…`` configures
       every stateful subsystem *and* the task queue. This is a derived
       *default*: an explicitly-set ``FASTMCP_DOCKET_URL`` outranks it
       (only an explicit ``tasks_url`` overrides an explicit native var).
    3. Otherwise the URL is left untouched: fastmcp's own default
       (``memory://``) applies, and a directly-set ``FASTMCP_DOCKET_URL``
       keeps working as the native escape hatch.

    When both ``<PREFIX>_TASKS_URL`` and ``FASTMCP_DOCKET_URL`` are set
    and disagree, pvl-core's surface wins and a warning names both vars.

    The Docket queue *name* is always derived from *env_prefix* (see
    :func:`_derive_queue_name`) unless the operator explicitly set
    ``FASTMCP_DOCKET_NAME``, which is respected as the native escape
    hatch — pvl-core exposes no variable of its own for it.

    pydocket ships with pvl-core's base dependencies, so a compatible
    install always has it; the availability guard survives for stripped
    forks and incompatible-pydocket environments, mirroring fastmcp's own
    activation conditions. In that degenerate case the helper is a no-op
    (debug log) — except that an explicitly set ``tasks_url`` is still
    validated (an operator typo must fail fast regardless) and, when
    valid, dropped with a warning rather than silently.

    Args:
        env_prefix: Env-var prefix of the consuming project (trailing
            underscore optional). Names the vars in diagnostics and
            derives the queue name.
        config: Universal server configuration; ``tasks_url``,
            ``kv_store_url``/``event_store_url`` and ``transport`` are
            consulted.

    Raises:
        ConfigurationError: If ``config.tasks_url`` is set to a URL whose
            scheme Docket does not support.
    """
    # Validate the explicit operator value before anything can short-
    # circuit: a typo in <PREFIX>_TASKS_URL must fail fast even when
    # pydocket is missing, matching the strict PORT precedent.
    #
    # Error/log messages name the SCHEME only, never the raw URL — an
    # operator-set URL may carry credentials in userinfo (the same
    # redaction rule _kv_store.py applies).
    url: str | None = None
    if config.tasks_url:
        scheme = urlparse(config.tasks_url).scheme
        if scheme not in _DOCKET_SCHEMES:
            raise ConfigurationError(
                f"{_resolve_key(env_prefix, 'TASKS_URL')} has unsupported "
                f"scheme {scheme!r}. Docket supports 'memory://' and "
                "'redis://...' only."
            )
        url = config.tasks_url

    from fastmcp.server.dependencies import is_docket_available

    if not is_docket_available():
        if url is not None:
            logger.warning(
                "%s is set but a compatible pydocket is not installed; "
                "task backend configuration skipped. pydocket ships with "
                "fastmcp-pvl-core's base dependencies — this environment "
                "is missing it or pins an incompatible version.",
                _resolve_key(env_prefix, "TASKS_URL"),
            )
        else:
            logger.debug("task backend configuration skipped: pydocket not installed")
        return

    native_url = os.environ.get("FASTMCP_DOCKET_URL")
    if url is not None:
        # Explicit vs explicit: pvl-core's documented surface wins, and
        # the divergence is never silent.
        if native_url is not None and native_url.strip() != url:
            logger.warning(
                "%s and FASTMCP_DOCKET_URL are both set and disagree; "
                "using %s (the pvl-core surface). Unset one of them.",
                _resolve_key(env_prefix, "TASKS_URL"),
                _resolve_key(env_prefix, "TASKS_URL"),
            )
    elif native_url is None:
        # Reuse the unified kv backend when it is a Redis Docket can use.
        # Same legacy-fallback order as build_kv_store: kv_store_url,
        # then event_store_url (whose own deprecation warning is emitted
        # by build_kv_store; not duplicated here). This is a *derived
        # default*, so an explicitly-set FASTMCP_DOCKET_URL — checked
        # above — outranks it, unlike the explicit tasks_url branch.
        kv_url = config.kv_store_url or config.event_store_url
        if kv_url and urlparse(kv_url).scheme == "redis":
            url = kv_url

    import fastmcp

    if url is not None:
        fastmcp.settings.docket.url = url

    if os.environ.get("FASTMCP_DOCKET_NAME") is None:
        fastmcp.settings.docket.name = _derive_queue_name(env_prefix)

    effective_scheme = urlparse(fastmcp.settings.docket.url).scheme
    if effective_scheme == "memory" and config.transport in ("http", "sse"):
        logger.info(
            "task backend=memory process_local=true lost_on_restart=true "
            "(set %s or a redis kv_store_url for a durable, multi-process "
            "queue)",
            _resolve_key(env_prefix, "TASKS_URL"),
        )
    else:
        logger.info(
            "task backend=%s queue=%s",
            effective_scheme,
            fastmcp.settings.docket.name,
        )
