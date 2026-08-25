"""Unit tests for the InstructionsBuilder.

Spec: 2026-08-25-instructions-builder-design.md
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    CAPABILITIES,
    DOCS,
    IDENTITY,
    INSTANCE,
    OPERATOR,
    WORKFLOWS,
    ConfigurationError,
    InstructionsBuilder,
    ServerConfig,
    finalize_instructions,
    instructions_for,
)

ALL = frozenset({"alpha", "beta", "gamma"})


class TestAnchors:
    def test_anchor_order(self):
        assert IDENTITY < DOCS < CAPABILITIES < WORKFLOWS < INSTANCE < OPERATOR
        assert (IDENTITY, DOCS, CAPABILITIES, WORKFLOWS, INSTANCE, OPERATOR) == (
            0,
            100,
            200,
            300,
            400,
            500,
        )


class TestAdd:
    def test_orders_by_priority_then_insertion(self):
        b = InstructionsBuilder()
        b.add("second", priority=WORKFLOWS)
        b.add("first", priority=IDENTITY)
        b.add("third", priority=WORKFLOWS)
        b.add("between", priority=CAPABILITIES + 10)
        assert b._render(ALL) == "first\n\nbetween\n\nsecond\n\nthird"

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    def test_rejects_blank_text(self, text: str):
        b = InstructionsBuilder()
        with pytest.raises(ConfigurationError, match="empty"):
            b.add(text, priority=WORKFLOWS)

    def test_strips_surrounding_whitespace(self):
        b = InstructionsBuilder()
        b.add("  padded  \n", priority=IDENTITY)
        assert b._render(ALL) == "padded"

    def test_identity_is_add_at_identity_priority(self):
        b = InstructionsBuilder()
        b.add("later", priority=DOCS)
        b.identity("Who I am.")
        assert b._render(ALL) == "Who I am.\n\nlater"

    def test_documentation_is_core_shaped_sentence_at_docs(self):
        b = InstructionsBuilder()
        b.identity("X.")
        b.add("caps", priority=CAPABILITIES)
        b.documentation("https://example.test/llms.txt")
        assert b._render(ALL) == (
            "X.\n\nFull documentation for this server: "
            "https://example.test/llms.txt\n\ncaps"
        )

    def test_documentation_rejects_blank_url(self):
        with pytest.raises(ConfigurationError, match="empty"):
            InstructionsBuilder().documentation("  ")


class TestPrune:
    def test_snippet_without_tools_is_kept(self):
        b = InstructionsBuilder()
        b.add("kept", priority=WORKFLOWS)
        assert b._render(frozenset()) == "kept"

    def test_snippet_whose_tools_are_all_exposed_is_kept(self):
        b = InstructionsBuilder()
        b.add("kept", priority=WORKFLOWS, tools={"alpha", "beta"})
        assert b._render(ALL) == "kept"

    def test_snippet_with_one_missing_tool_is_dropped(self):
        b = InstructionsBuilder()
        b.add("dropped", priority=WORKFLOWS, tools={"alpha", "zeta"})
        b.add("kept", priority=WORKFLOWS)
        assert b._render(ALL) == "kept"

    def test_drop_is_logged_at_debug_naming_the_tool(
        self, caplog: pytest.LogCaptureFixture
    ):
        b = InstructionsBuilder()
        b.add("dropped", priority=WORKFLOWS, tools={"zeta"})
        with caplog.at_level(logging.DEBUG, logger="fastmcp_pvl_core._instructions"):
            b._render(ALL)
        assert any(
            "zeta" in r.getMessage() and r.levelno == logging.DEBUG
            for r in caplog.records
        )


class TestRegistry:
    def test_same_server_same_builder(self):
        mcp = FastMCP("t")
        assert instructions_for(mcp) is instructions_for(mcp)

    def test_different_servers_different_builders(self):
        assert instructions_for(FastMCP("a")) is not instructions_for(FastMCP("b"))


def _server(*tool_names: str) -> FastMCP:
    mcp = FastMCP("t")
    for name in tool_names:
        mcp.tool(name=name)(lambda: "x")
    return mcp


class TestFinalize:
    def test_sets_mcp_instructions_and_returns_it(self):
        mcp = _server("alpha")
        instructions_for(mcp).identity("Ident.")
        text = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        assert text == "Ident."
        assert mcp.instructions == "Ident."

    def test_zero_identities_raise(self):
        mcp = _server()
        with pytest.raises(ConfigurationError, match="identity"):
            finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")

    def test_two_identities_raise(self):
        mcp = _server()
        b = instructions_for(mcp)
        b.identity("one")
        b.identity("two")
        with pytest.raises(ConfigurationError, match="identity"):
            finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")

    def test_prunes_operator_hidden_and_absent_tools(self):
        mcp = _server("alpha", "beta")
        b = instructions_for(mcp)
        b.identity("Ident.")
        b.add("uses alpha", priority=WORKFLOWS, tools={"alpha"})
        b.add("uses beta", priority=WORKFLOWS, tools={"beta"})
        b.add("uses ghost", priority=WORKFLOWS, tools={"ghost"})
        cfg = ServerConfig(tools_deny=("beta",))
        text = finalize_instructions(mcp, cfg, env_prefix="MY_APP")
        assert text == "Ident.\n\nuses alpha"

    def test_freezes_and_is_idempotent(self, caplog: pytest.LogCaptureFixture):
        mcp = _server()
        b = instructions_for(mcp)
        b.identity("Ident.")
        first = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        with pytest.raises(RuntimeError, match="finalized"):
            b.add("late", priority=WORKFLOWS)
        caplog.clear()
        second = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        assert first == second == mcp.instructions
        assert caplog.records == []


class TestEnvContract:
    def test_extra_appended_at_operator(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS_EXTRA", "  Vault uses PARA.  ")
        mcp = _server()
        b = instructions_for(mcp)
        b.add("instance fact", priority=INSTANCE)
        b.identity("Ident.")
        assert finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP") == (
            "Ident.\n\ninstance fact\n\nVault uses PARA."
        )

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_whitespace_extra_is_unset(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS_EXTRA", value)
        mcp = _server()
        instructions_for(mcp).identity("Ident.")
        assert (
            finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP") == "Ident."
        )

    def test_legacy_replaces_everything_and_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS", "Operator text.")
        mcp = _server()
        instructions_for(mcp).identity("Ident.")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._instructions"):
            text = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        assert text == mcp.instructions == "Operator text."
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "MY_APP_INSTRUCTIONS" in msg and "MY_APP_INSTRUCTIONS_EXTRA" in msg
        assert "ignored" not in msg

    def test_legacy_wins_over_extra_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS", "Operator text.")
        monkeypatch.setenv("MY_APP_INSTRUCTIONS_EXTRA", "extra")
        mcp = _server()
        instructions_for(mcp).identity("Ident.")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._instructions"):
            text = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        assert text == "Operator text."
        assert any(
            "ignored" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

    def test_whitespace_legacy_is_unset(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS", "   ")
        mcp = _server()
        instructions_for(mcp).identity("Ident.")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._instructions"):
            assert (
                finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
                == "Ident."
            )
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_legacy_skips_identity_requirement(self, monkeypatch: pytest.MonkeyPatch):
        """A verbatim operator text is complete on its own; do not fail a
        deployment that never added an identity because the legacy var is set."""
        monkeypatch.setenv("MY_APP_INSTRUCTIONS", "Operator text.")
        assert (
            finalize_instructions(_server(), ServerConfig(), env_prefix="MY_APP")
            == "Operator text."
        )

    def test_prefix_trailing_underscore_normalised(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS_EXTRA", "extra")
        a, b = _server(), _server()
        instructions_for(a).identity("I.")
        instructions_for(b).identity("I.")
        assert finalize_instructions(
            a, ServerConfig(), env_prefix="MY_APP"
        ) == finalize_instructions(b, ServerConfig(), env_prefix="MY_APP_")
