"""Matrix rows A1–A6, B1, B4, B6, C1–C2, D4–D8, F4, F5: upload route."""

import asyncio as _asyncio
import hashlib
import os
from typing import BinaryIO

import httpx
import pytest
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


async def test_route_require_digest_picks_required_algo_regardless_of_order():
    """requireDigest=[sha-256] must be honoured even when the client lists
    sha-512 first in a multi-algorithm Content-Digest dictionary. Without
    this, a parser that returned the first supported entry would falsely
    reject a fully-valid request."""
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
    body = b"hello world"
    raw256 = hashlib.sha256(body).digest()
    raw512 = hashlib.sha512(body).digest()
    b256 = base64.b64encode(raw256).decode("ascii")
    b512 = base64.b64encode(raw512).decode("ascii")
    # sha-512 first, sha-256 second — the route must prefer sha-256
    # because requireDigest lists it.
    header = f"sha-512=:{b512}:, sha-256=:{b256}:"
    async with await _client(mcp) as c:
        resp = await c.put(
            path,
            content=body,
            headers={"Content-Type": "text/plain", "Content-Digest": header},
        )
    assert resp.status_code == 204
    assert len(sink.calls) == 1


async def test_route_second_put_after_success_returns_404():
    """A1 ordering: token consumed after first success; second PUT -> 404."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    async with await _client(mcp) as c:
        r1 = await c.put(path, content=b"a", headers={"Content-Type": "text/plain"})
        r2 = await c.put(path, content=b"b", headers={"Content-Type": "text/plain"})
    assert r1.status_code == 204
    assert r2.status_code == 404
    assert len(sink.calls) == 1


class _RaisingSink:
    def __init__(self) -> None:
        self.attempts = 0

    async def store_artifact(self, artifact_id, metadata, stream):
        self.attempts += 1
        stream.read()
        if self.attempts == 1:
            raise RuntimeError("boom")


async def test_route_sink_raise_does_not_consume_token():
    """A2: sink raises -> 500, token still valid, retry succeeds."""
    sink = _RaisingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    async with await _client(mcp) as c:
        r1 = await c.put(path, content=b"a", headers={"Content-Type": "text/plain"})
        r2 = await c.put(path, content=b"b", headers={"Content-Type": "text/plain"})
    assert r1.status_code == 500
    assert r2.status_code == 204
    assert sink.attempts == 2


async def test_route_accept_mime_mismatch_415():
    """A3."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="https://route.test",
        ttl=120.0,
        expected=ArtifactConstraints(acceptMimeTypes=["application/json"]),
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    async with await _client(mcp) as c:
        resp = await c.put(path, content=b"hi", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert sink.calls == []
    assert await store.lookup(token) is not None


async def test_route_too_large_per_mint_cap_413():
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="https://route.test",
        ttl=120.0,
        expected=ArtifactConstraints(maxSize=4),
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    async with await _client(mcp) as c:
        resp = await c.put(
            path, content=b"too-many-bytes", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 413
    assert sink.calls == []
    assert await store.lookup(token) is not None


async def test_route_too_large_operator_cap_413():
    sink = _RecordingSink()
    cfg = ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=4,
    )
    mcp, store = await _mount(sink, config=cfg)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    async with await _client(mcp) as c:
        resp = await c.put(
            path, content=b"too-many", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 413


async def test_route_content_digest_mismatch_400():
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    bad = "sha-256=:" + "A" * 44 + ":"
    async with await _client(mcp) as c:
        resp = await c.put(
            path,
            content=b"hello",
            headers={"Content-Type": "text/plain", "Content-Digest": bad},
        )
    assert resp.status_code == 400
    assert sink.calls == []
    assert await store.lookup(token) is not None


async def test_route_require_digest_but_missing_header_400():
    """A6."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="https://route.test",
        ttl=120.0,
        expected=ArtifactConstraints(requireDigest=["sha-256"]),
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    async with await _client(mcp) as c:
        resp = await c.put(path, content=b"hi", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 400
    assert sink.calls == []


async def test_route_content_digest_unparseable_400():
    """D4."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    async with await _client(mcp) as c:
        resp = await c.put(
            path,
            content=b"hi",
            headers={"Content-Type": "text/plain", "Content-Digest": "garbage"},
        )
    assert resp.status_code == 400


async def test_route_hashlib_failure_does_not_leak_fd(monkeypatch):
    """B1: simulated post-fdopen pre-staging failure -> fd closed, temp unlinked."""
    import fastmcp_pvl_core._file_exchange._upload as up

    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]

    created: list[str] = []
    real_mkstemp = up.tempfile.mkstemp

    def spy_mkstemp(**kw):
        fd, path_ = real_mkstemp(**kw)
        created.append(path_)
        return fd, path_

    monkeypatch.setattr(up.tempfile, "mkstemp", spy_mkstemp)

    real_hashlib_new = up.hashlib.new

    def boom(name):
        if name == "sha256":
            raise RuntimeError("FIPS")
        return real_hashlib_new(name)

    monkeypatch.setattr(up.hashlib, "new", boom)

    async with await _client(mcp) as c:
        resp = await c.put(path, content=b"hi", headers={"Content-Type": "text/plain"})
    # 500 is the expected mapping for an unexpected RuntimeError inside the handler.
    assert resp.status_code == 500
    for p in created:
        assert not os.path.exists(p), f"temp leaked: {p}"


async def test_route_sink_does_not_close_fd_route_closes_it():
    """B4: route owns the fd handed to the sink and closes it."""
    closes: list[bool] = []

    class _SpySink:
        async def store_artifact(self, artifact_id, metadata, stream):
            stream.read()
            original_close = stream.close

            def tracked():
                closes.append(True)
                original_close()

            stream.close = tracked  # type: ignore[method-assign]

    mcp, store = await _mount(_SpySink())
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    async with await _client(mcp) as c:
        resp = await c.put(path, content=b"x", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 204
    assert closes == [True]


async def test_route_temp_reopen_failure_500(monkeypatch):
    """B6: open(tmp_path, 'rb') raises -> 500, sink not called."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]

    import builtins

    real_open = builtins.open

    def fake_open(p, mode="r", *a, **kw):
        if isinstance(p, str) and "fx-upload-" in p and "rb" in mode:
            raise OSError("simulated")
        return real_open(p, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    async with await _client(mcp) as c:
        resp = await c.put(path, content=b"x", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 500
    assert sink.calls == []


class _FxFailSink:
    async def store_artifact(self, artifact_id, metadata, stream):
        from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
        from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError

        stream.read()
        raise FileExchangeTransferError(
            TransferErrorCode.TRANSFER_FAILED, transport="upload", detail="x"
        )


async def test_route_sink_raises_file_exchange_error_500():
    """D6."""
    mcp, store = await _mount(_FxFailSink())
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    async with await _client(mcp) as c:
        resp = await c.put(path, content=b"x", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 500
    assert resp.content == b""


async def test_route_temp_write_oserror_500(monkeypatch):
    """D8."""
    import fastmcp_pvl_core._file_exchange._upload as up

    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]

    def boom(tmp, hasher, chunk):
        raise OSError("disk full")

    monkeypatch.setattr(up, "_write_chunk", boom)
    async with await _client(mcp) as c:
        resp = await c.put(
            path, content=b"hello", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 500
    assert sink.calls == []


async def test_route_ambient_authorization_ignored():
    """F5."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    async with await _client(mcp) as c:
        resp = await c.put(
            path,
            content=b"x",
            headers={
                "Content-Type": "text/plain",
                "Authorization": "Bearer fake",
                "Cookie": "session=abc",
            },
        )
    assert resp.status_code == 204
    assert len(sink.calls) == 1


async def test_route_ambient_authorization_on_invalid_token_still_404():
    sink = _RecordingSink()
    mcp, _ = await _mount(sink)
    async with await _client(mcp) as c:
        resp = await c.put(
            "/fx/u/nope",
            content=b"x",
            headers={
                "Content-Type": "text/plain",
                "Authorization": "Bearer admin",
            },
        )
    assert resp.status_code == 404


async def test_route_concurrent_puts_at_most_one_consumes():
    """C1: two concurrent PUTs against one token do not crash, the token
    is consumed exactly once, and the response set never contains two
    404s.

    Under ASGITransport's single-event-loop driver, the actual
    interleaving window between ``token_store.lookup`` and
    ``token_store.consume`` is not forced — the test pins the
    observable contract (consume-exactly-once, response-set sanity)
    rather than the specific race timing. A forced-interleaving test
    that injects an awaitable barrier into the token store would be a
    legitimate follow-up but is not required for the matrix row.
    """
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    async with await _client(mcp) as c:
        r1, r2 = await _asyncio.gather(
            c.put(path, content=b"a", headers={"Content-Type": "text/plain"}),
            c.put(path, content=b"b", headers={"Content-Type": "text/plain"}),
        )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses in ([204, 204], [204, 404])
    # Sink may be called once (lookup-then-consume serialised) or twice
    # (both pass lookup before either consumes), but never zero — at least
    # one of the two PUTs must have stored. The spec design tolerates the
    # duplicate-deposit case explicitly; pinning the upper bound at 2
    # prevents a regression that would let unrelated PUTs sneak through.
    assert 1 <= len(sink.calls) <= 2
    assert await store.lookup(token) is None


async def test_route_content_digest_unsupported_algo_400():
    """D5."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    async with await _client(mcp) as c:
        resp = await c.put(
            path,
            content=b"hi",
            headers={
                "Content-Type": "text/plain",
                "Content-Digest": "md5=:YWJjZA==:",
            },
        )
    assert resp.status_code == 400


async def test_route_tmp_close_failure_suppressed(monkeypatch):
    """B2: tmp.close() raises on the success path -> suppressed, route still 204."""
    import fastmcp_pvl_core._file_exchange._upload as up

    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    token = ticket.sinks[0].url.rsplit("/", 1)[1]

    real_fdopen = up.os.fdopen
    raised: list[bool] = []

    def wrap(*a, **kw):
        f = real_fdopen(*a, **kw)
        real_close = f.close

        def tracked_close():
            raised.append(True)
            real_close()
            raise OSError("close failed")

        f.close = tracked_close  # type: ignore[method-assign]
        return f

    monkeypatch.setattr(up.os, "fdopen", wrap)
    async with await _client(mcp) as c:
        resp = await c.put(
            path, content=b"hello", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 204
    assert len(sink.calls) == 1
    assert raised, "tmp.close should have been called"
    assert await store.lookup(token) is None


async def test_route_unlink_failure_suppressed_on_success(monkeypatch):
    """B3: os.unlink raises during cleanup -> suppressed, response still 204."""
    import fastmcp_pvl_core._file_exchange._upload as up

    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]

    real_unlink = up.os.unlink

    def boom(p):
        # Only fail for fx-upload temps; let other unlinks (if any) proceed.
        if "fx-upload-" in str(p):
            raise OSError("simulated unlink failure")
        return real_unlink(p)

    monkeypatch.setattr(up.os, "unlink", boom)
    async with await _client(mcp) as c:
        resp = await c.put(path, content=b"x", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 204
    assert len(sink.calls) == 1


async def test_route_revoke_before_put_returns_404():
    """C2 (a): revoke wins the race before lookup -> 404, no sink call."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    await store.revoke(token)
    async with await _client(mcp) as c:
        resp = await c.put(path, content=b"x", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 404
    assert sink.calls == []


async def test_route_revoke_after_lookup_still_succeeds():
    """C2 (b): revoke fires between lookup and consume -> sink succeeded,
    consume returns False, response still 204 (the bytes are in the sink;
    spec is at-most-once-success, not at-most-once-with-rollback).
    """
    import asyncio as _aio

    revoke_signal = _aio.Event()
    consume_signal = _aio.Event()

    class _SlowSink:
        def __init__(self):
            self.calls: list[bytes] = []

        async def store_artifact(self, artifact_id, metadata, stream):
            # Allow the test to revoke the token while the sink is mid-store.
            revoke_signal.set()
            await consume_signal.wait()
            self.calls.append(stream.read())

    sink = _SlowSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    token = ticket.sinks[0].url.rsplit("/", 1)[1]

    async def driver():
        await revoke_signal.wait()
        await store.revoke(token)
        consume_signal.set()

    async with await _client(mcp) as c:
        results = await _asyncio.gather(
            c.put(path, content=b"x", headers={"Content-Type": "text/plain"}),
            driver(),
        )
    resp = results[0]
    assert resp.status_code == 204
    assert len(sink.calls) == 1
    # Token is gone (consume returned False, but revoke deleted it).
    assert await store.lookup(token) is None


async def test_route_client_disconnect_propagates_without_500_response(monkeypatch):
    """starlette.requests.ClientDisconnect during request.stream() must
    propagate, not be caught as a generic OSError and turned into 500."""
    from starlette.requests import ClientDisconnect

    import fastmcp_pvl_core._file_exchange._upload as up

    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://route.test", ttl=120.0
    )
    path = ticket.sinks[0].url[len("https://route.test") :]
    token = ticket.sinks[0].url.rsplit("/", 1)[1]

    # Patch _write_chunk to raise ClientDisconnect when called (simulates
    # the body iterator yielding then the connection dropping).
    def disconnect(tmp, hasher, chunk):
        raise ClientDisconnect()

    monkeypatch.setattr(up, "_write_chunk", disconnect)
    async with await _client(mcp) as c:
        with pytest.raises(ClientDisconnect):
            await c.put(path, content=b"hello", headers={"Content-Type": "text/plain"})
    # Token not consumed (no response was produced)
    assert await store.lookup(token) is not None
    assert sink.calls == []
