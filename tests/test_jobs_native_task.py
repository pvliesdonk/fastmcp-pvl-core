"""The jobs verbs under a client that negotiates SEP-2663 tasks.

Everything else in the suite exercises the fallback: pvl-core's own job
store, reached because the client did not negotiate the tasks extension.
This file drives a real task-negotiating client instead, which is the
only way to observe what #324 is about — the fallback starting a *second*
background mechanism inside a native task that is already one.

The contract these tests pin: the job store is a fallback. When a native
task is running, it owns the lifecycle and the verbs get out of its way
(the same branch ``run_with_deadline`` has had since the native path
landed). What the verbs still do on that path is tell the client *why*
it is waiting, because a model that knows "queued, up to 20 minutes"
stops polling blindly.

``docs/reference/fastmcp-native-task-signals.md`` records which task
fields can carry that and which cannot.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP
from fastmcp_tasks.client import call_tool_task

from fastmcp_pvl_core import (
    JOB_POLL_TOOL_NAME,
    JobLimitExceededError,
    JobsConfig,
    ServerConfig,
    build_jobs,
    configure_task_backend,
    register_job_tools,
    register_long_running_tool,
)

_CONFIG = ServerConfig(kv_store_url="memory://", tasks_url="memory://")
_JOBS_CONFIG = JobsConfig(soft_deadline_s=5.0, result_ttl_s=60.0)
_REASON = "queued behind upstream; may take up to 20 minutes"


@pytest.fixture
def release() -> asyncio.Event:
    """Gate held by the deferred work, so polls happen while it runs."""
    return asyncio.Event()


def _server(release: asyncio.Event, body):
    """A task-serving server whose one tool defers work behind *release*.

    *body* receives the `Jobs` object and the gated coroutine and returns
    whatever the tool should return, so each test varies only the verb
    under test.
    """
    mcp = FastMCP("probe")
    configure_task_backend(mcp, "PROBE", _CONFIG)
    jobs = build_jobs(_CONFIG, _JOBS_CONFIG)

    async def work() -> dict:
        await release.wait()
        return {"answer": 42}

    @register_long_running_tool(mcp, jobs, name="probe_tool")
    async def probe_tool() -> dict:
        return await body(jobs, work())

    register_job_tools(mcp, jobs)
    return mcp


async def _poll_until(task, predicate, *, limit: int = 60):
    """Poll until *predicate* holds on a status, or fail the test."""
    status = None
    for _ in range(limit):
        status = await task.status()
        if predicate(status):
            return status
        await asyncio.sleep(0.05)
    pytest.fail(f"predicate never held; last status={status!r}")


async def _run(mcp, release: asyncio.Event, *, observe=None):
    """Submit the tool as a task, optionally observe it mid-flight, finish it.

    *observe* is called with the first status whose message is set, before
    the work is released — the window in which a deferral has something to
    say and the fallback would instead have returned a handle.
    """
    async with Client(mcp) as client:
        task = await call_tool_task(client, "probe_tool", {})
        if observe is not None:
            observe(await _poll_until(task, lambda s: s.status_message is not None))
        else:
            await _poll_until(task, lambda s: s.status == "working")
        release.set()
        await _poll_until(task, lambda s: s.status == "completed")
        return await task.result()


@pytest.mark.asyncio
class TestDeferInsideANativeTask:
    async def test_defer_returns_the_work_result_not_a_job_handle(self, release):
        """The task is the deferral; a second handle would be a second lifecycle."""

        async def deferring(jobs, coro):
            return await jobs.defer(coro, tool="probe_tool", reason=_REASON)

        result = await _run(_server(release, deferring), release)

        assert result.data == {"answer": 42}
        assert "job_id" not in result.data

    async def test_defer_tells_the_client_why_it_is_waiting(self, release):
        """The reason is the payload.

        A model that knows the wait stops polling blindly.
        """

        async def deferring(jobs, coro):
            return await jobs.defer(coro, tool="probe_tool", reason=_REASON)

        seen = []
        await _run(_server(release, deferring), release, observe=seen.append)

        assert seen[0].status == "working"
        assert _REASON in seen[0].status_message

    async def test_retry_after_is_folded_into_the_reason(self, release):
        """SEP-2663 fixes pollIntervalMs at submission, so the text carries it."""

        async def deferring(jobs, coro):
            return await jobs.defer(
                coro, tool="probe_tool", reason=_REASON, retry_after_s=120.0
            )

        seen = []
        await _run(_server(release, deferring), release, observe=seen.append)

        assert "120" in seen[0].status_message


@pytest.mark.asyncio
class TestStartInsideANativeTask:
    async def test_start_returns_the_work_result(self, release):
        """`start` has no reason to announce; it just gets out of the way."""

        async def starting(jobs, coro):
            return await jobs.start(coro, tool="probe_tool")

        result = await _run(_server(release, starting), release)

        assert result.data == {"answer": 42}
        assert "job_id" not in result.data


@pytest.mark.asyncio
class TestFallbackPathUnchanged:
    async def test_defer_still_returns_a_handle_without_a_task(self):
        """The common case today: no native task, so the job store answers."""
        jobs = build_jobs(_CONFIG, _JOBS_CONFIG)

        async def work() -> dict:
            return {"answer": 42}

        payload = dict(
            await jobs.defer(
                work(), tool="probe_tool", reason=_REASON, retry_after_s=2.0
            )
        )

        assert payload["status"] == "working"
        assert payload["reason"] == _REASON
        assert payload["retry_after_s"] == 2.0
        assert "job_id" in payload

    async def test_a_client_that_does_not_negotiate_tasks_runs_in_the_foreground(
        self, release, monkeypatch
    ):
        """The routing the fallback exists for (docs/reference/mcp-task-routing-
        is-requestor-driven.md): on a task-enabled server, a ``tools/call`` from
        a client that never advertised the tasks extension runs the tool body
        in the foreground and never becomes a task, so the job store answers.

        FastMCP's own ``Client`` advertises the extension on every session and
        resolves the task transparently, so it cannot play that client as-is:
        the internal extension fold-in is suppressed here, which is what a
        client built on another SDK, or a legacy-protocol connection, looks
        like to the server."""
        monkeypatch.setattr(
            "fastmcp.client.client.build_internal_client_extensions",
            lambda _callback: [],
        )
        mcp = _server(
            release,
            lambda jobs, coro: jobs.defer(coro, tool="probe_tool", reason=_REASON),
        )
        async with Client(mcp) as client:
            handle = (await client.call_tool("probe_tool", {})).structured_content
            assert handle["status"] == "working"
            assert handle["reason"] == _REASON
            release.set()
            polled = None
            for _ in range(60):
                polled = (
                    await client.call_tool(
                        JOB_POLL_TOOL_NAME, {"job_id": handle["job_id"]}
                    )
                ).structured_content
                if polled["status"] == "completed":
                    break
                await asyncio.sleep(0.05)
            assert polled is not None and polled["result"] == {"answer": 42}


@pytest.mark.asyncio
class TestAnnounceNeverRaises:
    """A lost expectation must never cost the work.

    `_announce_wait` reaches Docket's execution object, which can fail for
    open-ended reasons (its Redis client, an API change, a stripped
    install). Exercised directly rather than through a task: the failure
    has to be injected *below* the guard, and injecting it at the Docket
    lookup would also break the mode detection that runs earlier, testing
    something else entirely.
    """

    async def test_outside_a_task_it_is_a_no_op(self):
        """No execution to talk to; the common foreground case."""
        from fastmcp_pvl_core._jobs.manager import _announce_wait

        await _announce_wait("nobody is listening")

    async def test_a_failure_is_swallowed_and_reported(self, monkeypatch, caplog):
        import logging

        import docket.dependencies

        from fastmcp_pvl_core._jobs.manager import _announce_wait

        class _Exploding:
            def get(self):
                raise RuntimeError("docket went away")

        monkeypatch.setattr(docket.dependencies, "current_execution", _Exploding())

        with caplog.at_level(logging.WARNING):
            await _announce_wait("queued behind upstream")

        failures = [
            record
            for record in caplog.records
            if "deferral_announce_failed" in record.getMessage()
        ]
        assert failures, "the lost expectation must still be reported"
        assert failures[0].exc_info is not None, "exc_info is required here"


@pytest.mark.asyncio
class TestTheWorkIsNeverOrphaned:
    """A rejected or cancelled deferral must not strand the caller's coroutine.

    `defer` builds no coroutine of its own — the caller constructs it and
    hands it over. Every path that declines to run it therefore has to
    close it, or the work is dropped with nothing but a
    "coroutine was never awaited" warning to show for it. The
    `retry_after_s` rejection has always done this; the paths added for
    #324 have to as well, and the announce is a real suspension point (a
    Redis round-trip on a live backend) so cancellation can land there.
    """

    async def test_cancellation_during_the_announce_closes_the_coroutine(
        self, monkeypatch, recwarn
    ):
        from fastmcp_pvl_core._jobs import manager as manager_module

        async def hang(_message: str) -> None:
            await asyncio.sleep(3600)

        monkeypatch.setattr(manager_module, "_announce_wait", hang)
        monkeypatch.setattr(manager_module, "_native_task_active", lambda: True)

        jobs = build_jobs(_CONFIG, _JOBS_CONFIG)
        started = False

        async def work() -> dict:
            nonlocal started
            started = True
            return {"answer": 42}

        coro = work()
        task = asyncio.ensure_future(
            jobs.defer(coro, tool="probe_tool", reason=_REASON)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not started, "the work must not have begun"
        # Closed, not orphaned: awaiting a closed coroutine raises rather
        # than producing a "never awaited" warning at collection time.
        with pytest.raises(RuntimeError):
            await coro

    async def test_cap_rejection_closes_the_coroutine(self):
        jobs = build_jobs(_CONFIG, JobsConfig(soft_deadline_s=5.0, max_per_subject=1))

        async def blocker() -> dict:
            await asyncio.sleep(3600)
            return {}

        async def rejected() -> dict:
            return {"answer": 42}

        await jobs.start(blocker(), tool="probe_tool")
        coro = rejected()
        with pytest.raises(JobLimitExceededError):
            await jobs.start(coro, tool="probe_tool")

        with pytest.raises(RuntimeError):
            await coro
