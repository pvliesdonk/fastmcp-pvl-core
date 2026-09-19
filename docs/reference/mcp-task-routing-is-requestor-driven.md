---
type: Reference
title: Who decides whether a tool call runs as a task
description: Whether a receiver can turn a foreground tools/call into an SEP-2663 task after it has started, and how FastMCP applies the answer.
subject_version: "MCP 2025-11-25 / fastmcp 4.0.0 / fastmcp-tasks 4.0.0 / pydocket 0.24.1"
valid_for: "MCP 2025-11-25 and FastMCP 4.x"
generated:
  by: process:researching-references
  at: 2026-09-19
stale_after: 2027-03-19
status: stable
verified:
  - by: process:researching-references
    at: 2026-09-19
sources:
  - id: mcp-tasks-spec
    title: "Model Context Protocol specification 2025-11-25 — Tasks"
    resource: https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks
    accessed: 2026-09-19
  - id: tasks-extension
    title: "fastmcp_tasks.extension — the tools/call interceptor"
    resource: file:///.venv/lib/python3.10/site-packages/fastmcp_tasks/extension.py
    accessed: 2026-09-19
  - id: tasks-creation
    title: "fastmcp_tasks.creation — task submission"
    resource: file:///.venv/lib/python3.10/site-packages/fastmcp_tasks/creation.py
    accessed: 2026-09-19
  - id: tasks-components
    title: "fastmcp_tasks.components — Docket registration of task-enabled components"
    resource: file:///.venv/lib/python3.10/site-packages/fastmcp_tasks/components.py
    accessed: 2026-09-19
  - id: docket-worker
    title: "docket.worker — execution logging"
    resource: file:///.venv/lib/python3.10/site-packages/docket/worker.py
    accessed: 2026-09-19
---

# Who decides whether a tool call runs as a task

`pvl-core`'s jobs layer is a fallback for clients that do not negotiate the
SEP-2663 tasks extension. Whether that fallback can ever be retired on a
pvl-core release turns on one question this page answers: can the *server*
move a call that started in the foreground into a task once it turns out to
be slow? If it could, the fallback would be a stopgap with a version number;
if it cannot, the fallback serves a population of clients that only a
deployment can observe. The released specification and the installed
`fastmcp_tasks` are the primary sources.

## Scope

- Covers: who makes the task-versus-foreground decision for a `tools/call`,
  when it is made, and whether it can be revisited; what the server logs on
  the native path.
- Does not cover: task status fields while running (see
  `fastmcp-native-task-signals.md`), the input-required leg, cancellation,
  the worker's context snapshot.
- Depended on by: `src/fastmcp_pvl_core/_jobs/manager.py` (the fallback's
  reason to exist), `docs/jobs.md` ("When the fallback can go"),
  `docs/adr/0002-dual-mode-tasks.md` §8.

## Claims

### The decision is the requestor's, and the specification has no promotion

- Tasks are "requestor-driven": requestors augment requests with tasks and
  poll for their results, while receivers "tightly control which requests (if
  any) support task-based execution". [source: mcp-tasks-spec] (Overview)
- A tool declares `execution.taskSupport` as `required`, `optional` or
  `forbidden` in `tools/list`; when it is absent or `forbidden`, clients MUST
  NOT invoke the tool as a task. [source: mcp-tasks-spec] (Tool Support)
- A task-augmented request is accepted with a `CreateTaskResult` that is
  returned "as soon as possible after accepting the task"; the operation's
  result then arrives only through `tasks/result`. The two-phase shape is
  fixed at acceptance. [source: mcp-tasks-spec] (Task Creation; Requirements
  → Task Creation)
- The only receiver-side lever runs the other way: a receiver that declares
  the capability "MAY return an error for non-task-augmented requests,
  requiring requestors to use task augmentation". That is `taskSupport:
  required`, a refusal, not a promotion. [source: mcp-tasks-spec]
  (Requirements → Task Support and Handling)
- The task operations are `tasks/get`, `tasks/list`, `tasks/result`,
  `tasks/cancel` and the optional `notifications/tasks/status`. None of them
  attaches a task to a request already answered without one, and the page
  contains no "promote" or "upgrade" operation — a negative claim,
  established by sweeping the whole page for those terms and enumerating its
  method names, not by reading one section. [source: mcp-tasks-spec]
- The `draft` specification page served a ~10 KB stub on the access date and
  is not evidence either way. [unverified] A later draft that adds a
  receiver-initiated conversion would change every claim above; re-check the
  draft when its tasks page is substantive again.

### FastMCP applies the decision before the tool body runs, and never after

- `TasksExtension.intercept_tool_call` decides per call: a tool with no task
  support passes straight to `call_next()`; otherwise `opted_in` is "modern
  protocol era **and** the client sent extension settings for the tasks
  extension"; `required` creates a task or raises the missing-capability
  error; `optional` creates a task only when `opted_in`; everything else is
  `return await call_next()`. [source: tasks-extension] (`extension.py:213-271`)
- Its docstring states the consequence directly: "A non-task call passes
  straight through to the tool body." [source: tasks-extension]
  (`extension.py:219-225`)
- `create_task` is called from that interceptor and nowhere else in the
  package, so there is no code path by which a call already running in the
  foreground becomes a task — a package-wide negative, established by
  grepping every module for `create_task(`. [source: tasks-extension]
  [source: tasks-creation]
- Therefore, for a client that did not opt in, the tool body is the only
  place a slow call can still be answered without blocking, which is what
  `Jobs.run_with_deadline`'s promotion to a pvl-core job handle provides.
  [pins: tests/test_jobs.py::TestRegistration::test_wrapped_tool_inline_and_promoted,
  tests/test_jobs_native_task.py::TestStartInsideANativeTask::test_start_returns_the_work_result]

### What the native path logs

- Task-enabled components are registered with Docket under their component
  key, so the executed function is identified per tool.
  [source: tasks-components] (`components.py:62-84`)
- Docket's worker logs the start of every execution at INFO on the
  `docket.worker` logger (`↪ [<punctuality>] <call>`, with `↬` for a retry)
  and its completion (`↩ [<duration>] <call>`), where `<call>` is the
  registered function name with arguments elided. [source: docket-worker]
  (`worker.py:1036-1041`, `:1141-1146`; `call_repr` in `execution.py:745`)
- `fastmcp_tasks` itself logs nothing at task creation or routing in
  `extension.py` or `creation.py` — a negative, established by grepping both
  files for logger calls. [source: tasks-extension] [source: tasks-creation]

## Where pvl-core departs from the subject

None. pvl-core's fallback is additive inside the tool body — the only place
the specification leaves a non-negotiating client's slow call — and does not
alter the routing above. `configure_logging_from_env` caps `docket.worker`
at INFO when the root level is DEBUG, which removes Docket's poll trace and
leaves the execution lines above intact (`_logging.py`, `_DEBUG_FLOOD_LOGGERS`).

## Not covered

- Whether any client in the family's fleet negotiates tasks today. That is a
  per-deployment observation, which is the point of `docs/jobs.md` ("When
  the fallback can go"), not a property of the protocol.
- `notifications/tasks/status` delivery. No claim here depends on it.
