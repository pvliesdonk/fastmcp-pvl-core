"""Tests for ``register_server_info_tool``."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import __version__ as core_version
from fastmcp_pvl_core import register_server_info_tool


async def _call(mcp: FastMCP, tool_name: str = "get_server_info") -> dict:
    result = await mcp.call_tool(tool_name, {})
    return result.structured_content


class TestRegisterServerInfoTool:
    async def test_required_fields_only(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.2.3",
            server_name="my-mcp",
        )
        payload = await _call(mcp)
        assert payload == {
            "server_name": "my-mcp",
            "server_version": "1.2.3",
            "core_version": core_version,
        }

    async def test_no_upstream_block_when_provider_omitted(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="my-mcp",
            upstream_label="paperless",  # ignored without provider
        )
        payload = await _call(mcp)
        assert "paperless" not in payload
        assert "upstream" not in payload

    async def test_upstream_string_wrapped_under_label(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="paperless-mcp",
            upstream_version=lambda: "v2.20.14",
            upstream_label="paperless",
        )
        payload = await _call(mcp)
        assert payload["paperless"] == {"version": "v2.20.14"}
        assert payload["server_name"] == "paperless-mcp"
        assert payload["server_version"] == "1.0.0"
        assert payload["core_version"] == core_version

    async def test_upstream_dict_used_as_is(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="paperless-mcp",
            upstream_version=lambda: {
                "version": "v2.20.14",
                "update_available": False,
            },
            upstream_label="paperless",
        )
        payload = await _call(mcp)
        assert payload["paperless"] == {
            "version": "v2.20.14",
            "update_available": False,
        }

    async def test_upstream_async_callable(self):
        async def fetch():
            return "v9.9.9"

        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="my-mcp",
            upstream_version=fetch,
            upstream_label="up",
        )
        payload = await _call(mcp)
        assert payload["up"] == {"version": "v9.9.9"}

    async def test_upstream_failure_returns_structured_error(self):
        def boom():
            raise RuntimeError("upstream is on fire")

        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="my-mcp",
            upstream_version=boom,
            upstream_label="paperless",
        )
        payload = await _call(mcp)
        # Wrapper info still present.
        assert payload["server_name"] == "my-mcp"
        assert payload["server_version"] == "1.0.0"
        assert payload["core_version"] == core_version
        # Upstream block carries the error instead of a version.
        assert payload["paperless"] == {"error": "upstream is on fire"}

    async def test_upstream_returns_none(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="my-mcp",
            upstream_version=lambda: None,
            upstream_label="up",
        )
        payload = await _call(mcp)
        assert payload["up"] == {"version": None}

    async def test_default_upstream_label(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="my-mcp",
            upstream_version=lambda: "v1",
        )
        payload = await _call(mcp)
        assert payload["upstream"] == {"version": "v1"}

    async def test_custom_tool_name(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="my-mcp",
            tool_name="version_info",
        )
        payload = await _call(mcp, tool_name="version_info")
        assert payload["server_name"] == "my-mcp"

    async def test_tool_is_marked_read_only(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="my-mcp",
        )
        tools = await mcp.list_tools()
        target = next(t for t in tools if t.name == "get_server_info")
        assert target.annotations is not None
        assert target.annotations.readOnlyHint is True

    async def test_custom_description(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="my-mcp",
            description="My custom blurb.",
        )
        tools = await mcp.list_tools()
        target = next(t for t in tools if t.name == "get_server_info")
        assert target.description == "My custom blurb."

    async def test_default_description_mentions_server_name(self):
        mcp = FastMCP("t")
        register_server_info_tool(
            mcp,
            server_version="1.0.0",
            server_name="paperless-mcp",
        )
        tools = await mcp.list_tools()
        target = next(t for t in tools if t.name == "get_server_info")
        assert target.description is not None
        assert "paperless-mcp" in target.description


@pytest.mark.parametrize("upstream_value", [123, 4.2, True])
async def test_upstream_non_string_non_dict_coerced_to_str(upstream_value):
    mcp = FastMCP("t")
    register_server_info_tool(
        mcp,
        server_version="1.0.0",
        server_name="my-mcp",
        upstream_version=lambda: upstream_value,
        upstream_label="up",
    )
    result = await mcp.call_tool("get_server_info", {})
    assert result.structured_content["up"] == {"version": str(upstream_value)}
