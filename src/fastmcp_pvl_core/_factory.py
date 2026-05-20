"""Server-factory building blocks.

Each function returns a piece of the FastMCP wiring so downstream
projects can compose a ``make_server()`` without inheriting from a
base class.

Three orthogonal helpers live here:

- :func:`build_instructions` — read-only/read-write-aware MCP instructions
  template parameterized by environment-variable prefix and a
  domain-describing sentence.
- :func:`build_event_store` — construct an MCP event store backed by
  the unified storage selected by :func:`build_kv_store`.
- :func:`compute_app_domain` — derive the MCP Apps iframe domain for CSP
  sandboxing from either an explicit override or the host portion of the
  public ``base_url``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastmcp_pvl_core._config import ServerConfig

if TYPE_CHECKING:
    from fastmcp.server.event_store import EventStore

logger = logging.getLogger(__name__)


def build_instructions(
    *,
    read_only: bool,
    env_prefix: str,
    domain_line: str,
) -> str:
    """Build the default MCP instructions string.

    The returned text concatenates three parts: a domain-describing
    sentence supplied by the caller, a boilerplate line announcing whether
    the server is read-only or read-write, and a one-line hint telling
    operators how to override these instructions via environment variable.

    Args:
        read_only: Whether write tools are hidden on this instance.
        env_prefix: Environment-variable prefix for the consuming project
            (with or without a trailing underscore — it is normalized).
            Used to construct the override env var name
            ``{prefix}_INSTRUCTIONS``.
        domain_line: A sentence describing the service's domain and
            capabilities.  Included verbatim at the top of the instructions.

    Returns:
        A single string suitable for :class:`~fastmcp.FastMCP`'s
        ``instructions`` parameter.
    """
    write_line = (
        "This instance is READ-ONLY — write tools are not available."
        if read_only
        else "This instance is READ-WRITE — write tools are available."
    )
    prefix = env_prefix.rstrip("_")
    return (
        f"{domain_line} "
        f"{write_line} "
        f"Operators: set {prefix}_INSTRUCTIONS to describe this "
        "service's domain and capabilities."
    )


def build_event_store(env_prefix: str, config: ServerConfig) -> EventStore:
    """Construct an MCP event store backed by the unified KV factory.

    Delegates backend selection to :func:`build_kv_store` with
    ``namespace="events"``. URL resolution priority (handled by the
    KV factory):

    1. ``config.kv_store_url`` (recommended)
    2. ``config.event_store_url`` (legacy override)
    3. Default: ``file://`` at the package default directory

    Args:
        env_prefix: Env-var prefix of the consuming project. Forwarded
            to :func:`build_kv_store` so future per-project defaults
            apply consistently.
        config: A :class:`~fastmcp_pvl_core.ServerConfig` whose
            ``kv_store_url`` (or legacy ``event_store_url``) field
            selects the backend.

    Returns:
        A configured :class:`~fastmcp.server.event_store.EventStore`
        whose storage is namespaced under ``"events"``.

    Raises:
        ValueError: If the URL scheme is unsupported.
        ImportError: If a backend-specific extra is required but not
            installed.
    """
    # Local imports keep package import light for downstream that do not
    # use this helper.
    from fastmcp.server.event_store import EventStore as _EventStore

    from fastmcp_pvl_core._kv_store import build_kv_store

    storage = build_kv_store(env_prefix, config, namespace="events")
    return _EventStore(storage=storage, max_events_per_stream=100, ttl=3600)


def compute_app_domain(config: ServerConfig) -> str | None:
    """Derive the MCP Apps iframe domain for CSP sandboxing.

    Priority:

    1. ``config.app_domain`` (explicit operator override)
    2. Host portion (``netloc``) of ``config.base_url``
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
    """
    if config.app_domain:
        return config.app_domain
    if config.base_url:
        parsed = urlparse(config.base_url)
        return parsed.netloc or None
    return None
