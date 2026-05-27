"""Matrix rows A1–A6, B1, B4, B6, C1–C2, D4–D8, F4, F5: upload route."""

import hashlib
from typing import BinaryIO

import httpx
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints, ArtifactMetadata


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


class _RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, ArtifactMetadata, bytes]] = []

    async def store_artifact(
        self, artifact_id: str | None, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        data = stream.read()
        self.calls.append((artifact_id, metadata, data))


async def _mount(sink, *, config=None):
    cfg = config or ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=10 * 1024 * 1024,
    )
    store = build_capability_token_store(cfg)
    mcp = FastMCP("test")
    _upload.register_upload_route(mcp, token_store=store, sink=sink, config=cfg)
    return mcp, store


async def _client(mcp):
    transport = httpx.ASGITransport(app=mcp.http_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_route_happy_put_deposits_and_consumes():
    """A1 success: PUT 204 -> sink called once, token consumed."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="https://route.test",
        ttl=120.0,
    )
    url = ticket.sinks[0].url
    token = url.rsplit("/", 1)[1]
    path = "/" + url.split("/", 3)[3]
    async with await _client(mcp) as c:
        resp = await c.put(
            path, content=b"hello world", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 204
    assert len(sink.calls) == 1
    aid, meta, body = sink.calls[0]
    assert aid == "art-1"
    assert body == b"hello world"
    assert meta.mimeType == "text/plain"
    assert meta.size == len(b"hello world")
    assert meta.digest == "sha-256:" + hashlib.sha256(b"hello world").hexdigest()
    # Token consumed
    assert await store.lookup(token) is None


async def test_route_unknown_token_404():
    """A1 prelude: lookup miss -> 404."""
    sink = _RecordingSink()
    mcp, _ = await _mount(sink)
    async with await _client(mcp) as c:
        resp = await c.put(
            "/fx/u/does-not-exist", content=b"x", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 404
    assert sink.calls == []


async def test_route_require_digest_algorithm_mismatch_400():
    """A6 tightening: requireDigest=[sha-256] but client sends sha-512 -> 400."""
    import base64

    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="https://route.test",
        ttl=120.0,
        expected=ArtifactConstraints(requireDigest=["sha-256"]),
    )
    url = ticket.sinks[0].url
    path = url[len("https://route.test") :]
    # Compute a correct sha-512 digest of the body so the header parses cleanly.
    body = b"hello"
    raw512 = hashlib.sha512(body).digest()
    header = "sha-512=:" + base64.b64encode(raw512).decode("ascii") + ":"
    async with await _client(mcp) as c:
        resp = await c.put(
            path,
            content=body,
            headers={"Content-Type": "text/plain", "Content-Digest": header},
        )
    assert resp.status_code == 400
    assert sink.calls == []
