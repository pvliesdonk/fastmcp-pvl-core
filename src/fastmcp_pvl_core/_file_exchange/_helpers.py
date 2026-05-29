"""Umbrella tool-registration helpers for the file-exchange extension (#148).

Provides one setup call (``register_file_exchange``) that wires the
cross-cutting infrastructure once, plus four per-role helpers
(``register_file_exchange_provider`` / ``_receiver`` / ``_fetcher``
/ ``_sender``). Provider and receiver are decorators on
downstream-owned tool bodies; fetcher and sender are fully-generated
tool registrations.

See ``docs/superpowers/specs/2026-05-28-file-exchange-148-umbrella-helpers-design.md``
for the architecture and the per-role contracts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._download import (
    download_fetcher_consume,
    download_provider_mint,
)
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._filesystem import (
    filesystem_fetcher_consume,
    filesystem_sender_consume,
    filesystem_sink_writable,
    filesystem_source_readable,
)
from fastmcp_pvl_core._file_exchange._routes import register_file_exchange_routes
from fastmcp_pvl_core._file_exchange._selection import select_sink, select_source
from fastmcp_pvl_core._file_exchange._tokens import (
    CapabilityTokenStore,
    build_capability_token_store,
)
from fastmcp_pvl_core._file_exchange._upload import (
    upload_receiver_mint,
    upload_sender_consume,
)
from fastmcp_pvl_core._file_exchange._wire import (
    DownloadSource,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    TransferHandle,
    UploadSink,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
    from fastmcp_pvl_core._file_exchange._paths import VolumeMap


@dataclass(frozen=True)
class FileExchangeContext:
    """Shared state produced by :func:`register_file_exchange`.

    Consumed by the four per-role helpers.

    Frozen so a downstream that holds it cannot accidentally swap in a
    different token store or hook out from under in-flight registrations.
    """

    token_store: CapabilityTokenStore
    base_url: str
    config: ServerConfig
    source: ArtifactSource | None
    sink: ArtifactSink | None
    volume_map: VolumeMap | None = None


def register_file_exchange(
    mcp: FastMCP,
    *,
    config: ServerConfig,
    base_url: str,
    source: ArtifactSource | None = None,
    sink: ArtifactSink | None = None,
    volume_map: VolumeMap | None = None,
) -> FileExchangeContext:
    """One-shot file-exchange setup.

    Builds the token store, mounts the routes, and (in a later task)
    declares the Tasks capability. Returns the context the per-tool
    helpers consume.

    Kwargs (per CLAUDE.md classification):

    - ``config`` (**config**): operator-side ``ServerConfig``.
    - ``base_url`` (**config**): origin URL the capability URLs encode.
    - ``source`` (**hook**): downstream's :class:`ArtifactSource` — required
      if any provider or sender helper will be registered later.
    - ``sink`` (**hook**): downstream's :class:`ArtifactSink` — required if
      any receiver or fetcher helper will be registered later.
    - ``volume_map`` (**hook**): mapping of filesystem-transport volume
      names to mount roots — required only if a peer ever selects a
      ``filesystem`` descriptor through the fetcher or sender helper. A
      ``None`` here is fine for HTTP-only deployments; a runtime
      ``NO_SUPPORTED_TRANSPORT`` surfaces if a filesystem descriptor is
      actually selected without a configured map.

    Mounting validation (``source``-or-``sink``, ``sink``-needs-``config``)
    is delegated to :func:`register_file_exchange_routes`; the per-tool
    helpers further raise ``ValueError`` at *their* registration time if
    they need a hook the context lacks.
    """
    token_store = build_capability_token_store(config)
    register_file_exchange_routes(
        mcp,
        token_store=token_store,
        source=source,
        sink=sink,
        config=config,
    )
    # §14: declare the server-level Tasks capability so peers know this
    # server accepts tools/call as a task submission. Direct dict mutation
    # on ``experimental_capabilities`` — FastMCP merges this into the
    # wire capability advertisement at request time. We deliberately do
    # NOT set ``_support_tasks_by_default`` because that flips per-tool
    # ``task=True`` defaults that require the ``fastmcp[tasks]`` extra
    # (docket scheduler); the helpers here register tools without
    # ``task=True`` and only need the *capability declaration* per §14.
    mcp.experimental_capabilities["tasks"] = {"requests": {"tools": {"call": True}}}
    return FileExchangeContext(
        token_store=token_store,
        base_url=base_url,
        config=config,
        source=source,
        sink=sink,
        volume_map=volume_map,
    )


def register_file_exchange_provider(
    mcp: FastMCP,
    tool_name: str,
    fxctx: FileExchangeContext,
) -> Callable[
    [Callable[..., Awaitable[Any]]], Callable[..., Awaitable[TransferHandle]]
]:
    """Decorate a downstream ``(metadata, key)`` producer as a provider tool.

    The decorated function's parameters become the MCP tool's parameters;
    the decorator mints a :class:`TransferHandle` from the returned
    ``(metadata, key)`` and returns *that* as the tool's response. The
    source hook is NOT called at mint time — only when the peer fetches
    the minted capability URL.

    Raises ``ValueError`` at decoration time if ``fxctx.source`` is
    ``None`` — fail loudly at startup, not at first peer call.
    """
    if fxctx.source is None:
        raise ValueError(
            f"register_file_exchange_provider({tool_name!r}): fxctx has "
            "no source — set source= when calling register_file_exchange"
        )

    def _wrap(
        fn: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[TransferHandle]]:
        # Wrapper preserves the inner signature (so the MCP tool schema
        # comes from ``fn`` directly) and mints the handle on the way out.
        from functools import wraps

        @wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> TransferHandle:
            metadata, key = await fn(*args, **kwargs)
            return await download_provider_mint(
                metadata,
                key,
                token_store=fxctx.token_store,
                base_url=fxctx.base_url,
                ttl=fxctx.config.file_exchange_token_ttl,
                single_use=True,
            )

        # Task 7 adds the taskSupport annotation here.
        mcp.tool(name=tool_name)(_wrapped)
        return _wrapped

    return _wrap


def register_file_exchange_fetcher(
    mcp: FastMCP,
    tool_name: str,
    fxctx: FileExchangeContext,
) -> None:
    """Generate and register the file-exchange fetcher tool.

    Not a decorator — pvl-core owns the entire tool body because its
    input is the spec-defined :class:`TransferHandle` and there is
    nothing domain-specific for downstream to write. The generated tool
    selects a usable source descriptor via :func:`select_source` and
    dispatches to the appropriate per-transport fetcher
    (``filesystem`` or ``download``). Bytes land in ``fxctx.sink``; the
    tool returns ``None``.

    Raises ``ValueError`` at registration time if ``fxctx.sink`` is
    ``None`` — fail loudly at startup, not at first peer call.

    The generated tool raises ``FileExchangeTransferError`` with
    ``TransferErrorCode.NO_SUPPORTED_TRANSPORT`` (§13) when the
    submitted handle carries no descriptor satisfying pvl-core's known
    transports.
    """
    if fxctx.sink is None:
        raise ValueError(
            f"register_file_exchange_fetcher({tool_name!r}): fxctx has "
            "no sink — set sink= when calling register_file_exchange"
        )
    sink = fxctx.sink  # bind locally so mypy keeps the non-None narrowing

    async def _consume_transfer(handle: TransferHandle) -> None:
        is_accessible = (
            filesystem_source_readable(fxctx.volume_map)
            if fxctx.volume_map is not None
            else None
        )
        descriptor = select_source(handle, is_accessible=is_accessible)
        if descriptor is None:
            raise FileExchangeTransferError(
                TransferErrorCode.NO_SUPPORTED_TRANSPORT,
                transport=None,
                detail="no usable source in handle",
            )
        if isinstance(descriptor, FilesystemSource):
            # Filesystem support is opt-in at register_file_exchange time
            # via volume_map=. If a filesystem descriptor was selected but
            # no volume_map was configured, the server cannot satisfy the
            # transfer — surface a §13 NO_SUPPORTED_TRANSPORT so the peer
            # gets a clean envelope rather than an internal TypeError.
            if fxctx.volume_map is None:
                raise FileExchangeTransferError(
                    TransferErrorCode.NO_SUPPORTED_TRANSPORT,
                    transport="filesystem",
                    detail=(
                        "filesystem transport requires a volume_map at "
                        "register_file_exchange time"
                    ),
                )
            await filesystem_fetcher_consume(
                handle, descriptor, sink, volume_map=fxctx.volume_map
            )
        elif isinstance(descriptor, DownloadSource):
            await download_fetcher_consume(
                handle, descriptor, sink, config=fxctx.config
            )
        else:
            # select_source filters UnknownTransportDescriptor; defensive
            # guard for any forward-compat wire payload that slips through.
            raise FileExchangeTransferError(
                TransferErrorCode.NO_SUPPORTED_TRANSPORT,
                transport=descriptor.transport,
                detail=f"unsupported transport {descriptor.transport!r}",
            )

    _consume_transfer.__name__ = tool_name
    # Task 7 adds the taskSupport annotation here.
    mcp.tool(name=tool_name)(_consume_transfer)


def register_file_exchange_receiver(
    mcp: FastMCP,
    tool_name: str,
    fxctx: FileExchangeContext,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[IntakeTicket]]]:
    """Decorate a downstream ``(artifact_id, expected)`` producer as a receiver tool.

    The decorated function's parameters become the MCP tool's parameters;
    the decorator mints an :class:`IntakeTicket` from the returned
    ``(artifact_id, expected)`` and returns *that* as the tool's response.
    The sink hook is NOT called at mint time — only when the peer PUTs
    bytes to the minted capability URL.

    Raises ``ValueError`` at decoration time if ``fxctx.sink`` is
    ``None`` — fail loudly at startup, not at first peer call.
    """
    if fxctx.sink is None:
        raise ValueError(
            f"register_file_exchange_receiver({tool_name!r}): fxctx has "
            "no sink — set sink= when calling register_file_exchange"
        )

    def _wrap(
        fn: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[IntakeTicket]]:
        from functools import wraps

        @wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> IntakeTicket:
            artifact_id, expected = await fn(*args, **kwargs)
            return await upload_receiver_mint(
                artifact_id,
                token_store=fxctx.token_store,
                base_url=fxctx.base_url,
                ttl=fxctx.config.file_exchange_token_ttl,
                expected=expected,
            )

        # Task 7 adds the taskSupport annotation here.
        mcp.tool(name=tool_name)(_wrapped)
        return _wrapped

    return _wrap


def register_file_exchange_sender(
    mcp: FastMCP,
    tool_name: str,
    fxctx: FileExchangeContext,
) -> None:
    """Generate and register the file-exchange sender tool.

    Symmetric counterpart to :func:`register_file_exchange_fetcher` for
    the push direction. Not a decorator — pvl-core owns the entire tool
    body because its inputs are the spec-defined :class:`IntakeTicket`
    plus a downstream-supplied ``key`` (the local identifier for the
    artifact being sent). The generated tool selects a usable sink
    descriptor via :func:`select_sink` and dispatches to the
    appropriate per-transport sender (``filesystem`` or ``upload``).
    Bytes are pulled from ``fxctx.source``; the tool returns ``None``.

    Raises ``ValueError`` at registration time if ``fxctx.source`` is
    ``None`` — fail loudly at startup, not at first peer call.

    The generated tool raises ``FileExchangeTransferError`` with
    ``TransferErrorCode.NO_SUPPORTED_TRANSPORT`` (§13) when the
    submitted ticket carries no descriptor satisfying pvl-core's known
    transports, or when a filesystem descriptor is selected but
    ``fxctx.volume_map`` was not configured at setup.
    """
    if fxctx.source is None:
        raise ValueError(
            f"register_file_exchange_sender({tool_name!r}): fxctx has "
            "no source — set source= when calling register_file_exchange"
        )
    source = fxctx.source  # bind locally so mypy keeps the non-None narrowing

    async def _send_to_receiver(ticket: IntakeTicket, key: str) -> None:
        is_accessible = (
            filesystem_sink_writable(fxctx.volume_map)
            if fxctx.volume_map is not None
            else None
        )
        descriptor = select_sink(ticket, is_accessible=is_accessible)
        if descriptor is None:
            raise FileExchangeTransferError(
                TransferErrorCode.NO_SUPPORTED_TRANSPORT,
                transport=None,
                detail="no usable sink in ticket",
            )
        if isinstance(descriptor, FilesystemSink):
            # Filesystem support is opt-in at register_file_exchange time
            # via volume_map=. If a filesystem descriptor was selected but
            # no volume_map was configured, the server cannot satisfy the
            # transfer — surface a §13 NO_SUPPORTED_TRANSPORT so the peer
            # gets a clean envelope rather than an internal TypeError.
            if fxctx.volume_map is None:
                raise FileExchangeTransferError(
                    TransferErrorCode.NO_SUPPORTED_TRANSPORT,
                    transport="filesystem",
                    detail=(
                        "filesystem transport requires a volume_map at "
                        "register_file_exchange time"
                    ),
                )
            await filesystem_sender_consume(
                descriptor, source, key, volume_map=fxctx.volume_map
            )
        elif isinstance(descriptor, UploadSink):
            await upload_sender_consume(descriptor, source, key, config=fxctx.config)
        else:
            # select_sink filters UnknownTransportDescriptor; defensive
            # guard for any forward-compat wire payload that slips through.
            raise FileExchangeTransferError(
                TransferErrorCode.NO_SUPPORTED_TRANSPORT,
                transport=descriptor.transport,
                detail=f"unsupported transport {descriptor.transport!r}",
            )

    _send_to_receiver.__name__ = tool_name
    # Task 7 adds the taskSupport annotation here.
    mcp.tool(name=tool_name)(_send_to_receiver)
