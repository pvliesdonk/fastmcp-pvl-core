"""Integration tests for the POST /<ns>/uploads/{token} route."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, BinaryIO

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._file_exchange_runtime import (
    _UPLOAD_SPOOL_MAX_BYTES,
    RouteSink,
    register_upload_route,
)
from fastmcp_pvl_core._token_store import UploadRecord, UploadStore


def _build_app(
    sink: RouteSink, *, accepts: tuple[str, ...] = ("*/*",)
) -> tuple[FastMCP, UploadStore]:
    """Construct a FastMCP with the upload route mounted."""
    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(mcp, store=store, namespace="ns", sink=sink, accepts=accepts)
    return mcp, store


def _ok_sink() -> RouteSink:
    """A sink that drains the body and returns a fixed success dict."""

    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        stream.read()
        return {"ok": True}

    return sink


@pytest.mark.asyncio
async def test_post_happy_path_returns_sink_dict() -> None:
    captured: dict[str, Any] = {}

    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        body = stream.read()
        captured["origin_id"] = record.origin_id
        captured["body"] = body
        return {"path": record.origin_id, "size_bytes": len(body)}

    mcp, store = _build_app(sink)
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


@pytest.mark.asyncio
async def test_post_unknown_token_returns_404() -> None:
    mcp, _ = _build_app(_ok_sink())
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
    mcp, store = _build_app(_ok_sink())
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
    mcp, store = _build_app(_ok_sink())
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
    mcp, store = _build_app(_ok_sink())

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
    mcp, store = _build_app(_ok_sink())
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
    mcp, store = _build_app(_ok_sink())
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
        _ok_sink(),
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

    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        captured["called"] = True
        stream.read()
        return {"ok": True}

    mcp, store = _build_app(sink)
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
    # The oversize abort fires before the spool is handed to the sink.
    assert captured["called"] is False


@pytest.mark.asyncio
async def test_post_malformed_content_length_falls_through_to_chunk_reader() -> None:
    """A non-integer Content-Length is tolerated; chunk-reader enforces the cap."""
    captured: dict[str, Any] = {"called": False}

    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        captured["called"] = True
        body = stream.read()
        return {"size_bytes": len(body), "ok": True}

    mcp, store = _build_app(sink)
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

    # Tolerated: handler proceeds to chunk reader, body fits, sink runs.
    assert resp.status_code == 200
    assert captured["called"] is True
    assert resp.json() == {"size_bytes": 5, "ok": True}


@pytest.mark.asyncio
async def test_post_unaccepted_content_type_returns_415() -> None:
    mcp, store = _build_app(
        _ok_sink(),
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
    mcp, store = _build_app(_ok_sink(), accepts=("*/*",))
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
    mcp, store = _build_app(_ok_sink(), accepts=("image/*",))
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
        _ok_sink(),
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
    mcp, store = _build_app(_ok_sink(), accepts=("image/png",))
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
    mcp, store = _build_app(_ok_sink(), accepts=("image/*",))
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
async def test_post_sink_value_error_returns_400() -> None:
    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        raise ValueError("bad path")

    mcp, store = _build_app(sink)
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
async def test_post_sink_file_exists_returns_409() -> None:
    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        raise FileExistsError("already there")

    mcp, store = _build_app(sink)
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
async def test_post_sink_other_exception_returns_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    mcp, store = _build_app(sink)
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
async def test_post_async_sink_runs_to_completion() -> None:
    """The sink path accepts an async function reading the file-like body."""
    captured: dict[str, Any] = {}

    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        body = stream.read()
        captured["origin_id"] = record.origin_id
        captured["body"] = body
        return {"size": len(body), "ok": True}

    mcp, store = _build_app(sink)
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
async def test_post_sink_sees_full_body_from_chunked_request() -> None:
    """A chunked request body is spooled whole; the sink reads every byte.

    Previously exercised by the streaming-receiver path; with the unified
    file-like ``sink`` the same intent is asserting that a multi-chunk
    request arrives at the sink as one contiguous readable body.
    """
    seen: dict[str, Any] = {}

    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        body = stream.read()
        seen["body"] = body
        return {"total": len(body)}

    mcp, store = _build_app(sink)
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
    assert resp.json()["total"] == 7
    assert seen["body"] == b"abcdefg"


@pytest.mark.asyncio
async def test_post_oversize_chunked_request_aborts_before_sink() -> None:
    """Bounded body: 413 fires when the running total exceeds max_bytes.

    The body never reaches the sink when the cap is breached mid-stream.
    """
    sink_called: dict[str, Any] = {"called": False}

    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        sink_called["called"] = True
        stream.read()
        return {"ok": True}

    mcp, store = _build_app(sink)
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
    assert sink_called["called"] is False


@pytest.mark.asyncio
async def test_post_large_body_round_trips_through_spool() -> None:
    """A body larger than ``_UPLOAD_SPOOL_MAX_BYTES`` reaches the sink intact.

    The route spools the inbound body into a ``SpooledTemporaryFile`` —
    small bodies stay in memory, larger ones spill to disk. This pins
    that a body above the 1 MiB spool threshold (so the on-disk path is
    exercised) still arrives at the sink as the exact bytes that were
    sent.
    """
    payload = b"\x5a" * (_UPLOAD_SPOOL_MAX_BYTES + 512 * 1024)  # ~1.5 MiB
    captured: dict[str, Any] = {}

    async def sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        body = stream.read()
        captured["len"] = len(body)
        captured["match"] = body == payload
        return {"size_bytes": len(body)}

    mcp, store = _build_app(sink)
    token = store.reserve(origin_id="big.bin", max_bytes=len(payload))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"size_bytes": len(payload)}
    assert captured["len"] == len(payload)
    assert captured["match"] is True


@pytest.mark.asyncio
async def test_post_sync_sink_returning_plain_dict_rejected() -> None:
    """A non-awaitable sink return surfaces as 500.

    The route ``await``s the sink result unconditionally — the sink
    contract is an async callable. A sync sink that returns a plain
    dict (not a coroutine) is a programmer bug; awaiting a dict raises
    ``TypeError``, which the route maps to 500 like any other sink
    exception.

    Replaces the old ``test_post_sync_stream_receiver_returning_plain_dict``:
    the sync/async dispatch split is gone, so a plain-dict return is no
    longer a supported variant — it is a misuse.
    """

    def sync_sink(record: UploadRecord, stream: BinaryIO) -> dict[str, Any]:
        return {"ok": True}

    mcp, store = _build_app(sync_sink)  # type: ignore[arg-type]
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

    assert resp.status_code == 500
