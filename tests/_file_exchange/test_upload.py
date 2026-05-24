"""Tests for the ``upload`` transport data plane (#146)."""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _routes, _upload
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    IntakeTicket,
    UploadSink,
)

pytestmark = pytest.mark.anyio


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_receiver_mint_returns_ticket_with_upload_sink():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://recv.test", ttl=300.0
    )
    assert isinstance(ticket, IntakeTicket)
    assert ticket.artifactId == "art-1"
    assert len(ticket.sinks) == 1
    sink = ticket.sinks[0]
    assert isinstance(sink, UploadSink)
    assert sink.transport == "upload"
    assert sink.method == "PUT"
    assert sink.url.startswith("https://recv.test/fx/u/")
    token = sink.url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["artifact_id"] == "art-1"
    assert rec.metadata["expected"] is None


async def test_receiver_mint_threads_method_and_expected():
    store = _store()
    expected = ArtifactConstraints(maxSize=1024, acceptMimeTypes=["text/*"])
    ticket = await _upload.upload_receiver_mint(
        "art-2",
        token_store=store,
        base_url="https://recv.test",
        ttl=300.0,
        expected=expected,
        method="POST",
    )
    assert ticket.expected == expected
    assert ticket.sinks[0].method == "POST"
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["expected"] == {
        "maxSize": 1024,
        "acceptMimeTypes": ["text/*"],
        "requireDigest": None,
    }


def test_format_content_digest_rfc9530():
    raw = hashlib.sha256(b"abc").digest()
    out = _upload._format_content_digest("sha-256", raw)
    assert out == "sha-256=:" + base64.b64encode(raw).decode("ascii") + ":"


def test_parse_content_digest_roundtrip():
    raw = hashlib.sha256(b"abc").digest()
    header = _upload._format_content_digest("sha-256", raw)
    assert _upload._parse_content_digest(header) == ("sha-256", raw)


def test_parse_content_digest_picks_first_supported():
    raw = hashlib.sha512(b"abc").digest()
    md5_b64 = base64.b64encode(b"x").decode()
    sha512_b64 = base64.b64encode(raw).decode()
    header = f"md5=:{md5_b64}:, sha-512=:{sha512_b64}:"
    assert _upload._parse_content_digest(header) == ("sha-512", raw)


def test_parse_content_digest_rejects_unparseable():
    assert _upload._parse_content_digest("sha-256=not-a-byte-sequence") is None
    assert _upload._parse_content_digest("sha-256=:!!!notbase64!!!:") is None
    assert (
        _upload._parse_content_digest("md5=:" + base64.b64encode(b"x").decode() + ":")
        is None
    )


def test_parse_content_digest_ignores_member_params():
    raw = hashlib.sha256(b"abc").digest()
    b64 = base64.b64encode(raw).decode()
    # RFC 8941 member parameters after the byte sequence are stripped, not rejected.
    assert _upload._parse_content_digest(f"sha-256=:{b64}:;preference=3") == (
        "sha-256",
        raw,
    )


def test_parse_content_digest_supported_malformed_does_not_fall_through():
    raw = hashlib.sha512(b"abc").digest()
    b64 = base64.b64encode(raw).decode()
    # A supported algo with corrupt bytes is rejected outright, not skipped in
    # favour of a later well-formed member.
    assert _upload._parse_content_digest(f"sha-256=:!!!:, sha-512=:{b64}:") is None


@pytest.mark.parametrize(
    ("content_type", "accept", "ok"),
    [
        ("text/plain", ["text/plain"], True),
        ("text/plain; charset=utf-8", ["text/plain"], True),
        ("text/plain", ["text/*"], True),
        ("text/plain", ["*/*"], True),
        ("application/json", ["text/*"], False),
        ("application/json", ["text/*", "application/json"], True),
        (None, ["text/*"], False),
        ("not-a-media-type", ["*/*"], False),
        ("*/*", ["application/octet-stream"], False),
        ("*/*", ["*/*"], True),
    ],
)
def test_media_type_accepted(content_type, accept, ok):
    assert _upload._media_type_accepted(content_type, accept) is ok


class _CapturingSink:
    def __init__(self):
        self.calls = []

    async def store_artifact(self, artifact_id, metadata, stream):
        self.calls.append((artifact_id, metadata, stream.read()))


class _BoomSink:
    async def store_artifact(self, artifact_id, metadata, stream):
        raise RuntimeError("sink failure with /secret/path detail")


def _mount(sink, *, config=None):
    store = _store()
    mcp = FastMCP("receiver")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, sink=sink, config=config or ServerConfig()
    )
    return store, mcp


def _client(mcp):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()), base_url="http://up.test"
    )


async def _mint_token(store, *, artifact_id="art-1", expected=None):
    minted = await store.mint(
        {
            "artifact_id": artifact_id,
            "expected": expected.model_dump(mode="json") if expected else None,
        },
        ttl=300.0,
        single_use=True,
    )
    return minted.token


async def test_upload_route_unknown_token_404():
    _store_, mcp = _mount(_CapturingSink())
    async with _client(mcp) as client:
        resp = await client.put("/fx/u/nonexistent", content=b"x")
    assert resp.status_code == 404


async def test_upload_route_happy_deposits_and_consumes():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store)
    body = b"upload-payload" * 100
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=body)
    assert resp.status_code == 204
    assert len(sink.calls) == 1
    artifact_id, meta, deposited = sink.calls[0]
    assert artifact_id == "art-1"
    assert deposited == body
    assert meta.size == len(body)
    assert meta.digest == "sha-256:" + hashlib.sha256(body).hexdigest()
    assert await store.lookup(token) is None
    async with _client(mcp) as client:
        resp2 = await client.put(f"/fx/u/{token}", content=body)
    assert resp2.status_code == 404


async def test_upload_route_oversize_413_no_consume():
    store, mcp = _mount(
        sink := _CapturingSink(),
        config=ServerConfig(file_exchange_max_artifact_size=16),
    )
    token = await _mint_token(store)
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=b"x" * 64)
    assert resp.status_code == 413
    assert sink.calls == []
    assert await store.lookup(token) is not None


async def test_upload_route_maxsize_constraint_413():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store, expected=ArtifactConstraints(maxSize=8))
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=b"x" * 64)
    assert resp.status_code == 413
    assert sink.calls == []


async def test_upload_route_mime_reject_415_no_consume():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(
        store, expected=ArtifactConstraints(acceptMimeTypes=["text/*"])
    )
    async with _client(mcp) as client:
        resp = await client.put(
            f"/fx/u/{token}",
            content=b"{}",
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 415
    assert sink.calls == []
    assert await store.lookup(token) is not None


async def test_upload_route_valid_content_digest_verifies():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store)
    body = b"digest-checked-body"
    cd = _upload._format_content_digest("sha-256", hashlib.sha256(body).digest())
    async with _client(mcp) as client:
        resp = await client.put(
            f"/fx/u/{token}", content=body, headers={"content-digest": cd}
        )
    assert resp.status_code == 204
    assert sink.calls[0][2] == body


async def test_upload_route_digest_mismatch_400_no_sink_call():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store)
    body = b"the-real-body"
    wrong = _upload._format_content_digest("sha-256", hashlib.sha256(b"other").digest())
    async with _client(mcp) as client:
        resp = await client.put(
            f"/fx/u/{token}", content=body, headers={"content-digest": wrong}
        )
    assert resp.status_code == 400
    assert sink.calls == []
    assert await store.lookup(token) is not None


async def test_upload_route_require_digest_missing_header_400():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(
        store, expected=ArtifactConstraints(requireDigest=["sha-256"])
    )
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=b"no-digest-header")
    assert resp.status_code == 400
    assert sink.calls == []


async def test_upload_route_ambient_credentials_ignored():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store)
    async with _client(mcp) as client:
        resp = await client.put(
            f"/fx/u/{token}",
            content=b"ok",
            headers={"authorization": "Bearer bogus", "cookie": "x=y"},
        )
    assert resp.status_code == 204
    assert sink.calls[0][2] == b"ok"


async def test_upload_route_sink_failure_500_no_consume():
    store, mcp = _mount(_BoomSink())
    token = await _mint_token(store)
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=b"data")
    assert resp.status_code == 500
    assert resp.content == b""
    assert await store.lookup(token) is not None


async def test_register_routes_upload_requires_config():
    store = _store()
    mcp = FastMCP("receiver")
    with pytest.raises(ValueError, match="config"):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, sink=_CapturingSink(), config=None
        )
