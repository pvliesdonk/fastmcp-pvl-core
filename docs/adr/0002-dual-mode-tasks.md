# ADR 0002 — Background-task backend surface and dual-mode long-running tools

- **Status:** Proposed (study for [#264] and [#265]; no implementation in
  this document)
- **Date:** 2026-08-13
- **Deciders:** pvl-core maintainers
- **Relates to:** ADR 0001 (transfer lift — reused store/config idioms),
  markdown-vault-mcp#1033 (hand-rolled machinery this replaces downstream),
  markdown-vault-mcp#937 (the client-timeout failure motivating promotion),
  anthropics/claude-code#18617 (open request for client-side task support)

> This is an **implementor design**, not a wire specification. Nothing here
> describes bytes negotiated with a foreign implementation: the wire
> authority for task execution is MCP's own SEP-1686 (fastmcp 3.x built-in)
> and, later, SEP-2663 (`io.modelcontextprotocol/tasks`). Per `CLAUDE.md`,
> that is exactly why this lives in `docs/adr/`, not `docs/specs/`.

---

## 1. Context

Two issues, one subsystem:

- **[#264]** — enabling fastmcp's built-in background tasks
  (`fastmcp[tasks]` / Docket) bypasses pvl-core's unified backend-selection
  surface: the task backend is chosen through `FASTMCP_DOCKET_*` env vars
  that pvl-core neither wires, names per its conventions, nor exposes to
  config generation, while every other stateful subsystem resolves through
  one `kv_store_url`.
- **[#265]** — no current Anthropic client speaks MCP tasks, and fastmcp's
  only degradation for non-task clients is plain synchronous execution —
  which reintroduces the client-request-timeout failure a long-running tool
  exists to avoid. Downstream servers consequently hand-roll a job store +
  polling tool + promotion-on-deadline per server (markdown-vault-mcp
  carries three such surfaces). The pattern is domain-independent, so it
  belongs in pvl-core.

\#264 is the configuration prerequisite of #265: the dual-mode helper's
native path runs on Docket, so the backend surface must be settled first.

## 2. Research findings

All verified against the installed dependency set (fastmcp 3.3.1, the
`pyproject.toml` floor of the `>=3.3.1,<4` pin; the issues' own findings
against 3.4.7 agree — the task machinery is present and identically shaped
at both ends of the pin).

### 2.1 How fastmcp activates and configures Docket

`fastmcp/server/mixins/lifespan.py::_docket_lifespan_root` initializes task
infrastructure only when **both** hold: `pydocket` is importable (the
`fastmcp[tasks]` extra) **and** at least one registered component is
task-enabled (`task_config.mode != "forbidden"`). It then constructs
`Docket(name=settings.docket.name, url=settings.docket.url)` and a `Worker`
whose kwargs (`concurrency`, `redelivery_timeout`, `reconnection_delay`,
`minimum_check_interval`, optional `worker_name`) are all read from the
**global `fastmcp.settings.docket`** object — lazily, at server startup.

This answers #264's first open question: **programmatic injection is
possible without touching env vars.** `fastmcp.settings` is a module-level
pydantic-settings instance; assigning `fastmcp.settings.docket.url = ...`
before `mcp.run(...)` is observed by the lifespan, because nothing reads
the settings until the root lifespan enters. No fastmcp fork or env-var
round-trip is needed.

Docket supports exactly two backends: `memory://` (in-process, single
process only) and `redis://` (distributed). It is a task queue with
Redis-streams semantics, **not** an `AsyncKeyValue` — the kv-store backends
cannot literally back it (per #264's scope boundary, the unification target
is the *operator configuration surface*, not the storage protocol).

### 2.2 The primitives for a dual mode (all present in 3.3.1)

- **Per-request routing** — `server/tasks/routing.py::check_background_task`
  dispatches on request **task metadata**, not on the client's declared
  capability: `task=True` / `TaskConfig(mode="optional")` tools run on
  Docket when the request carries task metadata, and synchronously
  otherwise. `@mcp.tool(task=...)` accepts `bool | TaskConfig | None`.
- **Capability detection** — `mcp.types.ClientCapabilities` has a
  first-class `tasks: ClientTasksCapability | None` field, and
  `ServerSession.check_client_capability` handles it (via
  `check_tasks_capability`). pvl-core already wraps exactly this kind of
  probe: `client_supports_apps()` in `_apps.py` is the in-house precedent,
  and fastmcp's sampling fallback (`server/sampling/run.py`) is the
  upstream precedent for capability-conditional degradation.
- **Execution-mode introspection** — `get_task_context()`
  (`server/tasks/context.py`) returns task info inside a Docket worker and
  `None` in foreground, so shared tool code can tell which path it is on.
- **Auth scoping** — `get_task_scope()` composes `client_id|sub` for task
  isolation; the fallback job store needs the same scoping and pvl-core
  already owns subject extraction (`_subject.get_subject`).

### 2.3 What fastmcp does *not* provide

No job store, no generic polling tool, no promotion-on-deadline, no
pollable handle for a non-task client. That layer is app code in every
downstream today, and is the entire subject of #265.

### 2.4 In-repo idioms the design reuses

- `build_kv_store(config, namespace=...)` — one URL, isolated keyspaces;
  the fallback job store is a new namespace, not a new backend surface.
- `TransferStore` (ADR 0001 §6) — the KV-backed store idiom: TTL-driven
  expiry with no sweep loop, one in-process `asyncio.Lock` serialising
  read-modify-write over the CAS-less KV facade, restart survival.
- `TransferConfig` — the env-section idiom: literal
  `env_*(prefix, "LITERAL")` reads so `domain_env_suffixes` drift-gating
  sees the surface; operator tuning is env config, never kwargs.
- `register_server_info_tool` — the generic-tool idiom: pvl-core registers
  one tool with a pvl-core-owned name and result shape.
- `server_config_env_suffixes` explicitly *excludes* native `FASTMCP_*`
  variables — precedent that fastmcp-native env config is an acknowledged,
  separately documented axis rather than something pvl-core re-wraps
  wholesale.

## 3. Decision (summary)

1. **#264 — one new `ServerConfig` field, `tasks_url`** (env
   `<PREFIX>_TASKS_URL`), resolved with a derivation rule (§4) and injected
   programmatically into `fastmcp.settings.docket` by a new factory helper
   before serve. The Docket **queue name** is derived from the server's
   identity (parameterized, per the foldability rule) — pvl-core picks it;
   it is not operator config and not a kwarg. All remaining Docket worker
   tunables stay native `FASTMCP_DOCKET_*` vars.
2. **#265 — a new `_jobs/` subsystem**: a KV-backed `JobStore`
   (`namespace="jobs"`), a registration helper that wraps a long-running
   domain function into a dual-mode tool (native SEP-1686 task path when
   the request is task-augmented; foreground with soft-deadline promotion
   otherwise), and **one** generic polling tool registered per server. The
   fallback's status vocabulary mirrors the protocol task lifecycle so a
   later migration to protocol-native tasks is mechanical.
3. ~~A **`tasks` extra** on pvl-core (`tasks = ["fastmcp[tasks]"]`),
   mirroring the `redis`/`dynamodb`/`mongodb` extras: per-downstream
   opt-in, never a base dependency.~~ **Revised during implementation:
   `fastmcp[tasks]` is a base dependency — see §4.4 for the measurements
   and rationale.**

## 4. #264 — the task-backend configuration surface

### 4.1 Classification

Per the `CLAUDE.md` test: which backend Docket uses is **operator
configuration** (env var, never a kwarg); what the env var is called, how
it defaults, and what the queue is named are **shape** (pvl-core picks; no
override kwarg).

### 4.2 The field and its derivation rule

`ServerConfig` grows:

```
tasks_url: str | None    # <PREFIX>_TASKS_URL
```

Resolution, applied by a new `configure_task_backend(env_prefix, config)`
factory helper (name illustrative):

1. `config.tasks_url` set → written to `fastmcp.settings.docket.url`.
   Accepted schemes are Docket's: `memory://`, `redis://`; anything else is
   a `ConfigurationError` naming the var (strict, like `PORT`).
2. Unset, and `kv_store_url` (or legacy `event_store_url`) has scheme
   `redis://` → **reuse that same Redis URL** for Docket. One
   `<PREFIX>_KV_STORE_URL=redis://…` then configures every stateful
   subsystem *and* the task queue — the single-surface goal of #264, made
   safe by Docket's key prefixing plus a pvl-core-derived queue name.
3. Otherwise → leave `fastmcp.settings.docket.url` untouched. fastmcp's
   own default is `memory://`, and a directly-set `FASTMCP_DOCKET_URL`
   keeps working as the native escape hatch (consistent with the
   established "native `FASTMCP_*` is a separate axis" stance). When the
   resolved backend is `memory://` and transport is `http`, log one
   startup line noting tasks are process-local and lost on restart —
   the same operator signal `build_kv_store` gives for `memory://`.

If both `<PREFIX>_TASKS_URL` and `FASTMCP_DOCKET_URL` are set and differ,
pvl-core's surface wins (it is the documented contract) and a warning names
both vars — never a silent divergence.

The queue **name** is derived from the server identity pvl-core already
receives (the `env_prefix` / server name arguments — parameterized identity,
never the literal package name), replacing fastmcp's default `"fastmcp"`.
Two family servers sharing one Redis must not share a task queue by
default, exactly as kv namespaces must not collide.

### 4.3 What is deliberately *not* wrapped

`FASTMCP_DOCKET_CONCURRENCY`, `…_WORKER_NAME`, `…_REDELIVERY_TIMEOUT`,
`…_RECONNECTION_DELAY`, `…_MINIMUM_CHECK_INTERVAL` stay native. They are
worker tuning with sensible upstream defaults, not backend selection; the
divergent-naming complaint in #264 is about the *backend* being configured
twice. The env-reference generator gains a short "native fastmcp task
variables" note (the same treatment other `FASTMCP_*` vars get), so the
surface is documented without being renamed.

### 4.4 The tasks dependency (revised: base dependency, not an extra)

**Revision, 2026-08-13 (maintainer decision during #267 implementation;
supersedes the original extra-based text below).** `fastmcp[tasks]` is a
**base dependency** of pvl-core, not an optional extra. Two measurements
drove the change: (1) the delta is ~10 MB of site-packages (redis client
~7.6 MB, docket ~1.1 MB, small tools the rest) — a few percent of a
typical server image, with runtime cost limited to import weight since
fastmcp only starts Docket when task-enabled components exist; and
(2) fastmcp fails **hard at registration time** — `ImportError` from
`TaskConfig.validate_function` → `require_docket` — for any `task=True`
tool when pydocket is missing, so with nearly every family server
carrying long-running tools, a lean install is not a graceful fallback
but a startup crash waiting to happen. It also removes a conditional
registration branch from §5's dual-mode helper, which can now always
register `task=TaskConfig(mode="optional")`. This resolves #264's second
open question the other way from the original text: adoption of task
*execution* stays per-project (register `task=True` tools or don't); the
*dependency* is family baseline. `configure_task_backend` keeps its
no-op guard (debug log) for stripped forks and incompatible-pydocket
environments, mirroring fastmcp's own activation conditions.

*Original (superseded):* `fastmcp-pvl-core[tasks]` → `fastmcp[tasks]` as
a per-downstream opt-in extra, like `redis`, keeping pvl-core's import
surface lazy so nothing pulls `pydocket` uninvited.

## 5. #265 — dual-mode long-running tools

### 5.1 Module decomposition (`src/fastmcp_pvl_core/_jobs/`)

Mirroring `_transfer/`:

- `config.py` — `JobsConfig` env section (`JOBS_SOFT_DEADLINE_S`,
  `JOBS_RESULT_TTL_S`, `JOBS_MAX_PER_SUBJECT`, …; exact set fixed at
  implementation time). Literal reads, drift-gated, wizard-tagged.
- `store.py` — `JobStore` over `build_kv_store(config, namespace="jobs")`.
  Record: job id (UUID), subject scope, status, `created_at`, result or
  error envelope, TTL. Status vocabulary is the SEP-1686 `TaskStatus`
  literal set — `working`, `completed`, `failed`, `cancelled`
  (`input_required` omitted: the fallback has no elicitation channel, and a
  status the store can never emit would misdocument the surface) — so the
  polling tool's answer shape mirrors what a protocol-native `tasks/result`
  would say — #265's fourth open question, resolved in favour of mirroring. Same correctness boundary as
  `TransferStore`: one `asyncio.Lock` over the CAS-less KV facade, TTL
  expiry instead of a sweep loop.
- `register.py` —
  - `long_running_tool(...)` (name illustrative): wraps a domain coroutine
    and registers it with `task=TaskConfig(mode="optional")`. Inside the
    shared body: `get_task_context()` non-`None` → native path, just run
    (Docket owns lifecycle, results, TTL). Foreground → run under a soft
    deadline (`asyncio.wait` with timeout); on completion within the
    deadline return the result inline; on expiry, *promote*: detach the
    still-running coroutine as a server-side asyncio task that writes its
    outcome to `JobStore`, and return a structured job-handle payload
    (job id + polling-tool name + status) immediately.
  - `register_job_tools(mcp, config)`: registers the **one** generic
    polling tool (working name `get_job_result`), which looks up a job id
    *scoped to the calling subject* — a caller can never poll another
    subject's job. Scope composition follows `get_task_scope()`'s
    `client_id|sub` shape so native and fallback scoping stay congruent.

### 5.2 Kwarg classification (the `CLAUDE.md` test, applied)

- **Hooks (kwargs)**: the domain coroutine itself; a per-tool
  human-readable progress label if we find downstream needs one. These are
  answers pvl-core literally cannot give.
- **Shape (no kwargs)**: the polling tool's name and result schema, the
  job-handle payload shape, the status vocabulary, what happens on
  deadline expiry. pvl-core picks; downstream conforms; pre-existing
  downstream divergence (markdown-vault-mcp's `get_summary` polling tool,
  its bespoke store) resolves by **downstream migration**, tracked in
  markdown-vault-mcp#1033 — no compatibility shim.
- **Operator config (env, never kwargs)**: soft deadline, result TTL,
  per-subject job cap.

### 5.3 Resolved open questions from #265

- **Task-capable client calling synchronously** (its per-request right
  under SEP-1686): *still promote on deadline.* The failure being avoided
  — a transport-level request timeout — does not care why the call was
  synchronous, and one uniform foreground behaviour is a simpler shape
  than branching on advertised-but-unused capability. A client that wants
  guaranteed-inline semantics has none today either (it gets a timeout);
  a client that wants native lifecycle can send task metadata.
- **Reuse Docket's result retention for the fallback?** No — independent
  KV-backed store. The fallback must work when `fastmcp[tasks]` is not
  installed at all (a downstream can want promotion without adopting the
  extra), and coupling the fallback to Docket would make the *client's*
  capability determine which *server-side* backend holds results. The cost
  — two result stores when both paths are active — is acceptable: the
  native path's results belong to the protocol lifecycle, the fallback's
  to pvl-core, and TTLs bound both.
- **fastmcp 3 → 4 boundary (SEP-2663 / `fastmcp-tasks` extension):** the
  pvl-core public surface (registration helper, polling tool, `JobStore`)
  is the stability contract; the native-path wiring (`TaskConfig`,
  `get_task_context`, capability probe) is internal and swaps behind it.
  Mirroring SEP-2663's status shape in the polling result (above) is the
  concrete hedge. No pre-abstraction beyond that: pvl-core pins `<4`, and
  the 4.x adaptation is its own future change.

### 5.4 Failure and lifecycle notes for implementation

- A promoted job runs on the serving process's event loop and dies with
  the process; the `JobStore` record's TTL then reports the job as expired
  rather than perpetually `working`. Record a `started_at` and let the
  polling tool surface "unknown/expired" honestly. (Docket's redelivery
  handles this on the native path; the fallback does not promise
  cross-restart execution — only cross-restart *visibility* when the KV
  backend persists.)
- Promotion must snapshot what the continuation needs *before* the request
  context ends (auth subject for scoping; nothing session-bound), the same
  concern fastmcp's `TaskContextSnapshot` solves for Docket workers.
- The wrapped tool's result schema must be a union of "inline result" and
  "job handle" for non-task clients; the handle payload includes enough
  for a model to know to poll (tool name, job id, status, retry-after
  hint). Exact schema fixed at implementation time.

## 6. Alternatives rejected

- **Document-only for #264** (tell operators about `FASTMCP_DOCKET_URL`):
  leaves the backend outside config generation and the wizard, keeps two
  naming conventions — the observed problem, unfixed.
- **A pvl-core kwarg for the Docket URL/queue name**: operator config as a
  kwarg violates the config axis; shape as a kwarg violates the
  classification test.
- **Wrapping every `FASTMCP_DOCKET_*` knob in `<PREFIX>_*` vars**:
  re-wraps an acknowledged native axis for no unification gain (§4.3).
- **Building the fallback on Docket's own retention**: couples the
  fallback to the extra and to a client-visible behavioural fork (§5.3).
- **Per-tool polling tools** (markdown-vault-mcp's current shape): N tools
  where one suffices; the polling tool's identity is shape, owned here.

## 7. Deliverable — decomposition into shippable PRs

1. **PR 1 (#264):** `ServerConfig.tasks_url` + suffix set + surface
   metadata; `configure_task_backend` factory helper with the §4.2
   derivation, queue-name parameterization, and strict scheme validation;
   `tasks` extra; env-reference note for native `FASTMCP_DOCKET_*` vars;
   tests (derivation matrix, both-set warning, no-pydocket no-op).
2. **PR 2 (#265):** `_jobs/` — `JobsConfig`, `JobStore` (+ TTL/scope
   tests mirroring the `TransferStore` suite), registration helpers,
   generic polling tool; docs page for downstream adoption.
3. **PR 3 (downstream cutover):** file the markdown-vault-mcp migration
   issue under #1033 (replace bespoke store / `get_summary` / promotion
   with the pvl-core surface), plus template-side note in
   fastmcp-server-template#131. Breaking-shape ships in pvl-core first;
   the umbrella tracker coordinates, per `CLAUDE.md`.

PR 1 and PR 2 are independent enough to review separately but PR 2's
native path assumes PR 1's backend wiring exists.

## 8. Consequences

- One `<PREFIX>_KV_STORE_URL=redis://…` configures state *and* tasks; the
  wizard and env reference cover the whole surface again.
- Downstream long-running tools become one registration call; three
  hand-rolled surfaces in markdown-vault-mcp become deletable.
- pvl-core takes on a small amount of global-state mutation
  (`fastmcp.settings.docket`) — justified because fastmcp offers no
  constructor-level injection point, and confined to one helper that tests
  can exercise and reset.
- The fallback promotes on the serving process only; operators needing
  durable cross-restart execution use the native path with Redis. This is
  a documented limit, not a gap to engineer around now.

## 9. Open questions for implementation time

- Exact `JobsConfig` knob set and defaults (soft deadline in particular —
  markdown-vault-mcp's shipped inline-deadline value is the obvious
  starting point).
- Whether the promotion helper should also expose a `list_jobs`-style tool
  or only point lookups (lean: point lookups only, until a downstream
  demonstrates need).
- The job-handle payload's exact field names — to be fixed against
  SEP-2663's task-status shape at implementation time, not guessed here.
- Whether `configure_task_backend` folds into an existing composite wiring
  helper or stays standalone (depends on how downstream `make_server()`
  compositions read after PR 1).

[#264]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/264
[#265]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/265
