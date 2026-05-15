"""Integration tests for the POST /<ns>/uploads/{token} route."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._file_exchange_runtime import (
    BufferedReceiver,
    register_upload_route,
)
from fastmcp_pvl_core._token_store import UploadRecord, UploadStore


def _build_app(
    receiver: BufferedReceiver, *, accepts: tuple[str, ...] = ("*/*",)
) -> tuple[FastMCP, UploadStore]:
    """Construct a FastMCP with the upload route mounted."""
    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(
        mcp, store=store, namespace="ns", receiver=receiver, accepts=accepts
    )
    return mcp, store


@pytest.mark.asyncio
async def test_post_happy_path_returns_receiver_dict() -> None:
    captured: dict[str, Any] = {}

    def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        captured["origin_id"] = record.origin_id
        captured["body"] = body
        return {"path": record.origin_id, "size_bytes": len(body)}

    mcp, store = _build_app(recv)
    token = store.reserve(origin_id="hello.txt", max_bytes=1024)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"hello world",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"path": "hello.txt", "size_bytes": 11}
    assert captured["origin_id"] == "hello.txt"
    assert captured["body"] == b"hello world"


def test_register_upload_route_requires_exactly_one_receiver() -> None:
    mcp = FastMCP(name="t")
    store = UploadStore()
    # Neither receiver is provided.
    with pytest.raises(ValueError, match="exactly one"):
        register_upload_route(mcp, store=store, namespace="ns")
    # Both receivers are provided.
    with pytest.raises(ValueError, match="exactly one"):

        async def _stream(record, body):  # type: ignore[no-untyped-def]
            return {}

        register_upload_route(
            mcp,
            store=store,
            namespace="ns",
            receiver=lambda r, b: {},
            stream_receiver=_stream,
        )


@pytest.mark.asyncio
async def test_post_unknown_token_returns_404() -> None:
    mcp, _ = _build_app(lambda rec, body: {})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/ns/uploads/bogus",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_already_consumed_token_returns_404() -> None:
    mcp, store = _build_app(lambda rec, body: {"ok": True})
    token = store.reserve(origin_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        first = await client.post(f"/ns/uploads/{token}", content=b"x")
        second = await client.post(f"/ns/uploads/{token}", content=b"x")
    assert first.status_code == 200
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_upload_route_expired_token_returns_404_not_410() -> None:
    """Expired tokens return 404, not 410 (spec §http_upload anti-leak rule).

    The v0.3.0 spec mandates that unknown, expired, and already-consumed
    tokens are all indistinguishable to the caller — all return 404.
    """
    mcp, store = _build_app(lambda rec, body: {})
    token = store.reserve(origin_id="x", max_bytes=10, ttl_seconds=-1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 404
    assert resp.status_code != 410


@pytest.mark.asyncio
async def test_upload_route_unknown_and_consumed_and_expired_all_404() -> None:
    """Unknown, consumed, and expired tokens all return identical 404 responses.

    The spec anti-leak rule: callers cannot distinguish why a token is
    unusable — it may have never existed, already been consumed, or expired.
    All three conditions produce the same status code and body.
    """
    mcp, store = _build_app(lambda rec, body: {"ok": True})

    # Three tokens representing each unusable-token condition.
    never_minted = "a" * 32  # unknown: never minted

    # Consumed: mint, consume successfully (POST once), then POST again.
    consumed_token = store.reserve(origin_id="c", max_bytes=1024)

    # Expired: mint with negative TTL.
    expired_token = store.reserve(origin_id="e", max_bytes=10, ttl_seconds=-1)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        # First POST to consumed_token succeeds (token is consumed).
        first = await client.post(
            f"/ns/uploads/{consumed_token}",
            content=b"data",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert first.status_code == 200

        # Now collect the three 404 responses.
        r_unknown = await client.post(
            f"/ns/uploads/{never_minted}",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
        r_consumed = await client.post(
            f"/ns/uploads/{consumed_token}",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
        r_expired = await client.post(
            f"/ns/uploads/{expired_token}",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )

    # All three are 404 — indistinguishable.
    assert r_unknown.status_code == 404
    assert r_consumed.status_code == 404
    assert r_expired.status_code == 404

    # Bodies are byte-identical and empty (spec §http_upload anti-leak rule).
    assert r_unknown.content == r_consumed.content == r_expired.content
    assert r_unknown.content == b""


@pytest.mark.asyncio
async def test_post_oversize_by_content_length_returns_413() -> None:
    mcp, store = _build_app(lambda rec, body: {"ok": True})
    token = store.reserve(origin_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x" * 11,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "11",
            },
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_post_oversize_by_content_length_burns_token() -> None:
    """413 still consumes the token; retry on the same token gets 404.

    Pins the documented anti-replay behavior — the token is consumed
    at the lookup step, before precondition gates run, so any
    subsequent POST to the same URL (even with a body that would
    succeed) returns 404 rather than the original 413.
    """
    mcp, store = _build_app(lambda rec, body: {"ok": True})
    token = store.reserve(origin_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        first = await client.post(
            f"/ns/uploads/{token}",
            content=b"x" * 11,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "11",
            },
        )
        # Same token — already consumed at the lookup step.
        second = await client.post(
            f"/ns/uploads/{token}",
            content=b"x" * 5,  # within cap this time
            headers={"Content-Type": "application/octet-stream"},
        )
    assert first.status_code == 413
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_post_unaccepted_content_type_burns_token() -> None:
    """415 still consumes the token; retry with correct CT gets 404.

    Companion to ``test_post_oversize_by_content_length_burns_token``;
    pins the same anti-replay invariant for the Content-Type gate.
    """
    mcp, store = _build_app(
        lambda rec, body: {"ok": True},
        accepts=("application/octet-stream",),
    )
    token = store.reserve(origin_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        first = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "text/plain"},
        )
        second = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert first.status_code == 415
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_post_oversize_via_chunk_overrun_returns_413() -> None:
    """Defense-in-depth: client lies about Content-Length, real body is bigger."""
    captured: dict[str, Any] = {"called": False}

    def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        captured["called"] = True
        return {"ok": True}

    mcp, store = _build_app(recv)
    token = store.reserve(origin_id="x", max_bytes=10)

    async def chunk_iter() -> AsyncIterator[bytes]:
        yield b"x" * 8
        yield b"x" * 8  # total 16 > 10

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=chunk_iter(),
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 413
    assert captured["called"] is False


@pytest.mark.asyncio
async def test_post_malformed_content_length_falls_through_to_chunk_reader() -> None:
    """A non-integer Content-Length is tolerated; chunk-reader enforces the cap."""
    captured: dict[str, Any] = {"called": False}

    def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        captured["called"] = True
        return {"size_bytes": len(body), "ok": True}

    mcp, store = _build_app(recv)
    token = store.reserve(origin_id="x", max_bytes=100)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"hello",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "abc",  # malformed
            },
        )

    # Tolerated: handler proceeds to chunk reader, body fits, receiver runs.
    assert resp.status_code == 200
    assert captured["called"] is True
    assert resp.json() == {"size_bytes": 5, "ok": True}


@pytest.mark.asyncio
async def test_post_unaccepted_content_type_returns_415() -> None:
    mcp, store = _build_app(
        lambda rec, body: {"ok": True},
        accepts=("application/octet-stream", "image/png"),
    )
    token = store.reserve(origin_id="x", max_bytes=1024)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "text/plain"},
        )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_post_wildcard_accepts_disables_check() -> None:
    mcp, store = _build_app(lambda rec, body: {"ok": True}, accepts=("*/*",))
    token = store.reserve(origin_id="x", max_bytes=1024)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "audio/weird"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_glob_accepts_matches_subtype() -> None:
    mcp, store = _build_app(lambda rec, body: {"ok": True}, accepts=("image/*",))
    token = store.reserve(origin_id="x", max_bytes=1024)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "image/png"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_missing_content_type_with_explicit_accepts_returns_415() -> None:
    """No Content-Type header is rejected when accepts is non-wildcard."""
    mcp, store = _build_app(
        lambda rec, body: {"ok": True},
        accepts=("application/octet-stream",),
    )
    token = store.reserve(origin_id="x", max_bytes=1024)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        # No Content-Type header set — httpx does not auto-add one for
        # raw bytes content, so the handler sees an empty CT.
        resp = await client.post(f"/ns/uploads/{token}", content=b"x")
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_post_content_type_with_parameters_matches() -> None:
    """``image/png; charset=binary`` matches ``image/png`` (parameters stripped)."""
    mcp, store = _build_app(lambda rec, body: {"ok": True}, accepts=("image/png",))
    token = store.reserve(origin_id="x", max_bytes=1024)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "image/png; charset=binary"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_content_type_match_is_case_insensitive() -> None:
    """``Image/PNG`` against accepts ``image/*`` should match (case folded)."""
    mcp, store = _build_app(lambda rec, body: {"ok": True}, accepts=("image/*",))
    token = store.reserve(origin_id="x", max_bytes=1024)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "Image/PNG"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_receiver_value_error_returns_400() -> None:
    def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        raise ValueError("bad path")

    mcp, store = _build_app(recv)
    token = store.reserve(origin_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 400
    assert "bad path" in resp.text


@pytest.mark.asyncio
async def test_post_receiver_file_exists_returns_409() -> None:
    def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        raise FileExistsError("already there")

    mcp, store = _build_app(recv)
    token = store.reserve(origin_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_post_receiver_other_exception_returns_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    mcp, store = _build_app(recv)
    token = store.reserve(origin_id="x", max_bytes=10)
    with caplog.at_level(logging.ERROR):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp.http_app()),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                f"/ns/uploads/{token}",
                content=b"x",
                headers={"Content-Type": "application/octet-stream"},
            )
    assert resp.status_code == 500
    assert any("kaboom" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_post_receiver_returning_non_dict_returns_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Receiver mis-implementations surface as 500 with an ERROR log.

    The route's contract is "JSON object back to the agent"; if a receiver
    returns a string/list/None, that is a programmer bug (not a runtime
    or network condition). Treating it as success would let the agent
    that uploaded believe its file was accepted. The route logs at
    ERROR (operators can grep for "non-dict") and responds 500.
    """

    def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        return "not-a-dict"  # type: ignore[return-value]

    mcp, store = _build_app(recv)
    token = store.reserve(origin_id="x", max_bytes=10)
    with caplog.at_level(logging.ERROR):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp.http_app()),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                f"/ns/uploads/{token}",
                content=b"x",
                headers={"Content-Type": "application/octet-stream"},
            )
    assert resp.status_code == 500
    assert any(
        "non-dict" in r.getMessage() and "receiver bug" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_post_async_buffered_receiver_runs_to_completion() -> None:
    """The buffered receiver path also accepts an async function returning a dict."""
    captured: dict[str, Any] = {}

    async def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        captured["origin_id"] = record.origin_id
        captured["body"] = body
        return {"size": len(body), "ok": True}

    mcp, store = _build_app(recv)
    token = store.reserve(origin_id="x", max_bytes=100)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"hello async",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"size": 11, "ok": True}
    assert captured["body"] == b"hello async"


@pytest.mark.asyncio
async def test_post_stream_receiver_sees_chunks_live() -> None:
    seen: list[bytes] = []

    async def recv(record: UploadRecord, body: AsyncIterator[bytes]) -> dict[str, Any]:
        async for chunk in body:
            seen.append(chunk)
        return {"chunks": len(seen), "total": sum(len(c) for c in seen)}

    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(
        mcp,
        store=store,
        namespace="ns",
        stream_receiver=recv,
    )
    token = store.reserve(origin_id="x", max_bytes=1024)

    async def chunk_iter() -> AsyncIterator[bytes]:
        yield b"abc"
        yield b"defg"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=chunk_iter(),
            headers={"Content-Type": "application/octet-stream"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 7
    assert payload["chunks"] >= 1
    assert b"".join(seen) == b"abcdefg"


@pytest.mark.asyncio
async def test_post_stream_receiver_oversize_aborts_before_completion() -> None:
    """Bounded streaming: 413 fires mid-stream when running total exceeds max_bytes."""
    received_chunks: list[bytes] = []

    async def recv(record: UploadRecord, body: AsyncIterator[bytes]) -> dict[str, Any]:
        async for chunk in body:
            received_chunks.append(chunk)
        return {"ok": True}

    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(
        mcp,
        store=store,
        namespace="ns",
        stream_receiver=recv,
    )
    token = store.reserve(origin_id="x", max_bytes=5)

    async def chunk_iter() -> AsyncIterator[bytes]:
        yield b"abcd"
        yield b"efgh"  # cumulative 8 > 5

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=chunk_iter(),
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_post_sync_stream_receiver_returning_plain_dict() -> None:
    """A sync stream_receiver returning a plain dict (not a coroutine) works.

    Pins the round-2 fix that added ``inspect.isawaitable`` symmetry to
    the streaming path. Without the guard, ``await`` on a plain dict
    raises TypeError.
    """

    def sync_recv(record: UploadRecord, body: AsyncIterator[bytes]) -> dict[str, Any]:
        # Sync receiver — ignores the body iterator and returns a plain dict.
        return {"ok": True, "origin_id": record.origin_id}

    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(mcp, store=store, namespace="ns", stream_receiver=sync_recv)
    token = store.reserve(origin_id="x.md", max_bytes=100)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"hello",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "origin_id": "x.md"}


@pytest.mark.asyncio
async def test_post_sync_receiver_runs_in_threadpool() -> None:
    """Sync buffered receivers dispatch via ``asyncio.to_thread``.

    A sync receiver doing blocking I/O would otherwise stall the event
    loop for the I/O duration. The handler dispatches sync receivers
    onto a thread; the receiver therefore runs on a non-main worker
    thread (i.e. not the event-loop thread). Pins the threadpool
    dispatch added in the post-Gemini-round-2 follow-up.
    """
    import threading

    captured: dict[str, Any] = {}

    def sync_recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        captured["is_main"] = threading.current_thread() is threading.main_thread()
        captured["thread_name"] = threading.current_thread().name
        return {"ok": True}

    mcp, store = _build_app(sync_recv)
    token = store.reserve(origin_id="x.md", max_bytes=100)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"hello",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert resp.status_code == 200
    # Sync receiver ran on a worker thread, not the event-loop thread.
    assert captured["is_main"] is False


@pytest.mark.asyncio
async def test_post_sync_stream_receiver_runs_in_threadpool() -> None:
    """Sync stream_receiver dispatches via asyncio.to_thread (not on event loop).

    Symmetric with the buffered-receiver threadpool dispatch test.
    Sync stream receivers can't iterate the async body generator, but
    they may do blocking bookkeeping (DB lookups, etc.); offloading to
    a thread keeps the event loop healthy.
    """
    import threading

    captured: dict[str, Any] = {}

    def sync_recv(record: UploadRecord, body: AsyncIterator[bytes]) -> dict[str, Any]:
        # Ignore body (degenerate case); record the running thread.
        captured["thread"] = threading.current_thread().name
        captured["is_main"] = threading.current_thread() is threading.main_thread()
        return {"ok": True}

    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(mcp, store=store, namespace="ns", stream_receiver=sync_recv)
    token = store.reserve(origin_id="x.md", max_bytes=100)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"hello",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert resp.status_code == 200
    # Sync stream receiver ran on a worker thread.
    assert captured["is_main"] is False
