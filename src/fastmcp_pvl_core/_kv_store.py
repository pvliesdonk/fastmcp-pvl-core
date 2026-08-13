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

With no URL configured at all the default is ``file://`` at
``/data/state`` — the volume family Docker images mount — degrading to
``memory://`` on a host where that directory is not usable (see
:func:`_default_url`).

Backend imports are lazy so memory/file deployments do not pull in
optional client libraries.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from ._config import ServerConfig

if TYPE_CHECKING:
    from key_value.aio.protocols.key_value import AsyncKeyValue

logger = logging.getLogger(__name__)


_DEFAULT_KV_STORE_DIR = "/data/state"
"""Preferred directory for the file-tree backend when no URL is given.

Downstream Docker images mount a persistent volume at ``/data``. The
default is *contingent* on that convention actually holding — see
:func:`_default_url`, which degrades to ``memory://`` on a host where
the directory is not usable rather than crashing server construction.
Tests monkey-patch this module attribute to redirect the default to a
tmp path rather than touching the real ``/data/state``.
"""


_default_fallback_warned: bool = False
"""Process-wide flag for the unusable-default-directory warning.

Same one-shot discipline as :data:`_legacy_url_warned`: an operator
whose server builds three namespaced stores (events, transfer, jobs)
sees one warning, not three.

Tests reset this via ``monkeypatch.setattr`` so each test sees a fresh
process-state.
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
    3. Default: ``file://`` at :data:`_DEFAULT_KV_STORE_DIR` where that
       directory is usable, else ``memory://`` — see :func:`_default_url`

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
            # Log only the scheme — operator-set URLs may carry
            # credentials in userinfo (redis://user:pass@host/0,
            # mongodb://user:pass@host/db). The rest of the dispatch
            # paths log only parsed.hostname for the same reason.
            logger.warning(
                "kv_store_url=<unset>; falling back to legacy "
                "event_store_url (scheme=%r). Set <PREFIX>_KV_STORE_URL "
                "to migrate.",
                urlparse(url).scheme,
            )
            _legacy_url_warned = True
    if not url:
        url = _default_url()

    backend = _build_backend(url)

    from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

    return PrefixCollectionsWrapper(key_value=backend, prefix=namespace)


def _default_url() -> str:
    """Resolve the backend URL for a deployment that configured none.

    ``file://`` at :data:`_DEFAULT_KV_STORE_DIR` is the intended
    default — the Docker convention every family image follows. But the
    same code also runs unconfigured on hosts that never heard of
    ``/data``: CI runners, `uvx`/pipx installs, the stdio plugin
    channel. There the eager ``mkdir`` in the file branch raises
    ``PermissionError`` at *construction* time, so a downstream that
    merely wires ``build_jobs`` into ``make_server`` cannot build a
    server at all.

    So the default is contingent: probe the directory, and where it is
    not usable fall back to ``memory://`` with one warning naming the
    variable to set. State is then in-process and lost on restart —
    which is exactly what an unconfigured deployment on such a host can
    honestly offer, and it is what the memory backend already logs.

    The probe requires the *parent* to exist rather than creating it:
    ``/data`` is a mount point, and a host where it is absent is a host
    that never opted into the convention. Creating it there would leak
    a root-level directory (and, running as root, silently "succeed"
    into a path nobody mounted).

    An **explicitly configured** ``file://`` URL is never degraded —
    that operator asked for a specific directory and gets a hard error
    if it is unusable. Only this unset-URL default is best-effort.
    """
    directory = Path(_DEFAULT_KV_STORE_DIR)
    reason = _unusable_reason(directory)
    if reason is None:
        return f"file://{_DEFAULT_KV_STORE_DIR}"

    global _default_fallback_warned
    if not _default_fallback_warned:
        logger.warning(
            "kv_store_url=<unset> and the default directory is unusable "
            "(%s); falling back to memory:// — state is in-process and "
            "lost on restart. Set <PREFIX>_KV_STORE_URL to choose a "
            "backend explicitly.",
            reason,
        )
        _default_fallback_warned = True
    return "memory://"


def _unusable_reason(directory: Path) -> str | None:
    """Why *directory* cannot back the default store, or ``None`` if it can.

    Only ever called on :data:`_DEFAULT_KV_STORE_DIR`, so the paths in
    the returned message are pvl-core's own default — never an
    operator-supplied URL that might carry credentials.
    """
    parent = directory.parent
    if not parent.is_dir():
        return f"{parent} does not exist"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"{directory} is not creatable ({exc.strerror or exc})"
    # mkdir(exist_ok=True) is a no-op on a pre-existing directory, so a
    # read-only mount gets past the branch above and would only fail on
    # the first job promotion — later, and harder to diagnose.
    if not os.access(directory, os.W_OK | os.X_OK):
        return f"{directory} is not writable"
    return None


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
        #
        # Error messages name the SCHEME only, never the raw URL — an
        # operator may have typed credentials into a misconfigured URL
        # (e.g. file://user:pass@host/path), and ValueError text ends
        # up in process logs / Sentry alongside the legacy-warning
        # path that's already redacted.
        if parsed.netloc:
            raise ValueError(
                "file:// URL has a host component. Use the three-slash "
                "form: 'file:///absolute/path'."
            )
        if not parsed.path:
            raise ValueError(
                "file:// URL is missing a path. Use 'file:///absolute/path'."
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
