"""Server-factory building blocks.

Each function returns a piece of the FastMCP wiring so downstream
projects can compose a ``make_server()`` without inheriting from a
base class.

Three orthogonal helpers live here:

- :func:`build_instructions` — read-only/read-write-aware MCP instructions
  template parameterized by environment-variable prefix and a
  domain-describing sentence.
- :func:`build_event_store` — construct an MCP event store (in-memory or
  file-tree-backed) from a :class:`~fastmcp_pvl_core.ServerConfig`.
- :func:`compute_app_domain` — derive the MCP Apps iframe domain for CSP
  sandboxing from either an explicit override or the host portion of the
  public ``base_url``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastmcp_pvl_core._config import ServerConfig

if TYPE_CHECKING:
    from fastmcp.server.event_store import EventStore

logger = logging.getLogger(__name__)


_DEFAULT_EVENT_STORE_DIR = "/data/state/events"
"""Fallback directory for the file-tree event store when no URL is given.

Downstream Docker images typically mount a persistent volume at
``/data/state``.  Tests monkey-patch this module attribute to redirect the
default to a tmp path rather than touching the real ``/data/state``.
"""


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
    """Construct an MCP event store based on ``config.event_store_url``.

    Parses the URL scheme to select a backend:

    - ``None`` or empty → file-tree store at :data:`_DEFAULT_EVENT_STORE_DIR`
    - ``file:///path`` → file-tree store at the given path
    - ``memory://`` → in-memory (lost on restart, for development)

    Args:
        env_prefix: Env-var prefix of the consuming project.  Currently
            unused but reserved for future per-project defaults (e.g.
            ``{prefix}_EVENT_STORE_DIR``).
        config: A :class:`~fastmcp_pvl_core.ServerConfig` whose
            ``event_store_url`` field selects the backend.

    Returns:
        A configured :class:`~fastmcp.server.event_store.EventStore`.

    Raises:
        ValueError: If the URL scheme is neither ``file`` nor ``memory``.
        ImportError: If the file-tree backend is requested but
            ``key_value.aio.stores.filetree`` is not installed.
    """
    # Local imports so importing ``fastmcp_pvl_core`` stays light.
    from fastmcp.server.event_store import EventStore as _EventStore

    del env_prefix  # currently unused; reserved for future per-project config

    url = config.event_store_url
    if not url:
        url = f"file://{_DEFAULT_EVENT_STORE_DIR}"

    parsed = urlparse(url)

    if parsed.scheme == "memory":
        logger.info("event_store backend=memory lost_on_restart=true")
        return _EventStore(max_events_per_stream=100, ttl=3600)

    if parsed.scheme == "file":
        directory = parsed.path or _DEFAULT_EVENT_STORE_DIR
        Path(directory).mkdir(parents=True, exist_ok=True)
        logger.info("event_store backend=file directory=%s", directory)

        try:
            from key_value.aio.stores.filetree import FileTreeStore
        except ImportError as exc:
            raise ImportError(
                "FileTreeStore requires fastmcp>=3.0 with key-value support. "
                "Install the optional key-value dependency (e.g. "
                "'pip install \"fastmcp[key-value]\"') or switch to "
                "EVENT_STORE_URL='memory://'."
            ) from exc

        storage = FileTreeStore(data_directory=directory)
        return _EventStore(storage=storage, max_events_per_stream=100, ttl=3600)

    raise ValueError(
        f"Unsupported event store URL scheme {parsed.scheme!r}. "
        "Use 'file:///path' or 'memory://'."
    )


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
