"""End-to-end upload push: server A sends, server B mints + receives.

Exercises the whole ``upload`` data plane together — ``upload_receiver_mint``,
``register_file_exchange_routes`` (upload route mounted on a real ASGI app),
``select_sink``, and ``upload_sender_consume`` — over a loopback ASGI transport.
The SSRF guard is replaced with one that routes to server B's app, because we are
exercising the push flow, not the guard (which has its own tests).
"""

import contextlib

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _routes, _upload
from fastmcp_pvl_core._file_exchange._selection import select_sink
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

pytestmark = pytest.mark.anyio


class _BytesSource:
    def __init__(self, key, body):
        self._key, self._body = key, body

    async def open_artifact(self, key):
        import io

        assert key == self._key
        return io.BytesIO(self._body), ArtifactMetadata(
            name="a", mimeType="application/octet-stream", size=len(self._body)
        )


class _CapturingSink:
    def __init__(self):
        self.calls = []

    async def store_artifact(self, artifact_id, metadata, stream):
        self.calls.append((artifact_id, metadata, stream.read()))


class _FakeGuarded:
    def __init__(self, status):
        self.status = status


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_two_server_push_upload(monkeypatch):
    body = b"end-to-end-upload-payload" * 64

    # Server B: receiver mint + upload route on a real ASGI app.
    store = _store()
    sink = _CapturingSink()
    recv = FastMCP("receiver")
    _routes.register_file_exchange_routes(
        recv, token_store=store, sink=sink, config=ServerConfig()
    )
    app_b = recv.http_app()

    # Server A's guard, redirected at B's ASGI app (no real network / SSRF check).
    @contextlib.asynccontextmanager
    async def guard_to_app_b(
        method, url, *, config, transport, headers=None, content=None
    ):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://recv.test"
        )
        try:
            path = url.split("recv.test", 1)[1]
            req = client.build_request(
                method, path, headers=headers or {}, content=content
            )
            resp = await client.send(req)
            try:
                yield _FakeGuarded(resp.status_code)
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_upload, "guarded_stream", guard_to_app_b)

    # B mints an intake ticket; A selects the upload sink and pushes.
    ticket = await _upload.upload_receiver_mint(
        "art-e2e", token_store=store, base_url="https://recv.test", ttl=300.0
    )
    descriptor = select_sink(ticket)
    assert descriptor is not None and descriptor.transport == "upload"

    await _upload.upload_sender_consume(
        descriptor, _BytesSource("k", body), "k", config=ServerConfig()
    )

    # The bytes landed in B's sink, correlated to artifactId.
    assert len(sink.calls) == 1
    assert sink.calls[0][0] == "art-e2e"
    assert sink.calls[0][2] == body
    # The single-use token was consumed by the completed upload.
    assert await store.lookup(descriptor.url.rsplit("/", 1)[1]) is None
