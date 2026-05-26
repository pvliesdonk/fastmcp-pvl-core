"""End-to-end upload push: server B (receiver) mints + serves, server A (sender) pushes.

Exercises the whole ``upload`` data plane together —
``upload_receiver_mint``, ``register_upload_route`` (mounted on a real
ASGI app), ``select_sink``, and ``upload_sender_consume`` — over a loopback
ASGI transport. The SSRF guard is replaced with one that routes to server
B's app, because we are exercising the push flow, not the guard (which has
its own tests).
"""

from __future__ import annotations

import contextlib
import hashlib
import io

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._selection import select_sink
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _BytesSource:
    """ArtifactSource serving fixed bytes for a single key."""

    def __init__(self, key: str, body: bytes) -> None:
        self._key, self._body = key, body

    async def open_artifact(self, key: str):
        assert key == self._key
        return io.BytesIO(self._body), ArtifactMetadata(
            name="artifact",
            mimeType="application/octet-stream",
            size=len(self._body),
        )


class _CapturingSink:
    """ArtifactSink that records artifact_id, metadata, and bytes received."""

    def __init__(self) -> None:
        self.calls = 0
        self.received_id: str | None = None
        self.received_meta: ArtifactMetadata | None = None
        self.received_bytes: bytes = b""

    async def store_artifact(self, artifact_id, metadata, stream) -> None:
        self.calls += 1
        self.received_id = artifact_id
        self.received_meta = metadata
        self.received_bytes = stream.read()


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_two_server_push_upload(monkeypatch):
    body = b"end-to-end-upload-payload" * 64
    expected_sha256 = hashlib.sha256(body).hexdigest()

    # Server B (receiver): mint capability URL + serve the upload route.
    recv_store = _store()
    recv_sink = _CapturingSink()
    recv_mcp = FastMCP("receiver")
    cfg = ServerConfig()
    _upload.register_upload_route(
        recv_mcp, token_store=recv_store, sink=recv_sink, config=cfg
    )
    app_b = recv_mcp.http_app()

    # Mint an IntakeTicket on server B.
    ticket = await _upload.upload_receiver_mint(
        "artifact-42",
        token_store=recv_store,
        base_url="https://recv.test",
        ttl=300.0,
    )
    assert ticket.artifactId == "artifact-42"

    # Server A selects the upload sink.
    descriptor = select_sink(ticket)
    assert descriptor is not None
    assert descriptor.transport == "upload"

    # Replace guarded_stream with one that routes to B's ASGI app.
    @contextlib.asynccontextmanager
    async def guard_to_app_b(
        method, url, *, config, transport, headers=None, content=None
    ):
        from fastmcp_pvl_core._file_exchange._outbound import GuardedResponse

        # Collect streaming content into bytes so ASGITransport can replay.
        body_bytes = b""
        if content is not None:
            async for chunk in content:
                body_bytes += chunk

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://recv.test"
        )
        try:
            # Extract path from the URL (strip scheme+host).
            path = url.split("recv.test", 1)[1] if "recv.test" in url else url
            req = client.build_request(
                method, path, headers=headers or {}, content=body_bytes
            )
            resp = await client.send(req, stream=True)
            try:
                yield GuardedResponse(
                    status=resp.status_code,
                    headers=resp.headers,
                    _response=resp,
                )
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_upload, "guarded_stream", guard_to_app_b)

    # Server A runs upload_sender_consume.
    source = _BytesSource("doc", body)
    await _upload.upload_sender_consume(
        descriptor, source, "doc", config=ServerConfig()
    )

    # Verify bytes landed in B's sink.
    assert recv_sink.calls == 1
    assert recv_sink.received_id == "artifact-42"
    assert recv_sink.received_bytes == body

    # Metadata: size and sha-256 digest recorded.
    meta = recv_sink.received_meta
    assert meta is not None
    assert meta.size == len(body)
    assert meta.digest == f"sha-256:{expected_sha256}"
    assert meta.mimeType == "application/octet-stream"

    # The single-use token was consumed.
    token = descriptor.url.rsplit("/", 1)[1]
    assert await recv_store.lookup(token) is None


async def test_two_server_push_upload_content_digest_verified(monkeypatch):
    """Verify that the route actually checks the Content-Digest sent by the sender."""
    body = b"integrity-check-payload"
    recv_store = _store()
    recv_sink = _CapturingSink()
    recv_mcp = FastMCP("receiver")
    cfg = ServerConfig()
    _upload.register_upload_route(
        recv_mcp, token_store=recv_store, sink=recv_sink, config=cfg
    )
    app_b = recv_mcp.http_app()

    ticket = await _upload.upload_receiver_mint(
        "art-check",
        token_store=recv_store,
        base_url="https://recv.test",
        ttl=300.0,
    )
    descriptor = select_sink(ticket)
    assert descriptor is not None

    # Tamper: replace guarded_stream with one that corrupts the body bytes
    # but keeps the original Content-Digest header (computed by sender from
    # the real body). The route must reject with 400.
    tampered_body = b"corrupted-" + body

    @contextlib.asynccontextmanager
    async def tamper_guard(
        method, url, *, config, transport, headers=None, content=None
    ):
        from fastmcp_pvl_core._file_exchange._outbound import GuardedResponse

        # Drain the real content but send the corrupted body to the server.
        if content is not None:
            async for _ in content:
                pass

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://recv.test"
        )
        try:
            path = url.split("recv.test", 1)[1] if "recv.test" in url else url
            req = client.build_request(
                method, path, headers=headers or {}, content=tampered_body
            )
            resp = await client.send(req, stream=True)
            try:
                yield GuardedResponse(
                    status=resp.status_code,
                    headers=resp.headers,
                    _response=resp,
                )
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_upload, "guarded_stream", tamper_guard)

    source = _BytesSource("doc", body)
    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(
            descriptor, source, "doc", config=ServerConfig()
        )
    # The receiver rejected with 400 (digest mismatch) → sender raises TRANSFER_FAILED.
    assert ei.value.code.value == "transfer-failed"
    # Sink never called.
    assert recv_sink.calls == 0
