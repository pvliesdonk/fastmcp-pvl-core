"""Tests for register_file_exchange_upload public facade."""

from __future__ import annotations

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
