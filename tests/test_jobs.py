"""Tests for the ``Jobs`` manager and the registration entry points.

Covers the dual-mode verb (inline / promote / native), path-1
registration (``register_long_running_tool`` + ``register_job_tools``),
path-2 parity (a hand-built tool on ``build_jobs`` shares the store and
polling contract), subject scoping, and the handle/poll payload shapes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from fastmcp_pvl_core import (
    JOB_POLL_TOOL_NAME,
    JobsConfig,
    ServerConfig,
    build_jobs,
    register_job_tools,
    register_long_running_tool,
)
from fastmcp_pvl_core._jobs.manager import Jobs

_FAST_CONFIG = JobsConfig(soft_deadline_s=0.15, result_ttl_s=60.0)


def _jobs(jobs_config: JobsConfig = _FAST_CONFIG) -> Jobs:
    return build_jobs(ServerConfig(kv_store_url="memory://"), jobs_config)


async def _drain(jobs: Jobs) -> None:
    """Wait for every promoted task (and outcome writer) to finish."""
    while jobs._background:
        await asyncio.wait(set(jobs._background))


class TestRunWithDeadline:
    async def test_fast_work_returns_inline(self):
        jobs = _jobs()

        async def work() -> dict[str, Any]:
            return {"ok": True}

        assert await jobs.run_with_deadline(work(), tool="t") == {"ok": True}

    async def test_fast_failure_raises_inline(self):
        jobs = _jobs()

        async def work() -> dict[str, Any]:
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            await jobs.run_with_deadline(work(), tool="t")

    async def test_slow_work_promotes_to_handle(self):
        jobs = _jobs()

        async def work() -> dict[str, Any]:
            await asyncio.sleep(0.4)
            return {"late": True}

        handle = await jobs.run_with_deadline(work(), tool="my_tool")
        assert handle["status"] == "working"
        assert handle["poll_with"] == JOB_POLL_TOOL_NAME
        assert "my_tool" in handle["message"]
        # The work keeps running and its outcome lands in the store.
        await _drain(jobs)
        payload = await jobs.poll(handle["job_id"])
        assert payload["status"] == "completed"
        assert payload["result"] == {"late": True}

    async def test_failure_after_promotion_reported_via_poll(self):
        jobs = _jobs()

        async def work() -> dict[str, Any]:
            await asyncio.sleep(0.3)
            raise RuntimeError("backend exploded")

        handle = await jobs.run_with_deadline(work(), tool="t")
        await _drain(jobs)
        payload = await jobs.poll(handle["job_id"])
        assert payload["status"] == "failed"
        assert "backend exploded" in payload["error"]
        assert payload["result"] is None

    async def test_cancelled_promoted_task_reported_cancelled(self):
        jobs = _jobs()

        async def work() -> dict[str, Any]:
            await asyncio.sleep(30.0)
            return {}

        handle = await jobs.run_with_deadline(work(), tool="t")
        for task in set(jobs._background):
            task.cancel()
        await _drain(jobs)
        payload = await jobs.poll(handle["job_id"])
        assert payload["status"] == "cancelled"

    async def test_non_mapping_result_wrapped_as_value(self):
        jobs = _jobs()

        async def work() -> str:
            await asyncio.sleep(0.3)
            return "just text"

        handle = await jobs.run_with_deadline(work(), tool="t")
        await _drain(jobs)
        payload = await jobs.poll(handle["job_id"])
        assert payload["result"] == {"value": "just text"}

    async def test_working_poll_carries_progress_hints(self):
        jobs = _jobs()

        async def work() -> dict[str, Any]:
            await asyncio.sleep(0.5)
            return {}

        handle = await jobs.run_with_deadline(work(), tool="t")
        payload = await jobs.poll(handle["job_id"])
        assert payload["status"] == "working"
        assert payload["running_for_s"] >= 0.0
        assert payload["retry_after_s"] > 0
        await _drain(jobs)

    async def test_native_task_context_runs_direct(self, monkeypatch):
        # Inside a Docket worker the verb must not double-manage the work:
        # the coroutine runs to completion with no store record.
        class _Ctx:
            task_id = "t1"
            task_scope = None

        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_task_context", lambda: _Ctx()
        )
        jobs = _jobs(JobsConfig(soft_deadline_s=0.01, result_ttl_s=60.0))

        async def work() -> dict[str, Any]:
            await asyncio.sleep(0.1)  # would promote in foreground mode
            return {"native": True}

        assert await jobs.run_with_deadline(work(), tool="t") == {"native": True}
        assert not jobs._background


class TestStart:
    async def test_start_returns_handle_immediately(self):
        jobs = _jobs()

        async def work() -> dict[str, Any]:
            return {"done": True}

        handle = await jobs.start(work(), tool="t")
        assert handle["status"] == "working"
        await _drain(jobs)
        payload = await jobs.poll(handle["job_id"])
        assert payload["status"] == "completed"


class TestSubjectScoping:
    async def test_scope_captured_at_promotion_isolated_on_poll(self, monkeypatch):
        jobs = _jobs()
        current = {"subject": "alice"}
        monkeypatch.setattr(
            "fastmcp_pvl_core._subject.get_subject", lambda: current["subject"]
        )

        async def work() -> dict[str, Any]:
            await asyncio.sleep(0.3)
            return {}

        handle = await jobs.run_with_deadline(work(), tool="t")
        await _drain(jobs)
        # Same subject sees the job; another subject sees nothing.
        assert (await jobs.poll(handle["job_id"]))["status"] == "completed"
        current["subject"] = "bob"
        from fastmcp_pvl_core import JobNotFoundError

        with pytest.raises(JobNotFoundError):
            await jobs.poll(handle["job_id"])


class TestRegistration:
    async def test_wrapped_tool_inline_and_promoted(self):
        mcp = FastMCP("t")
        jobs = _jobs()

        @register_long_running_tool(mcp, jobs)
        async def echo(text: str, delay: float = 0.0) -> dict[str, Any]:
            await asyncio.sleep(delay)
            return {"echo": text}

        register_job_tools(mcp, jobs)

        inline = await mcp.call_tool("echo", {"text": "hi"})
        assert inline.structured_content == {"echo": "hi"}

        promoted = await mcp.call_tool("echo", {"text": "later", "delay": 0.4})
        handle = promoted.structured_content
        assert handle["status"] == "working"
        await _drain(jobs)
        polled = await mcp.call_tool(JOB_POLL_TOOL_NAME, {"job_id": handle["job_id"]})
        assert polled.structured_content["status"] == "completed"
        assert polled.structured_content["result"] == {"echo": "later"}

    async def test_wrapped_tool_registered_task_optional(self):
        mcp = FastMCP("t")
        jobs = _jobs()

        @register_long_running_tool(mcp, jobs)
        async def echo(text: str) -> dict[str, Any]:
            return {"echo": text}

        tool = await mcp.get_tool("echo")
        assert tool.task_config.mode == "optional"

    async def test_task_kwarg_rejected(self):
        mcp = FastMCP("t")
        jobs = _jobs()
        with pytest.raises(ValueError, match="task"):
            register_long_running_tool(mcp, jobs, task=True)

    async def test_unknown_job_id_is_tool_error(self):
        mcp = FastMCP("t")
        jobs = _jobs()
        register_job_tools(mcp, jobs)
        with pytest.raises(ToolError, match="unknown"):
            await mcp.call_tool(JOB_POLL_TOOL_NAME, {"job_id": "nope"})

    async def test_note_appended_to_description(self):
        mcp = FastMCP("t")
        jobs = _jobs()
        register_job_tools(mcp, jobs, note="Summaries expire nightly.")
        tool = await mcp.get_tool(JOB_POLL_TOOL_NAME)
        assert tool.description is not None
        assert tool.description.endswith("Summaries expire nightly.")
        assert "background job" in tool.description


class TestPathTwoParity:
    async def test_hand_built_tool_shares_store_and_poller(self):
        # Path 2: a downstream tool composed directly on build_jobs must
        # mint handles the generic polling tool resolves.
        mcp = FastMCP("t")
        jobs = _jobs()
        register_job_tools(mcp, jobs)

        @mcp.tool
        async def domain_batch(n: int) -> dict[str, Any]:
            async def work() -> dict[str, Any]:
                await asyncio.sleep(0.3)
                return {"processed": n}

            return dict(await jobs.start(work(), tool="domain_batch"))

        started = await mcp.call_tool("domain_batch", {"n": 3})
        handle = started.structured_content
        assert handle["poll_with"] == JOB_POLL_TOOL_NAME
        await _drain(jobs)
        polled = await mcp.call_tool(JOB_POLL_TOOL_NAME, {"job_id": handle["job_id"]})
        assert polled.structured_content["result"] == {"processed": 3}
