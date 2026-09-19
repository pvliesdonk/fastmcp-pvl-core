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

This store is a **fallback**. It exists because most clients do not
negotiate the SEP-2663 tasks extension yet; where one does, the native
task already is the background mechanism and every verb here yields to
it rather than starting a second one (#324). When the fallback can go
is a per-deployment observation, never a pvl-core version: the client
decides task-versus-foreground before the tool body runs and nothing
promotes a foreground call later, so the store is idle only where every
client negotiates tasks. ``docs/jobs.md`` ("When the fallback can go")
records the criterion and the log lines that evidence it (#346). Mode
introspection stays inside the verbs — path-2 authors never branch on
execution mode themselves:

- :meth:`Jobs.run_with_deadline` — native task → just run; foreground
  within the soft deadline → inline result; foreground past it → promote
  and return a :class:`~.records.JobHandle`.
- :meth:`Jobs.start` — native task → run inline; otherwise background
  immediately with a handle.
- :meth:`Jobs.defer` — the same, and on the native path the deferral
  *reason* is delivered as the running task's status message, because a
  model that knows "queued, up to 20 minutes" stops polling blindly.

What a native task can and cannot tell its client — the status message
can carry the reason, the poll interval is fixed at submission and
cannot carry a per-call retry hint — is recorded with its evidence in
``docs/reference/fastmcp-native-task-signals.md``. Read that before
changing what these verbs report.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import TYPE_CHECKING, Any

from .._config import ServerConfig
from .._kv_store import build_kv_store
from .config import JobsConfig
from .records import (
    JOB_POLL_TOOL_NAME,
    JOB_RETRY_AFTER_S,
    DeferredJobHandle,
    JobHandle,
    JobLimitExceededError,
    JobRecord,
)
from .store import JobStore

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator

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


def _native_task_active() -> bool:
    """Whether this call is running inside a native SEP-2663 task.

    The job store is a *fallback*: it exists because most clients do not
    negotiate the tasks extension yet. When a native task is running, it
    already is the background mechanism, and every verb here gets out of
    its way rather than starting a second one (#324).

    ``fastmcp_tasks`` ships with pvl-core's base dependencies; the guard
    survives for stripped forks, where the native path cannot be active
    and the fallback must still work. Deliberately ``ModuleNotFoundError``
    and not ``ImportError``: a *present* ``fastmcp_tasks`` missing the
    symbol is version skew that must stay loud, not be silently routed to
    the fallback.
    """
    try:
        from fastmcp_tasks.context import get_task_context
    except ModuleNotFoundError:
        return False
    return get_task_context() is not None


@contextlib.contextmanager
def _closing_on_failure(coro: Coroutine[Any, Any, Any]) -> Iterator[None]:
    """Close *coro* if the guarded block does not hand it off.

    The caller of :meth:`Jobs.start` / :meth:`Jobs.defer` constructs the
    coroutine and hands it over, so every path that declines to run it owes
    it a ``close()``. Otherwise the work is dropped with nothing to show for
    it but a "coroutine was never awaited" warning at collection time — a
    silent loss, and the same commitment the ``retry_after_s`` rejection in
    :meth:`Jobs.defer` has always made.

    Catches ``BaseException`` and re-raises: cancellation is the case that
    matters most (the announce below suspends on a Redis round trip, so a
    ``tasks/cancel`` can land inside it), and cancellation is not an
    ``Exception``. Nothing is swallowed — the exception always propagates.
    """
    try:
        yield
    except BaseException:
        coro.close()
        raise


def _resolve_mode(coro: Coroutine[Any, Any, Any]) -> bool:
    """Whether a native task is active, closing *coro* if the check itself fails.

    :func:`_native_task_active` deliberately lets a non-``ModuleNotFoundError``
    ``ImportError`` propagate — version skew must stay loud. At that point the
    caller's coroutine has been constructed but not yet awaited, so it is
    closed rather than left to surface as "coroutine was never awaited" noise
    stacked on top of the real error. Same commitment the ``retry_after_s``
    rejection in :meth:`Jobs.defer` already makes.
    """
    with _closing_on_failure(coro):
        return _native_task_active()


async def _announce_wait(message: str) -> None:
    """Tell a polling client why its native task is still working.

    This is the part of a deferral that survives on the native path. A
    handle cannot be returned — the task is the handle — but the *reason*
    is what the model acts on: "queued, up to 20 minutes" is the
    difference between waiting and polling blindly.

    ``tasks/get`` reports a running task's ``statusMessage`` from the live
    Docket execution's progress message and from nothing else, so that is
    the channel. Reaching it through ``current_execution`` couples this to
    Docket's execution object — the same coupling FastMCP's own
    ``Context.report_progress`` has in its background branch, but through
    an internal rather than a public wrapper. ``Context`` itself is not
    usable here: it exists only when the domain tool declared a ``ctx``
    parameter, and pvl-core will not make that a condition of being told
    why you are waiting. See
    ``docs/reference/fastmcp-native-task-signals.md``.

    Never raises: losing the message costs the client an expectation,
    while raising would cost it the work.

    Note that this message and the task's own ``pollIntervalMs`` can
    disagree — the latter is fixed at submission from static tool config,
    so a per-call retry hint reaches the client only as prose here.
    """
    try:
        from docket.dependencies import current_execution

        await current_execution.get().progress.set_message(message)
    except Exception:  # noqa: BLE001 — a lost expectation must not lose the work
        # Broad deliberately: this must not raise, and the failure modes are
        # open-ended (Docket's execution API, its Redis client, a stripped
        # install). Narrowing risks the one outcome the guard exists to
        # prevent — losing the work to a diagnostic. WARNING because the
        # client is now polling without the expectation it should have had.
        logger.warning(
            "deferral_announce_failed consequence=%s",
            "client polls without an expectation",
            exc_info=True,
        )


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
            return await jobs.start(work(), tool="rebuild_index")

    Path-2 rules: import from ``fastmcp_pvl_core.jobs`` (or the package
    root) only — ``fastmcp_pvl_core._jobs`` is internal; return what the
    verb gives you unmodified rather than restyling it (the payload shape
    is pvl-core's even when the tool is yours — and under a native task
    what comes back is the work's own result, not a handle); still call
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
                at promotion time. The already-running work is cancelled
                first (unless it finished while the rejection was being
                decided, in which case its result is returned inline and
                no error is raised).
        """
        if _native_task_active():
            # Native SEP-2663 execution: Docket owns the lifecycle,
            # results, and TTL — nothing for the fallback to do.
            return await coro

        started_at = time.time()
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        done, _pending = await asyncio.wait(
            {task}, timeout=self._config.soft_deadline_s
        )
        if task in done:
            # The one fallback outcome with no INFO-level trace: a client
            # that did not negotiate tasks, served within the deadline. The
            # retirement criterion in ``docs/jobs.md`` needs to see exactly
            # those clients, so the outcome is logged — at DEBUG, being
            # per-call detail; success and inline failure alike.
            logger.debug(
                "job_ran_inline tool=%s elapsed_s=%s",
                tool,
                round(time.time() - started_at, 3),
            )
            return task.result()  # re-raises an inline failure unchanged

        # Capture the subject scope NOW — the request context ends when
        # this call returns the handle, and the done-callback fires
        # outside any request.
        scope = _current_scope()
        try:
            record = await self._store.create(scope, started_at=started_at)
        except JobLimitExceededError:
            # The task was started before the record could exist (it had
            # to, to allow inline completion), so a cap rejection must not
            # orphan it: if it finished while the rejection was being
            # decided, its outcome is simply the inline answer; otherwise
            # cancel it — a rejected promotion must actually stop the
            # work, and an untracked task must not outlive this frame.
            if task.done() and not task.cancelled():
                return task.result()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
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

    async def start(self, coro: Coroutine[Any, Any, Any], *, tool: str) -> Any:
        """Run *coro* in the background, or inline under a native task.

        For downstream tools whose work is *always* long-running and
        should return a handle immediately.

        Inside a native SEP-2663 task there is nothing to start: the task
        already is the background mechanism, and returning a job handle
        would hand a client that is following one lifecycle a second one
        to follow (#324). There *coro* is awaited and its own result
        returned, the same way :meth:`run_with_deadline` yields to the
        native path.

        Returns:
            The :class:`~.records.JobHandle` payload for the new job, or
            the coroutine's own result when a native task is running.
            Polymorphic for the same reason :meth:`run_with_deadline` is:
            the caller returns this straight to the client, and what the
            client should receive differs by mode.

        Raises:
            JobLimitExceededError: If the caller is at its live-job cap.
                Cannot arise on the native path, which creates no record.
        """
        if _resolve_mode(coro):
            return await coro
        return await self._start_fallback(coro, tool=tool)

    async def _start_fallback(
        self, coro: Coroutine[Any, Any, Any], *, tool: str
    ) -> JobHandle:
        """Background *coro* in pvl-core's own store and return its handle.

        The fallback half of :meth:`start`, split out so its concrete
        :class:`~.records.JobHandle` type survives the polymorphic public
        signature, and so :meth:`defer` can reach it without re-running the
        native-task check its own branch already answered.
        """
        scope = _current_scope()
        # The cap (or a store failure) rejects before ``ensure_future``
        # takes ownership, so nothing else would ever await *coro*.
        with _closing_on_failure(coro):
            record = await self._store.create(scope)
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(
            lambda t: self._schedule_outcome(scope, record.job_id, t)
        )
        logger.info("job_started tool=%s job_id=%s", tool, record.job_id)
        return self._handle(record.job_id, tool=tool)

    async def defer(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        tool: str,
        reason: str,
        retry_after_s: float = JOB_RETRY_AFTER_S,
    ) -> Any:
        """Start background work deferred by a domain-specific condition.

        Use when a domain tool knows why foreground work cannot proceed yet,
        such as an upstream rate limit. This is additive to :meth:`start`
        on the fallback path: the handle is ``start``'s shape plus
        ``reason``, and ``start``'s own handle is unchanged. Neither verb
        returns a handle under a native task, where the task itself carries
        the deferral (#324).

        Args:
            coro: The domain work to continue in the background.
            tool: The registered domain tool name, a domain hook used in the
                generic handle message.
            reason: Client-visible explanation of the deferral, a required
                domain hook because pvl-core cannot know the runtime cause.
            retry_after_s: Suggested delay before the first poll, a domain
                hook when an upstream response supplies a better interval;
                otherwise pvl-core's standard polling interval applies.

        Returns:
            The shared job handle plus the supplied ``reason``, or the
            coroutine's own result when a native task is running — there
            the task carries the deferral and *reason* is delivered as its
            status message instead. Polymorphic for the same reason
            :meth:`run_with_deadline` is.

        Raises:
            JobLimitExceededError: If the caller is at its live-job cap.
                Cannot arise on the native path, which creates no record.
            ValueError: If ``retry_after_s`` is not a finite positive number.
        """
        if not math.isfinite(retry_after_s) or retry_after_s <= 0:
            # The coroutine was constructed by the caller before it reached
            # this validation branch; close it so rejecting the handle does
            # not leave an unawaited coroutine behind.
            coro.close()
            raise ValueError("retry_after_s must be a finite positive number")

        if _resolve_mode(coro):
            # The task is the deferral, so there is no handle to hand back
            # — but the reason still reaches the client, as the running
            # task's status message. ``retry_after_s`` goes into that text
            # rather than the protocol: SEP-2663 fixes ``pollIntervalMs``
            # at submission from static tool config, so a per-call
            # interval has no native channel (see the reference page).
            logger.info(
                "job_deferred_natively tool=%s retry_after_s=%s",
                tool,
                retry_after_s,
            )
            # ``_announce_wait`` swallows ``Exception`` itself; the guard
            # here is for what it deliberately does not catch — cancellation
            # landing mid-round-trip.
            with _closing_on_failure(coro):
                await _announce_wait(f"{reason} (retry in {retry_after_s:g}s)")
            return await coro

        handle = await self._start_fallback(coro, tool=tool)
        return DeferredJobHandle(
            status=handle["status"],
            job_id=handle["job_id"],
            poll_with=handle["poll_with"],
            retry_after_s=retry_after_s,
            message=(
                f"{tool} was deferred and continues in the background. Call "
                f"{JOB_POLL_TOOL_NAME} with job_id={handle['job_id']!r} to "
                f"retrieve the outcome (poll after {retry_after_s:g}s)."
            ),
            reason=reason,
        )

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
        A :class:`Jobs` object ready for ``run_with_deadline`` / ``start`` /
        ``defer`` / ``get`` / ``poll``.
    """
    storage = build_kv_store(config, namespace="jobs")
    store = JobStore(
        storage,
        result_ttl_s=jobs_config.result_ttl_s,
        max_per_subject=jobs_config.max_per_subject,
    )
    return Jobs(store, jobs_config)
