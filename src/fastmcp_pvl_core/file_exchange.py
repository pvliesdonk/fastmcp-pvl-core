"""MCP File Exchange — public facade.

Single-entry-point wiring for downstream MCP servers that want to
participate in the File Exchange convention (spec v0.2.5). Composes
the artifact store, the protocol surface, and the exchange-volume
runtime, and registers the spec-compliant ``create_download_link`` and
``fetch_file`` MCP tools.

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
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
from urllib.parse import urlsplit

import httpx

from fastmcp_pvl_core._artifacts import ArtifactStore, set_artifact_store
from fastmcp_pvl_core._env import env, parse_bool
from fastmcp_pvl_core._file_exchange_protocol import (
    ExchangeURI,
    ExchangeURIError,
    FileExchangeCapability,
    FileRef,
    FileRefPreview,
    register_file_exchange_capability,
)
from fastmcp_pvl_core._file_exchange_runtime import (
    ExchangeGroupMismatch,
    FileExchange,
    FileExchangeConfigError,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


_DEFAULT_DOWNLOAD_TOOL = "create_download_link"
_DEFAULT_FETCH_TOOL = "fetch_file"
_DEFAULT_TTL_SECONDS = 3600.0
_DEFAULT_HTTP_FETCH_TIMEOUT = 30.0
_DEFAULT_HTTP_FETCH_MAX_BYTES = 256 * 1024 * 1024  # 256 MiB hard cap


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
    download_tool_name: str = _DEFAULT_DOWNLOAD_TOOL
    fetch_tool_name: str = _DEFAULT_FETCH_TOOL
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
            if eager_bytes is not None:
                payload = eager_bytes
            elif eager_path is not None:
                payload = await asyncio.to_thread(eager_path.read_bytes)
                eager_bytes = payload  # cache for http reuse
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
    artifact_store: ArtifactStore | None = None,
    transport: Literal["http", "stdio", "auto"] = "auto",
    download_tool_name: str = _DEFAULT_DOWNLOAD_TOOL,
    fetch_tool_name: str = _DEFAULT_FETCH_TOOL,
) -> FileExchangeHandle:
    """Wire MCP File Exchange (v0.2.5) onto ``mcp``.

    Performs four pieces of wiring, each gated by env vars and the
    ``transport`` argument:

    1. Builds (or adopts) an :class:`ArtifactStore`, mounts its
       ``/artifacts/{token}`` route, and installs the module-level
       singleton.
    2. Resolves the :class:`FileExchange` runtime from
       ``MCP_EXCHANGE_DIR`` (deployer-controlled, unprefixed).
    3. Advertises ``experimental.file_exchange`` on the MCP
       ``initialize`` response (spec §"Capability declaration").
    4. Registers ``create_download_link`` (spec §"Transfer Methods /
       http") and ``fetch_file`` (spec §"Transfer Negotiation") MCP
       tools as appropriate for the
       resolved producer / consumer / transport state.

    Args:
        mcp: The :class:`fastmcp.FastMCP` server instance.
        namespace: This server's logical name. Used as both the
            ``FileRef.origin_server`` and the exchange namespace.
        env_prefix: Per-server env-var prefix (e.g.
            ``"IMAGE_GENERATION_MCP"``).
        produces: MIME types this server emits as file references —
            advertised in the capability declaration.
        consumes: MIME types this server can ingest via ``fetch_file``.
        consumer_sink: Required to register ``fetch_file``. Receives
            the resolved bytes and a :class:`FetchContext`; returns a
            :class:`FetchResult`.
        artifact_store: Optional pre-built store. When ``None`` and
            HTTP is enabled, the facade builds one with ``base_url``
            from ``{PREFIX}_BASE_URL`` and TTL from
            ``{PREFIX}_FILE_EXCHANGE_TTL``.
        transport: ``"auto"`` (default) infers from
            ``{PREFIX}_TRANSPORT`` / ``FASTMCP_TRANSPORT``; ``"http"``
            and ``"stdio"`` force the choice.
        download_tool_name: Override the default ``create_download_link``
            tool name.
        fetch_tool_name: Override the default ``fetch_file`` tool name.

    Returns:
        A :class:`FileExchangeHandle`. Stash it where your producer-side
        tools can reach it.
    """
    resolved_transport = _resolve_transport(env_prefix, transport)
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
    store: ArtifactStore | None = artifact_store
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
    capability: FileExchangeCapability | None = None
    if enabled:
        transfer_methods = _build_transfer_methods(
            produce=produce,
            consume=consume,
            exchange=exchange,
            store=store,
            download_tool_name=download_tool_name,
            fetch_tool_name=fetch_tool_name,
        )
        if transfer_methods:
            capability = FileExchangeCapability(
                namespace=namespace,
                exchange_id=exchange.exchange_id if exchange is not None else None,
                produces=tuple(produces) if produce else (),
                consumes=tuple(consumes) if consume else (),
                transfer_methods=transfer_methods,
            )
            register_file_exchange_capability(mcp, capability)

    handle = FileExchangeHandle(
        namespace=namespace,
        enabled=enabled,
        produce=produce,
        consume=consume,
        artifact_store=store,
        exchange=exchange,
        capability=capability,
        download_tool_name=download_tool_name,
        fetch_tool_name=fetch_tool_name,
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
    env_prefix: str, override: Literal["http", "stdio", "auto"]
) -> Literal["http", "stdio"]:
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


def _build_transfer_methods(
    *,
    produce: bool,
    consume: bool,
    exchange: FileExchange | None,
    store: ArtifactStore | None,
    download_tool_name: str,
    fetch_tool_name: str,
) -> dict[str, dict[str, Any]]:
    methods: dict[str, dict[str, Any]] = {}
    if exchange is not None and (produce or consume):
        methods["exchange"] = {}
    if produce and store is not None and store.has_base_url:
        methods["http"] = {"tool": download_tool_name}
    elif consume:
        methods["http"] = {"tool": fetch_tool_name}
    return methods


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
            data = await asyncio.to_thread(record.eager_path.read_bytes)
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

    try:
        async with httpx.AsyncClient(
            timeout=_DEFAULT_HTTP_FETCH_TIMEOUT, follow_redirects=False
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
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


__all__ = [
    "ConsumerSink",
    "FetchContext",
    "FetchResult",
    "FileExchangeHandle",
    "register_file_exchange",
]
