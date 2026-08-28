"""Tests for ``apply_tool_visibility``."""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from fastmcp_pvl_core import ConfigurationError, ServerConfig, apply_tool_visibility
from fastmcp_pvl_core._visibility import exposed_tool_names, registered_tool_names


def build_server() -> FastMCP:
    mcp = FastMCP("t")

    @mcp.tool
    def alpha() -> str:
        return "a"

    @mcp.tool
    def beta() -> str:
        return "b"

    @mcp.tool
    def gamma() -> str:
        return "g"

    @mcp.resource("res://thing")
    def thing() -> str:
        return "r"

    @mcp.prompt
    def hello() -> str:
        return "p"

    return mcp


async def visible_tools(mcp: FastMCP) -> list[str]:
    async with Client(mcp) as client:
        return sorted(t.name for t in await client.list_tools())


class TestDenylist:
    async def test_denied_tools_hidden_from_listing(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_deny=("beta", "gamma")))
        assert await visible_tools(mcp) == ["alpha"]

    async def test_denied_tool_cannot_be_called(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_deny=("beta",)))
        async with Client(mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool("beta", {})

    async def test_other_component_types_untouched(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_deny=("beta",)))
        async with Client(mcp) as client:
            assert [str(r.uri) for r in await client.list_resources()] == [
                "res://thing"
            ]
            assert [p.name for p in await client.list_prompts()] == ["hello"]

    async def test_unknown_names_are_inert(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_deny=("nonexistent",)))
        assert await visible_tools(mcp) == ["alpha", "beta", "gamma"]

    async def test_covers_tools_registered_after_the_call(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_deny=("late",)))

        @mcp.tool
        def late() -> str:
            return "x"

        assert await visible_tools(mcp) == ["alpha", "beta", "gamma"]


class TestAllowlist:
    async def test_only_allowed_tools_exposed(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_allow=("alpha", "gamma")))
        assert await visible_tools(mcp) == ["alpha", "gamma"]

    async def test_unlisted_tool_cannot_be_called(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_allow=("alpha",)))
        async with Client(mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool("beta", {})

    async def test_other_component_types_untouched(self):
        """The allowlist must not fall back to fastmcp's ``only=True`` mode,
        which allowlists across *all* component types and would hide every
        resource and prompt."""
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_allow=("alpha",)))
        async with Client(mcp) as client:
            assert [str(r.uri) for r in await client.list_resources()] == [
                "res://thing"
            ]
            assert [p.name for p in await client.list_prompts()] == ["hello"]

    async def test_covers_tools_registered_after_the_call(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_allow=("alpha",)))

        @mcp.tool
        def late() -> str:
            return "x"

        assert await visible_tools(mcp) == ["alpha"]

    async def test_unknown_names_yield_empty_tool_list(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig(tools_allow=("nonexistent",)))
        assert await visible_tools(mcp) == []


class TestZeroMatchWarning:
    """A fully unmatched allowlist is a silent total tool outage; it must
    warn. These tests are deliberately sync — the diagnostic runs the async
    listing path in a private event loop, which requires no loop running."""

    def test_fully_unmatched_allowlist_warns(self, caplog: pytest.LogCaptureFixture):
        mcp = build_server()
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._visibility"):
            apply_tool_visibility(mcp, ServerConfig(tools_allow=("serach",)))
        assert "exposes ZERO tools" in caplog.text
        assert "serach" in caplog.text

    def test_matching_allowlist_does_not_warn(self, caplog: pytest.LogCaptureFixture):
        mcp = build_server()
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._visibility"):
            apply_tool_visibility(mcp, ServerConfig(tools_allow=("alpha",)))
        assert "ZERO tools" not in caplog.text

    def test_partially_matching_allowlist_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Partial mismatch stays silent by design (forward compat)."""
        mcp = build_server()
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._visibility"):
            apply_tool_visibility(
                mcp, ServerConfig(tools_allow=("alpha", "nonexistent"))
            )
        assert "ZERO tools" not in caplog.text

    async def test_check_is_skipped_inside_a_running_loop(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Inside a running event loop the diagnostic cannot run the async
        listing path synchronously, so it is skipped — no warning, no error,
        and the filtering itself still applies."""
        mcp = build_server()
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._visibility"):
            apply_tool_visibility(mcp, ServerConfig(tools_allow=("serach",)))
        assert "ZERO tools" not in caplog.text
        assert await visible_tools(mcp) == []


class TestGuards:
    async def test_neither_list_set_is_a_noop(self):
        mcp = build_server()
        apply_tool_visibility(mcp, ServerConfig())
        assert await visible_tools(mcp) == ["alpha", "beta", "gamma"]

    def test_both_lists_set_raises(self):
        mcp = build_server()
        with pytest.raises(ConfigurationError, match="at most one"):
            apply_tool_visibility(
                mcp, ServerConfig(tools_allow=("alpha",), tools_deny=("beta",))
            )


class TestExposedToolNames:
    """Sync rule must agree with client visibility after apply_tool_visibility."""

    async def test_no_filter_exposes_all(self):
        mcp = build_server()
        cfg = ServerConfig()
        apply_tool_visibility(mcp, cfg)
        assert exposed_tool_names(mcp, cfg) == frozenset(await visible_tools(mcp))

    async def test_denylist(self):
        mcp = build_server()
        cfg = ServerConfig(tools_deny=("beta", "nonexistent"))
        apply_tool_visibility(mcp, cfg)
        assert (
            exposed_tool_names(mcp, cfg)
            == frozenset(await visible_tools(mcp))
            == {"alpha", "gamma"}
        )

    async def test_allowlist(self):
        mcp = build_server()
        cfg = ServerConfig(tools_allow=("gamma", "nonexistent"))
        apply_tool_visibility(mcp, cfg)
        assert (
            exposed_tool_names(mcp, cfg)
            == frozenset(await visible_tools(mcp))
            == {"gamma"}
        )

    def test_both_set_raises_like_apply(self):
        with pytest.raises(ConfigurationError):
            exposed_tool_names(
                build_server(), ServerConfig(tools_allow=("a",), tools_deny=("b",))
            )


class TestRegisteredToolNames:
    """The pre-visibility set that tells "hidden" and "never registered" apart."""

    def test_lists_every_tool_regardless_of_visibility_config(self):
        mcp = build_server()
        cfg = ServerConfig(tools_deny=("beta",))
        apply_tool_visibility(mcp, cfg)
        assert registered_tool_names(mcp) == {"alpha", "beta", "gamma"}

    def test_exposed_is_a_subset(self):
        mcp = build_server()
        cfg = ServerConfig(tools_allow=("gamma",))
        apply_tool_visibility(mcp, cfg)
        registered = registered_tool_names(mcp)
        assert exposed_tool_names(mcp, cfg) < registered
        assert "beta" in registered
