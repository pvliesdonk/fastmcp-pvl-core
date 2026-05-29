"""Tests for #148 umbrella helpers."""

from __future__ import annotations

from typing import BinaryIO

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _helpers
from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


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
