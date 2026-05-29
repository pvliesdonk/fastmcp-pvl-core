"""Tests for #148 umbrella helpers."""

from __future__ import annotations

from typing import BinaryIO

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
