"""Tests for #148 umbrella helpers."""

from __future__ import annotations

from typing import BinaryIO

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _helpers
from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    ArtifactMetadata,
    IntakeTicket,
)


class _Sink:
    async def store_artifact(
        self, artifact_id: str | None, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:  # pragma: no cover - unused in setup-only test
        raise AssertionError


class _Source:
    async def open_artifact(
        self, key: str
    ):  # pragma: no cover - unused in setup-only test
        raise AssertionError


def _cfg() -> ServerConfig:
    return ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=1024,
    )


def test_register_file_exchange_returns_context_with_token_store_and_inputs():
    cfg = _cfg()
    mcp = FastMCP("t")
    source = _Source()
    sink = _Sink()
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=source,
        sink=sink,
    )
    assert isinstance(fxctx, _helpers.FileExchangeContext)
    assert isinstance(fxctx.token_store, CapabilityTokenStore)
    assert fxctx.base_url == "https://my.example"
    assert fxctx.config is cfg
    assert fxctx.source is source
    assert fxctx.sink is sink


def test_register_file_exchange_mounts_routes():
    cfg = _cfg()
    mcp = FastMCP("t")
    _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),
        sink=_Sink(),
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/d") for p in paths)
    assert any(p.startswith("/fx/u") for p in paths)


def test_register_file_exchange_source_only_mounts_download_only():
    cfg = _cfg()
    mcp = FastMCP("t")
    _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/d") for p in paths)
    assert not any(p.startswith("/fx/u") for p in paths)


def test_register_file_exchange_declares_tasks_capability():
    """The setup call advertises ``tasks.requests.tools.call`` so peers
    know the server accepts tools/call as a task submission (§14).
    Mutates ``mcp.experimental_capabilities`` (FastMCP merges this dict
    into the wire capability advertisement; this path does not require
    the ``fastmcp[tasks]`` / ``docket`` extra)."""
    cfg = _cfg()
    mcp = FastMCP("t")
    _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),
    )
    assert (
        mcp.experimental_capabilities.get("tasks", {})
        .get("requests", {})
        .get("tools", {})
        .get("call")
        is True
    )


async def test_provider_decorator_mints_transfer_handle():
    """The decorated tool returns a TransferHandle whose download
    descriptor's url is the minted capability URL; the source hook is
    NOT called at mint time."""
    cfg = _cfg()
    mcp = FastMCP("t")
    source_calls: list[str] = []

    class _RecSource:
        async def open_artifact(self, key):  # pragma: no cover - mint only
            source_calls.append(key)
            raise AssertionError

    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        source=_RecSource(),
    )

    captured_args: dict = {}

    @_helpers.register_file_exchange_provider(mcp, "get_report", fxctx)
    async def get_report(report_id: str) -> tuple[ArtifactMetadata, str]:
        captured_args["report_id"] = report_id
        return ArtifactMetadata(size=11, mimeType="application/pdf"), report_id

    # Resolve the registered tool and invoke it.
    tool = await mcp.get_tool("get_report")
    handle = await tool.fn(report_id="rpt-1")
    from fastmcp_pvl_core._file_exchange._wire import TransferHandle

    assert isinstance(handle, TransferHandle)
    assert handle.artifact.size == 11
    assert handle.artifact.mimeType == "application/pdf"
    assert len(handle.sources) == 1
    download_url = handle.sources[0].url  # type: ignore[union-attr]
    assert download_url.startswith("https://route.test/fx/d/")
    assert source_calls == []
    # The user function received its domain arg.
    assert captured_args["report_id"] == "rpt-1"


def test_provider_decorator_without_source_raises_value_error():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        sink=_Sink(),  # sink only; no source
    )
    with pytest.raises(ValueError):

        @_helpers.register_file_exchange_provider(mcp, "get_report", fxctx)
        async def get_report(report_id: str) -> tuple[ArtifactMetadata, str]:
            return ArtifactMetadata(), report_id


async def test_receiver_decorator_mints_intake_ticket():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
    )

    @_helpers.register_file_exchange_receiver(mcp, "accept_report", fxctx)
    async def accept_report(case_id: str) -> tuple[str, ArtifactConstraints | None]:
        return f"case-{case_id}-attachment", ArtifactConstraints(maxSize=1024)

    tool = await mcp.get_tool("accept_report")
    ticket = await tool.fn(case_id="42")
    assert isinstance(ticket, IntakeTicket)
    assert ticket.artifactId == "case-42-attachment"
    assert ticket.expected is not None
    assert ticket.expected.maxSize == 1024
    assert len(ticket.sinks) == 1
    assert ticket.sinks[0].url.startswith("https://route.test/fx/u/")  # type: ignore[union-attr]


async def test_receiver_decorator_no_expected_constraints_is_none():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
    )

    @_helpers.register_file_exchange_receiver(mcp, "accept_blob", fxctx)
    async def accept_blob(blob_id: str):
        return blob_id, None

    tool = await mcp.get_tool("accept_blob")
    ticket = await tool.fn(blob_id="b1")
    assert ticket.expected is None


def test_receiver_decorator_without_sink_raises_value_error():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),  # source only; no sink
    )
    with pytest.raises(ValueError):

        @_helpers.register_file_exchange_receiver(mcp, "accept_report", fxctx)
        async def accept_report(case_id: str):
            return f"c-{case_id}", None


async def test_fetcher_generated_tool_dispatches_to_download_consume(monkeypatch):
    """The fetcher tool selects a source descriptor and dispatches to the
    download fetcher when descriptor.transport == "download"."""
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
    )

    _helpers.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)

    # Build a TransferHandle with one download descriptor.
    from datetime import datetime, timezone

    from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
    from fastmcp_pvl_core._file_exchange._wire import (
        DownloadSource,
        TransferHandle,
    )

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(size=4),
        sources=[
            DownloadSource(
                transport="download",
                url="https://peer.test/fx/d/abc",
                expiresAt=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
        ],
    )

    captured: dict = {}

    async def fake_dl_fetch(h, d, s, *, config):
        captured["handle"] = h
        captured["descriptor"] = d
        captured["sink"] = s
        captured["config"] = config

    monkeypatch.setattr(_helpers, "download_fetcher_consume", fake_dl_fetch)

    tool = await mcp.get_tool("consume_transfer")
    result = await tool.fn(handle=handle)
    assert result is None
    assert captured["handle"] is handle
    assert captured["descriptor"].transport == "download"
    assert captured["sink"] is fxctx.sink
    assert captured["config"] is fxctx.config


def test_fetcher_without_sink_raises_value_error():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),  # source only
    )
    with pytest.raises(ValueError):
        _helpers.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)


def test_register_file_exchange_volume_map_threads_through():
    from pathlib import Path

    cfg = _cfg()
    mcp = FastMCP("t")
    vm = {"vol": Path("/tmp")}
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),
        volume_map=vm,
    )
    assert fxctx.volume_map is vm


def test_register_file_exchange_volume_map_defaults_to_none():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),
    )
    assert fxctx.volume_map is None


async def test_fetcher_filesystem_dispatches_with_volume_map(monkeypatch):
    from pathlib import Path

    from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
    from fastmcp_pvl_core._file_exchange._wire import (
        FilesystemSource,
        TransferHandle,
    )

    # Bypass the §9 accessibility precheck — the dispatch is what we're
    # testing here, not the readability gate (covered in test_filesystem).
    monkeypatch.setattr(
        _helpers, "filesystem_source_readable", lambda vm: lambda d: True
    )

    cfg = _cfg()
    mcp = FastMCP("t")
    vm = {"vol": Path("/tmp")}
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
        volume_map=vm,
    )

    _helpers.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(size=4),
        sources=[FilesystemSource(transport="filesystem", uri="exchange://vol/x.bin")],
    )

    captured: dict = {}

    async def fake_fs_fetch(h, d, s, *, volume_map):
        captured["volume_map"] = volume_map
        captured["sink"] = s
        captured["descriptor"] = d

    monkeypatch.setattr(_helpers, "filesystem_fetcher_consume", fake_fs_fetch)

    tool = await mcp.get_tool("consume_transfer")
    await tool.fn(handle=handle)
    assert captured["volume_map"] is vm
    assert captured["sink"] is fxctx.sink
    assert captured["descriptor"].transport == "filesystem"


async def test_fetcher_filesystem_without_volume_map_raises_transfer_error():
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._errors import (
        FileExchangeTransferError,
    )
    from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
    from fastmcp_pvl_core._file_exchange._wire import (
        FilesystemSource,
        TransferHandle,
    )

    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
    )  # no volume_map

    _helpers.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(size=4),
        sources=[FilesystemSource(transport="filesystem", uri="exchange://vol/x.bin")],
    )

    tool = await mcp.get_tool("consume_transfer")
    with pytest.raises(FileExchangeTransferError) as ei:
        await tool.fn(handle=handle)
    assert ei.value.code == TransferErrorCode.NO_SUPPORTED_TRANSPORT


async def test_sender_generated_tool_dispatches_to_upload_consume(monkeypatch):
    """The sender tool selects a sink descriptor and dispatches to the
    upload sender when descriptor.transport == "upload"."""
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        source=_Source(),
    )

    _helpers.register_file_exchange_sender(mcp, "send_to_receiver", fxctx)

    from datetime import datetime, timezone

    from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
    from fastmcp_pvl_core._file_exchange._wire import UploadSink

    ticket = IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId="art-1",
        sinks=[
            UploadSink(
                transport="upload",
                url="https://peer.test/fx/u/abc",
                expiresAt=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
        ],
    )

    captured: dict = {}

    async def fake_up_send(descriptor, source, key, *, config):
        captured["descriptor"] = descriptor
        captured["source"] = source
        captured["key"] = key
        captured["config"] = config

    monkeypatch.setattr(_helpers, "upload_sender_consume", fake_up_send)

    tool = await mcp.get_tool("send_to_receiver")
    result = await tool.fn(ticket=ticket, key="local-doc-key")
    assert result is None
    assert captured["descriptor"].transport == "upload"
    assert captured["source"] is fxctx.source
    assert captured["key"] == "local-doc-key"
    assert captured["config"] is fxctx.config


def test_sender_without_source_raises_value_error():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        sink=_Sink(),  # sink only
    )
    with pytest.raises(ValueError):
        _helpers.register_file_exchange_sender(mcp, "send_to_receiver", fxctx)


async def test_sender_filesystem_dispatches_with_volume_map(monkeypatch):
    from pathlib import Path

    from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
    from fastmcp_pvl_core._file_exchange._wire import FilesystemSink

    monkeypatch.setattr(_helpers, "filesystem_sink_writable", lambda vm: lambda d: True)

    cfg = _cfg()
    mcp = FastMCP("t")
    vm = {"vol": Path("/tmp")}
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        source=_Source(),
        volume_map=vm,
    )

    _helpers.register_file_exchange_sender(mcp, "send_to_receiver", fxctx)

    ticket = IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId="art-1",
        sinks=[FilesystemSink(transport="filesystem", uri="exchange://vol/x.bin")],
    )

    captured: dict = {}

    async def fake_fs_send(sink, source, key, *, volume_map):
        captured["volume_map"] = volume_map
        captured["sink"] = sink
        captured["source"] = source
        captured["key"] = key

    monkeypatch.setattr(_helpers, "filesystem_sender_consume", fake_fs_send)

    tool = await mcp.get_tool("send_to_receiver")
    await tool.fn(ticket=ticket, key="k")
    assert captured["volume_map"] is vm
    assert captured["source"] is fxctx.source
    assert captured["key"] == "k"
    assert captured["sink"].transport == "filesystem"


async def test_sender_filesystem_without_volume_map_raises_transfer_error():
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._errors import (
        FileExchangeTransferError,
    )
    from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
    from fastmcp_pvl_core._file_exchange._wire import FilesystemSink

    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        source=_Source(),
    )  # no volume_map

    _helpers.register_file_exchange_sender(mcp, "send_to_receiver", fxctx)

    ticket = IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId="art-1",
        sinks=[FilesystemSink(transport="filesystem", uri="exchange://vol/x.bin")],
    )

    tool = await mcp.get_tool("send_to_receiver")
    with pytest.raises(FileExchangeTransferError) as ei:
        await tool.fn(ticket=ticket, key="k")
    assert ei.value.code == TransferErrorCode.NO_SUPPORTED_TRANSPORT


async def test_sender_no_supported_transport_raises_transfer_error():
    """When ``select_sink`` returns None, sender raises NO_SUPPORTED_TRANSPORT."""
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._errors import (
        FileExchangeTransferError,
    )
    from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
    from fastmcp_pvl_core._file_exchange._wire import UnknownTransportDescriptor

    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        source=_Source(),
    )
    _helpers.register_file_exchange_sender(mcp, "send_to_receiver", fxctx)

    ticket = IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId="art-1",
        sinks=[UnknownTransportDescriptor(transport="future-transport")],
    )

    tool = await mcp.get_tool("send_to_receiver")
    with pytest.raises(FileExchangeTransferError) as ei:
        await tool.fn(ticket=ticket, key="k")
    assert ei.value.code == TransferErrorCode.NO_SUPPORTED_TRANSPORT


async def test_fetcher_no_supported_transport_raises_transfer_error():
    """When ``select_source`` returns None (no descriptor in the handle
    satisfies pvl-core's known transports), the generated tool raises
    FileExchangeTransferError(NO_SUPPORTED_TRANSPORT)."""
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
    )
    _helpers.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)

    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._errors import (
        FileExchangeTransferError,
    )
    from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
    from fastmcp_pvl_core._file_exchange._wire import (
        TransferHandle,
        UnknownTransportDescriptor,
    )

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(size=4),
        sources=[UnknownTransportDescriptor(transport="future-transport")],
    )

    tool = await mcp.get_tool("consume_transfer")
    with pytest.raises(FileExchangeTransferError) as ei:
        await tool.fn(handle=handle)
    assert ei.value.code == TransferErrorCode.NO_SUPPORTED_TRANSPORT


async def test_helpers_inject_task_support_optional():
    """All four role helpers must annotate the registered tool with
    ``taskSupport="optional"`` (§14). Verified via the wire form of the
    tool that FastMCP emits."""
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        source=_Source(),
        sink=_Sink(),
    )

    @_helpers.register_file_exchange_provider(mcp, "p1", fxctx)
    async def p1(x: str) -> tuple[ArtifactMetadata, str]:
        return ArtifactMetadata(), x

    @_helpers.register_file_exchange_receiver(mcp, "r1", fxctx)
    async def r1(x: str) -> tuple[str, ArtifactConstraints | None]:
        return x, None

    _helpers.register_file_exchange_fetcher(mcp, "f1", fxctx)
    _helpers.register_file_exchange_sender(mcp, "s1", fxctx)

    for name in ("p1", "r1", "f1", "s1"):
        tool = await mcp.get_tool(name)
        wire = tool.to_mcp_tool().model_dump(exclude_none=True)
        annotations = wire.get("annotations") or {}
        assert annotations.get("taskSupport") == "optional", (
            f"tool {name!r} missing taskSupport annotation; "
            f"wire annotations: {annotations!r}"
        )
