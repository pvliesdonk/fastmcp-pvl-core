"""Wire dual-mode long-running tools onto a FastMCP server (ADR 0002 §5).

Two entry points, both built on the :class:`~.manager.Jobs` seam:

- :func:`register_long_running_tool` — **path 1**: register a domain
  coroutine once and get both behaviours — native SEP-1686 task execution
  when the request is task-augmented (the tool is registered with
  ``task=TaskConfig(mode="optional")``), and foreground execution with
  soft-deadline promotion otherwise.
- :func:`register_job_tools` — the **one** generic polling tool,
  ``get_job_result``, whose name, result schema, and metadata are
  pvl-core-owned shape. Registered once per server; it retrieves every
  job on the shared :class:`~.manager.Jobs`, whichever path minted it.

The *wrapped tool's* identity (name, description, parameters,
annotations) is the downstream's — it is their domain tool; keyword
arguments here pass through to ``FastMCP.tool`` untouched, except
``task``, which is pvl-core shape and not overridable. A downstream whose
tool cannot be expressed as "wrap one coroutine" (its own promotion
decision, polling embedded in a domain tool) composes on
:func:`~.manager.build_jobs` directly — path 2 — and returns the same
public handle/poll shapes.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

import base64
import functools
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from mcp.types import Icon, ToolAnnotations

from .manager import Jobs
from .records import JOB_POLL_TOOL_NAME, JobNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp import FastMCP

# Lucide "loader" icon (MIT) — raw SVG, base64-encoded once at import time
# into a data URI so the polling tool carries a universal icon with no
# file-system or network dependency (foldable and offline-capable).
_POLL_SVG = (
    '<svg width="24" height="24" xmlns="http://www.w3.org/2000/svg"'
    ' viewBox="0 0 24 24">'
    '<g fill="none" stroke="currentColor" stroke-linecap="round"'
    ' stroke-linejoin="round" stroke-width="2">'
    '<path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83'
    'M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48l2.83-2.83"/>'
    "</g></svg>"
)
_POLL_ICON = Icon(
    src="data:image/svg+xml;base64," + base64.b64encode(_POLL_SVG.encode()).decode(),
    mimeType="image/svg+xml",
)


def register_long_running_tool(
    mcp: FastMCP,
    jobs: Jobs,
    **tool_kwargs: Any,
) -> Callable[[Callable[..., Any]], Any]:
    """Register a long-running domain coroutine as a dual-mode tool.

    Use as a decorator factory, exactly like ``FastMCP.tool``::

        from fastmcp_pvl_core import (
            JobsConfig, build_jobs, register_job_tools,
            register_long_running_tool,
        )

        jobs_config = JobsConfig.from_env("MY_APP")   # MY_APP_JOBS_* knobs
        jobs = build_jobs(config, jobs_config)

        @register_long_running_tool(mcp, jobs, tags={"summarize"})
        async def summarize(paths: list[str]) -> dict[str, Any]:
            ...   # domain work; may take minutes

        register_job_tools(mcp, jobs)   # once per server — see below

    The decorated coroutine is the **domain hook**: a plain ``async``
    function (background execution needs a coroutine) returning a
    JSON-serialisable value, written once and never branching on how it
    is executed. The wrapped body delegates to
    :meth:`Jobs.run_with_deadline`, so a call behaves as:

    - **task-augmented request** (a client that speaks SEP-1686 tasks) →
      native background-task execution; Docket owns lifecycle and
      results.
    - **plain request, finishes within the soft deadline** → the
      coroutine's own result, inline, exactly as if unwrapped — and an
      exception raised within the deadline propagates to the caller
      unchanged.
    - **plain request, deadline expires** → the work continues in the
      background and the caller immediately receives a
      :class:`~.records.JobHandle` payload
      (``{"status": "working", "job_id": ..., "poll_with":
      "get_job_result", "retry_after_s": 5.0, "message": ...}``),
      retrievable via the generic polling tool until the record's TTL. A
      failure *after* promotion is reported through polling, not raised.

    Because the caller receives either your result *or* a handle — both
    JSON objects — annotate the coroutine's return type as
    ``dict[str, Any]``.

    Registration keyword arguments (name, description, annotations,
    icons, tags…) pass through to ``FastMCP.tool`` — the tool's identity
    is the downstream's. ``task`` is the one exception: the tool is
    always registered with ``TaskConfig(mode="optional")`` (pvl-core
    shape — that is what makes it dual-mode), so a ``task`` kwarg raises.

    Pair with :func:`register_job_tools` (once per server, same *jobs*)
    so promoted handles are actually retrievable; a server whose tool
    this wrapper cannot express composes on :func:`~.manager.build_jobs`
    directly instead — see :class:`Jobs`.

    Args:
        mcp: The server to register on.
        jobs: The shared :class:`Jobs` mechanics (from
            :func:`~.manager.build_jobs`).
        **tool_kwargs: Passed through to ``FastMCP.tool`` — downstream's
            tool identity (a domain hook, not a shape override).

    Returns:
        The decorator to apply to the domain coroutine.

    Raises:
        ValueError: If ``task`` is passed — dual-mode registration is
            pvl-core shape, not a per-tool choice.
    """
    if "task" in tool_kwargs:
        raise ValueError(
            "register_long_running_tool owns the task registration mode "
            "(TaskConfig(mode='optional')); do not pass 'task'. A tool "
            "that must never run as a task is a plain @mcp.tool."
        )
    from fastmcp.utilities.tasks import TaskConfig

    def decorator(fn: Callable[..., Any]) -> Any:
        tool_name = tool_kwargs.get("name") or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await jobs.run_with_deadline(fn(*args, **kwargs), tool=tool_name)

        return mcp.tool(task=TaskConfig(mode="optional"), **tool_kwargs)(wrapper)

    return decorator


def register_job_tools(
    mcp: FastMCP,
    jobs: Jobs,
    *,
    note: str | None = None,
) -> None:
    """Register the generic ``get_job_result`` polling tool.

    Call once per server, with the same :class:`Jobs` the long-running
    tools run on — every job on the server resolves through this one
    tool, whether it was minted by ``register_long_running_tool`` or by a
    downstream tool composed on :func:`~.manager.build_jobs`. Do not
    write per-tool pollers; one polling contract per server is the point.

    The tool returns :meth:`Jobs.poll`'s payload::

        {"job_id": ..., "status": "working", "result": None,
         "error": None, "running_for_s": 41.3, "retry_after_s": 5.0,
         "message": "Still running. ..."}

        {"job_id": ..., "status": "completed", "result": {...}, "error": None}
        {"job_id": ..., "status": "failed", "result": None, "error": "..."}

    Lookups are scoped to the calling subject: another subject's job id
    answers exactly like an unknown one (a ``ToolError``), so ids are not
    probeable across tenants. Records expire ``result_ttl_s`` after
    creation, after which the id is simply unknown — clients should fetch
    results promptly.

    The tool's name, payload, and metadata are pvl-core-owned shape;
    *note* is the one domain hook — a sentence appended to (never
    replacing) the generic description, for domain context pvl-core
    cannot know (mirroring path 1's transfer notes).

    Args:
        mcp: The server to register on.
        jobs: The shared :class:`Jobs` mechanics.
        note: Optional domain sentence appended to the tool description.
    """
    description = (
        "Retrieve the outcome of a background job started by a "
        "long-running tool on this server. When such a tool answers with "
        'status "working" and a job_id, call this tool with that job_id '
        "every few seconds until the status is terminal. Job records "
        "expire after a while — fetch results soon after completion."
    )
    if note:
        description = f"{description} {note}"

    @mcp.tool(
        name=JOB_POLL_TOOL_NAME,
        description=description,
        icons=[_POLL_ICON],
        tags={"jobs"},
        annotations=ToolAnnotations(
            title="Get Job Result",
            readOnlyHint=True,
            destructiveHint=False,
            # The same job_id yields "working" then a terminal result as
            # the background work lands — not idempotent across time.
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def get_job_result(job_id: str) -> dict[str, Any]:
        try:
            return await jobs.poll(job_id)
        except JobNotFoundError as exc:
            raise ToolError(str(exc)) from exc
