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
  ``redis`` extra)
- ``dynamodb://<table_name>[?region=...&endpoint=...]`` →
  :class:`DynamoDBStore` (requires the ``dynamodb`` extra)
- ``mongodb://host:port[/db]`` → :class:`MongoDBStore` (requires the
  ``mongodb`` extra)

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


def build_kv_store(
    env_prefix: str,
    config: ServerConfig,
    *,
    namespace: str,
) -> AsyncKeyValue:
    """Build a namespaced ``AsyncKeyValue`` store from operator config.

    URL resolution priority:

    1. ``config.kv_store_url`` (recommended; one variable selects the
       backend for every pvl-core subsystem)
    2. ``config.event_store_url`` (legacy override; emits a one-shot
       deprecation warning when used)
    3. Default: ``file://`` at :data:`_DEFAULT_KV_STORE_DIR`

    The returned store is wrapped in
    :class:`~key_value.aio.wrappers.prefix_collections.PrefixCollectionsWrapper`
    with ``prefix=namespace``, so different subsystems sharing a backend
    cannot collide on collection names even if they happen to pick the
    same one.

    Args:
        env_prefix: Env-var prefix of the consuming project. Currently
            unused but reserved for future per-project defaults
            (e.g. ``{prefix}_KV_STORE_DIR``).
        config: A :class:`ServerConfig` whose ``kv_store_url`` /
            ``event_store_url`` field selects the backend.
        namespace: Logical subsystem name used as the
            ``PrefixCollectionsWrapper`` prefix (a domain hook —
            callers pick a name unique to their subsystem).

    Returns:
        A configured ``AsyncKeyValue`` wrapped for namespace isolation.

    Raises:
        ValueError: If the URL scheme is unrecognised.
        ImportError: If a backend-specific extra is required but not
            installed; the message names the extra to install.
    """
    del env_prefix  # reserved for future per-project defaults

    url = config.kv_store_url
    if url is None and config.event_store_url is not None:
        url = config.event_store_url
        logger.warning(
            "kv_store_url=<unset>; falling back to legacy "
            "event_store_url=%s. Set <PREFIX>_KV_STORE_URL to migrate.",
            url,
        )
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
        directory = parsed.path or _DEFAULT_KV_STORE_DIR
        Path(directory).mkdir(parents=True, exist_ok=True)
        logger.info("kv_store backend=file directory=%s", directory)
        try:
            from key_value.aio.stores.filetree import FileTreeStore
        except ImportError as exc:  # pragma: no cover — fastmcp pulls this in
            raise ImportError(
                "FileTreeStore requires 'py-key-value-aio[filetree]'. "
                "fastmcp pulls this in transitively; reinstall fastmcp "
                "or add 'py-key-value-aio[filetree]' to your dependencies."
            ) from exc
        return FileTreeStore(data_directory=directory)

    if scheme == "redis":
        try:
            from key_value.aio.stores.redis import RedisStore
        except ImportError as exc:
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
        except ImportError as exc:
            raise ImportError(
                "DynamoDBStore requires the 'dynamodb' extra. Install "
                "with `pip install 'fastmcp-pvl-core[dynamodb]'` or "
                "add 'py-key-value-aio[dynamodb]' to your dependencies."
            ) from exc
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
        except ImportError as exc:
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
