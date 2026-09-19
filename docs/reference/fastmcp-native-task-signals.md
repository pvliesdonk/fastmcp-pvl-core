---
type: Reference
title: What a FastMCP native task can tell its client while it runs
description: Which SEP-2663 task fields a running tool can influence, and which are fixed at submission.
subject_version: "fastmcp 4.0.0 / fastmcp-tasks 4.0.0"
valid_for: "FastMCP 4.x"
generated:
  by: process:researching-references
  at: 2026-09-19
stale_after: 2027-03-19
status: stable
verified:
  - by: process:researching-references
    at: 2026-09-19
sources:
  - id: tasks-models
    title: "fastmcp_tasks.models — task result field definitions"
    resource: file:///.venv/lib/python3.10/site-packages/fastmcp_tasks/models.py
    accessed: 2026-09-19
  - id: tasks-handlers
    title: "fastmcp_tasks.handlers — tasks/get response assembly"
    resource: file:///.venv/lib/python3.10/site-packages/fastmcp_tasks/handlers.py
    accessed: 2026-09-19
  - id: tasks-creation
    title: "fastmcp_tasks.creation — task submission and poll-interval persistence"
    resource: file:///.venv/lib/python3.10/site-packages/fastmcp_tasks/creation.py
    accessed: 2026-09-19
  - id: tasks-context
    title: "fastmcp_tasks.context — get_task_context and the worker snapshot"
    resource: file:///.venv/lib/python3.10/site-packages/fastmcp_tasks/context.py
    accessed: 2026-09-19
  - id: fastmcp-context
    title: "fastmcp.server.context.Context.report_progress"
    resource: file:///.venv/lib/python3.10/site-packages/fastmcp/server/context.py
    accessed: 2026-09-19
---

# What a FastMCP native task can tell its client while it runs

`pvl-core`'s jobs layer has two background mechanisms that can be active at
once: its own job store, and FastMCP's native SEP-2663 tasks. Deciding what
`Jobs.defer` should do inside a native task (#324) turns on a single question
this page answers — whether a running task can convey "not yet, and here is
why" to the client without inventing a second polling contract.

The installed package is the primary source here: it is the version pvl-core
actually runs. Claims marked `[observed: ...]` were reproduced against an
in-process `memory://` task backend, because the field assembly is spread
across submission, Redis, and the `tasks/get` handler, and reading any one of
them alone does not settle what a client sees.

## Scope

- Covers: the `tasks/get` fields a *running* tool can and cannot influence —
  `status`, `statusMessage`, `pollIntervalMs` — and how a tool reaches them.
- Does not cover: task submission and routing, the input-required /
  elicitation legs, worker authentication and the context snapshot, task
  cancellation, or the encryption of that snapshot.
- Depended on by: `src/fastmcp_pvl_core/_jobs/manager.py`
  (`run_with_deadline`, `start`, `defer`), `docs/jobs.md`.

## Claims

### Task status vocabulary

- A task's status is one of `working`, `input_required`, `completed`,
  `failed`, `cancelled`. [source: tasks-models] (`TaskStatus`, `models.py:53`)
- `pvl-core`'s own job statuses are deliberately that vocabulary minus
  `input_required`, so a later move to protocol-native tasks is mechanical for
  clients. See `docs/jobs.md`; that is a pvl-core decision, not a FastMCP one.

### The reason a task is waiting *can* be surfaced

- A task result carries `status_message` (wire: `statusMessage`), optional and
  defaulting to `None`. [source: tasks-models] (`models.py:75`)
- For a task still running, `tasks/get` populates `statusMessage` from the live
  Docket execution's progress message, and from nothing else:
  `if execution.progress and execution.progress.message`.
  [source: tasks-handlers] (`handlers.py:327-330`)
- `Context.report_progress(progress, total=None, message=None)` works in both
  execution modes: with a progress token it sends an MCP progress
  notification; inside a task it updates the Docket execution's progress in
  Redis, which is what `tasks/get` then reports.
  [source: fastmcp-context] (`context.py:453-515`, the background branch;
  `set_message` is at `:511`)
- A bare `progress.set_message(...)`, with no preceding `increment()` or
  `set_total()`, is enough: the handler's test is
  `if execution.progress and execution.progress.message`, and Docket writes
  the message field unconditionally. This is the call pvl-core actually
  makes — it wants to say *why*, not to report a fraction.
  [source: tasks-handlers] (`handlers.py:327-330`)
  [pins: tests/test_jobs_native_task.py::TestDeferInsideANativeTask::test_defer_tells_the_client_why_it_is_waiting]
- Therefore a tool running as a native task can give its client a
  human-readable "why I am still working" string at any point, without
  completing, and the status stays `working`.
  [observed: a `register_long_running_tool` tool calling
  `ctx.report_progress(0, None, message="throttled by upstream; retry in 2s")`
  then sleeping; polled via `fastmcp_tasks.client.call_tool_task(...).status()`.
  Five consecutive polls returned `status='working'` with
  `statusMessage='throttled by upstream; retry in 2s'`, then `status='completed'`
  with `statusMessage=None`. fastmcp 4.0.0 / fastmcp-tasks 4.0.0, Python 3.10.20,
  `memory://` backend.]
  [pins: tests/test_jobs_native_task.py::TestDeferInsideANativeTask::test_defer_tells_the_client_why_it_is_waiting]
- A terminal status replaces the message: a completed task reports
  `statusMessage=None`, and a failed one reports the exception's message.
  [source: tasks-handlers] (`handlers.py:322`) [observed: same probe — the
  final poll returned `statusMessage=None`.]

### The poll interval *cannot* be changed by the running tool

- `pollIntervalMs` is computed once at submission from the component's static
  task configuration (`component.task_config.poll_interval`) and written to a
  per-task Redis key with the task's TTL.
  [source: tasks-creation] (`creation.py:105-119, 142`)
- `tasks/get` reads that key back on every poll, falling back to a default when
  it is absent. [source: tasks-handlers] (`handlers.py:140, 176-180`)
- No code path in `fastmcp_tasks` writes that key after submission; `creation.py`
  is its only writer — a package-wide negative, so established by sweep rather
  than by reading one file. A tool therefore cannot revise its own poll interval
  through any supported API, and a dynamic value (an upstream `Retry-After`, say) has no native
  channel. [source: tasks-creation] [observed: across five polls of a task whose
  tool reported progress, `pollIntervalMs` stayed `5000.0`, the configured value.]
- Rewriting the Redis key directly would work mechanically but reaches into
  `fastmcp_tasks`' private key layout, which this reference does not treat as an
  API. [unverified] Verifying would mean asserting the key format stays stable
  across releases, which is exactly the commitment upstream has not made.

### Detecting that you are inside a task

- `get_task_context()` returns a `TaskContextInfo(task_id, task_scope)` inside a
  Docket worker and `None` in foreground execution; it reads Docket's
  `current_execution` context var and returns `None` on `LookupError`.
  [source: tasks-context] (`context.py:98-125`)
- It returns `None` when Docket is unavailable, so the check is safe in a
  stripped install. [source: tasks-context]
- `pvl-core` uses exactly this check to make all three background verbs
  mode-agnostic — `run_with_deadline`, `start` and `defer` (#324).
  [pins: tests/test_jobs.py, tests/test_jobs_native_task.py]

## Not covered

- Whether a client is *required* to surface `statusMessage` to a user. That is
  a client-side presentation question the MCP specification does not settle
  here, and no claim above depends on it.
- `notifications/tasks/status`, mentioned in `report_progress`'s docstring as a
  second delivery path for the same progress data. [unverified] Verifying would
  mean driving a client that subscribes to task notifications rather than
  polling.
