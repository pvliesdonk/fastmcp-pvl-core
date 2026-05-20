"""Unified key-value storage factory.

One URL-driven dispatcher returning a namespaced
:class:`~key_value.aio.protocols.key_value.AsyncKeyValue` so every
pvl-core subsystem that needs persistent state — event store, OAuth
proxy client storage, future file-exchange token store — resolves to
the same operator-chosen backend with isolated keyspaces.

URL scheme dispatch:

- ``memory://`` → :class:`MemoryStore` (in-process, lost on restart;
  development default)
- ``file:///path`` → :class:`FileTreeStore` (single-server persistence)
- ``redis://host:port[/db]`` → :class:`RedisStore` (requires the
  ``redis`` extra; the URL is forwarded verbatim to the store)
- ``dynamodb://<table_name>[?region=...&endpoint=...]`` →
  :class:`DynamoDBStore` (requires the ``dynamodb`` extra)
- ``mongodb://host:port[/db]`` → :class:`MongoDBStore` (requires the
  ``mongodb`` extra; the URL is forwarded verbatim to the store)

Backend imports are lazy so memory/file deployments do not pull in
optional client libraries.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from fastmcp_pvl_core._config import ServerConfig

if TYPE_CHECKING:
    from key_value.aio.protocols.key_value import AsyncKeyValue

logger = logging.getLogger(__name__)


_DEFAULT_KV_STORE_DIR = "/data/state"
"""Fallback directory for the file-tree backend when no URL is given.

Downstream Docker images typically mount a persistent volume at
``/data/state``. Tests monkey-patch this module attribute to redirect
the default to a tmp path rather than touching the real ``/data/state``.
"""


_legacy_url_warned: bool = False
"""Process-wide flag for the legacy-URL deprecation warning.

Set to ``True`` the first time :func:`build_kv_store` falls back to
``config.event_store_url``; suppresses the warning on subsequent calls
within the same process so an operator with several subsystems sees
exactly one log line, not one per subsystem.

Tests reset this via ``monkeypatch.setattr`` so each test sees a fresh
process-state.
"""


def build_kv_store(
    config: ServerConfig,
    *,
    namespace: str,
) -> AsyncKeyValue:
    """Build a namespaced ``AsyncKeyValue`` store from operator config.

    URL resolution priority:

    1. ``config.kv_store_url`` (recommended; one variable selects the
       backend for every pvl-core subsystem)
    2. ``config.event_store_url`` (legacy override; emits a one-shot
       per-process deprecation warning when used)
    3. Default: ``file://`` at :data:`_DEFAULT_KV_STORE_DIR`

    The returned store is wrapped in
    :class:`~key_value.aio.wrappers.prefix_collections.PrefixCollectionsWrapper`
    with ``prefix=namespace``, so different subsystems sharing a backend
    cannot collide on collection names even if they happen to pick the
    same one.

    Args:
        config: A :class:`ServerConfig` whose ``kv_store_url`` /
            ``event_store_url`` field selects the backend.
        namespace: Logical subsystem name used as the
            ``PrefixCollectionsWrapper`` prefix (a domain hook —
            callers pick a name unique to their subsystem). Must be a
            non-empty string.

    Returns:
        A configured ``AsyncKeyValue`` wrapped for namespace isolation.

    Raises:
        ValueError: If ``namespace`` is empty, or the URL scheme is
            unrecognised, or a ``file://`` URL is malformed.
        ImportError: If a backend-specific extra is required but not
            installed; the message names the extra to install.
    """
    if not namespace.strip():
        raise ValueError(
            "namespace must be a non-empty string — it is the "
            "PrefixCollectionsWrapper prefix that isolates subsystems "
            "sharing the same backend."
        )

    url = config.kv_store_url
    if url is None and config.event_store_url is not None:
        url = config.event_store_url
        global _legacy_url_warned
        if not _legacy_url_warned:
            logger.warning(
                "kv_store_url=<unset>; falling back to legacy "
                "event_store_url=%s. Set <PREFIX>_KV_STORE_URL to migrate.",
                url,
            )
            _legacy_url_warned = True
    if not url:
        url = f"file://{_DEFAULT_KV_STORE_DIR}"

    backend = _build_backend(url)

    from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

    return PrefixCollectionsWrapper(key_value=backend, prefix=namespace)


def _build_backend(url: str) -> AsyncKeyValue:
    """Dispatch a URL to its backing AsyncKeyValue store.

    Kept private so callers cannot bypass the namespace wrapper that
    :func:`build_kv_store` applies.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme

    if scheme == "memory":
        from key_value.aio.stores.memory import MemoryStore

        logger.info("kv_store backend=memory lost_on_restart=true")
        return MemoryStore()

    if scheme == "file":
        # `file://host/path` (non-empty netloc) is technically valid URL
        # syntax but operators almost always meant the three-slash form;
        # reject explicitly rather than silently routing the netloc-as-
        # host away from the intended path.
        if parsed.netloc:
            raise ValueError(
                f"file:// URL {url!r} has a host component "
                f"({parsed.netloc!r}). Use the three-slash form, "
                f"e.g. 'file:///{parsed.netloc}{parsed.path}'."
            )
        if not parsed.path:
            raise ValueError(
                f"file:// URL {url!r} is missing a path. Use 'file:///absolute/path'."
            )
        # Verify the backend is importable BEFORE creating the directory,
        # so a missing extra does not leave an orphan directory behind.
        try:
            from key_value.aio.stores.filetree import FileTreeStore
        except ModuleNotFoundError as exc:  # pragma: no cover — fastmcp pulls this in
            raise ImportError(
                "FileTreeStore requires 'py-key-value-aio[filetree]'. "
                "fastmcp pulls this in transitively; reinstall fastmcp "
                "or add 'py-key-value-aio[filetree]' to your dependencies."
            ) from exc
        directory = parsed.path
        Path(directory).mkdir(parents=True, exist_ok=True)
        logger.info("kv_store backend=file directory=%s", directory)
        return FileTreeStore(data_directory=directory)

    if scheme == "redis":
        try:
            from key_value.aio.stores.redis import RedisStore
        except ModuleNotFoundError as exc:
            raise ImportError(
                "RedisStore requires the 'redis' extra. Install with "
                "`pip install 'fastmcp-pvl-core[redis]'` or add "
                "'py-key-value-aio[redis]' to your dependencies."
            ) from exc
        logger.info("kv_store backend=redis host=%s", parsed.hostname)
        return RedisStore(url=url)

    if scheme == "dynamodb":
        try:
            from key_value.aio.stores.dynamodb import DynamoDBStore
        except ModuleNotFoundError as exc:
            raise ImportError(
                "DynamoDBStore requires the 'dynamodb' extra. Install "
                "with `pip install 'fastmcp-pvl-core[dynamodb]'` or "
                "add 'py-key-value-aio[dynamodb]' to your dependencies."
            ) from exc
        # DynamoDB table names live in netloc; there is no host:port
        # convention. Tolerate (and discard) a stray ":..." for URL-
        # grammar consistency rather than failing on a benign extra.
        table_name = parsed.netloc.split(":")[0]
        if not table_name:
            raise ValueError(
                "dynamodb:// URL must include a table name, e.g. "
                "'dynamodb://my-table?region=us-east-1'"
            )
        query = parse_qs(parsed.query)
        region_name = query.get("region", [None])[0]
        endpoint_url = query.get("endpoint", [None])[0]
        logger.info("kv_store backend=dynamodb table=%s", table_name)
        return DynamoDBStore(
            table_name=table_name,
            region_name=region_name,
            endpoint_url=endpoint_url,
        )

    if scheme == "mongodb":
        try:
            from key_value.aio.stores.mongodb import MongoDBStore
        except ModuleNotFoundError as exc:
            raise ImportError(
                "MongoDBStore requires the 'mongodb' extra. Install "
                "with `pip install 'fastmcp-pvl-core[mongodb]'` or add "
                "'py-key-value-aio[mongodb]' to your dependencies."
            ) from exc
        logger.info("kv_store backend=mongodb host=%s", parsed.hostname)
        return MongoDBStore(url=url)

    raise ValueError(
        f"Unsupported kv_store URL scheme {scheme!r}. Use one of: "
        "'memory://', 'file:///path', 'redis://...', "
        "'dynamodb://<table>?region=...', 'mongodb://...'."
    )
