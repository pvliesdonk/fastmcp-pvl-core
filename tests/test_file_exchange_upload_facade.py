"""Tests for register_file_exchange_upload public facade."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, BinaryIO

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import SinkContext, register_file_exchange_upload


async def _sink(stream: BinaryIO, ctx: SinkContext) -> Mapping[str, Any]:
    """An async ``SinkHook`` that drains the stream and reports the byte count."""
    data = stream.read()
    return {"stored": True, "bytes": len(data)}


@pytest.mark.asyncio
async def test_registration_adds_create_upload_link_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    async def recv(stream: BinaryIO, ctx: SinkContext) -> Mapping[str, Any]:
        return {"origin_id": ctx.origin_id}

    mcp = FastMCP(name="test")
    handle = register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=recv,
    )
    assert handle.namespace == "ns"
    assert handle.tool_name == "create_upload_link"

    # Tool should be registered.
    tool_names = {t.name for t in await mcp.list_tools()}
    assert "create_upload_link" in tool_names


@pytest.mark.asyncio
async def test_create_upload_link_success_returns_url_ttl_maxbytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    mcp = FastMCP(name="test")
    handle = register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    result = await tool.run({"origin_id": "x"})
    payload = result.structured_content or {}
    assert set(payload) == {"url", "ttl_seconds", "max_bytes"}
    assert payload["url"].startswith("http://srv.test/ns/uploads/")
    assert payload["ttl_seconds"] > 0
    assert payload["max_bytes"] == handle.max_bytes_default


@pytest.mark.asyncio
async def test_missing_base_url_returns_disabled_handle(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When transport is http but BASE_URL is unset, the registrar disables upload."""
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.delenv("TEST_UPLOAD_BASE_URL", raising=False)

    mcp = FastMCP(name="test")
    with caplog.at_level(logging.WARNING):
        handle = register_file_exchange_upload(
            mcp,
            namespace="ns",
            env_prefix="TEST_UPLOAD",
            sink=_sink,
        )

    assert handle.enabled is False
    assert handle.upload_store is None
    assert handle.namespace == "ns"
    assert handle.tool_name == "create_upload_link"

    # No tool registered.
    tools = await mcp.list_tools()
    assert all(t.name != "create_upload_link" for t in tools)

    # Operator-visible warning emitted.
    assert any(
        "TEST_UPLOAD_BASE_URL" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_create_upload_link_rejection_returns_transfer_failed_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pre_link_validator raising ValueError returns a transfer_failed envelope."""
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    def reject(origin_id: str, destination: str | None) -> None:
        raise ValueError("bad destination")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
        pre_link_validator=reject,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    result = await tool.run({"origin_id": "x", "destination": "../etc"})
    payload = result.structured_content or {}
    assert payload == {
        "error": "transfer_failed",
        "method": "http_upload",
        "receiver_server": "ns",
        "origin_id": "x",
        "message": "bad destination",
    }


@pytest.mark.asyncio
async def test_create_upload_link_content_type_mismatch_returns_transfer_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content-type hint mismatching accepts list returns a transfer_failed envelope."""
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
        accepts=("text/markdown",),
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    result = await tool.run({"origin_id": "x", "content_type": "image/png"})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert payload["receiver_server"] == "ns"
    assert payload["origin_id"] == "x"


@pytest.mark.asyncio
async def test_create_upload_link_clamps_ttl_and_max_bytes_to_ceilings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ttl_seconds and max_bytes in the response are clamped to operator ceilings."""
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_TTL_MAX", "600")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_MAX_BYTES", "1000000")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    result = await tool.run(
        {"origin_id": "x", "ttl_seconds": 99999, "max_bytes": 99999999}
    )
    payload = result.structured_content or {}
    assert payload["ttl_seconds"] == 600
    assert payload["max_bytes"] == 1_000_000


@pytest.mark.asyncio
async def test_pre_link_validator_blocks_invalid_origin_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    def reject(origin_id: str, destination: str | None) -> None:
        # ``origin_id`` shape passes the baseline segment rules;
        # the validator rejects on a domain-specific rule.
        if not origin_id.endswith(".md"):
            raise ValueError(f"only .md uploads accepted: {origin_id}")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
        pre_link_validator=reject,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    # ValueError from pre_link_validator surfaces as transfer_failed envelope.
    result = await tool.run({"origin_id": "passwd.txt"})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert "only .md uploads accepted" in payload["message"]


@pytest.mark.asyncio
async def test_pre_link_validator_other_exception_logs_as_bug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-ValueError validator failures propagate AND log as server-side bugs.

    A validator that raises ``RuntimeError`` (typo, AttributeError on
    extra-dict access, etc.) is a programming bug, not a caller-input
    error. The exception still propagates so FastMCP returns a tool
    error, but the runtime additionally logs an ERROR with a
    "non-ValueError" marker so operators can distinguish server-side
    validator bugs from client-side validation rejections.
    """
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    def buggy(origin_id: str, destination: str | None) -> None:
        raise RuntimeError("kaboom")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
        pre_link_validator=buggy,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    with caplog.at_level(logging.ERROR):
        with pytest.raises(Exception, match="kaboom"):
            await tool.run({"origin_id": "x.md"})
    assert any("non-ValueError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_pre_link_validator_async_is_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``async def`` validator must actually run, not be silently no-op'd.

    Regression guard: without ``inspect.isawaitable`` at the call site
    the coroutine returned by an async validator would be discarded
    unawaited and validation would silently pass.
    """
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    seen: dict[str, Any] = {}

    async def vlog(origin_id: str, destination: str | None) -> None:
        seen["origin_id"] = origin_id
        seen["called"] = True

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
        pre_link_validator=vlog,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    await tool.run({"origin_id": "x.md"})
    assert seen == {"origin_id": "x.md", "called": True}


@pytest.mark.asyncio
async def test_env_overrides_max_bytes_and_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_MAX_BYTES", "5000000")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_TTL", "120")

    mcp = FastMCP(name="test")
    h = register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
    )
    assert h.max_bytes_default == 5_000_000
    assert h.ttl_default == 120.0


@pytest.mark.asyncio
async def test_env_override_ttl_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{PREFIX}_UPLOAD_TTL_MAX` overrides the operator ceiling."""
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_TTL_MAX", "7200")

    mcp = FastMCP(name="test")
    h = register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
    )
    assert h.ttl_max == 7200.0


def test_missing_sink_raises_type_error() -> None:
    """``sink`` is a required kwarg; omitting it is a plain ``TypeError``.

    Replaces the former ``test_neither_receiver_raises`` /
    ``test_mutual_exclusion_of_receivers`` pair. The four divergent
    receiver shapes collapsed into a single required ``sink`` hook, so
    there is no "exactly one of receiver/stream_receiver" rule and no
    library-raised ``ValueError`` — a missing ``sink`` is simply a
    missing required argument, caught by Python itself.
    """
    mcp = FastMCP(name="test")
    with pytest.raises(TypeError):
        register_file_exchange_upload(  # type: ignore[call-arg]
            mcp,
            namespace="ns",
            env_prefix="TEST_X",
        )


def test_stdio_transport_returns_disabled_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "stdio")
    mcp = FastMCP(name="test")
    h = register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
    )
    assert h.enabled is False
    assert h.upload_store is None


# ---------------------------------------------------------------------------
# UploadHandle.create_link direct edges (escape valve, used by advanced wraps)
# ---------------------------------------------------------------------------


def test_create_link_raises_when_upload_disabled() -> None:
    """``upload_store=None`` (disabled handle) makes create_link fail loudly.

    Covers the RuntimeError branch — the direct API is the documented escape
    valve for advanced callers, and a silent miss here would mint links
    against the wrong store after a transport-mode misconfiguration.
    """
    from fastmcp_pvl_core import UploadHandle

    handle = UploadHandle(
        namespace="ns",
        tool_name="create_upload_link",
        enabled=False,
        upload_store=None,
        ttl_default=300.0,
        ttl_max=3600.0,
        max_bytes_default=10 * 1024 * 1024,
    )
    with pytest.raises(RuntimeError, match="upload not enabled"):
        handle.create_link(origin_id="x")


def test_create_link_floors_non_positive_ttl_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ttl_seconds <= 0`` falls back to ``ttl_default``; never born-expired."""
    monkeypatch.setenv("TEST_UPLOAD_FLOOR_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_FLOOR_BASE_URL", "http://srv.test")
    mcp = FastMCP(name="floor")
    handle = register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD_FLOOR",
        sink=_sink,
    )
    # Set ttl_default via env for test isolation.
    _, eff_zero, _ = handle.create_link(origin_id="x", ttl_seconds=0)
    assert eff_zero == handle.ttl_default
    _, eff_neg, _ = handle.create_link(origin_id="y", ttl_seconds=-5)
    assert eff_neg == handle.ttl_default
    _, eff_none, _ = handle.create_link(origin_id="z", ttl_seconds=None)
    assert eff_none == handle.ttl_default


@pytest.mark.asyncio
async def test_create_upload_link_rejects_path_traversal_origin_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline ExchangeURI.validate_segment fires before pre_link_validator.

    The spec requires ``origin_id`` to follow the same character rules as
    ``exchange://`` segments. The ``create_upload_link`` tool calls
    ``ExchangeURI.validate_segment(..., role="json_param")`` BEFORE running
    ``pre_link_validator``, so a traversal attempt is rejected in-band even
    when no validator is configured — consistent with all other in-band
    rejections in this tool.
    """
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    # ExchangeURIError is wrapped and returned as a transfer_failed
    # envelope — consistent with destination/content_type/pre_link_validator
    # rejections — rather than surfacing as a raw tool error.
    result = await tool.run({"origin_id": "../etc/passwd"})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert payload["receiver_server"] == "ns"
    assert payload["origin_id"] == "../etc/passwd"


@pytest.mark.asyncio
async def test_create_upload_link_rejects_destination_with_leading_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built-in destination guard rejects leading/trailing whitespace.

    This validation runs BEFORE any pre_link_validator, so no validator
    is registered here — the tool's own guard is under test.
    """
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    result = await tool.run({"origin_id": "x", "destination": " foo"})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert payload["receiver_server"] == "ns"
    assert payload["origin_id"] == "x"


@pytest.mark.asyncio
async def test_create_upload_link_rejects_destination_with_control_char(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built-in destination guard rejects control characters (ord < 0x20).

    This validation runs BEFORE any pre_link_validator, so no validator
    is registered here — the tool's own guard is under test.
    """
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    result = await tool.run({"origin_id": "x", "destination": "foo\x01bar"})
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert payload["receiver_server"] == "ns"
    assert payload["origin_id"] == "x"


def test_malformed_upload_max_bytes_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric ``{PREFIX}_UPLOAD_MAX_BYTES`` raises a named error.

    A bare ``int()`` would surface ``ValueError: invalid literal for
    int() with base 10: 'ten'`` — operator has to grep the source to
    locate the offending env var. Wrapping in ``ConfigurationError``
    names the variable explicitly.
    """
    from fastmcp_pvl_core._errors import ConfigurationError

    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_MAX_BYTES", "ten")

    mcp = FastMCP(name="test")
    with pytest.raises(ConfigurationError, match="UPLOAD_MAX_BYTES.*ten"):
        register_file_exchange_upload(
            mcp,
            namespace="ns",
            env_prefix="TEST_UPLOAD",
            sink=_sink,
        )


def test_malformed_upload_ttl_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric ``{PREFIX}_UPLOAD_TTL`` raises a named error."""
    from fastmcp_pvl_core._errors import ConfigurationError

    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_TTL", "soon")

    mcp = FastMCP(name="test")
    with pytest.raises(ConfigurationError, match="UPLOAD_TTL.*soon"):
        register_file_exchange_upload(
            mcp,
            namespace="ns",
            env_prefix="TEST_UPLOAD",
            sink=_sink,
        )


def test_malformed_upload_ttl_max_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric ``{PREFIX}_UPLOAD_TTL_MAX`` raises a named error."""
    from fastmcp_pvl_core._errors import ConfigurationError

    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_TTL_MAX", "lots")

    mcp = FastMCP(name="test")
    with pytest.raises(ConfigurationError, match="UPLOAD_TTL_MAX.*lots"):
        register_file_exchange_upload(
            mcp,
            namespace="ns",
            env_prefix="TEST_UPLOAD",
            sink=_sink,
        )


def test_negative_upload_max_bytes_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-positive ``{PREFIX}_UPLOAD_MAX_BYTES`` raises a named error."""
    from fastmcp_pvl_core._errors import ConfigurationError

    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_MAX_BYTES", "-1")

    mcp = FastMCP(name="test")
    with pytest.raises(ConfigurationError, match="UPLOAD_MAX_BYTES.*positive"):
        register_file_exchange_upload(
            mcp,
            namespace="ns",
            env_prefix="TEST_UPLOAD",
            sink=_sink,
        )


def test_negative_upload_ttl_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-positive ``{PREFIX}_UPLOAD_TTL`` raises a named error."""
    from fastmcp_pvl_core._errors import ConfigurationError

    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_TTL", "-5")

    mcp = FastMCP(name="test")
    with pytest.raises(ConfigurationError, match="UPLOAD_TTL.*positive"):
        register_file_exchange_upload(
            mcp,
            namespace="ns",
            env_prefix="TEST_UPLOAD",
            sink=_sink,
        )


def test_negative_upload_ttl_max_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-positive ``{PREFIX}_UPLOAD_TTL_MAX`` raises a named error."""
    from fastmcp_pvl_core._errors import ConfigurationError

    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_TTL_MAX", "-1")

    mcp = FastMCP(name="test")
    with pytest.raises(ConfigurationError, match="UPLOAD_TTL_MAX.*positive"):
        register_file_exchange_upload(
            mcp,
            namespace="ns",
            env_prefix="TEST_UPLOAD",
            sink=_sink,
        )


@pytest.mark.asyncio
async def test_sync_pre_link_validator_runs_in_threadpool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync ``pre_link_validator`` dispatches via ``asyncio.to_thread``.

    Symmetric pin to ``test_post_sync_receiver_runs_in_threadpool`` for
    the receiver path. Without the threadpool dispatch, a sync validator
    doing DB / filesystem I/O would stall the event loop.
    """
    import threading

    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    captured: dict[str, Any] = {}

    def sync_vld(origin_id: str, destination: str | None) -> None:
        captured["thread"] = threading.current_thread().name
        captured["is_main"] = threading.current_thread() is threading.main_thread()

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        sink=_sink,
        pre_link_validator=sync_vld,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    await tool.run({"origin_id": "x.md"})
    assert captured["is_main"] is False
