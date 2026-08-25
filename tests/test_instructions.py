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
