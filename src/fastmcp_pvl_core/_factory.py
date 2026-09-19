"""Server-factory building blocks.

Each function returns a piece of the FastMCP wiring so downstream
projects can compose a ``make_server()`` without inheriting from a
base class.

Two orthogonal helpers live here:

- :func:`build_event_store` — construct an MCP event store backed by
  the unified storage selected by :func:`build_kv_store`.
- :func:`compute_app_domain` — derive the MCP Apps iframe domain for CSP
  sandboxing from either an explicit override or the host portion of the
  public ``base_url``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._config import ServerConfig
from ._url import parse_operator_url, safe_netloc

if TYPE_CHECKING:
    from fastmcp.server.event_store import EventStore


def build_event_store(env_prefix: str, config: ServerConfig) -> EventStore:
    """Construct an MCP event store backed by the unified KV factory.

    Delegates backend selection to :func:`build_kv_store` with
    ``namespace="events"``. URL resolution priority (handled by the
    KV factory):

    1. ``config.kv_store_url`` (recommended; driven by
       ``<PREFIX>_KV_STORE_URL``)
    2. ``config.event_store_url`` (legacy override; driven by
       ``<PREFIX>_EVENT_STORE_URL``)
    3. Default: ``file://`` at the package default directory

    Args:
        env_prefix: Env-var prefix of the consuming project. Accepted
            for API compatibility with sibling ``build_*`` helpers;
            currently unused inside this function.
        config: A :class:`~fastmcp_pvl_core.ServerConfig` whose
            ``kv_store_url`` (or legacy ``event_store_url``) field
            selects the backend.

    Returns:
        A configured :class:`~fastmcp.server.event_store.EventStore`
        whose storage is namespaced under ``"events"``.

    Raises:
        ConfigurationError: If the URL scheme is unsupported, the URL
            is malformed, or importing the backend it selects fails.
            Delegated from :func:`build_kv_store`; see its ``Raises:``
            for the one failure this does not cover (#337).
    """
    # Local imports keep package import light for downstream that do not
    # use this helper.
    from fastmcp.server.event_store import EventStore as _EventStore

    from ._kv_store import build_kv_store

    del env_prefix  # accepted for API compatibility; not consumed here

    storage = build_kv_store(config, namespace="events")
    return _EventStore(storage=storage, max_events_per_stream=100, ttl=3600)


def compute_app_domain(config: ServerConfig) -> str | None:
    """Derive the MCP Apps iframe domain for CSP sandboxing.

    Priority:

    1. ``config.app_domain`` (explicit operator override)
    2. Host (and port) of ``config.base_url``, with any ``user:pass@``
       userinfo removed — this value becomes a CSP origin, so credentials
       must not travel in it (#343)
    3. ``None`` when neither is set

    Projects that need a domain-specific fallback (e.g. a hash-based
    sandbox subdomain for a specific client) should compute that value in
    their own code and either pass it as ``app_domain`` or handle the
    ``None`` return here.

    Args:
        config: Universal server configuration.

    Returns:
        The iframe domain, or ``None`` when neither override nor
        ``base_url`` host is available.

    Raises:
        ConfigurationError: ``config.base_url`` does not parse as a URL.
            The message names the variable, never its value, which may
            carry credentials.
    """
    if config.app_domain:
        return config.app_domain
    if config.base_url:
        parsed = parse_operator_url(config.base_url, variable="{PREFIX}_BASE_URL")
        return safe_netloc(parsed)
    return None
