"""End-to-end download pull: server A mints + serves, server B fetches.

Exercises the whole ``download`` data plane together — ``download_provider_mint``,
``register_file_exchange_routes`` (mounted on a real ASGI app), ``select_source``,
and ``download_fetcher_consume`` — over a loopback ASGI transport. The SSRF guard
is replaced with one that routes to server A's app, because we are exercising the
pull flow, not the guard (which has its own tests).
"""

import contextlib
import hashlib
import io

import httpx
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _download
from fastmcp_pvl_core._file_exchange._selection import select_source
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _BytesSource:
    def __init__(self, key, body):
        self._key, self._body = key, body

    async def open_artifact(self, key):
        assert key == self._key
        return io.BytesIO(self._body), ArtifactMetadata(
            name="a", mimeType="application/octet-stream", size=len(self._body)
        )


class _CapturingSink:
    def __init__(self):
        self.deposited = None

    async def store_artifact(self, artifact_id, metadata, stream):
        self.deposited = stream.read()


class _FakeGuarded:
    """Mirror the production GuardedResponse surface (status + aiter_bytes)."""

    def __init__(self, resp):
        self.status = resp.status_code
        self._resp = resp

    def aiter_bytes(self):
        return self._resp.aiter_bytes()


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_two_server_pull_download(monkeypatch):
    body = b"end-to-end-download-payload" * 64
    digest = "sha-256:" + hashlib.sha256(body).hexdigest()

    # Server A: provider mint + serving route on a real ASGI app.
    store = _store()
    mcp = FastMCP("provider")
    _download.register_file_exchange_routes(
        mcp, token_store=store, source=_BytesSource("doc", body)
    )
    app_a = mcp.http_app()

    # Server B's guard, redirected at A's ASGI app (no real network / SSRF check
    # — the guard is exercised elsewhere; here we test the pull flow).
    @contextlib.asynccontextmanager
    async def guard_to_app_a(
        method, url, *, config, transport, headers=None, content=None
    ):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="http://prov.test"
        )
        try:
            path = url.split("prov.test", 1)[1]
            req = client.build_request(method, path, headers=headers or {})
            resp = await client.send(req, stream=True)
            try:
                yield _FakeGuarded(resp)
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_download, "guarded_stream", guard_to_app_a)

    # A mints a handle; the artifact carries the digest A computed out of band.
    handle = await _download.download_provider_mint(
        ArtifactMetadata(
            name="a", mimeType="application/octet-stream", size=len(body), digest=digest
        ),
        "doc",
        token_store=store,
        base_url="https://prov.test",
        ttl=300.0,
        single_use=True,
    )

    # B selects the download source and fetches it into its sink.
    descriptor = select_source(handle)
    assert descriptor is not None and descriptor.transport == "download"
    sink = _CapturingSink()
    await _download.download_fetcher_consume(
        handle, descriptor, sink, config=ServerConfig()
    )
    assert sink.deposited == body

    # The single-use token was consumed by the completed retrieval.
    assert await store.lookup(descriptor.url.rsplit("/", 1)[1]) is None
