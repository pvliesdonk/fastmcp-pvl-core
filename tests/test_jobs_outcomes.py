"""How the job tools end, on the wire and in the logs (ADR 0005, #364/#369/#370).

A client that does not negotiate the tasks extension is modelled by
suppressing FastMCP ``Client``'s internal extension fold-in, the same way as
``tests/test_jobs_native_task.py``: FastMCP's own client would otherwise run
every long-running call as a native task and never reach the fallback.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool
from mcp.types import TextContent

from fastmcp_pvl_core import (
    JobsConfig,
    ServerConfig,
    build_jobs,
    configure_task_backend,
    is_tool_boundary,
    register_job_tools,
    register_long_running_tool,
    register_server_info_tool,
    wire_middleware_stack,
)
from fastmcp_pvl_core._jobs.records import JOB_POLL_TOOL_NAME
from fastmcp_pvl_core._tool_boundary import FAULT_MESSAGE

_BOUNDARY_LOGGER = "fastmcp_pvl_core._tool_boundary"


@pytest.fixture
def non_task_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fastmcp.client.client.build_internal_client_extensions",
        lambda _callback: [],
    )


def _server(*, max_per_subject: int = 10) -> FastMCP:
    config = ServerConfig(kv_store_url="memory://")
    mcp = FastMCP("t", mask_error_details=False)
    wire_middleware_stack(mcp)
    configure_task_backend(mcp, "APP", config)
    jobs = build_jobs(
        config,
        JobsConfig(
            soft_deadline_s=0.05, result_ttl_s=60.0, max_per_subject=max_per_subject
        ),
    )
    register_job_tools(mcp, jobs)

    @register_long_running_tool(mcp, jobs)
    async def slow_crash() -> dict[str, Any]:
        await asyncio.sleep(0.2)
        raise RuntimeError("internal detail /srv/secret")

    @register_long_running_tool(mcp, jobs)
    async def fast_crash() -> dict[str, Any]:
        raise RuntimeError("internal detail /srv/secret")

    @register_long_running_tool(mcp, jobs)
    async def slow_ok() -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"ok": True}

    return mcp


def _text(result: Any) -> str:
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text


async def _poll_until_done(client: Client, job_id: str) -> dict[str, Any]:
    for _ in range(100):
        polled = (
            await client.call_tool(JOB_POLL_TOOL_NAME, {"job_id": job_id})
        ).structured_content
        assert polled is not None
        if polled["status"] != "working":
            return polled
        await asyncio.sleep(0.02)
    raise AssertionError("job never finished")


async def test_every_registered_tool_carries_the_boundary():
    mcp = _server()
    register_server_info_tool(mcp, server_version="1", server_name="t")
    for name in (JOB_POLL_TOOL_NAME, "slow_crash", "get_server_info"):
        tool = await mcp.get_tool(name)
        assert isinstance(tool, FunctionTool), name
        assert is_tool_boundary(tool.fn), name


async def test_unknown_job_id_is_a_request_to_change(caplog):
    async with Client(_server()) as client:
        with caplog.at_level(logging.DEBUG):
            result = await client.call_tool_mcp(JOB_POLL_TOOL_NAME, {"job_id": "nope"})

    assert result.is_error is True
    assert _text(result) == (
        "No job 'nope' for this caller: the id is unknown, has expired, or "
        "belongs to another caller. Run the original tool again if you still "
        "need its result."
    )
    loud = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(("fastmcp", "fastmcp_"))
    ]
    assert loud == []


@pytest.fixture(params=["native", "inline"])
def call_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test on both paths a call can end on before the deadline.

    ``native``: FastMCP's client negotiates tasks, so the tool runs as a
    SEP-2663 task. ``inline``: a client without the tasks extension, so the
    fallback runs the work in the foreground.
    """
    if request.param == "inline":
        monkeypatch.setattr(
            "fastmcp.client.client.build_internal_client_extensions",
            lambda _callback: [],
        )
    return str(request.param)


async def test_fault_before_the_deadline_is_the_fault_message(call_path, caplog):
    async with Client(_server()) as client:
        with caplog.at_level(logging.DEBUG):
            result = await client.call_tool_mcp("fast_crash", {})

    assert result.is_error is True
    assert _text(result) == FAULT_MESSAGE
    # One traceback, from the inner boundary around the domain coroutine.
    boundary = [r for r in caplog.records if r.name == _BOUNDARY_LOGGER]
    assert [(r.levelno, r.exc_info is not None) for r in boundary] == [
        (logging.ERROR, True)
    ]
    assert "function=fast_crash" in boundary[0].getMessage()


async def test_fault_after_promotion_is_the_fault_message(non_task_client, caplog):
    async with Client(_server()) as client:
        with caplog.at_level(logging.DEBUG):
            handle = (await client.call_tool("slow_crash", {})).structured_content
            assert handle is not None and handle["status"] == "working"
            polled = await _poll_until_done(client, handle["job_id"])

    # The poller never sees the exception's text, masked or not (#369).
    assert polled["status"] == "failed"
    assert polled["error"] == FAULT_MESSAGE
    boundary = [r for r in caplog.records if r.name == _BOUNDARY_LOGGER]
    assert [(r.levelno, r.exc_info is not None) for r in boundary] == [
        (logging.ERROR, True)
    ]
    # job_failed follows the boundary's ToolError: ERROR, and no second
    # traceback (#370).
    (failed,) = [r for r in caplog.records if r.getMessage().startswith("job_failed ")]
    assert failed.levelno == logging.ERROR
    assert not failed.exc_info


async def test_job_cap_is_a_request_to_retry_later(non_task_client, caplog):
    async with Client(_server(max_per_subject=1)) as client:
        first = (await client.call_tool("slow_ok", {})).structured_content
        assert first is not None and first["status"] == "working"
        with caplog.at_level(logging.DEBUG):
            result = await client.call_tool_mcp("slow_ok", {})

    assert result.is_error is True
    assert _text(result) == (
        "slow_ok ran past its foreground time limit, and this caller already "
        "has the maximum number of background jobs, so it was stopped. Retry "
        "it later, once older jobs have expired."
    )
    loud = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(("fastmcp", "fastmcp_"))
    ]
    assert loud == []


async def test_domain_tool_error_passes_through(call_path):
    config = ServerConfig(kv_store_url="memory://")
    mcp = FastMCP("t")
    configure_task_backend(mcp, "APP", config)
    jobs = build_jobs(config, JobsConfig(soft_deadline_s=5.0, result_ttl_s=60.0))

    @register_long_running_tool(mcp, jobs)
    async def refuse() -> dict[str, Any]:
        raise ToolError("No note at 'a.md'.", log_level=logging.INFO)

    async with Client(mcp) as client:
        result = await client.call_tool_mcp("refuse", {})
    assert result.is_error is True
    assert _text(result) == "No note at 'a.md'."
