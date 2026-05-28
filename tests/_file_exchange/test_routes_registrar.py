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
    """E2: source=None, sink=None is misconfiguration. The precondition
    gate runs before any mount happens, so the registrar must leave the
    server with zero ``/fx/`` routes — symmetric with the E3 test below."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    with pytest.raises(ValueError):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, source=None, sink=None, config=cfg
        )
    paths = {r.path for r in mcp.http_app().routes}
    assert not any(p.startswith("/fx/") for p in paths)


def test_registrar_sink_without_config_raises_value_error():
    """E3: sink requires config (operator size cap). Like the other
    precondition tests, the registrar must leave the server with zero
    ``/fx/`` routes — completing the symmetric A7 coverage across all
    four precondition-failure paths."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    with pytest.raises(ValueError):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, source=None, sink=_Sink(), config=None
        )
    paths = {r.path for r in mcp.http_app().routes}
    assert not any(p.startswith("/fx/") for p in paths)


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


async def test_cross_transport_token_upload_route_returns_404():
    """A unified ``token_store`` (#148) holds both download- and upload-minted
    tokens; presenting a *download* token to the *upload* route must return
    404, not raise a KeyError on missing metadata. Uniform with the
    unknown-token branch — no token-state leak across transports."""
    import httpx

    from fastmcp_pvl_core._file_exchange import _download, _upload

    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, source=_Source(), sink=_Sink(), config=cfg
    )
    from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

    handle = await _download.download_provider_mint(
        ArtifactMetadata(size=11),
        "doc-key",
        token_store=store,
        base_url="https://route.test",
        ttl=120.0,
    )
    token = handle.sources[0].url.rsplit("/", 1)[1]  # type: ignore[union-attr]
    transport = httpx.ASGITransport(app=mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.put(
            f"{_upload.UPLOAD_PREFIX}/{token}",
            content=b"x",
            headers={"Content-Type": "text/plain"},
        )
    assert resp.status_code == 404


async def test_cross_transport_token_download_route_returns_404():
    """Mirror of the above: an upload-minted token presented to the download
    route returns 404, not a misleading 500 with the wrong log attribution."""
    import httpx

    from fastmcp_pvl_core._file_exchange import _download, _upload

    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, source=_Source(), sink=_Sink(), config=cfg
    )

    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    transport = httpx.ASGITransport(app=mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(f"{_download.DOWNLOAD_PREFIX}/{token}")
    assert resp.status_code == 404
