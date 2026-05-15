"""Tests for register_file_exchange_upload_sender — the http_upload sender."""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    ResolvedSource,
    register_file_exchange_upload_sender,
)


def _resolver(payload: bytes, content_type: str | None = None):
    """A byte_source returning the given payload for any origin_id."""

    def resolve(origin_id: str) -> ResolvedSource:
        return ResolvedSource(
            stream=io.BytesIO(payload),
            content_type=content_type,
            size_bytes=len(payload),
        )

    return resolve


@pytest.mark.asyncio
async def test_registration_adds_upload_tool() -> None:
    mcp = FastMCP(name="t")
    handle = register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=_resolver(b"x")
    )
    assert handle.namespace == "ns"
    assert handle.tool_name == "upload"
    assert "upload" in {t.name for t in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_capability_advertises_http_upload_source() -> None:
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=_resolver(b"x")
    )
    builder = mcp._pvl_file_exchange_builder  # type: ignore[attr-defined]
    cap = builder.build()
    assert cap is not None
    assert cap.to_capability_dict()["transfer_methods"]["http_upload"] == {
        "source": {"tool": "upload"},
    }


@pytest.mark.asyncio
async def test_upload_success_returns_status_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["body"] = request.content
        posted["content_type"] = request.headers.get("content-type")
        return httpx.Response(201, json={"saved": "ok"})

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND",
        byte_source=_resolver(b"PAYLOAD", content_type="application/pdf"),
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "https://recv.test/ns/uploads/tok", "origin_id": "doc-1"}
    )
    payload = result.structured_content or {}
    assert payload == {"status": 201, "body": {"saved": "ok"}}
    assert posted["body"] == b"PAYLOAD"
    assert posted["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_upload_content_type_param_overrides_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ct"] = request.headers.get("content-type")
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND",
        byte_source=_resolver(b"x", content_type="application/pdf"),
    )
    tool = await mcp.get_tool("upload")
    await tool.run(
        {
            "url": "https://recv.test/u/t",
            "origin_id": "d",
            "content_type": "text/markdown",
        }
    )
    assert seen["ct"] == "text/markdown"  # param wins over resolver


@pytest.mark.asyncio
async def test_upload_ssrf_guard_rejects_loopback_url() -> None:
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "http://169.254.169.254/u/t", "origin_id": "d"}
    )
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert payload["origin_id"] == "d"


@pytest.mark.asyncio
async def test_upload_resolver_value_error_returns_transfer_failed() -> None:
    def bad_resolver(origin_id: str) -> ResolvedSource:
        raise ValueError("unknown origin_id")

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=bad_resolver
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "https://recv.test/u/t", "origin_id": "d"}
    )
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert "unknown origin_id" in payload["message"]


@pytest.mark.asyncio
async def test_upload_4xx_transfer_failed_body_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = {
        "error": "transfer_failed",
        "method": "http_upload",
        "receiver_server": "vault",
        "origin_id": "d",
        "message": "destination rejected",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=envelope)

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "https://recv.test/u/t", "origin_id": "d"}
    )
    assert (result.structured_content or {}) == envelope


@pytest.mark.asyncio
async def test_upload_async_resolver_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    async def aresolve(origin_id: str) -> ResolvedSource:
        return ResolvedSource(stream=io.BytesIO(b"y"), content_type=None, size_bytes=1)

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=aresolve
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "https://recv.test/u/t", "origin_id": "d"}
    )
    assert (result.structured_content or {})["status"] == 200
