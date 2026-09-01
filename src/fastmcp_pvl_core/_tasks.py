"""Background-task backend wiring (ADR 0002 §4).

fastmcp's SEP-2663 background tasks live in the ``fastmcp-tasks``
extension package (via ``fastmcp[tasks]`` — a pvl-core **base**
dependency, so every consumer has it). Two things must happen before
the server starts: a ``TasksExtension`` must be registered (fastmcp
refuses to start a server carrying ``task=``-enabled tools without
one), and the extension's Docket backend must be selected — natively
through the ``FASTMCP_DOCKET_*`` env surface, outside pvl-core's
unified ``kv_store_url`` contract. :func:`configure_task_backend`
closes both gaps: it resolves the backend from pvl-core's own operator
config, constructs a ``TasksExtension`` with the result, and registers
it on the server.

Docket supports exactly two backends: ``memory://`` (in-process, single
process only) and ``redis://`` (distributed). It is a task queue, not an
``AsyncKeyValue`` — the kv-store backends cannot literally back it; what
this module unifies is the *operator configuration surface*.

Worker tunables (``FASTMCP_DOCKET_CONCURRENCY``, ``…_WORKER_NAME``,
``…_REDELIVERY_TIMEOUT``, ``…_RECONNECTION_DELAY``,
``…_MINIMUM_CHECK_INTERVAL``) are deliberately not wrapped: they are
worker tuning with sensible upstream defaults, read by the extension's
own settings as part of the acknowledged native ``FASTMCP_*`` axis
(ADR 0002 §4.3).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastmcp import FastMCP

from ._config import ServerConfig
from ._env import _resolve_key
from ._errors import ConfigurationError

if TYPE_CHECKING:
    from fastmcp_tasks import TasksExtension

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


def _tasks_available() -> bool:
    """Whether the tasks extension can actually run in this environment.

    Mirrors fastmcp's own activation conditions: a compatible pydocket
    (``is_docket_available``) *and* an importable ``fastmcp_tasks``. Both
    ship with pvl-core's base dependencies; the guard survives for
    stripped forks and incompatible-pydocket environments.
    """
    from fastmcp.server.dependencies import is_docket_available

    if not is_docket_available():
        return False
    # Deliberately ModuleNotFoundError, not ImportError (mirroring the
    # guard in ``_jobs/manager.py``): a *present* fastmcp_tasks that
    # fails to import is version skew and must stay loud, not be
    # misreported as "not installed".
    try:
        import fastmcp_tasks  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def _env_override(var: str) -> str | None:
    """The native env var's value, with empty/whitespace treated as unset.

    Otherwise ``FASTMCP_DOCKET_URL=""`` would suppress the kv derivation
    and ``FASTMCP_DOCKET_NAME=""`` would collapse every family server
    onto one empty queue name.
    """
    return (os.environ.get(var) or "").strip() or None


def _resolve_url_override(
    env_prefix: str, config: ServerConfig, explicit_url: str | None
) -> str | None:
    """Resolve the ``url`` constructor override for the extension.

    Implements the URL precedence rules documented on
    :func:`configure_task_backend`. ``None`` means "no override": the
    extension falls back to its own ``FASTMCP_DOCKET_*`` env defaults.
    """
    native_url = _env_override("FASTMCP_DOCKET_URL")
    if explicit_url is not None:
        # Explicit vs explicit: pvl-core's documented surface wins, and
        # the divergence is never silent. (``not in`` covers both "env
        # unset" and "env agrees".)
        if native_url not in (None, explicit_url):
            logger.warning(
                "%s and FASTMCP_DOCKET_URL are both set and disagree; "
                "using %s (the pvl-core surface). Unset one of them.",
                _resolve_key(env_prefix, "TASKS_URL"),
                _resolve_key(env_prefix, "TASKS_URL"),
            )
        return explicit_url
    if native_url is not None:
        # The native escape hatch stands; leave the env value in charge.
        return None
    # Reuse the unified kv backend when it is a Redis Docket can use.
    # Same legacy-fallback order as build_kv_store: kv_store_url,
    # then event_store_url (whose own deprecation warning is emitted
    # by build_kv_store; not duplicated here). This is a *derived
    # default*, so an explicitly-set FASTMCP_DOCKET_URL — checked
    # above — outranks it, unlike the explicit tasks_url branch.
    kv_url = config.kv_store_url or config.event_store_url
    if kv_url and urlparse(kv_url).scheme == "redis":
        return kv_url
    return None


def _resolve_name_override(env_prefix: str) -> str | None:
    """Derive the queue name unless ``FASTMCP_DOCKET_NAME`` is set."""
    if _env_override("FASTMCP_DOCKET_NAME") is None:
        return _derive_queue_name(env_prefix)
    return None


def configure_task_backend(
    mcp: FastMCP, env_prefix: str, config: ServerConfig
) -> TasksExtension | None:
    """Resolve the background-task backend and register the tasks extension.

    Constructs a ``fastmcp_tasks.TasksExtension`` with the resolved
    backend and registers it via ``mcp.add_extension(...)`` — fastmcp
    rejects duplicate extension identifiers and post-startup
    registration, so call once, before ``mcp.run(...)``. Anything not
    resolved here falls back to the extension's own ``FASTMCP_DOCKET_*``
    env defaults. Argument categories per the ``CLAUDE.md`` axis: *mcp*
    is the server under assembly, *env_prefix* is caller identity
    (parameterized, like the sibling ``build_*`` helpers), *config*
    carries operator configuration. There are no hook or shape kwargs —
    backend selection is operator config, and every naming/derivation
    decision here is pvl-core-owned shape.

    URL resolution (ADR 0002 §4.2):

    1. ``config.tasks_url`` set → validated against Docket's schemes
       (``memory://``, ``redis://``) and passed to the extension.
    2. Unset, and ``config.kv_store_url`` (or the legacy
       ``event_store_url``) has scheme ``redis://`` → that same URL is
       reused, so one ``<PREFIX>_KV_STORE_URL=redis://…`` configures
       every stateful subsystem *and* the task queue. This is a derived
       *default*: an explicitly-set ``FASTMCP_DOCKET_URL`` outranks it
       (only an explicit ``tasks_url`` overrides an explicit native var).
    3. Otherwise no URL is passed: the extension's own default applies
       (``FASTMCP_DOCKET_URL`` when set, ``memory://`` otherwise), so a
       directly-set native var keeps working as the escape hatch.

    When both ``<PREFIX>_TASKS_URL`` and ``FASTMCP_DOCKET_URL`` are set
    and disagree, pvl-core's surface wins and a warning names both vars.

    Native-var precedence is read from the **process environment** only.
    ``DocketSettings`` additionally loads an optional dotenv file
    (``FASTMCP_ENV_FILE``, default ``.env``); a ``FASTMCP_DOCKET_*``
    value supplied only there is invisible to these checks and is
    overridden by pvl-core's derived URL and queue name.

    The Docket queue *name* is always derived from *env_prefix* (see
    :func:`_derive_queue_name`) unless the operator explicitly set
    ``FASTMCP_DOCKET_NAME``, which is respected as the native escape
    hatch — pvl-core exposes no variable of its own for it.

    ``fastmcp-tasks`` and pydocket ship with pvl-core's base
    dependencies, so a compatible install always has them; the
    availability guard survives for stripped forks and
    incompatible-pydocket environments, mirroring fastmcp's own
    activation conditions. In that degenerate case the helper registers
    nothing and returns ``None`` (debug log) — except that an explicitly
    set ``tasks_url`` is still validated (an operator typo must fail
    fast regardless) and, when valid, dropped with a warning rather
    than silently.

    Args:
        mcp: The server under assembly; the extension is registered on
            it. Must not have started yet, and must be the root server
            you ``run()`` — a mounted child's extensions do not
            propagate to the root, so registering on a to-be-mounted
            server leaves its task tools without a running extension.
        env_prefix: Env-var prefix of the consuming project (trailing
            underscore optional). Names the vars in diagnostics and
            derives the queue name.
        config: Universal server configuration; ``tasks_url``,
            ``kv_store_url``/``event_store_url`` and ``transport`` are
            consulted.

    Returns:
        The registered ``TasksExtension``, or ``None`` when the tasks
        machinery is unavailable and nothing was registered.

    Raises:
        ConfigurationError: If ``config.tasks_url`` is set to a URL whose
            scheme Docket does not support.
    """
    # Validate the explicit operator value before anything can short-
    # circuit: a typo in <PREFIX>_TASKS_URL must fail fast even when
    # the tasks machinery is missing, matching the strict PORT precedent.
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

    if not _tasks_available():
        if url is not None:
            logger.warning(
                "%s is set but the tasks machinery is unavailable "
                "(fastmcp-tasks or a compatible pydocket is not "
                "installed); no tasks extension registered. Both ship "
                "with fastmcp-pvl-core's base dependencies — this "
                "environment is missing them or pins an incompatible "
                "version.",
                _resolve_key(env_prefix, "TASKS_URL"),
            )
        else:
            logger.debug(
                "tasks extension not registered: fastmcp-tasks/pydocket not installed"
            )
        return None

    url = _resolve_url_override(env_prefix, config, url)
    name = _resolve_name_override(env_prefix)

    from fastmcp_tasks import TasksExtension

    # ``None`` kwargs fall back to the extension's FASTMCP_DOCKET_* env
    # defaults, which is exactly the escape-hatch semantics above.
    extension = TasksExtension(url=url, name=name)
    mcp.add_extension(extension)

    effective = extension.docket_settings
    effective_scheme = urlparse(effective.url).scheme
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
            effective.name,
        )
    return extension
