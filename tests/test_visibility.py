"""Tests for ``apply_tool_visibility``."""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from fastmcp_pvl_core import ConfigurationError, ServerConfig, apply_tool_visibility
from fastmcp_pvl_core._visibility import effective_tool_names, registered_tool_names


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


def visible_tools_sync(mcp: FastMCP) -> list[str]:
    """List through a real client outside an already-running event loop."""
    return asyncio.run(visible_tools(mcp))


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


class TestEffectiveToolNames:
    """FastMCP listing is the synchronous finalizer's visibility oracle."""

    def test_no_filter_exposes_all(self):
        mcp = build_server()
        cfg = ServerConfig()
        apply_tool_visibility(mcp, cfg)
        assert effective_tool_names(mcp, cfg) == frozenset(visible_tools_sync(mcp))

    def test_denylist(self):
        mcp = build_server()
        cfg = ServerConfig(tools_deny=("beta", "nonexistent"))
        apply_tool_visibility(mcp, cfg)
        assert (
            effective_tool_names(mcp, cfg)
            == frozenset(visible_tools_sync(mcp))
            == {"alpha", "gamma"}
        )

    def test_allowlist(self):
        mcp = build_server()
        cfg = ServerConfig(tools_allow=("gamma", "nonexistent"))
        apply_tool_visibility(mcp, cfg)
        assert (
            effective_tool_names(mcp, cfg)
            == frozenset(visible_tools_sync(mcp))
            == {"gamma"}
        )

    def test_both_set_raises_like_apply(self):
        with pytest.raises(ConfigurationError):
            effective_tool_names(
                build_server(), ServerConfig(tools_allow=("a",), tools_deny=("b",))
            )

    def test_tag_disable_uses_fastmcp_effective_visibility(self):
        mcp = build_server()
        mcp.disable(tags={"hidden"}, components={"tool"})

        @mcp.tool(tags={"hidden"})
        def hidden() -> str:
            return "hidden"

        assert (
            effective_tool_names(mcp, ServerConfig())
            == frozenset(visible_tools_sync(mcp))
            == {"alpha", "beta", "gamma"}
        )

    def test_later_transform_overrides_earlier_transform(self):
        mcp = build_server()
        mcp.disable(names={"beta"}, components={"tool"})
        mcp.enable(names={"beta"}, components={"tool"})
        assert (
            effective_tool_names(mcp, ServerConfig())
            == frozenset(visible_tools_sync(mcp))
            == {"alpha", "beta", "gamma"}
        )

    def test_key_disable_uses_fastmcp_effective_visibility(self):
        mcp = build_server()
        beta_key = next(
            tool.key for tool in asyncio.run(mcp.list_tools()) if tool.name == "beta"
        )
        mcp.disable(keys={beta_key})
        assert (
            effective_tool_names(mcp, ServerConfig())
            == frozenset(visible_tools_sync(mcp))
            == {"alpha", "gamma"}
        )

    def test_mounted_namespaced_tools_are_included(self):
        child = FastMCP("child")

        @child.tool
        def mounted() -> str:
            return "mounted"

        mcp = build_server()
        mcp.mount(child, namespace="child")
        assert (
            effective_tool_names(mcp, ServerConfig())
            == frozenset(visible_tools_sync(mcp))
            == {"alpha", "beta", "child_mounted", "gamma"}
        )

    async def test_active_event_loop_is_rejected(self):
        with pytest.raises(RuntimeError, match="synchronous server construction"):
            effective_tool_names(build_server(), ServerConfig())


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
        assert effective_tool_names(mcp, cfg) < registered
        assert "beta" in registered
