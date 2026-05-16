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
    """A source hook returning the given payload for any origin_id."""

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
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"x")
    )
    assert handle.namespace == "ns"
    assert handle.tool_name == "upload"
    assert "upload" in {t.name for t in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_capability_advertises_http_upload_source() -> None:
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"x")
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
        mcp,
        namespace="ns",
        env_prefix="TEST_SEND",
        source=_resolver(b"PAYLOAD", content_type="application/pdf"),
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
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
        mcp,
        namespace="ns",
        env_prefix="TEST_SEND",
        source=_resolver(b"x", content_type="application/pdf"),
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
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
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "http://169.254.169.254/u/t", "origin_id": "d"})
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
        mcp, namespace="ns", env_prefix="TEST_SEND", source=bad_resolver
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
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
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
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
        mcp, namespace="ns", env_prefix="TEST_SEND", source=aresolve
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    assert (result.structured_content or {})["status"] == 200


@pytest.mark.asyncio
async def test_upload_transport_error_returns_transfer_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"data")
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert "upload POST failed" in payload["message"]


@pytest.mark.asyncio
async def test_upload_closes_stream_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ClosingBytesIO(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.closed_flag = False

        def close(self) -> None:
            self.closed_flag = True
            super().close()

    tracking_stream = _ClosingBytesIO(b"data")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    def source_with_tracking(origin_id: str) -> ResolvedSource:
        return ResolvedSource(stream=tracking_stream, content_type=None, size_bytes=4)

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=source_with_tracking
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    assert tracking_stream.closed_flag is True


@pytest.mark.asyncio
async def test_upload_sets_content_length_from_size_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    payload = b"hello world"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_length"] = request.headers.get("content-length")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp,
        namespace="ns",
        env_prefix="TEST_SEND",
        source=_resolver(payload),
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    assert captured["content_length"] == str(len(payload))


@pytest.mark.asyncio
async def test_upload_omits_content_length_when_size_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["has_cl"] = "content-length" in request.headers
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    def no_size_resolver(origin_id: str) -> ResolvedSource:
        return ResolvedSource(
            stream=io.BytesIO(b"abc"), content_type=None, size_bytes=None
        )

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=no_size_resolver
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    assert captured["has_cl"] is False


@pytest.mark.asyncio
async def test_upload_non_json_body_passed_through_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="plain ack", headers={"content-type": "text/plain"}
        )

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    assert (result.structured_content or {}) == {"status": 200, "body": "plain ack"}


# ---------------------------------------------------------------------------
# H1: Invalid origin_id returns transfer_failed; POST is never invoked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["a/b", "..", ".\x00a", " leading", "trailing "])
async def test_upload_invalid_origin_id_returns_transfer_failed(
    bad_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Segment-rule violations → transfer_failed without touching the network."""
    call_count: list[int] = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": bad_id})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert call_count[0] == 0, "POST handler must not be called for invalid origin_id"


# ---------------------------------------------------------------------------
# H2: Non-envelope 4xx/5xx → synthesized transfer_failed (no crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,response_kwargs",
    [
        (500, {"text": "Internal Server Error"}),
        (422, {"json": {"detail": "bad request"}}),
    ],
)
async def test_upload_non_envelope_error_response_returns_transfer_failed(
    status: int,
    response_kwargs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-transfer_failed error responses produce a synthesized transfer_failed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, **response_kwargs)

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert str(status) in payload["message"]


# ---------------------------------------------------------------------------
# H3: content_type=None from resolver + no param → application/octet-stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_content_type_octet_stream_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When resolver returns content_type=None and caller omits it, use octet-stream."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ct"] = request.headers.get("content-type")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    def no_ct_resolver(origin_id: str) -> ResolvedSource:
        return ResolvedSource(stream=io.BytesIO(b"bytes"), content_type=None)

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=no_ct_resolver
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    assert seen["ct"] == "application/octet-stream"
    assert (result.structured_content or {}).get("status") == 200


# ---------------------------------------------------------------------------
# H4: JSON content-type with malformed body → fallback to resp.text, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_malformed_json_body_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """application/json response with truncated JSON body falls back to text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"broken":',
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    payload = result.structured_content or {}
    # 200 response, body should be the raw text since JSON parse failed
    assert payload["status"] == 200
    assert isinstance(payload["body"], str)
    assert '{"broken":' in payload["body"]


# ---------------------------------------------------------------------------
# H5: Single POST, no retry — handler invoked exactly once on transport error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_single_post_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upload tool makes exactly one POST attempt, never retries."""
    call_count: list[int] = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=_resolver(b"payload")
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert call_count[0] == 1, f"expected exactly 1 POST attempt, got {call_count[0]}"


# ---------------------------------------------------------------------------
# H6: Sync resolver returning an awaitable (inspect.isawaitable branch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_sync_resolver_returning_awaitable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain (non-async def) callable returning an awaitable is handled correctly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    async def _inner(origin_id: str) -> ResolvedSource:
        return ResolvedSource(stream=io.BytesIO(b"awaitable"), content_type=None)

    # A regular function (not async def) that returns a coroutine object.
    # inspect.iscoroutinefunction returns False, but inspect.isawaitable
    # returns True on the result — this tests the second branch.
    def sync_returning_awaitable(origin_id: str) -> Any:
        return _inner(origin_id)

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp,
        namespace="ns",
        env_prefix="TEST_SEND",
        source=sync_returning_awaitable,
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    assert (result.structured_content or {}).get("status") == 200


# ---------------------------------------------------------------------------
# H7: Stream .read() raises OSError mid-body → transfer_failed (finding D)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_stream_read_oserror_returns_transfer_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError from the source hook's stream during POST body → transfer_failed."""

    class _FailingStream(io.BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            raise OSError("disk read failed")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    def failing_resolver(origin_id: str) -> ResolvedSource:
        return ResolvedSource(stream=_FailingStream(), content_type=None)

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", source=failing_resolver
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert "read" in payload["message"].lower()


# ---------------------------------------------------------------------------
# H8: Stream .close() raises → real result still returned (finding E)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_stream_close_error_does_not_mask_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If stream.close() raises, the tool's real return value is still returned."""

    class _ClosingRaisesStream(io.BytesIO):
        def close(self) -> None:
            # Do NOT call super().close() — just raise to simulate a
            # flush-on-close OSError without actually closing the buffer.
            raise OSError("close failed")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"saved": True})

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    def resolver_with_bad_close(origin_id: str) -> ResolvedSource:
        return ResolvedSource(
            stream=_ClosingRaisesStream(b"hello"), content_type=None, size_bytes=5
        )

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp,
        namespace="ns",
        env_prefix="TEST_SEND",
        source=resolver_with_bad_close,
    )
    tool = await mcp.get_tool("upload")
    assert tool is not None
    result = await tool.run({"url": "https://recv.test/u/t", "origin_id": "d"})
    # The real result (success) must not be masked by the close() error.
    payload = result.structured_content or {}
    assert payload.get("status") == 200
    assert payload.get("body") == {"saved": True}
