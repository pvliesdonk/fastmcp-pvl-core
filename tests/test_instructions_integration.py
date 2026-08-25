"""Real server, real visibility, real initialize: the instructions a client
receives are the pruned, finalized text (spec §Testing / integration)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastmcp import Client, FastMCP

from fastmcp_pvl_core import (
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
)
from fastmcp_pvl_core._jobs.records import JOB_POLL_TOOL_NAME


class _Sink:
    async def read(self, handle: str) -> TransferReadResult:
        return TransferReadResult(b"x", "text/plain", "x.txt")

    async def write(self, handle: str, body: bytes) -> Mapping[str, Any]:
        return {"stored": handle}


async def _validate(ref: str, kind: str) -> str:
    return f"{kind}:{ref}"


async def test_client_receives_pruned_finalized_instructions(monkeypatch):
    monkeypatch.setenv("APP_INSTRUCTIONS_EXTRA", "This deployment is a demo.")
    config = ServerConfig(
        base_url="https://x.example.com",
        kv_store_url="memory://",
        tools_deny=("create_upload_link",),
    )
    mcp = FastMCP("app")
    instructions_for(mcp).identity("A demo server.")
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

    async with Client(mcp) as client:
        received = client.initialize_result.instructions
        listed = {t.name for t in await client.list_tools()}

    assert received == text == mcp.instructions
    assert text.startswith("A demo server.")
    assert JOB_POLL_TOOL_NAME in text  # job tool exposed → snippet kept
    # transfer snippet dropped: upload hidden
    assert "create_download_link" not in text
    assert text.endswith("This deployment is a demo.")
    assert "create_upload_link" not in listed and JOB_POLL_TOOL_NAME in listed
