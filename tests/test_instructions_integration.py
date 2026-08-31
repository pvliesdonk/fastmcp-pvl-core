"""Real server, real visibility, real initialize: the instructions a client
receives are the pruned, finalized text (spec §Testing / integration)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from fastmcp import Client, FastMCP

from fastmcp_pvl_core import (
    CLAUDE_CODE_INSTRUCTIONS_LIMIT_UTF16,
    InstructionRole,
    JobsConfig,
    ServerConfig,
    TransferConfig,
    TransferReadResult,
    apply_tool_visibility,
    build_jobs,
    finalize_instructions,
    instructions_for,
    register_job_tools,
    register_long_running_tool,
    register_transfer_routes,
    utf16_code_units,
)
from fastmcp_pvl_core._jobs.records import JOB_POLL_TOOL_NAME


class _Sink:
    async def read(self, handle: str) -> TransferReadResult:
        return TransferReadResult(b"x", "text/plain", "x.txt")

    async def write(self, handle: str, body: bytes) -> Mapping[str, Any]:
        return {"stored": handle}


async def _validate(ref: str, kind: str) -> str:
    return f"{kind}:{ref}"


async def _client_view(mcp: FastMCP) -> tuple[str | None, set[str]]:
    """Return initialize instructions and the real client tool listing."""
    async with Client(mcp) as client:
        return (
            client.initialize_result.instructions,
            {tool.name for tool in await client.list_tools()},
        )


def test_client_receives_pruned_finalized_instructions(monkeypatch):
    monkeypatch.setenv("APP_INSTANCE_DESCRIPTION", "Contains demo data.")
    monkeypatch.setenv("APP_INSTRUCTIONS_EXTRA", "Use demo-safe behavior.")
    config = ServerConfig(
        base_url="https://x.example.com",
        kv_store_url="memory://",
        tools_deny=("create_upload_link",),
    )
    mcp = FastMCP("app")
    builder = instructions_for(mcp)
    builder.identity("app", "A demo server.")
    builder.add("This instance is READ-WRITE.", role=InstructionRole.INSTANCE)
    builder.add("Provides demo operations.", role=InstructionRole.CAPABILITIES)
    builder.documentation("https://example.test/llms.txt")
    register_transfer_routes(
        mcp,
        config,
        TransferConfig(
            ttl_default_s=10,
            ttl_max_s=20,
            grace_ttl_s=5,
            lease_s=5,
            max_upload_bytes=1024,
        ),
        sink=_Sink(),
        validate=_validate,
    )
    jobs = build_jobs(config, JobsConfig(soft_deadline_s=0.1, result_ttl_s=60.0))

    @register_long_running_tool(mcp, jobs, name="slow")
    async def slow() -> str:
        return "done"

    register_job_tools(mcp, jobs)
    apply_tool_visibility(mcp, config)
    text = finalize_instructions(mcp, config, env_prefix="APP")

    received, listed = asyncio.run(_client_view(mcp))

    assert received == text == mcp.instructions
    assert text.startswith("app: A demo server.\n\nContains demo data.")
    assert text.index("Contains demo data.") < text.index(
        "This instance is READ-WRITE."
    )
    assert text.index("This instance is READ-WRITE.") < text.index(
        "Use demo-safe behavior."
    )
    assert text.index("Use demo-safe behavior.") < text.index(
        "Provides demo operations."
    )
    assert text.index("Provides demo operations.") < text.index(JOB_POLL_TOOL_NAME)
    assert JOB_POLL_TOOL_NAME in text  # job tool exposed → snippet kept
    # transfer snippet dropped: upload hidden
    assert "create_download_link" not in text
    assert "create_upload_link" not in listed and JOB_POLL_TOOL_NAME in listed
    assert text.endswith(
        "Full documentation for this server: https://example.test/llms.txt"
    )
    assert utf16_code_units(text) <= CLAUDE_CODE_INSTRUCTIONS_LIMIT_UTF16
