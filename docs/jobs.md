# Long-running tools — downstream implementation guide

How to give a slow tool dual-mode behaviour with `fastmcp_pvl_core.jobs`
(ADR 0002 §5): protocol-native background-task execution for clients that
speak MCP tasks (SEP-1686), and a job-handle + polling fallback for the
clients that don't — today, that is every Anthropic client.

> This is an implementor guide for `*-mcp` family servers. Design
> rationale lives in [ADR 0002](adr/0002-dual-mode-tasks.md); the
> operator-facing summary lives in the
> [README](../README.md#long-running-tools-dual-mode).

## The problem this solves

A tool that can run for minutes will outlive the client's request
timeout. fastmcp's native answer is SEP-1686 tasks — but a client that
does not send task metadata gets plain synchronous execution, which is
exactly the timeout failure again. The jobs subsystem closes that gap:
register the tool once, and

- a **task-augmented request** runs as a native background task (Docket
  owns the lifecycle; the client drives `tasks/*`);
- a **plain request** runs in the foreground up to a soft deadline. If
  the work beats the deadline, the caller gets the result inline, exactly
  as if nothing were wrapped. If not, the still-running work is promoted
  to a background job and the caller immediately gets a *job handle* to
  poll.

Your domain coroutine is written once and never branches on any of this.

## Path 1 — the standard wiring

Everything slots into `make_server()` next to the wiring you already
have:

```python
from fastmcp_pvl_core import (
    JobsConfig,
    ServerConfig,
    build_jobs,
    configure_task_backend,
    register_job_tools,
    register_long_running_tool,
)

config = ServerConfig.from_env("MY_APP")
jobs_config = JobsConfig.from_env("MY_APP")     # MY_APP_JOBS_* knobs

configure_task_backend("MY_APP", config)        # native-path backend (ADR §4)
jobs = build_jobs(config, jobs_config)          # one Jobs object per server

@register_long_running_tool(mcp, jobs, tags={"reports"})
async def build_report(paths: list[str], focus: str | None = None) -> dict:
    """Build a report over *paths*.  May take minutes."""
    ...  # your domain work — a plain async function

register_job_tools(mcp, jobs)                   # the generic polling tool
```

Rules of the road:

- The decorated function **must be async** (background execution needs a
  coroutine) and should return a JSON-serialisable value — a `dict` is
  the natural shape. Annotate the return type as `dict[str, Any]`: the
  caller receives either your result *or* a job handle, and both are
  JSON objects.
- The decorator passes your keyword arguments (`name`, `description`,
  `tags`, `annotations`, `icons`, …) straight through to `FastMCP.tool`
  — the tool's identity is yours. The one argument you cannot pass is
  `task`: dual-mode registration *is* the feature, so pvl-core always
  registers `TaskConfig(mode="optional")`.
- Call `register_job_tools` **once per server**, with the same `jobs`
  object. It registers `get_job_result` — the single generic polling
  tool every job on the server resolves through. Do not write your own
  per-tool poller; that is the divergence this subsystem exists to end.
  The optional `note=` kwarg appends one domain sentence to the generic
  tool description (it never replaces it).
- Inline failures behave as if the wrapper were absent: an exception
  raised before the deadline propagates to the caller unchanged. Only a
  failure *after* promotion is reported through polling instead.

## What the client sees

A call that beats the soft deadline returns your result unchanged. A
promoted call returns immediately with:

```json
{
  "status": "working",
  "job_id": "m9Q_dXROZqJ__jjJYt_dKA",
  "poll_with": "get_job_result",
  "retry_after_s": 5.0,
  "message": "build_report is still running after 25s and now continues in the background. Call get_job_result with job_id='…' to retrieve the outcome (poll every few seconds)."
}
```

`get_job_result(job_id=…)` then yields, in order:

```json
{"job_id": "…", "status": "working", "result": null, "error": null,
 "running_for_s": 41.3, "retry_after_s": 5.0, "message": "Still running. …"}
```

and, once the work lands, one of:

```json
{"job_id": "…", "status": "completed", "result": {…your dict…}, "error": null}
{"job_id": "…", "status": "failed",    "result": null, "error": "<message>"}
```

A non-`dict` return value is wrapped as `{"value": …}` in `result`. The
status vocabulary (`working` / `completed` / `failed` / `cancelled`) is
the SEP-1686 task lifecycle minus `input_required`, so a later move to
protocol-native tasks is a mechanical change for clients, not a semantic
one.

## Semantics you can rely on (and their limits)

- **Subject scoping.** Every job belongs to the calling subject (via
  `get_subject`, uniform across auth modes). Another subject polling the
  same id gets the same error as for an unknown id — job ids are not
  probeable across tenants.
- **Retention.** A job record lives `JOBS_RESULT_TTL_S` seconds from
  creation. Settling a job never extends that. After expiry the id is
  simply unknown — tell your users to fetch results promptly.
- **Per-subject cap.** At most `JOBS_MAX_PER_SUBJECT` live records per
  subject; promotion past the cap raises `JobLimitExceededError`.
- **Process lifetime.** A *promoted* job runs on the serving process and
  dies with it. Its record then reports honestly: polls show `working`
  with a growing `running_for_s` until the record's TTL removes it —
  never a fabricated result. Durable cross-restart execution is what the
  **native** task path with a `redis://` backend is for; the fallback
  does not promise it (ADR §8).
- **Storage.** Records live in the unified KV backend
  (`build_kv_store(config, namespace="jobs")`), so one
  `MY_APP_KV_STORE_URL` covers them along with every other pvl-core
  subsystem. With the variable unset, records land in
  `file:///data/state` where that directory is usable (the volume the
  family Docker images mount) and in `memory://` — with a warning —
  where it is not: a CI runner, a `uvx`/pipx install, the stdio plugin
  channel. Wiring `build_jobs` into `make_server` therefore never
  depends on `/data` existing; a deployment that wants job records to
  outlive a restart names a backend explicitly.

## Operator knobs

| Variable | Default | Meaning |
|---|---|---|
| `MY_APP_JOBS_SOFT_DEADLINE_S` | `25` | Foreground window before promotion. Keep below the strictest client request timeout in play. |
| `MY_APP_JOBS_RESULT_TTL_S` | `3600` | Job-record retention from creation. |
| `MY_APP_JOBS_MAX_PER_SUBJECT` | `256` | Live-job cap per calling subject. |
| `MY_APP_TASKS_URL` | *(derived)* | Native-path (Docket) backend — see the [README](../README.md#background-task-backend). |

All reads are strict: a malformed value fails at startup, naming the
variable. The surface is drift-gated (`domain_env_suffixes(JobsConfig)`),
so config generators pick it up automatically.

## Path 2 — building your own tool on the mechanics

The wrapper covers "wrap one coroutine, promote on deadline". When your
tool cannot be expressed that way — it always runs long and should hand
out a handle immediately, it decides *itself* when to go background, or
polling is embedded in a domain tool — compose on the same mechanics
directly:

```python
from fastmcp_pvl_core.jobs import build_jobs

jobs = build_jobs(config, jobs_config)

@mcp.tool
async def rebuild_index(scope: str) -> dict:
    """Rebuild the index for *scope*.  Always long-running."""
    async def work() -> dict:
        ...  # minutes of work
    return dict(await jobs.start(work(), tool="rebuild_index"))
```

The `Jobs` verbs:

- `run_with_deadline(coro, *, tool)` — the full dual-mode behaviour the
  wrapper uses: native → run; inline within deadline → result; expiry →
  promote + handle. Correct in every execution mode, so your code never
  checks which mode it is in.
- `start(coro, *, tool)` — background unconditionally, handle
  immediately.
- `get(job_id)` / `poll(job_id)` — the calling subject's record / the
  exact polling payload the generic tool returns. Use `poll` if a domain
  tool of yours reports job state, so the payload shape stays identical.

Path-2 rules:

- Import from `fastmcp_pvl_core.jobs` (or the package root) only.
  `fastmcp_pvl_core._jobs` is internal; reaching into it is the
  private-import mistake the transfer feature already had to correct
  (#247/#249) — the seam above exists so you never need to.
- Your tool's *identity* is yours; the handle and poll payload *shapes*
  are pvl-core's. Return the handle unmodified (`dict(handle)`) rather
  than restyling it.
- Still call `register_job_tools` once: path-2 jobs resolve through the
  same generic `get_job_result` as path-1 jobs — one polling contract
  per server.
- The public error types (`JobNotFoundError`, `JobLimitExceededError`)
  are what cross the seam; catch those, not internals.

## Migrating a hand-rolled queue-and-poll surface

If your server carries a bespoke job store and its own polling tool
(e.g. a `get_summary`-style companion), migrate rather than adapt
(`CLAUDE.md`: shape divergence resolves by downstream migration):

1. Re-register the slow tool with `register_long_running_tool` (or
   path 2 if it is handle-first) and delete the bespoke store,
   promotion, and sweep code.
2. Replace the bespoke polling tool with `register_job_tools`. The old
   tool name disappears; clients follow the `poll_with` field in the
   handle, so the rename is self-describing at the protocol level.
3. Map your old statuses onto the SEP-1686 vocabulary
   (`in_progress` → `working`).
4. Delete per-tool TTL/eviction knobs in favour of the `JOBS_*` surface.

## Testing your integration

`JobsConfig` is directly constructible, so tests can shrink the deadline
instead of sleeping for real:

```python
jobs = build_jobs(ServerConfig(kv_store_url="memory://"),
                  JobsConfig(soft_deadline_s=0.05, result_ttl_s=60.0))
```

Call your tool through `mcp.call_tool`, assert on the handle payload,
then poll `get_job_result` until terminal. pvl-core's own
`tests/test_jobs.py` shows the full pattern, including path-2 parity.
