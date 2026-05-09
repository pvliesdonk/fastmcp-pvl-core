"""Tests for register_file_exchange_upload public facade."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import UploadRecord, register_file_exchange_upload


@pytest.mark.asyncio
async def test_registration_adds_create_upload_link_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        return {"target_id": record.target_id}

    mcp = FastMCP(name="test")
    handle = register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        receiver=recv,
    )
    assert handle.namespace == "ns"
    assert handle.tool_name == "create_upload_link"

    # Tool should be registered.
    tool_names = {t.name for t in await mcp.list_tools()}
    assert "create_upload_link" in tool_names


@pytest.mark.asyncio
async def test_create_upload_link_returns_url_and_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        receiver=lambda rec, body: {"ok": True},
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    result = await tool.run({"target_id": "vault/foo.md"})
    payload = result.structured_content or {}
    assert payload["target_id"] == "vault/foo.md"
    assert payload["upload_url"].startswith("http://srv.test/ns/uploads/")
    assert payload["expires_in_seconds"] > 0


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
            receiver=lambda rec, body: {"ok": True},
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
async def test_pre_link_validator_blocks_invalid_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    def reject(target_id: str, extra: dict[str, Any] | None) -> None:
        if ".." in target_id:
            raise ValueError(f"path traversal rejected: {target_id}")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        receiver=lambda rec, body: {"ok": True},
        pre_link_validator=reject,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    with pytest.raises(Exception, match="path traversal rejected"):
        await tool.run({"target_id": "../../etc/passwd"})


@pytest.mark.asyncio
async def test_pre_link_validator_passes_extra_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    seen: dict[str, Any] = {}

    def vlog(target_id: str, extra: dict[str, Any] | None) -> None:
        seen["target_id"] = target_id
        seen["extra"] = extra

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UPLOAD",
        receiver=lambda rec, body: {"ok": True},
        pre_link_validator=vlog,
    )
    tool = await mcp.get_tool("create_upload_link")
    assert tool is not None
    await tool.run({"target_id": "x.md", "extra": {"k": 1}})
    assert seen == {"target_id": "x.md", "extra": {"k": 1}}
