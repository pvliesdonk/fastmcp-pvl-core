"""The tools pvl-core registers ship model-facing text that passes the skill's gates.

pvl-core registers ``get_server_info``, ``get_job_result`` and the two
transfer link tools on every downstream's server, so their descriptions,
parameter descriptions and instructions snippets are read by every model
that talks to the family. The claims these tests pin are recorded, with
sources, in ``docs/reference/mcp-model-facing-text.md``; the rules are the
``writing-model-facing-text`` skill's.

Every test lists the tools through a :class:`fastmcp.Client`, so the
asserted text is the wire text, after FastMCP has parsed any docstring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from fastmcp_pvl_core import (
    JobsConfig,
    ServerConfig,
    TransferConfig,
    TransferReadResult,
    build_jobs,
    finalize_instructions,
    instructions_for,
    register_job_tools,
    register_server_info_tool,
    register_transfer_routes,
    utf16_code_units,
)

# Claude Code's default per-description and per-instructions cut, in
# JavaScript string units (UTF-16 code units).
CLAUDE_CODE_CUT = 2_048

_CORE_TOOLS = frozenset(
    {"get_server_info", "get_job_result", "create_download_link", "create_upload_link"}
)

# A Google-style docstring section heading on its own line, from the set
# FastMCP strips when it parses a docstring.
_SECTION_RE = re.compile(
    r"^\s*(Args|Arguments|Parameters|Returns|Yields|Raises):\s*$", re.MULTILINE
)

# Text a model cannot act on: reST markup and the names of pvl-core's own
# domain hooks and package, none of which reach the model as anything it can
# see or call.
_DEVELOPER_TEXT_RE = re.compile(r"``|:(?:class|func|meth):|\bvalidate\b|\bsink\b|pvl")


class _Sink:
    async def read(self, handle: str) -> TransferReadResult:
        raise AssertionError("not called")

    async def write(self, handle: str, body: bytes) -> dict[str, Any]:
        raise AssertionError("not called")


async def _validate(ref: str, kind: str) -> str:
    return ref


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    parameters: dict[str, str]


@pytest.fixture
def server() -> FastMCP:
    """A server carrying every tool and snippet pvl-core registers, no notes."""
    config = ServerConfig(base_url="https://x.example.com", kv_store_url="memory://")
    mcp = FastMCP("probe")
    register_server_info_tool(mcp, server_name="probe", server_version="1.0.0")
    register_job_tools(
        mcp, build_jobs(config, JobsConfig(soft_deadline_s=1.0, result_ttl_s=60.0))
    )
    register_transfer_routes(
        mcp,
        config,
        TransferConfig(
            ttl_default_s=100.0,
            ttl_max_s=200.0,
            grace_ttl_s=60.0,
            lease_s=60.0,
            max_upload_bytes=1024,
        ),
        sink=_Sink(),
        validate=_validate,
    )
    instructions_for(mcp).identity("probe", "Probe server.")
    finalize_instructions(mcp, config, env_prefix="PROBE")
    return mcp


async def _tools(server: FastMCP) -> list[_Tool]:
    async with Client(server) as client:
        return [
            _Tool(
                t.name,
                t.description or "",
                {
                    name: schema.get("description") or ""
                    for name, schema in t.input_schema.get("properties", {}).items()
                },
            )
            for t in await client.list_tools()
        ]


async def test_fixture_registers_every_core_tool(server: FastMCP) -> None:
    """Guard: the assertions below cover every tool pvl-core registers."""
    assert {t.name for t in await _tools(server)} == _CORE_TOOLS


async def test_descriptions_carry_no_docstring_sections(server: FastMCP) -> None:
    """No ``Args:``/``Returns:``/``Raises:`` heading reaches the wire."""
    leaked = [
        f"{t.name}: {m.group(1)}:"
        for t in await _tools(server)
        for m in _SECTION_RE.finditer(t.description)
    ]
    assert not leaked, f"docstring sections shipped as model-facing text: {leaked}"


async def test_descriptions_carry_no_developer_text(server: FastMCP) -> None:
    """No reST markup, hook name or package name reaches the model."""
    found = [
        f"{t.name}: {m.group(0)!r}"
        for t in await _tools(server)
        for text in (t.description, *t.parameters.values())
        for m in _DEVELOPER_TEXT_RE.finditer(text)
    ]
    assert not found, f"developer text in model-facing descriptions: {found}"


async def test_every_parameter_has_a_description(server: FastMCP) -> None:
    """Each argument's meaning lives in its own description, where it is read."""
    missing = [
        f"{t.name}.{name}"
        for t in await _tools(server)
        for name, description in t.parameters.items()
        if not description.strip()
    ]
    assert not missing, f"parameters without a description: {missing}"


async def test_descriptions_fit_the_claude_code_cut(server: FastMCP) -> None:
    """Each tool description and the instructions stay under 2,048 units."""
    over = [
        f"{t.name}: {utf16_code_units(t.description)} units"
        for t in await _tools(server)
        if utf16_code_units(t.description) > CLAUDE_CODE_CUT
    ]
    instructions = server.instructions or ""
    if utf16_code_units(instructions) > CLAUDE_CODE_CUT:
        over.append(f"instructions: {utf16_code_units(instructions)} units")
    assert not over, f"over Claude Code's {CLAUDE_CODE_CUT}-unit cut: {over}"
