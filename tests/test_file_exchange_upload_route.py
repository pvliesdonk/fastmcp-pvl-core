"""Integration tests for the POST /<ns>/uploads/{token} route."""

from __future__ import annotations

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
        captured["target_id"] = record.target_id
        captured["body"] = body
        return {"path": record.target_id, "size_bytes": len(body)}

    mcp, store = _build_app(recv)
    token = store.reserve(target_id="hello.txt", max_bytes=1024)

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
    assert captured["target_id"] == "hello.txt"
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
    token = store.reserve(target_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        first = await client.post(f"/ns/uploads/{token}", content=b"x")
        second = await client.post(f"/ns/uploads/{token}", content=b"x")
    assert first.status_code == 200
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_post_expired_token_returns_410() -> None:
    mcp, store = _build_app(lambda rec, body: {})
    token = store.reserve(target_id="x", max_bytes=10, ttl_seconds=-1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 410
