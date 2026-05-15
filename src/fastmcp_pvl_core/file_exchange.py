"""MCP File Exchange — public facade.

Single-entry-point wiring for downstream MCP servers that want to
participate in the File Exchange convention (spec v0.3.0). Composes
the artifact store, the protocol surface, and the exchange-volume
runtime, and registers the spec-compliant ``create_download_link``
and ``fetch_file`` MCP tools.

The ``experimental.file_exchange`` capability this module advertises
uses the v0.3.0 ``transfer_methods`` shape: ``http`` and
``http_upload`` are separate method keys, each carrying ``source`` /
``sink`` role sub-objects for whichever role(s) the server fills.

Downstream usage::

    from fastmcp_pvl_core import register_file_exchange, FileRefPreview

    handle = register_file_exchange(
        mcp,
        namespace="image-mcp",
        env_prefix="IMAGE_GENERATION_MCP",
        produces=("image/png", "image/webp"),
    )

    # Inside a tool body that produces content:
    file_ref = await handle.publish(
        source=image_bytes,
        mime_type="image/png",
        preview=FileRefPreview(description=prompt, dimensions=(w, h)),
    )
    return {"image_id": image_id, "file_ref": file_ref.to_dict()}

The whole feature is gated by ``{PREFIX}_FILE_EXCHANGE_ENABLED`` (default
true on HTTP/SSE transports, false on stdio). The ``exchange`` transfer
method activates only when the deployer sets ``MCP_EXCHANGE_DIR``.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
import mimetypes
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple
from urllib.parse import urlsplit

import httpx

from fastmcp_pvl_core._env import env, parse_bool
from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange_protocol import (
    ExchangeURI,
    ExchangeURIError,
    FileExchangeCapability,
    FileRef,
    FileRefPreview,
    _FileExchangeCapabilityBuilder,
    register_file_exchange_capability,
)
from fastmcp_pvl_core._file_exchange_runtime import (
    BufferedReceiver,
    ExchangeGroupMismatch,
    FileExchange,
    FileExchangeConfigError,
    StreamReceiver,
    _accepts_match,
    register_upload_route,
)
from fastmcp_pvl_core._token_store import (
    ArtifactStore,
    UploadStore,
    set_artifact_store,
    set_upload_store,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


_DEFAULT_DOWNLOAD_TOOL = "create_download_link"
_DEFAULT_FETCH_TOOL = "fetch_file"
_DEFAULT_TTL_SECONDS = 3600.0
_DEFAULT_HTTP_FETCH_TIMEOUT = 30.0
_DEFAULT_HTTP_FETCH_MAX_BYTES = 256 * 1024 * 1024  # 256 MiB hard cap


# Private test seam: when set, ``register_file_exchange`` uses this store
# instead of building one from env vars. NOT public API — leading
# underscore is the signal. Tests that mutate this should request the
# ``reset_artifact_store_test_seam`` fixture in tests/conftest.py to
# reset it on teardown.
_TEST_ARTIFACT_STORE: ArtifactStore | None = None


def _set_artifact_store_for_test(store: ArtifactStore | None) -> None:
    """Test-only seam for replacing the lazy-built artifact store.

    NOT public API. ``register_file_exchange``'s kwarg surface
    exposes only domain hooks; downstream production code has no
    domain-specific basis to inject a different store at runtime.
    Tests reach in here when they need fixture-level control.
    """
    global _TEST_ARTIFACT_STORE
    _TEST_ARTIFACT_STORE = store


# ---------------------------------------------------------------------------
# Per-FastMCP capability builder registry
# ---------------------------------------------------------------------------
#
# A single FastMCP instance may have both ``register_file_exchange``
# (download direction) and ``register_file_exchange_upload`` (upload
# direction) called against it. Both registrars need to contribute to
# the same ``experimental.file_exchange`` capability dict. We stash the
# builder on the FastMCP instance via a private attribute (mirroring
# the ``_pvl_experimental_middleware`` pattern in
# :mod:`fastmcp_pvl_core._file_exchange_protocol`). This avoids the
# ``id(mcp)`` reuse hazard a module-level registry would have: when a
# FastMCP instance is garbage-collected, CPython is free to reuse its
# id for a future allocation, which would alias unrelated instances'
# state.

_BUILDER_ATTR: Final = "_pvl_file_exchange_builder"


def _get_or_create_builder(
    mcp: FastMCP,
    *,
    namespace: str,
    exchange_id: str | None = None,
    produces: tuple[str, ...] = (),
    consumes: tuple[str, ...] = (),
) -> _FileExchangeCapabilityBuilder:
    """Return (creating if needed) the per-FastMCP capability builder.

    The builder is stashed on the FastMCP instance via a private
    attribute (mirrors the ``_pvl_experimental_middleware`` pattern in
    :mod:`fastmcp_pvl_core._file_exchange_protocol`). This avoids the
    ``id(mcp)`` reuse hazard a module-level registry would have.

    Subsequent calls on the same ``mcp`` reuse the existing builder and
    additively extend ``produces`` / ``consumes`` (so download and
    upload registrations don't clobber each other's MIME lists).

    Merge contract for the non-mergeable field ``namespace``:
    first-caller-wins. A subsequent caller passing a different
    namespace logs a WARNING and the original value is retained.
    Likewise, ``exchange_id`` is set by the first caller that supplies
    a non-``None`` value and not overwritten thereafter.
    """
    builder: _FileExchangeCapabilityBuilder | None = getattr(mcp, _BUILDER_ATTR, None)
    if builder is None:
        builder = _FileExchangeCapabilityBuilder(
            namespace=namespace,
            exchange_id=exchange_id,
            produces=produces,
            consumes=consumes,
        )
        # If FastMCP ever forbids dynamic attribute assignment (e.g. via
        # ``ConfigDict(extra="forbid")``) the AttributeError will surface
        # here at registration — louder failure than a silent fallback,
        # but appropriate for an internal-API change we'd want to learn
        # about quickly.
        # ``setattr`` rather than ``mcp._pvl_file_exchange_builder =``
        # so pyright does not require a per-line type-ignore — the
        # constant is the same one ``_emit_capability`` reads via
        # ``getattr`` below.
        setattr(mcp, _BUILDER_ATTR, builder)
    else:
        # Existing builder — merge new contributions.
        if namespace != builder.namespace:
            logger.warning(
                "_get_or_create_builder: namespace mismatch on FastMCP "
                "id=%d — first caller registered %r, second caller "
                "passed %r; first-caller-wins (the second namespace is "
                "dropped). Verify both registrars use the same namespace.",
                id(mcp),
                builder.namespace,
                namespace,
            )
        builder.exchange_id = builder.exchange_id or exchange_id
        # Set-union the MIME lists; using ``set`` and back to tuple
        # gives us order-independent dedup. Order is not part of the
        # spec contract for produces/consumes.
        builder.produces = tuple(set(builder.produces) | set(produces))
        builder.consumes = tuple(set(builder.consumes) | set(consumes))
    return builder


def _emit_capability(mcp: FastMCP) -> FileExchangeCapability | None:
    """Materialise the per-FastMCP builder and advertise the capability.

    Idempotent: each call rebuilds and re-advertises. Safe to call
    after each registrar adds its contribution — the underlying
    middleware stores the latest payload, so redundant calls just
    overwrite-with-same-value.
    """
    builder: _FileExchangeCapabilityBuilder | None = getattr(mcp, _BUILDER_ATTR, None)
    if builder is None:
        return None
    cap = builder.build()
    if cap is not None:
        register_file_exchange_capability(mcp, cap)
    return cap


class FetchTransportError(RuntimeError):
    """A transfer attempt failed with a domain-specific reason.

    Used for SSRF refusals, oversize-response refusals, and HTTP-status
    failures inside ``fetch_file``. Caught by the fetch dispatcher and
    translated into a structured ``transfer_failed`` envelope per
    spec §"Step 2: Attempt transfer".
    """


# ---------------------------------------------------------------------------
# Consumer-sink types
# ---------------------------------------------------------------------------


class FetchContext(NamedTuple):
    """Per-call context handed to the consumer sink.

    Attributes:
        url: The URL that was fetched (``exchange://`` or ``http(s)://``).
        file_ref: Full file reference if the caller passed one to
            ``fetch_file``; otherwise ``None``.
        mime_type: Preferred mime type (from the file_ref or the
            HTTP Content-Type header).
        suggested_filename: A producer-suggested filename when
            available.
        params: The caller-supplied ``path`` plus any other ``**extra``
            arguments forwarded into ``fetch_file``.
        handle: The :class:`FileExchangeHandle` that owns this sink —
            handy for sinks that want to re-publish what they just
            consumed (chaining).
    """

    url: str
    file_ref: FileRef | None
    mime_type: str | None
    suggested_filename: str | None
    params: Mapping[str, Any]
    handle: FileExchangeHandle


@dataclass
class FetchResult:
    """Return value the consumer sink hands back to ``fetch_file``.

    Attributes:
        stored_at: Optional path/URI describing where the consumer
            stored the bytes.
        bytes_written: Number of bytes the sink wrote (typically equal
            to ``len(data)``).
        extra: Arbitrary extra keys merged into the tool's response —
            e.g. ``{"document_id": 42}`` for paperless-mcp.
    """

    stored_at: str | None = None
    bytes_written: int = 0
    extra: Mapping[str, Any] | None = None


ConsumerSink = Callable[[bytes, FetchContext], Awaitable[FetchResult]]


# ---------------------------------------------------------------------------
# Publish registry (per-handle origin_id → record)
# ---------------------------------------------------------------------------


@dataclass
class _PublishRecord:
    """One published file — what the http branch needs to mint a download.

    Stored in ``FileExchangeHandle.publish_registry`` keyed by ``origin_id``.
    Lazy callables stay un-invoked until ``create_download_link`` actually
    needs the bytes.
    """

    mime_type: str
    ext: str
    filename: str
    eager_bytes: bytes | None = None
    eager_path: Path | None = None
    lazy: Callable[[], Awaitable[bytes] | bytes] | None = None
    expires_at: float = 0.0


# ---------------------------------------------------------------------------
# Handle returned by register_file_exchange
# ---------------------------------------------------------------------------


@dataclass
class FileExchangeHandle:
    """The downstream-facing surface returned by :func:`register_file_exchange`.

    Stash this on ``mcp.state`` (or in a module-level singleton) so
    your producer-side tool bodies can call :meth:`publish`.
    """

    namespace: str
    enabled: bool
    produce: bool
    consume: bool
    artifact_store: ArtifactStore | None
    exchange: FileExchange | None
    capability: FileExchangeCapability | None
    # pvl-core owns these tool names as part of the shared shape (see
    # framing principle in CLAUDE.md). ``init=False`` keeps them out of
    # the constructor so a directly-constructed handle cannot advertise
    # a tool name that doesn't match what pvl-core actually registered
    # — silently breaking the file_ref ``transfer["http"]["tool"]``
    # contract. The decorator sites read ``handle.download_tool_name``
    # / ``handle.fetch_tool_name`` to register the tools, so the field
    # stays as the single source of truth for "what tool name pvl-core
    # advertises and registers."
    download_tool_name: str = field(default=_DEFAULT_DOWNLOAD_TOOL, init=False)
    fetch_tool_name: str = field(default=_DEFAULT_FETCH_TOOL, init=False)
    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    publish_registry: dict[str, _PublishRecord] = field(default_factory=dict)
    # Throttle: skip ``expire_publish_registry`` if it ran more recently
    # than this many seconds ago. The registry is in-process and short-
    # lived per record, so a once-per-30s sweep keeps memory bounded
    # without the O(N) scan running on every download-link request.
    # ``init=False`` keeps these out of the constructor signature —
    # they're internal scheduler state, not config the caller picks.
    _expiry_sweep_interval: float = field(default=30.0, init=False)
    _last_expiry_sweep: float = field(default=0.0, init=False)
    # Reused httpx client for the consumer ``http`` branch. Lazy-
    # initialised on first fetch so handles that never consume don't
    # pay for an idle connection pool. Closed via :meth:`aclose`.
    _http_client: httpx.AsyncClient | None = field(default=None, init=False)

    @property
    def http_enabled(self) -> bool:
        """``True`` iff the http transfer method is wired and producing."""
        return (
            self.enabled
            and self.produce
            and self.artifact_store is not None
            and self.artifact_store.has_base_url
        )

    @property
    def exchange_enabled(self) -> bool:
        """``True`` iff the exchange transfer method is wired and producing."""
        return self.enabled and self.produce and self.exchange is not None

    # ---- producer entry point ---------------------------------------------

    async def publish(
        self,
        source: bytes | Path | None = None,
        *,
        lazy: Callable[[], Awaitable[bytes] | bytes] | None = None,
        origin_id: str | None = None,
        mime_type: str,
        ext: str | None = None,
        filename: str | None = None,
        size_bytes: int | None = None,
        preview: FileRefPreview | None = None,
    ) -> FileRef:
        """Materialise (or register) a file and return a :class:`FileRef`.

        Exactly one of ``source`` (bytes / :class:`pathlib.Path`) or
        ``lazy`` (callable returning bytes, sync or async) MUST be
        provided. ``lazy`` is preferred when bytes are expensive to
        compute (e.g. on-the-fly image transforms) and the file is not
        being written into the exchange volume — ``lazy`` plus an
        active exchange volume materialises eagerly with a warning,
        because the exchange method has no pull trigger.

        Args:
            source: Eager bytes or a :class:`pathlib.Path` that already
                exists on disk.
            lazy: Sync or async callable that returns the bytes when
                invoked (per ``create_download_link`` call — not cached).
            origin_id: Producer-chosen opaque id. Defaults to a fresh
                UUID4 hex. Validated as a raw JSON parameter (spec
                §"Security and Path Resolution" — never URI-decoded).
            mime_type: MIME type of the file. Required. Used to pick a
                default extension when ``ext`` is omitted.
            ext: File extension (no leading dot). Defaults to a sensible
                value derived from ``mime_type`` via :mod:`mimetypes`.
            filename: ``Content-Disposition`` filename for HTTP downloads.
                Defaults to ``{origin_id}.{ext}``.
            size_bytes: Optional precomputed size; if omitted, derived
                from ``source`` when possible.
            preview: Optional :class:`FileRefPreview` for the LLM.

        Returns:
            A :class:`FileRef` whose ``transfer`` block reflects only
            the methods this server can actually serve.
        """
        if not self.enabled or not self.produce:
            raise RuntimeError(
                f"FileExchangeHandle({self.namespace}): publishing is "
                "disabled — check the {prefix}_FILE_EXCHANGE_ENABLED and "
                "{prefix}_FILE_EXCHANGE_PRODUCE env vars (where {prefix} "
                "is the env_prefix passed to register_file_exchange)"
            )
        if (source is None) == (lazy is None):
            raise ValueError("publish() requires exactly one of source= or lazy=")

        origin_id = origin_id or uuid.uuid4().hex
        ExchangeURI.validate_segment(origin_id, role="json_param")
        if origin_id.startswith("."):
            raise ExchangeURIError(
                f"origin_id MUST NOT start with a dot: {origin_id!r}"
            )

        if ext is None:
            guessed = mimetypes.guess_extension(mime_type or "") or ".bin"
            ext = guessed.lstrip(".")
        ExchangeURI.validate_segment(ext, role="json_param")

        if filename is None:
            filename = f"{origin_id}.{ext}"

        # Resolve eager_bytes / eager_path / lazy.
        eager_bytes: bytes | None = None
        eager_path: Path | None = None
        if isinstance(source, (bytes, bytearray)):
            eager_bytes = bytes(source)
            if size_bytes is None:
                size_bytes = len(eager_bytes)
        elif isinstance(source, Path):
            eager_path = source
            if size_bytes is None:
                # Fail fast: if we can't stat the source, the producer
                # is publishing a reference to a file we'll be unable to
                # read at create_download_link time anyway. Surfacing
                # the error here gives a stack at the actual call site
                # instead of a deferred mystery error in the tool body.
                stat_result = await asyncio.to_thread(source.stat)
                size_bytes = stat_result.st_size
        elif source is not None:
            raise TypeError(
                f"publish() source must be bytes or pathlib.Path, "
                f"got {type(source).__name__}"
            )

        # Lazy + exchange enabled: spec has no pull trigger for files on
        # the exchange volume, so we have to materialise the bytes now.
        # Log a warning so producers know the laziness is silently lost.
        if lazy is not None and self.exchange_enabled:
            logger.warning(
                "publish(lazy=...) with exchange volume active — "
                "materialising eagerly (origin_id=%s)",
                origin_id,
            )
            eager_bytes = await _resolve_lazy(lazy)
            if size_bytes is None:
                size_bytes = len(eager_bytes)
            lazy = None

        # If exchange is enabled, write the bytes into the volume now.
        exchange_uri_str: str | None = None
        if self.exchange_enabled:
            # exchange_enabled implies self.exchange is not None.
            exchange_runtime = self.exchange
            if exchange_runtime is None:
                raise RuntimeError(
                    "exchange_enabled is True but exchange runtime is None"
                )
            payload: bytes | Path
            if eager_bytes is not None:
                payload = eager_bytes
            elif eager_path is not None:
                # Pass the Path through to write_atomic — it streams
                # from disk in 64 KiB chunks so a 256 MiB source never
                # lands in memory in one piece. (The http registry
                # also stores eager_path and re-reads on demand, so the
                # whole publish→download flow is constant-memory.)
                payload = eager_path
            else:
                # Lazy callables are materialised eagerly above when
                # exchange is on, so reaching here means the input
                # validation at the top of publish() let through a
                # combination it shouldn't have. Surface as
                # RuntimeError so we get a meaningful traceback.
                raise RuntimeError(
                    "publish(): no byte source after lazy materialisation"
                )
            exchange_uri = await asyncio.to_thread(
                exchange_runtime.write_atomic,
                origin_id=origin_id,
                ext=ext,
                content=payload,
            )
            exchange_uri_str = str(exchange_uri)

        # Register for the http branch (only when http is enabled).
        if self.http_enabled:
            self.publish_registry[origin_id] = _PublishRecord(
                mime_type=mime_type,
                ext=ext,
                filename=filename,
                eager_bytes=eager_bytes,
                eager_path=eager_path if eager_bytes is None else None,
                lazy=lazy,
                expires_at=time.time() + self.ttl_seconds,
            )

        transfer: dict[str, dict[str, Any]] = {}
        if exchange_uri_str is not None:
            transfer["exchange"] = {"uri": exchange_uri_str}
        if self.http_enabled:
            transfer["http"] = {"tool": self.download_tool_name}
        if not transfer:
            raise RuntimeError(
                f"FileExchangeHandle({self.namespace}): no transfer methods "
                "available — neither MCP_EXCHANGE_DIR nor base_url is set"
            )

        return FileRef(
            origin_server=self.namespace,
            origin_id=origin_id,
            transfer=transfer,
            mime_type=mime_type,
            size_bytes=size_bytes,
            preview=preview,
        )

    # ---- lifecycle --------------------------------------------------------

    def _get_http_client(self) -> httpx.AsyncClient:
        """Return the shared :class:`httpx.AsyncClient`, creating on demand.

        Reusing one client across all ``fetch_file`` calls enables HTTP
        keep-alive and TLS-session resumption to the same producer host
        — repeated cross-server transfers don't pay for a fresh TCP+TLS
        handshake every time. Lifecycle is owned by :meth:`aclose`.
        """
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=_DEFAULT_HTTP_FETCH_TIMEOUT,
                follow_redirects=False,
            )
        return self._http_client

    async def aclose(self) -> None:
        """Release any background resources (currently the http client).

        Safe to call multiple times; no-op when no client was lazily
        created. Downstream servers that hold this handle for the
        process lifetime usually don't need to call this — it exists
        for tests and for embedders that build/tear down handles
        repeatedly.
        """
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def expire_publish_registry(self, *, force: bool = False) -> int:
        """Drop registry records past their TTL.

        Throttled: returns ``0`` immediately if a sweep ran within the
        last ``_expiry_sweep_interval`` seconds. Pass ``force=True`` to
        bypass the throttle (useful in tests and explicit
        ``aclose``-style shutdown).

        Returns:
            The number of records removed (``0`` when throttled).
        """
        now = time.time()
        if not force and (now - self._last_expiry_sweep) < self._expiry_sweep_interval:
            return 0
        self._last_expiry_sweep = now
        expired = [k for k, r in self.publish_registry.items() if r.expires_at < now]
        for k in expired:
            del self.publish_registry[k]
        return len(expired)


async def _resolve_lazy(
    lazy: Callable[[], Awaitable[bytes] | bytes],
) -> bytes:
    """Call a sync-or-async lazy provider and return bytes.

    A sync callable is dispatched via :func:`asyncio.to_thread` so any
    blocking I/O it performs doesn't stall the event loop; the thread's
    return value is then awaited (in case the sync callable returned an
    awaitable, which is unusual but legal).
    """
    if asyncio.iscoroutinefunction(lazy):
        result: Any = await lazy()
    else:
        result = await asyncio.to_thread(lazy)
        if inspect.isawaitable(result):
            result = await result
    if not isinstance(result, bytes):
        raise TypeError(
            "lazy provider must return bytes or an awaitable yielding "
            f"bytes, got {type(result).__name__}"
        )
    return result


# ---------------------------------------------------------------------------
# register_file_exchange — the public facade
# ---------------------------------------------------------------------------


def register_file_exchange(
    mcp: FastMCP,
    *,
    namespace: str,
    env_prefix: str,
    produces: Sequence[str] = (),
    consumes: Sequence[str] = (),
    consumer_sink: ConsumerSink | None = None,
) -> FileExchangeHandle:
    """Wire MCP File Exchange (v0.3.0) onto ``mcp``.

    The kwarg surface is intentionally minimal — five domain hooks,
    no operator-config kwargs, no override seams. Operator config
    goes to environment variables (see "Environment" below).
    Implementation choices pvl-core makes (tool names, transport
    resolution, capability shape) are not overridable; downstream
    collisions resolve by downstream migration. See ``CLAUDE.md``
    "framing principle" for the rationale.

    Performs four pieces of wiring, each gated by env vars:

    1. Builds (or adopts) an :class:`ArtifactStore`, mounts its
       ``/artifacts/{token}`` route, and installs the module-level
       singleton.
    2. Resolves the :class:`FileExchange` runtime from
       ``MCP_EXCHANGE_DIR`` (deployer-controlled, unprefixed).
    3. Advertises ``experimental.file_exchange`` on the MCP
       ``initialize`` response (spec §"Capability declaration").
    4. Registers ``create_download_link`` (spec §"Transfer Methods /
       http") and ``fetch_file`` (spec §"Transfer Negotiation") MCP
       tools as appropriate for the resolved producer / consumer /
       transport state.

    Args:
        mcp: The :class:`fastmcp.FastMCP` server instance.
        namespace: **Domain hook.** This server's logical name. Used
            as both the ``FileRef.origin_server`` and the exchange
            namespace.
        env_prefix: **Domain hook.** Per-server env-var prefix (e.g.
            ``"IMAGE_GENERATION_MCP"``).
        produces: **Domain hook.** MIME types this server emits as
            file references — advertised in the capability declaration.
        consumes: **Domain hook.** MIME types this server can ingest
            via ``fetch_file``.
        consumer_sink: **Domain hook.** Required to register
            ``fetch_file``. Receives the resolved bytes and a
            :class:`FetchContext`; returns a :class:`FetchResult`.
            When ``None``, the consumer side is not advertised in the
            capability declaration and ``fetch_file`` is not registered.

    Environment:
        Operator-controlled configuration. ``{PREFIX}`` matches the
        ``env_prefix`` argument.

        - ``{PREFIX}_TRANSPORT`` (fallback ``FASTMCP_TRANSPORT``, default
          ``"stdio"``): selects transport. ``"http"`` / ``"sse"`` /
          ``"streamable-http"`` enable HTTP-side wiring.
        - ``{PREFIX}_BASE_URL``: required for the HTTP-side
          ``create_download_link`` tool to produce reachable URLs;
          unset means the producer side is silently skipped.
        - ``{PREFIX}_FILE_EXCHANGE_TTL`` (default 3600 seconds): TTL
          for issued download URLs and for published file records.
        - ``{PREFIX}_FILE_EXCHANGE_PRODUCE`` (default ``"true"``):
          operator opt-out of producer side independent of transport.
        - ``{PREFIX}_FILE_EXCHANGE_CONSUME`` (default ``"true"``):
          operator opt-out of consumer side independent of transport.
        - ``MCP_EXCHANGE_DIR`` (unprefixed, deployer-controlled): the
          shared volume for the ``exchange`` transfer method;
          unset means the exchange-volume side is skipped.

    Returns:
        A :class:`FileExchangeHandle`. Stash it where your producer-side
        tools can reach it.
    """
    resolved_transport = _resolve_transport(env_prefix)
    enabled = _resolve_enabled(env_prefix, resolved_transport)
    produce = enabled and parse_bool(env(env_prefix, "FILE_EXCHANGE_PRODUCE", "true"))
    consume_env = parse_bool(env(env_prefix, "FILE_EXCHANGE_CONSUME", "true"))
    consume = enabled and consumer_sink is not None and consume_env
    if enabled and consume_env and consumer_sink is None:
        # Operator opted in but the downstream code didn't supply a sink
        # — capability advertisement and fetch_file will both be silently
        # absent, which is exactly the kind of inconsistency that turns
        # into "tool not found" support tickets.
        logger.warning(
            "%s_FILE_EXCHANGE_CONSUME is true but no consumer_sink was "
            "passed to register_file_exchange — consumer side will NOT "
            "be advertised",
            env_prefix,
        )
    ttl_raw = env(env_prefix, "FILE_EXCHANGE_TTL")
    ttl_seconds = float(ttl_raw) if ttl_raw else _DEFAULT_TTL_SECONDS

    # --- Artifact store ---
    base_url = env(env_prefix, "BASE_URL")
    # Lazy build from env vars; tests override via _set_artifact_store_for_test.
    store: ArtifactStore | None = _TEST_ARTIFACT_STORE
    if enabled and produce and store is None:
        # Only build a store if we'll actually serve over http; without
        # base_url, build_url would fail and the store would be
        # producer-useless.
        if base_url is not None:
            store = ArtifactStore(ttl_seconds=ttl_seconds, base_url=base_url)
    if enabled and store is not None:
        ArtifactStore.register_route(mcp, store)
        set_artifact_store(store)

    # --- Exchange volume ---
    # FileExchange.from_env raises on misconfiguration (set-but-empty,
    # missing dir, group-id mismatch). Let those exceptions propagate so
    # the operator fixes the deployment rather than silently running
    # without exchange. We log first so the failure is searchable in
    # startup logs even when the surrounding boot harness doesn't print
    # the exception cleanly.
    try:
        exchange = FileExchange.from_env(
            default_namespace=namespace, ttl_seconds=ttl_seconds
        )
    except (FileExchangeConfigError, ExchangeGroupMismatch) as exc:
        logger.error(
            "register_file_exchange: file_exchange runtime config invalid: %s",
            exc,
        )
        raise

    # --- Capability declaration ---
    # Push contributions into the per-FastMCP builder so that an upload
    # registrar on the same ``mcp`` can merge into one capability dict.
    # The shape emitted is the v0.3.0 ``source``/``sink`` role-keyed form.
    capability: FileExchangeCapability | None = None
    if enabled:
        builder = _get_or_create_builder(
            mcp,
            namespace=namespace,
            exchange_id=exchange.exchange_id if exchange is not None else None,
            produces=tuple(produces) if produce else (),
            consumes=tuple(consumes) if consume else (),
        )
        if exchange is not None and (produce or consume):
            builder.set_exchange(True)
        # The two http roles are independent — a server that both produces
        # and consumes fills both. ``create_download_link`` is the producer
        # (``source``) tool; ``fetch_file`` is the consumer (``sink``) tool.
        if produce and store is not None and store.has_base_url:
            builder.set_http_source(tool_name=_DEFAULT_DOWNLOAD_TOOL)
        if consume:
            builder.set_http_sink(tool_name=_DEFAULT_FETCH_TOOL)
        capability = _emit_capability(mcp)

    handle = FileExchangeHandle(
        namespace=namespace,
        enabled=enabled,
        produce=produce,
        consume=consume,
        artifact_store=store,
        exchange=exchange,
        capability=capability,
        ttl_seconds=ttl_seconds,
    )

    # --- Tool registration ---
    if enabled and produce and store is not None and base_url is not None:
        _register_create_download_link(mcp, handle)
    if enabled and consume and consumer_sink is not None:
        _register_fetch_file(mcp, handle, consumer_sink)

    return handle


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _resolve_transport(
    env_prefix: str,
    override: Literal["http", "stdio", "auto"] = "auto",
) -> Literal["http", "stdio"]:
    """Resolve transport from ``{PREFIX}_TRANSPORT`` / ``FASTMCP_TRANSPORT``.

    Defaults to ``"stdio"`` when neither env var is set. ``override``
    allows callers to force a specific transport; both
    :func:`register_file_exchange` and :func:`register_file_exchange_upload`
    currently rely on the ``"auto"`` default so resolution goes through
    the env-var path.
    """
    if override != "auto":
        return override
    raw = (
        env(env_prefix, "TRANSPORT") or env("FASTMCP", "TRANSPORT") or "stdio"
    ).lower()
    if raw in ("http", "sse", "streamable-http"):
        return "http"
    return "stdio"


def _resolve_enabled(env_prefix: str, transport: Literal["http", "stdio"]) -> bool:
    raw = env(env_prefix, "FILE_EXCHANGE_ENABLED")
    if raw is not None:
        return parse_bool(raw)
    # Default: enabled on HTTP, disabled on stdio (mirrors the existing
    # ``transport != "stdio"`` guards downstream).
    return transport == "http"


# ---------------------------------------------------------------------------
# create_download_link tool
# ---------------------------------------------------------------------------


def _register_create_download_link(mcp: FastMCP, handle: FileExchangeHandle) -> None:
    """Register the spec-compliant ``create_download_link`` MCP tool."""

    @mcp.tool(name=handle.download_tool_name)
    async def create_download_link(
        origin_id: str,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Mint a one-time HTTP download URL for a previously-published file.

        See spec §"Transfer Methods / http". ``origin_id`` is the opaque handle from a
        ``file_ref.origin_id`` field. ``ttl_seconds`` is clamped to the
        server's configured maximum.
        """
        try:
            ExchangeURI.validate_segment(origin_id, role="json_param")
        except ExchangeURIError as exc:
            return _transfer_failed(
                origin_server=handle.namespace,
                origin_id=origin_id,
                method="http",
                message=f"origin_id failed validation: {exc}",
            )

        # Throttled bulk sweep keeps the registry from growing unbounded
        # between create_download_link calls (an O(N) operation that
        # would otherwise run every request). The per-record TTL check
        # below is what enforces freshness for *this* lookup — the
        # bulk sweep can return 0 while the requested record is
        # individually expired, and we still need to refuse to mint a
        # fresh URL for it.
        handle.expire_publish_registry()
        record = handle.publish_registry.get(origin_id)
        if record is None or record.expires_at < time.time():
            return _transfer_failed(
                origin_server=handle.namespace,
                origin_id=origin_id,
                method="http",
                message="origin_id is unknown or has expired",
            )

        effective_ttl: float
        if ttl_seconds is None or ttl_seconds <= 0:
            effective_ttl = handle.ttl_seconds
        else:
            # Clamp to the publish-side TTL — never serve a download URL
            # that outlives the bytes the server is willing to retain.
            effective_ttl = min(float(ttl_seconds), handle.ttl_seconds)

        # Resolve bytes (eager / Path / lazy).
        if record.eager_bytes is not None:
            data = record.eager_bytes
        elif record.eager_path is not None:
            try:
                data = await asyncio.to_thread(record.eager_path.read_bytes)
            except FileNotFoundError:
                # Producer published a Path that has since been deleted
                # from disk. Surface as a structured transfer_failed so
                # the client can try other methods rather than crashing
                # the tool with a raw stack trace.
                return _transfer_failed(
                    origin_server=handle.namespace,
                    origin_id=origin_id,
                    method="http",
                    message=(
                        f"published file no longer exists on disk: {record.eager_path}"
                    ),
                )
        elif record.lazy is not None:
            data = await _resolve_lazy(record.lazy)
        else:
            return _transfer_failed(
                origin_server=handle.namespace,
                origin_id=origin_id,
                method="http",
                message="record has no resolvable byte source (internal error)",
            )

        # http_enabled guarantees this in normal operation; defend
        # against future refactors that might bypass that check.
        if handle.artifact_store is None:
            raise RuntimeError("create_download_link reached without an artifact store")
        url = handle.artifact_store.put_ephemeral(
            data,
            content_type=record.mime_type,
            filename=record.filename,
            ttl_seconds=effective_ttl,
        )
        return {
            "url": url,
            "ttl_seconds": effective_ttl,
            "mime_type": record.mime_type,
        }


def _transfer_failed(
    *,
    origin_server: str,
    origin_id: str,
    method: str,
    message: str,
    remaining_transfer: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a ``transfer_failed`` envelope (spec §"Step 2: Attempt transfer")."""
    out: dict[str, Any] = {
        "error": "transfer_failed",
        "origin_server": origin_server,
        "origin_id": origin_id,
        "method": method,
        "message": message,
    }
    if remaining_transfer is not None:
        out["remaining_transfer"] = remaining_transfer
    return out


def _upload_transfer_failed(
    *,
    receiver_server: str,
    origin_id: str,
    message: str,
) -> dict[str, Any]:
    """Build an ``http_upload`` in-band ``transfer_failed`` envelope.

    The upload direction identifies the responding server as
    ``receiver_server`` (not ``origin_server``): no file has been
    produced, so the server is the receiver of an attempted upload.
    See spec §"Transfer Methods / http_upload".
    """
    return {
        "error": "transfer_failed",
        "method": "http_upload",
        "receiver_server": receiver_server,
        "origin_id": origin_id,
        "message": message,
    }


def _transfer_exhausted(
    *,
    origin_server: str,
    origin_id: str,
    attempted_methods: list[str],
    message: str,
    attempt_errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a ``transfer_exhausted`` envelope (spec §"Step 3: Exhaustion").

    ``attempt_errors`` is non-spec but the spec is silent on carrying
    per-attempt failure reasons. Including them is strictly additive
    (spec-aware clients ignore unknown fields) and gives operators
    something to grep for when transfer_exhausted lands in production.
    """
    out: dict[str, Any] = {
        "error": "transfer_exhausted",
        "origin_server": origin_server,
        "origin_id": origin_id,
        "attempted_methods": attempted_methods,
        "message": message,
    }
    if attempt_errors:
        out["attempt_errors"] = attempt_errors
    return out


# ---------------------------------------------------------------------------
# fetch_file tool
# ---------------------------------------------------------------------------


def _register_fetch_file(
    mcp: FastMCP, handle: FileExchangeHandle, sink: ConsumerSink
) -> None:
    """Register the spec-compliant ``fetch_file`` MCP tool."""

    @mcp.tool(name=handle.fetch_tool_name)
    async def fetch_file(
        file_ref: dict[str, Any] | None = None,
        url: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a file reference (or URL) and hand the bytes to a sink.

        Accepts either a full ``file_ref`` dict (see spec §"File
        Reference") or a bare ``url`` (``exchange://`` or
        ``http(s)://``). When given a ``file_ref``, walks ``transfer``
        in spec priority (``exchange`` before ``http``), building
        ``remaining_transfer`` on each method failure per spec
        §"Transfer Negotiation".

        HTTP redirects are NOT followed — producers configure
        non-redirecting download URLs (the spec mandates one-time
        unguessable URLs which are issued already-resolved). This
        avoids redirect-based SSRF where a redirect target would slip
        past the up-front host guard.
        """
        if (file_ref is None) == (url is None):
            return {
                "error": "invalid_input",
                "message": "fetch_file requires exactly one of file_ref or url",
            }
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path

        if url is not None:
            return await _fetch_via_url(handle, sink, url, params)

        # url is None here (the (file_ref is None) == (url is None)
        # check above guarantees one is set).
        if file_ref is None:
            raise RuntimeError("fetch_file: input validation should have caught this")
        return await _fetch_via_file_ref(handle, sink, file_ref, params)


async def _fetch_via_url(
    handle: FileExchangeHandle,
    sink: ConsumerSink,
    url: str,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    parts = urlsplit(url)
    if parts.scheme == "exchange":
        # Derive origin_server from the URI so the error envelope is
        # informative even when the caller passed only a bare URL.
        try:
            parsed = ExchangeURI.parse(url)
            origin_server = parsed.namespace
            origin_id = parsed.id
        except ExchangeURIError:
            origin_server = ""
            origin_id = ""
        try:
            return await _consume_exchange(
                handle, sink, url, file_ref=None, params=params
            )
        except (
            ExchangeURIError,
            ExchangeGroupMismatch,
            FileExchangeConfigError,
            OSError,
        ) as exc:
            logger.warning("fetch_file exchange url failed: %s", exc)
            return _transfer_failed(
                origin_server=origin_server,
                origin_id=origin_id,
                method="exchange",
                message=str(exc),
            )
    if parts.scheme in ("http", "https"):
        try:
            return await _consume_http(handle, sink, url, file_ref=None, params=params)
        except FetchTransportError as exc:
            logger.warning("fetch_file http url failed: %s", exc)
            return _transfer_failed(
                origin_server="",
                origin_id="",
                method="http",
                message=str(exc),
            )
    return {
        "error": "invalid_input",
        "message": f"unsupported URL scheme {parts.scheme!r}; expected "
        "exchange:// or http(s)://",
    }


# Methods this consumer can attempt directly when handed a file_ref.
# ``http`` is excluded because spec §"Transfer Negotiation / Step 2 /
# For http" assigns URL-acquisition to the *client* (call producer's
# tool, get URL, hand URL back to fetch_file). The consumer can't
# dispatch the producer's tool itself, so listing http here would only
# manufacture spurious failures and send naive clients into retry
# loops on `remaining_transfer`.
_CONSUMER_DISPATCHABLE_METHODS = ("exchange",)


async def _fetch_via_file_ref(
    handle: FileExchangeHandle,
    sink: ConsumerSink,
    raw: dict[str, Any],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        ref = FileRef.from_dict(raw)
    except (ValueError, TypeError) as exc:
        return {"error": "invalid_input", "message": str(exc)}

    method_order = [m for m in _CONSUMER_DISPATCHABLE_METHODS if m in ref.transfer]

    if not method_order:
        # Nothing this consumer can dispatch directly. If the producer
        # offers http, point the client at the orchestration it has to
        # do itself (per spec §"Transfer Negotiation / Step 2 / For
        # http"). Use a non-`transfer_failed` error code so the client
        # doesn't keep retrying the same shape.
        if "http" in ref.transfer:
            tool = ref.transfer["http"].get("tool")
            return {
                "error": "client_orchestration_required",
                "origin_server": ref.origin_server,
                "origin_id": ref.origin_id,
                "method": "http",
                "http_tool": tool,
                "message": (
                    "the http transfer method requires client "
                    f"orchestration: call {tool!r} on "
                    f"{ref.origin_server!r} to obtain a download URL, "
                    "then call fetch_file(url=...) here"
                ),
            }
        return _transfer_exhausted(
            origin_server=ref.origin_server,
            origin_id=ref.origin_id,
            attempted_methods=[],
            message="no transfer methods this consumer can dispatch",
        )

    attempted: list[str] = []
    attempt_errors: list[dict[str, str]] = []
    for i, method in enumerate(method_order):
        attempted.append(method)
        meta = ref.transfer[method]
        remaining = {m: dict(ref.transfer[m]) for m in method_order[i + 1 :]}
        # If the producer offered http alongside exchange, surface it as
        # remaining so a client can fall through to client-orchestrated
        # http after our exchange attempt fails.
        if "http" in ref.transfer and "http" not in remaining:
            remaining["http"] = dict(ref.transfer["http"])
        if method == "exchange":
            uri = meta.get("uri")
            if not isinstance(uri, str) or not uri:
                attempt_errors.append(
                    {
                        "method": "exchange",
                        "error": "invalid_uri",
                        "message": (
                            "exchange URI is missing or empty in "
                            "file_ref.transfer['exchange']"
                        ),
                    }
                )
                continue
            try:
                return await _consume_exchange(
                    handle, sink, uri, file_ref=ref, params=params
                )
            except (
                ExchangeURIError,
                ExchangeGroupMismatch,
                FileExchangeConfigError,
                OSError,
            ) as exc:
                logger.warning("fetch_file exchange method failed: %s", exc)
                attempt_errors.append(
                    {
                        "method": "exchange",
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                if remaining:
                    return _transfer_failed(
                        origin_server=ref.origin_server,
                        origin_id=ref.origin_id,
                        method="exchange",
                        message=str(exc),
                        remaining_transfer=remaining,
                    )
                continue

    return _transfer_exhausted(
        origin_server=ref.origin_server,
        origin_id=ref.origin_id,
        attempted_methods=attempted,
        message="no transfer method succeeded",
        attempt_errors=attempt_errors,
    )


async def _consume_exchange(
    handle: FileExchangeHandle,
    sink: ConsumerSink,
    uri: str,
    *,
    file_ref: FileRef | None,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    if handle.exchange is None:
        raise FileExchangeConfigError(
            "exchange method requested but MCP_EXCHANGE_DIR is not configured"
        )
    data = await asyncio.to_thread(
        handle.exchange.read_exchange_uri,
        uri,
        max_bytes=_DEFAULT_HTTP_FETCH_MAX_BYTES,
    )
    ctx = FetchContext(
        url=uri,
        file_ref=file_ref,
        mime_type=file_ref.mime_type if file_ref else None,
        suggested_filename=None,
        params=params,
        handle=handle,
    )
    result = await sink(data, ctx)
    return _sink_response(result, method="exchange")


async def _consume_http(
    handle: FileExchangeHandle,
    sink: ConsumerSink,
    url: str,
    *,
    file_ref: FileRef | None,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    _ssrf_guard(url)

    client = handle._get_http_client()
    try:
        async with client.stream("GET", url) as resp:
            # ``raise_for_status`` only raises for 4xx/5xx — a 3xx
            # redirect with follow_redirects=False would otherwise
            # stream the redirect body (a small "Moved" HTML page)
            # as if it were the file content, silently corrupting
            # what reaches the consumer sink.
            if resp.is_redirect:
                raise FetchTransportError(
                    f"http fetch failed: redirect ({resp.status_code}) not allowed"
                )
            resp.raise_for_status()
            # Fail fast if the server's advertised Content-Length
            # already exceeds the cap, so we don't waste a single
            # chunk's worth of bandwidth on a response we'll
            # reject anyway. The streaming check below stays as
            # defence in depth for servers that lie about content
            # length or omit the header.
            cl_str = resp.headers.get("content-length")
            if (
                cl_str
                and cl_str.isdigit()
                and int(cl_str) > _DEFAULT_HTTP_FETCH_MAX_BYTES
            ):
                raise FetchTransportError(
                    f"response exceeds {_DEFAULT_HTTP_FETCH_MAX_BYTES} "
                    f"bytes (content-length={cl_str})"
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _DEFAULT_HTTP_FETCH_MAX_BYTES:
                    raise FetchTransportError(
                        f"response exceeds {_DEFAULT_HTTP_FETCH_MAX_BYTES} bytes"
                    )
                chunks.append(chunk)
            data = b"".join(chunks)
            content_type = resp.headers.get("content-type")
            suggested = _filename_from_disposition(
                resp.headers.get("content-disposition")
            )
    except httpx.HTTPError as exc:
        # Translate the entire httpx error hierarchy (timeouts, connect
        # errors, status errors, transport errors) into our own domain
        # error so callers don't depend on httpx types.
        raise FetchTransportError(f"http fetch failed: {exc}") from exc

    ctx = FetchContext(
        url=url,
        file_ref=file_ref,
        mime_type=content_type or (file_ref.mime_type if file_ref else None),
        suggested_filename=suggested,
        params=params,
        handle=handle,
    )
    result = await sink(data, ctx)
    return _sink_response(result, method="http")


def _sink_response(result: FetchResult, *, method: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "method": method,
        "bytes_written": result.bytes_written,
    }
    if result.stored_at is not None:
        out["stored_at"] = result.stored_at
    if result.extra:
        out.update(dict(result.extra))
    return out


# Hostnames that aren't IP literals but are well-known aliases for
# loopback or cloud metadata endpoints. The check is exact-match
# case-insensitive — DNS-name patterns (``*.internal``) and
# resolver-side games (``localtest.me`` → 127.0.0.1) are deliberately
# out of scope; the goal is catching the cheap LLM mistake of
# ``http://localhost/admin``, not building a perimeter firewall.
_SSRF_HOSTNAME_DENYLIST = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.aws",
        "metadata.amazonaws.com",
        "metadata.azure.com",
    }
)


def _ssrf_guard(url: str) -> None:
    """Reject URLs whose host is a private/loopback IP or a known alias.

    DNS-resolved hostnames in general are out of scope — the deployer
    is responsible for the network they expose. The guard catches the
    cheap mistakes: IP literals in private/loopback/link-local ranges
    (incl. AWS IMDS at 169.254.169.254 and IPv6 ``::1``) and the
    handful of named aliases for loopback / cloud metadata endpoints
    (``localhost``, ``metadata.google.internal``, etc.).
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in _SSRF_HOSTNAME_DENYLIST:
        raise FetchTransportError(
            f"refusing to fetch URL with denylisted hostname: {host}"
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise FetchTransportError(
            f"refusing to fetch URL with private/loopback host: {host}"
        )


def _filename_from_disposition(value: str | None) -> str | None:
    """Extract the filename from a ``Content-Disposition`` header value.

    Uses :class:`email.message.Message` so quoted strings, escaped
    characters, embedded semicolons (``filename="report;v1.csv"``), and
    RFC 5987 ``filename*=UTF-8''...`` extended forms are all handled
    correctly. Falls back to ``None`` when the header is absent or
    contains no filename parameter.
    """
    if not value:
        return None
    msg = Message()
    msg["Content-Disposition"] = value
    return msg.get_filename()


# ---------------------------------------------------------------------------
# Upload direction (intake) — public facade
# ---------------------------------------------------------------------------


_DEFAULT_UPLOAD_TOOL = "create_upload_link"
_DEFAULT_UPLOAD_TTL_SECONDS = 300.0
_DEFAULT_UPLOAD_TTL_MAX_SECONDS = 3600.0
_DEFAULT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024

PreLinkValidator = Callable[
    [str, "str | None"],
    "None | Awaitable[None]",
]


@dataclass(frozen=True)
class UploadHandle:
    """Handle returned by :func:`register_file_exchange_upload`.

    Attributes:
        namespace: The server's logical name (also the URL prefix for
            the upload route).
        tool_name: The name under which ``create_upload_link`` was
            registered (overridable; default ``"create_upload_link"``).
        enabled: ``True`` iff upload is wired (transport != stdio AND
            ``{PREFIX}_BASE_URL`` is set AND opt-in env not disabled).
        upload_store: The :class:`UploadStore` instance, or ``None``
            when upload is disabled.
        ttl_default: Default TTL applied when ``ttl_seconds`` is omitted
            on ``create_upload_link``.
        ttl_max: Operator ceiling — requested TTL is clamped to this
            value, with the effective TTL returned to the caller.
        max_bytes_default: Default ``max_bytes`` applied when caller
            omits the field.
    """

    namespace: str
    tool_name: str
    enabled: bool
    upload_store: UploadStore | None
    ttl_default: float
    ttl_max: float
    max_bytes_default: int

    def __post_init__(self) -> None:
        """Validate field invariants at construction time.

        ``enabled`` and ``upload_store is not None`` redundantly encode
        the same fact; reject inconsistent combinations so downstream
        code can rely on either form. Disabled handles pass TTL / bytes
        through as caller introspection metadata; only enabled handles
        need the positivity guards (the route would mint born-expired
        tokens otherwise).
        """
        if self.enabled != (self.upload_store is not None):
            raise ValueError(
                "UploadHandle.enabled must agree with "
                "(upload_store is not None) — got "
                f"enabled={self.enabled!r}, "
                f"upload_store={self.upload_store!r}"
            )
        if self.enabled:
            if self.ttl_default <= 0:
                raise ValueError(
                    f"ttl_default must be > 0 when enabled; got {self.ttl_default}"
                )
            if self.ttl_max <= 0:
                raise ValueError(
                    f"ttl_max must be > 0 when enabled; got {self.ttl_max}"
                )
            if self.max_bytes_default <= 0:
                raise ValueError(
                    "max_bytes_default must be > 0 when enabled; got "
                    f"{self.max_bytes_default}"
                )

    def create_link(
        self,
        *,
        origin_id: str,
        ttl_seconds: float | None = None,
        max_bytes: int | None = None,
        destination: str | None = None,
        content_type: str | None = None,
    ) -> tuple[str, float, int]:
        """Mint an upload reservation directly. Escape valve for advanced wraps.

        Returns:
            ``(upload_url, effective_ttl_seconds, effective_max_bytes)``.

        Raises:
            RuntimeError: if upload is not enabled (transport is stdio,
                ``{PREFIX}_BASE_URL`` unset, or upload disabled by env).
        """
        if self.upload_store is None:
            raise RuntimeError(
                "upload not enabled (transport=stdio, missing BASE_URL, or disabled)"
            )
        if ttl_seconds is None or ttl_seconds <= 0:
            ttl = float(self.ttl_default)
        else:
            ttl = float(ttl_seconds)
        ttl = min(ttl, self.ttl_max)
        if max_bytes is None or max_bytes <= 0:
            cap = int(self.max_bytes_default)
        else:
            cap = min(int(max_bytes), int(self.max_bytes_default))
        token = self.upload_store.reserve(
            origin_id=origin_id,
            max_bytes=cap,
            ttl_seconds=ttl,
            destination=destination,
            content_type=content_type,
        )
        return self.upload_store.build_url(token), ttl, cap


def _disabled_upload_handle(
    *,
    namespace: str,
    ttl_default: float,
    ttl_max: float,
    max_bytes_default: int,
) -> UploadHandle:
    """Return a no-op UploadHandle for the upload-disabled path."""
    return UploadHandle(
        namespace=namespace,
        tool_name=_DEFAULT_UPLOAD_TOOL,
        enabled=False,
        upload_store=None,
        ttl_default=ttl_default,
        ttl_max=ttl_max,
        max_bytes_default=max_bytes_default,
    )


def register_file_exchange_upload(
    mcp: FastMCP,
    *,
    namespace: str,
    env_prefix: str,
    receiver: BufferedReceiver | None = None,
    stream_receiver: StreamReceiver | None = None,
    pre_link_validator: PreLinkValidator | None = None,
    accepts: tuple[str, ...] = ("*/*",),
) -> UploadHandle:
    """Wire MCP File Exchange upload direction onto ``mcp``.

    Mirrors :func:`register_file_exchange` for the inbound half. Exactly
    one of ``receiver`` / ``stream_receiver`` MUST be supplied. The
    helper:

    1. Resolves transport from env (``{PREFIX}_TRANSPORT`` /
       ``FASTMCP_TRANSPORT``).
    2. Reads ``{PREFIX}_UPLOAD_ENABLED`` (default ``true``),
       ``{PREFIX}_UPLOAD_MAX_BYTES``, ``{PREFIX}_UPLOAD_TTL``,
       ``{PREFIX}_UPLOAD_TTL_MAX`` for operator-side configuration.
    3. Builds an :class:`UploadStore`, mounts ``POST /<namespace>/uploads/{token}``
       via :func:`register_upload_route`, installs the module-level
       singleton.
    4. Registers an MCP tool named ``create_upload_link`` with tags
       ``{"write"}``. The tool wraps :meth:`UploadHandle.create_link`
       and runs ``pre_link_validator`` (if provided) before token
       creation so an invalid ``origin_id`` or ``destination`` surfaces
       as a clean in-band rejection rather than after a wasted upload
       round-trip.

    When transport is stdio OR ``{PREFIX}_BASE_URL`` is unset, the
    helper returns an :class:`UploadHandle` with ``enabled=False`` and
    no route or tool registered.

    The mounted route consumes the token at the lookup step for
    anti-replay safety: every non-success response (4xx, 5xx) burns
    the token. Clients MUST re-call ``create_upload_link`` to retry
    after any failure — a retry on the same URL returns 404, not the
    original error code.

    Args:
        mcp: The :class:`fastmcp.FastMCP` server instance.
        namespace: Logical server name; used as the URL prefix for the
            upload route.
        env_prefix: Per-server env var prefix (e.g.
            ``"MARKDOWN_VAULT_MCP"``).
        receiver: Buffered receiver; mutually exclusive with
            ``stream_receiver``.
        stream_receiver: Streaming receiver; receives chunks live.
        pre_link_validator: Optional callback
            ``(origin_id, destination) -> None`` (sync OR async) run
            inside the registered tool AFTER baseline ``origin_id``
            character validation but BEFORE token creation. Raising
            ``ValueError`` surfaces as an in-band ``transfer_failed``
            envelope to the caller. Any other exception type also
            propagates but is additionally logged at ERROR with a
            "non-ValueError" marker so operators can distinguish
            server-side validator bugs from caller-input errors.
            ``ValueError`` is the conventional choice for caller-facing
            diagnostics. Sync validators run in an asyncio threadpool
            (``asyncio.to_thread``) so blocking work (DB lookups,
            filesystem checks) does not stall the event loop; async
            validators run on the loop.
        accepts: ``Content-Type`` filter at the route layer. Default
            ``("*/*",)`` disables the gate. See
            :func:`register_upload_route` for semantics. Also used as a
            pre-filter in ``create_upload_link``: a ``content_type`` hint
            that does not match ``accepts`` returns a ``transfer_failed``
            envelope in-band before any token is minted.

    Returns:
        An :class:`UploadHandle`. Stash if you need ``create_link`` for
        advanced wrapping; otherwise discard — registration side-effects
        are sufficient for normal use.

    Raises:
        ValueError: if neither or both of ``receiver``/``stream_receiver``
            are supplied.
    """
    if (receiver is None) == (stream_receiver is None):
        raise ValueError(
            "register_file_exchange_upload requires exactly one of "
            "receiver= or stream_receiver="
        )

    max_bytes_default = _DEFAULT_UPLOAD_MAX_BYTES
    ttl_default = _DEFAULT_UPLOAD_TTL_SECONDS
    ttl_max = _DEFAULT_UPLOAD_TTL_MAX_SECONDS

    # Env-driven knob overrides — validate first so a malformed value
    # fails fast with an operator-readable error before any state is
    # half-installed (transport resolution, set_upload_store, route
    # registration). A bare ``int()``/``float()`` would surface a raw
    # ``ValueError`` whose message does not name the offending env var.
    mb_raw = env(env_prefix, "UPLOAD_MAX_BYTES")
    if mb_raw:
        try:
            max_bytes_default = int(mb_raw)
        except ValueError as exc:
            raise ConfigurationError(
                f"{env_prefix}_UPLOAD_MAX_BYTES must be a positive "
                f"integer; got {mb_raw!r}"
            ) from exc
        if max_bytes_default <= 0:
            raise ConfigurationError(
                f"{env_prefix}_UPLOAD_MAX_BYTES must be positive; "
                f"got {max_bytes_default}"
            )
    ttl_raw = env(env_prefix, "UPLOAD_TTL")
    if ttl_raw:
        try:
            ttl_default = float(ttl_raw)
        except ValueError as exc:
            raise ConfigurationError(
                f"{env_prefix}_UPLOAD_TTL must be a positive number "
                f"(seconds); got {ttl_raw!r}"
            ) from exc
        if ttl_default <= 0:
            raise ConfigurationError(
                f"{env_prefix}_UPLOAD_TTL must be positive; got {ttl_default}"
            )
    ttl_max_raw = env(env_prefix, "UPLOAD_TTL_MAX")
    if ttl_max_raw:
        try:
            ttl_max = float(ttl_max_raw)
        except ValueError as exc:
            raise ConfigurationError(
                f"{env_prefix}_UPLOAD_TTL_MAX must be a positive number "
                f"(seconds); got {ttl_max_raw!r}"
            ) from exc
        if ttl_max <= 0:
            raise ConfigurationError(
                f"{env_prefix}_UPLOAD_TTL_MAX must be positive; got {ttl_max}"
            )

    resolved_transport = _resolve_transport(env_prefix)
    upload_enabled_env = parse_bool(env(env_prefix, "UPLOAD_ENABLED", "true"))
    enabled = resolved_transport != "stdio" and upload_enabled_env
    if not enabled:
        if resolved_transport == "stdio":
            logger.info(
                "register_file_exchange_upload(%s): upload disabled (transport=stdio)",
                namespace,
            )
        else:
            logger.info(
                "register_file_exchange_upload(%s): upload disabled "
                "(%s_UPLOAD_ENABLED=false)",
                namespace,
                env_prefix,
            )
        return _disabled_upload_handle(
            namespace=namespace,
            ttl_default=ttl_default,
            ttl_max=ttl_max,
            max_bytes_default=max_bytes_default,
        )

    base_url = env(env_prefix, "BASE_URL")
    if not base_url:
        logger.warning(
            "register_file_exchange_upload: %s_BASE_URL not set; upload "
            "endpoint disabled (would mint links without a public URL)",
            env_prefix,
        )
        return _disabled_upload_handle(
            namespace=namespace,
            ttl_default=ttl_default,
            ttl_max=ttl_max,
            max_bytes_default=max_bytes_default,
        )

    store = UploadStore(
        ttl_seconds=ttl_default,
        base_url=base_url,
        route_path=f"/{namespace}/uploads/{{token}}",
    )
    # Module-level singleton: route handler uses closure-captured store
    # (not this), so the singleton is for downstream `get_upload_store()`
    # convenience. Multi-direction registration: last-caller-wins; see
    # follow-up issue #65 for the architectural fix.
    set_upload_store(store)
    register_upload_route(
        mcp,
        store=store,
        namespace=namespace,
        receiver=receiver,
        stream_receiver=stream_receiver,
        accepts=accepts,
    )

    handle = UploadHandle(
        namespace=namespace,
        tool_name=_DEFAULT_UPLOAD_TOOL,
        enabled=True,
        upload_store=store,
        ttl_default=ttl_default,
        ttl_max=ttl_max,
        max_bytes_default=max_bytes_default,
    )

    # Push upload contribution into the shared per-FastMCP builder so a
    # paired ``register_file_exchange`` (download direction) advertises
    # both halves under one ``http`` block instead of clobbering.
    # ``accepts`` is passed verbatim — including the default
    # ``("*/*",)`` wildcard. Per spec §"Transfer Methods / http_upload",
    # an absent ``accepts`` key means the route inherits the server-wide
    # ``consumes`` list; if a server has a non-empty ``consumes`` AND a
    # wildcard route, omitting ``accepts`` would mislead clients into
    # thinking only ``consumes`` types are accepted. Advertising
    # ``["*/*"]`` explicitly keeps the wire signal aligned with the
    # route's actual permissiveness.
    builder = _get_or_create_builder(mcp, namespace=namespace)
    builder.set_http_upload_sink(
        tool_name=_DEFAULT_UPLOAD_TOOL,
        max_bytes=int(max_bytes_default),
        max_ttl_seconds=int(ttl_max),
        accepts=accepts,
    )
    _emit_capability(mcp)

    @mcp.tool(name=_DEFAULT_UPLOAD_TOOL, tags={"write"})
    async def create_upload_link(
        origin_id: str,
        destination: str | None = None,
        content_type: str | None = None,
        ttl_seconds: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        r"""Mint a one-time HTTPS POST URL for an inbound upload.

        Args:
            origin_id: The sender's opaque stable handle for the bytes
                (the *what*). Validated against the spec's segment rules
                (§"Security and Path Resolution"): no ``/``, ``\``, ``.``,
                ``..``, control bytes, leading/trailing whitespace.
            destination: Optional destination instruction (the *where*).
                Only null bytes, control characters, and leading/trailing
                whitespace are rejected at the spec level; the receiver
                validates the rest per its own domain rules via
                ``pre_link_validator``.
            content_type: Optional hint of the ``Content-Type`` the POST
                will declare; pre-filtered against the receiver's
                ``accepts`` list.
            ttl_seconds: Optional requested lifetime in seconds; clamped
                to the server's TTL ceiling.
            max_bytes: Optional requested body cap; clamped to the
                server's ``max_bytes`` ceiling.

        Returns:
            On success, ``{url, ttl_seconds, max_bytes}`` — the effective
            (post-clamp) values. On in-band rejection, a ``transfer_failed``
            envelope.
        """
        # origin_id: strict spec segment grammar (the WHAT identifier).
        ExchangeURI.validate_segment(origin_id, role="json_param")
        # destination: relaxed validation (the WHERE) — reject only null
        # bytes, control chars, and leading/trailing whitespace; the
        # receiver validates the rest.
        if destination is not None:
            if destination != destination.strip():
                return _upload_transfer_failed(
                    receiver_server=namespace,
                    origin_id=origin_id,
                    message="destination must not have leading or trailing whitespace",
                )
            if any(ord(c) < 0x20 for c in destination):
                return _upload_transfer_failed(
                    receiver_server=namespace,
                    origin_id=origin_id,
                    message="destination must not contain control characters",
                )
        # content_type hint: pre-filter against the receiver's accepts
        # list so a mismatched hint is rejected in-band, before a wasted
        # POST round-trip (the route still enforces 415 on the actual
        # POST Content-Type header). _accepts_match is imported from
        # _file_exchange_runtime — add it to that module's imports in
        # file_exchange.py.
        if content_type is not None and not _accepts_match(content_type, accepts):
            return _upload_transfer_failed(
                receiver_server=namespace,
                origin_id=origin_id,
                message=f"content_type {content_type!r} is not accepted",
            )
        if pre_link_validator is not None:
            try:
                if inspect.iscoroutinefunction(pre_link_validator):
                    await pre_link_validator(origin_id, destination)
                else:
                    validator_result = await asyncio.to_thread(
                        pre_link_validator, origin_id, destination
                    )
                    if inspect.isawaitable(validator_result):
                        await validator_result
            except ValueError as exc:
                # Caller-facing rejection — return the spec transfer_failed
                # envelope rather than letting it surface as a tool error.
                return _upload_transfer_failed(
                    receiver_server=namespace,
                    origin_id=origin_id,
                    message=str(exc),
                )
            except Exception:
                logger.exception(
                    "pre_link_validator raised non-ValueError "
                    "(origin_id=%r) — server-side bug, not a client "
                    "validation failure",
                    origin_id,
                )
                raise
        url, eff_ttl, eff_max_bytes = handle.create_link(
            origin_id=origin_id,
            ttl_seconds=ttl_seconds,
            max_bytes=max_bytes,
            destination=destination,
            content_type=content_type,
        )
        return {
            "url": url,
            "ttl_seconds": int(eff_ttl),
            "max_bytes": int(eff_max_bytes),
        }

    return handle


__all__ = [
    "ConsumerSink",
    "FetchContext",
    "FetchResult",
    "FileExchangeHandle",
    "PreLinkValidator",
    "UploadHandle",
    "register_file_exchange",
    "register_file_exchange_upload",
]
