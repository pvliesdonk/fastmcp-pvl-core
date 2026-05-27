"""End-to-end push: sender + guard + receiver route + sink.

Two FastMCP-built mock servers wired through ASGITransport: server B
mints + serves the upload route; server A selects + sends through a
guarded_stream patched to land on B's ASGI app. The SSRF guard is
replaced because we are exercising the push flow, not the guard
(which has its own tests).
"""

import contextlib
import hashlib
import io
from typing import BinaryIO

import httpx
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._routes import register_file_exchange_routes
from fastmcp_pvl_core._file_exchange._selection import select_sink
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata, UploadSink


class _Sink:
    def __init__(self) -> None:
        self.received: tuple[str, ArtifactMetadata, bytes] | None = None

    async def store_artifact(
        self, artifact_id: str, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        self.received = (artifact_id, metadata, stream.read())


class _Src:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def open_artifact(self, key: str) -> tuple[io.BytesIO, ArtifactMetadata]:
        return io.BytesIO(self.data), ArtifactMetadata(mimeType="application/json")


async def test_e2e_push_two_servers(monkeypatch):
    payload = b'{"hello":"world"}'

    # Server B (receiver): mint + serve the upload route on a real ASGI app.
    cfg_b = ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=1024,
    )
    store_b = build_capability_token_store(cfg_b)
    sink_b = _Sink()
    mcp_b = FastMCP("B")
    register_file_exchange_routes(mcp_b, token_store=store_b, sink=sink_b, config=cfg_b)

    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store_b, base_url="https://b.test", ttl=120.0
    )

    transport_b = httpx.ASGITransport(app=mcp_b.http_app())
    client_b = httpx.AsyncClient(transport=transport_b, base_url="https://b.test")

    # Server A's guard, redirected at B's ASGI app — no real network /
    # SSRF check (the guard is exercised elsewhere; here we test the push
    # flow). The fake mirrors the production GuardedResponse surface that
    # ``upload_sender_consume`` consumes (``status`` only).
    @contextlib.asynccontextmanager
    async def fake_guarded_stream(
        method, url, *, config, transport, headers=None, content=None
    ):
        assert transport == "upload"
        body = b""
        if content is not None:
            async for chunk in content:
                body += chunk
        resp = await client_b.request(method, url, headers=headers, content=body)

        class R:
            status = resp.status_code

        yield R()

    monkeypatch.setattr(_upload, "guarded_stream", fake_guarded_stream)

    # Server A (sender): select the sink, push the bytes.
    cfg_a = ServerConfig(
        kv_store_url="memory://",
        file_exchange_http_timeout=30.0,
    )
    selected = select_sink(ticket)
    assert isinstance(selected, UploadSink)
    try:
        await _upload.upload_sender_consume(
            selected, _Src(payload), "art-1", config=cfg_a
        )
    finally:
        await client_b.aclose()

    assert sink_b.received is not None
    aid, meta, body = sink_b.received
    assert aid == "art-1"
    assert body == payload
    assert meta.size == len(payload)
    assert meta.digest == "sha-256:" + hashlib.sha256(payload).hexdigest()

    # The single-use token was consumed by the completed deposit.
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    assert await store_b.lookup(token) is None
