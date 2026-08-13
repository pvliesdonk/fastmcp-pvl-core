"""The ``Jobs`` capability object and its factory (ADR 0002 §5, path 2).

:func:`build_jobs` is the jobs analog of ``build_transfer_links``: it
constructs the mechanics — the KV-backed record store fronted by a
:class:`Jobs` object with a narrow verb surface — and registers **no
tools**. A downstream whose long-running tool the generic wrapper cannot
express (its own name, domain parameters, a domain-specific promotion
decision) composes on this seam instead of importing pvl-core internals.
Path 1 (``register_long_running_tool`` / ``register_job_tools``) is built
on the same object, so both paths share one store and one polling
contract and cannot drift.

The dual-mode decision lives in **one verb**,
:meth:`Jobs.run_with_deadline`, which is correct in every execution mode:
running as a native Docket task → just run (the protocol owns lifecycle);
foreground within the soft deadline → inline result; foreground past the
deadline → promote and return a :class:`~.records.JobHandle`. Path-2
authors never branch on execution mode themselves — mode introspection
stays inside the verb.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from .._config import ServerConfig
from .._kv_store import build_kv_store
from .config import JobsConfig
from .records import (
    JOB_POLL_TOOL_NAME,
    JOB_RETRY_AFTER_S,
    JobHandle,
    JobRecord,
)
from .store import JobStore

if TYPE_CHECKING:
    from collections.abc import Coroutine

logger = logging.getLogger(__name__)

_ANONYMOUS_SCOPE = "anonymous"
"""Scope for callers with no resolvable subject (shape, not config)."""


def _current_scope() -> str:
    """Resolve the calling subject's job scope.

    Delegates to :func:`fastmcp_pvl_core.get_subject`, which already
    unifies subject extraction across every auth mode (bearer, OIDC,
    stdio's ``"local"``). ``None`` — auth configured but no token, or no
    request context — collapses to one anonymous scope.
    """
    from .._subject import get_subject

    subject = get_subject()
    return subject if subject else _ANONYMOUS_SCOPE


def _as_result_mapping(value: Any) -> dict[str, Any]:
    """Normalise a tool return value into a JSON-object result payload.

    A mapping is stored as-is; anything else is wrapped as
    ``{"value": ...}`` so the stored record (and the polling tool's
    ``result`` field) is always a JSON object. The value must be
    JSON-serialisable — the same constraint the tool's inline return
    already carries.
    """
    if isinstance(value, dict):
        return value
    return {"value": value}


class Jobs:
    """Dual-mode execution and job-handle mechanics for one server.

    Construct via :func:`build_jobs`. All verbs resolve the caller's
    subject scope internally — a caller can only ever see its own jobs.

    This is the **path-2 seam**: a downstream tool the generic wrapper
    cannot express (always long-running and handle-first, its own
    promotion decision, polling embedded in a domain tool) composes on
    these verbs directly and stays on the shared shapes::

        from fastmcp_pvl_core.jobs import build_jobs

        jobs = build_jobs(config, jobs_config)

        @mcp.tool
        async def rebuild_index(scope: str) -> dict[str, Any]:
            \"\"\"Rebuild the index. Always long-running.\"\"\"
            async def work() -> dict[str, Any]:
                ...  # minutes of work
            return dict(await jobs.start(work(), tool="rebuild_index"))

    Path-2 rules: import from ``fastmcp_pvl_core.jobs`` (or the package
    root) only — ``fastmcp_pvl_core._jobs`` is internal; return the
    handle unmodified rather than restyling it (the payload shape is
    pvl-core's even when the tool is yours); still call
    ``register_job_tools`` once, because these handles resolve through
    the same generic polling tool; and catch the public error types
    (:class:`~.records.JobNotFoundError`,
    :class:`~.records.JobLimitExceededError`), not internals.

    A promoted or started job runs on the serving process and dies with
    it; its record then reports ``working`` until the TTL removes it —
    never a fabricated result. Durable cross-restart execution is the
    native task path's job (``redis://`` backend), not the fallback's.
    """

    def __init__(self, store: JobStore, config: JobsConfig) -> None:
        self._store = store
        self._config = config
        # Strong references to promoted tasks so the event loop cannot
        # garbage-collect them mid-run; discarded on completion.
        self._background: set[asyncio.Task[Any]] = set()

    async def run_with_deadline(
        self, coro: Coroutine[Any, Any, Any], *, tool: str
    ) -> Any:
        """Run *coro* dual-mode: native task, inline, or promote.

        Args:
            coro: The domain work (the hook — pvl-core cannot know what
                it does).
            tool: The registered tool name this call serves, echoed in
                the promotion log and the handle message so a client
                knows which call the handle came from.

        Returns:
            The coroutine's own result when it finishes natively or
            within the soft deadline; a :class:`~.records.JobHandle`
            payload when the work was promoted.

        Raises:
            Whatever *coro* raises, when it fails before the deadline —
            inline failures propagate exactly as they would without the
            wrapper. A failure after promotion is reported through the
            polling tool instead.
            JobLimitExceededError: If the caller is at its live-job cap
                at promotion time.
        """
        from fastmcp.server.dependencies import get_task_context

        if get_task_context() is not None:
            # Native SEP-1686 execution: Docket owns the lifecycle,
            # results, and TTL — nothing for the fallback to do.
            return await coro

        started_at = time.time()
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        done, _pending = await asyncio.wait(
            {task}, timeout=self._config.soft_deadline_s
        )
        if task in done:
            return task.result()  # re-raises an inline failure unchanged

        # Capture the subject scope NOW — the request context ends when
        # this call returns the handle, and the done-callback fires
        # outside any request.
        scope = _current_scope()
        record = await self._store.create(scope, started_at=started_at)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(
            lambda t: self._schedule_outcome(scope, record.job_id, t)
        )
        logger.info(
            "job_promoted tool=%s job_id=%s soft_deadline_s=%s",
            tool,
            record.job_id,
            self._config.soft_deadline_s,
        )
        return self._handle(record.job_id, tool=tool)

    async def start(self, coro: Coroutine[Any, Any, Any], *, tool: str) -> JobHandle:
        """Run *coro* as a background job unconditionally (no inline try).

        For downstream tools whose work is *always* long-running and
        should return a handle immediately.

        Returns:
            The :class:`~.records.JobHandle` payload for the new job.

        Raises:
            JobLimitExceededError: If the caller is at its live-job cap.
        """
        scope = _current_scope()
        record = await self._store.create(scope)
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(
            lambda t: self._schedule_outcome(scope, record.job_id, t)
        )
        logger.info("job_started tool=%s job_id=%s", tool, record.job_id)
        return self._handle(record.job_id, tool=tool)

    async def get(self, job_id: str) -> JobRecord:
        """Return the calling subject's record for *job_id*.

        Raises:
            JobNotFoundError: Unknown/expired id, or another subject's.
        """
        return await self._store.get(_current_scope(), job_id)

    async def poll(self, job_id: str) -> dict[str, Any]:
        """Return the polling payload for *job_id* (the shared shape).

        This is the body of the generic ``get_job_result`` tool, public
        so a path-2 downstream that embeds polling in its own tool keeps
        the exact same payload shape::

            {"job_id": ..., "status": "working", "result": None,
             "error": None, "running_for_s": 41.3, "retry_after_s": 5.0,
             "message": "Still running. ..."}

            {"job_id": ..., "status": "completed", "result": {...},
             "error": None}
            {"job_id": ..., "status": "failed", "result": None,
             "error": "<message>"}

        A tool that returned a non-mapping value completes with it
        wrapped as ``result={"value": ...}``.

        Raises:
            JobNotFoundError: Unknown/expired id, or another subject's.
        """
        record = await self.get(job_id)
        payload: dict[str, Any] = {
            "job_id": record.job_id,
            "status": record.status,
            "result": record.result,
            "error": record.error,
        }
        if record.status == "working":
            payload["running_for_s"] = round(time.time() - record.started_at, 1)
            payload["retry_after_s"] = JOB_RETRY_AFTER_S
            payload["message"] = (
                "Still running. Poll again with the same job_id in a few seconds."
            )
        return payload

    def _handle(self, job_id: str, *, tool: str) -> JobHandle:
        """Build the promotion payload (one shape for both paths)."""
        return JobHandle(
            status="working",
            job_id=job_id,
            poll_with=JOB_POLL_TOOL_NAME,
            retry_after_s=JOB_RETRY_AFTER_S,
            message=(
                f"{tool} is still running after "
                f"{self._config.soft_deadline_s:g}s and now continues in "
                f"the background. Call {JOB_POLL_TOOL_NAME} with "
                f"job_id={job_id!r} to retrieve the outcome (poll every "
                "few seconds)."
            ),
        )

    def _schedule_outcome(
        self, scope: str, job_id: str, task: asyncio.Task[Any]
    ) -> None:
        """Done-callback: record the finished task's outcome in the store.

        Runs synchronously on the loop; the actual (async) store write is
        scheduled as a task and strong-ref'd like the job itself.
        """
        writer = asyncio.ensure_future(self._record_outcome(scope, job_id, task))
        self._background.add(writer)
        writer.add_done_callback(self._background.discard)

    async def _record_outcome(
        self, scope: str, job_id: str, task: asyncio.Task[Any]
    ) -> None:
        if task.cancelled():
            await self._store.cancel(scope, job_id)
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("job_failed job_id=%s error=%s", job_id, exc, exc_info=exc)
            await self._store.fail(job_id=job_id, scope=scope, error=str(exc))
            return
        await self._store.finish(scope, job_id, _as_result_mapping(task.result()))


def build_jobs(config: ServerConfig, jobs_config: JobsConfig) -> Jobs:
    """Build the jobs mechanics — store plus :class:`Jobs` — no tools.

    The path-2 entry point (and the substrate path 1 registers on). Build
    **one** ``Jobs`` per server and share it between every long-running
    tool and ``register_job_tools``, so all handles resolve through the
    one polling contract. Both arguments are operator config; there are
    no hook or shape kwargs — every naming/shape decision inside is
    pvl-core-owned. Records land in the unified KV backend
    (``build_kv_store(config, namespace="jobs")``), so one
    ``<PREFIX>_KV_STORE_URL`` covers them along with every other pvl-core
    subsystem.

    Tests shrink the deadline instead of sleeping for real::

        jobs = build_jobs(
            ServerConfig(kv_store_url="memory://"),
            JobsConfig(soft_deadline_s=0.05, result_ttl_s=60.0),
        )

    Args:
        config: Universal server configuration; its ``kv_store_url``
            selects the backing store (namespace ``"jobs"``).
        jobs_config: The jobs env section (deadline, TTL, cap) —
            typically ``JobsConfig.from_env("<PREFIX>")``.

    Returns:
        A :class:`Jobs` object ready for ``run_with_deadline`` /
        ``start`` / ``get`` / ``poll``.
    """
    storage = build_kv_store(config, namespace="jobs")
    store = JobStore(
        storage,
        result_ttl_s=jobs_config.result_ttl_s,
        max_per_subject=jobs_config.max_per_subject,
    )
    return Jobs(store, jobs_config)
