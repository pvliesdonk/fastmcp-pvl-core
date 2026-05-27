"""Matrix rows A7, A8, E2, E3: register_file_exchange_routes shape."""

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _routes
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store


class _Sink:
    async def store_artifact(self, artifact_id, metadata, stream):  # pragma: no cover
        raise AssertionError


class _Source:
    async def open_artifact(self, key):  # pragma: no cover
        raise AssertionError


def _cfg():
    return ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=1024,
    )


def test_registrar_both_none_raises_value_error():
    """E2: source=None, sink=None is misconfiguration."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    with pytest.raises(ValueError):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, source=None, sink=None, config=cfg
        )


def test_registrar_sink_without_config_raises_value_error():
    """E3: sink requires config (operator size cap)."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    with pytest.raises(ValueError):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, source=None, sink=_Sink(), config=None
        )


def test_registrar_source_only_mounts_download_route():
    """A8: source-only mounts only the download route."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, source=_Source(), sink=None, config=None
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/d") for p in paths)
    assert not any(p.startswith("/fx/u") for p in paths)


def test_registrar_sink_only_mounts_upload_route():
    """A8: sink-only mounts only the upload route."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, source=None, sink=_Sink(), config=cfg
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/u") for p in paths)
    assert not any(p.startswith("/fx/d") for p in paths)


def test_registrar_both_mounts_both():
    """A8: source+sink mounts both routes."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, source=_Source(), sink=_Sink(), config=cfg
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/d") for p in paths)
    assert any(p.startswith("/fx/u") for p in paths)


def test_registrar_precondition_failure_mounts_nothing():
    """A7: ValueError from precondition validation -> no routes mounted."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    with pytest.raises(ValueError):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, source=_Source(), sink=_Sink(), config=None
        )
    paths = {r.path for r in mcp.http_app().routes}
    assert not any(p.startswith("/fx/") for p in paths)
