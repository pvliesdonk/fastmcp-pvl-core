---
type: Reference
title: FastMCP tool-search transform and CodeMode
description: What FastMCP's search transforms change on the wire, what they leave reachable, what they break for task-capable tools, and where CodeMode stands.
subject_version: "FastMCP 4.0.0 (this repository's lock) and 4.0.5; fastmcp-tasks 4.0.0; mcp 2.1.1"
valid_for: "FastMCP 4.x"
generated:
  by: process:researching-references
  at: 2026-09-24
stale_after: 2027-03-24
verified:
  - by: process:researching-references-refute
    at: 2026-09-24
status: stable
sources:
  - id: fastmcp-tool-search
    title: FastMCP docs, Tool search transform
    resource: https://gofastmcp.com/servers/transforms/tool-search
    accessed: 2026-09-24
  - id: fastmcp-code-mode
    title: FastMCP docs, Code Mode (experimental)
    resource: https://gofastmcp.com/servers/transforms/code-mode
    accessed: 2026-09-24
  - id: fastmcp-search-base
    title: FastMCP source, fastmcp/server/transforms/search/base.py
    resource: https://github.com/PrefectHQ/fastmcp/blob/main/fastmcp_slim/fastmcp/server/transforms/search/base.py
    accessed: 2026-09-24
  - id: fastmcp-catalog
    title: FastMCP source, fastmcp/server/transforms/catalog.py
    resource: https://github.com/PrefectHQ/fastmcp/blob/main/fastmcp_slim/fastmcp/server/transforms/catalog.py
    accessed: 2026-09-24
  - id: fastmcp-server
    title: FastMCP source, fastmcp/server/server.py (get_tasks, list_tools)
    resource: https://github.com/PrefectHQ/fastmcp/blob/main/fastmcp_slim/fastmcp/server/server.py
    accessed: 2026-09-24
  - id: fastmcp-tasks-lifespan
    title: fastmcp-tasks source, fastmcp_tasks/lifespan.py
    resource: https://github.com/PrefectHQ/fastmcp/blob/main/fastmcp_tasks/fastmcp_tasks/lifespan.py
    accessed: 2026-09-24
  - id: fastmcp-3-1-0
    title: FastMCP release v3.1.0, "Code to Joy"
    resource: https://github.com/PrefectHQ/fastmcp/releases/tag/v3.1.0
    accessed: 2026-09-24
  - id: fastmcp-4925
    title: FastMCP issue 4925, CodeMode has no way to keep some tools direct
    resource: https://github.com/PrefectHQ/fastmcp/issues/4925
    accessed: 2026-09-24
  - id: fastmcp-4418
    title: FastMCP issue 4418, search transform proxy tools missing description
    resource: https://github.com/PrefectHQ/fastmcp/issues/4418
    accessed: 2026-09-24
  - id: fastmcp-4414
    title: FastMCP issue 4414, search transform proxy tools missing title (closed 2026-07-28)
    resource: https://github.com/PrefectHQ/fastmcp/issues/4414
    accessed: 2026-09-24
  - id: fastmcp-5152
    title: FastMCP issue 5152, BM25 search returns an old input schema after a newer tool version
    resource: https://github.com/PrefectHQ/fastmcp/issues/5152
    accessed: 2026-09-24
  - id: ha-mcp-categorized
    title: ha-mcp, src/ha_mcp/transforms/categorized_search.py
    resource: https://github.com/homeassistant-ai/ha-mcp/blob/master/src/ha_mcp/transforms/categorized_search.py
    accessed: 2026-09-24
  - id: mvm-budget
    title: markdown-vault-mcp, tests/test_client_surface_budget.py (server fixture reused for measurement)
    resource: https://github.com/pvliesdonk/markdown-vault-mcp/blob/main/tests/test_client_surface_budget.py
    accessed: 2026-09-24
---

# FastMCP tool-search transform and CodeMode

FastMCP ships two ways to take a large tool catalog out of `tools/list`: the
search transforms (`RegexSearchTransform`, `BM25SearchTransform`), which
replace the listing with a `search_tools` / `call_tool` pair, and the
experimental `CodeMode`, which replaces it with discovery tools plus a
sandboxed `execute`. This page records what each does on the wire, what it
leaves reachable, and what it silently breaks, so that ADR 0004 (tool
catalog mode) rests on observed behaviour rather than on the docs' summary.
Every observation was run on fastmcp 4.0.0 with mcp 2.1.1 (this
repository's lock) under CPython 3.10.20, the interpreter CI pins lowest,
and again on fastmcp 4.0.5 under CPython 3.11.14 (markdown-vault-mcp's
environment); the first pass also ran on 4.0.0 under CPython 3.14.5. Every
behaviour agreed across all runs. One number did not: the proxy's listing
size, because of the 3.10 `Annotated` loss recorded in
[`mcp-model-facing-text.md`](mcp-model-facing-text.md).

## Scope

- Covers: `BaseSearchTransform` and its `always_visible`, `search_tools`
  and `call_tool` mechanics; what the proxy preserves and what it does not;
  the interaction with `get_tasks()` and the tasks extension (Docket); the
  interaction with pvl-core's instruction finalisation; catalog size on a
  representative family server; CodeMode's status and packaging.
- Does not cover: search relevance tuning beyond one measured example;
  `RegexSearchTransform` and the Jev transform beyond their existence;
  transport-level behaviour; the client side (see
  [`mcp-client-tool-discovery.md`](mcp-client-tool-discovery.md)).
- The probe scripts named below (`probe_a.py` … `probe_f.py`,
  `measure_mvm*.py`) were throwaway scratch files; each claim states what
  the script did. The `get_tasks()` reproduction is attached to the
  upstream issue the ADR files.
- Depended on by: `docs/adr/0004-tool-catalog-mode.md`; the future
  `_catalog.py` the ADR's follow-up issue implements; `_instructions.py`
  (finalisation order) and `_jobs/register.py` (task-capable tools must be
  pinned) once that lands.

## Claims

### What the transform changes on the wire

- With a search transform added, `list_tools()` returns "only: any tools
  listed in `always_visible` (pinned), a search tool that finds tools
  matching a query, a `call_tool` proxy that executes tools discovered via
  search". [source: fastmcp-search-base] [observed: `probe_a3.py`, both
  version pairs: a server with two tools listed `search_tools` and
  `call_tool` alone; with `always_visible=["delete_thing"]` it listed
  `delete_thing, search_tools, call_tool`]
- Search transforms arrived in FastMCP 3.1.0 together with CodeMode.
  [source: fastmcp-3-1-0]
- "The original tools are still callable. They're hidden from the listing
  but remain fully functional — the search transform controls *discovery*,
  not *access*." [source: fastmcp-tool-search] A hidden tool called by name
  through `tools/call` runs. [observed: `probe_a3.py`, both pairs:
  `call_tool("read_thing", ...)` against a listing without `read_thing`
  returned the tool's result]
- The synthetic `search_tools` and `call_tool` carry **no annotations**:
  they are built with `Tool.from_function(fn=..., name=...)` and nothing
  else, so `annotations` is absent on the wire and a client applying the
  protocol defaults reads them as `readOnlyHint: false`,
  `destructiveHint: true`. [source: fastmcp-search-base] [observed:
  `probe_a3.py`, both pairs: both synthetic tools listed with
  `annotations=None`; the derived titles "Search Tools" and "Call Tool"
  and the docstring descriptions were present]
- The two synthetic tools cost about 1.2 kB of listing: 660 bytes for
  `search_tools` and 539 for `call_tool` on CPython 3.11 and 3.14, 491 for
  `call_tool` on CPython 3.10, where the proxy's `arguments` parameter
  loses its `Annotated` description (the 3.10 `get_type_hints` behaviour
  recorded in `mcp-model-facing-text.md`). [observed: `probe_a3.py`, all
  three runs]
- Search results are rendered "in the same JSON format as `list_tools`,
  including the full input schema"; a markdown serialiser
  (`serialize_tools_for_output_markdown`) is available and is documented
  as "~65-70% fewer tokens than JSON". [source: fastmcp-tool-search]
  [source: fastmcp-search-base] Search results keep the hidden tool's
  annotations and its `execution.taskSupport` field. [observed:
  `probe_a3.py` and `probe_b2.py`, both pairs: a result for a read-only
  tool carried `{"readOnlyHint": true, "idempotentHint": true}`; a result
  for a task-optional tool carried `{"taskSupport": "optional"}`]
- Pinned tools "are excluded from search results to avoid duplication".
  [source: fastmcp-tool-search] [observed: `probe_e.py`: `search_tools("delete")`
  returned `[]` when `delete_thing` was pinned]
- The search index is text over "tool names, descriptions, parameter
  names, and parameter descriptions". [source: fastmcp-tool-search] The
  BM25 index is rebuilt when a hash of that text changes, so a newer tool
  version with the same text but a different schema keeps advertising the
  old schema (open issue 5152, on `main` as of 2026-09-24).
  [source: fastmcp-5152]

### What `always_visible` fences, and what it does not

- `always_visible` fences **discovery only**. The `call_tool` proxy checks
  the requested name against `get_tool_catalog(ctx)`, which is the full
  auth-filtered catalog with the transform bypassed, pinned tools included;
  a pinned destructive tool is therefore reachable through the proxy with
  the proxy's (absent) annotations. [source: fastmcp-search-base]
  [source: fastmcp-catalog] [observed: `probe_e.py`, both pairs and both
  protocol eras: with `always_visible=["delete_thing"]`,
  `call_tool(name="delete_thing")` returned "deleted k"]
- The proxy refuses only its own two synthetic names and names absent from
  the catalog. [source: fastmcp-search-base]
- The catalog the proxy and the search see excludes tools the model may not
  see (`is_model_visible`, the MCP Apps `visibility=["app"]` tools) and is
  deduplicated to the highest version. [source: fastmcp-catalog]
- Operator visibility (`mcp.disable(...)`, which is how pvl-core's
  `TOOLS_ALLOW` / `TOOLS_DENY` are applied) and component auth apply before
  the catalog reaches the search and the proxy: "search results respect the
  full auth pipeline: middleware, visibility transforms, and
  component-level auth checks all apply". [source: fastmcp-search-base]
  [observed: `mcp.disable(names={"read_thing"})`, which is what
  pvl-core's `apply_tool_visibility` issues for `TOOLS_DENY`, plus
  `BM25SearchTransform`, fastmcp 4.0.0 / CPython 3.10, both eras: the
  denied tool was absent from `search_tools` results and both the proxy
  and a direct call answered "Unknown tool"] The implementation issue
  pins it with a test.

### What the proxy preserves

- On a legacy (2025-11-25) connection the proxy passes through, unchanged:
  structured content, the tool's `_meta`, an elicitation round-trip made by
  the hidden tool, and pvl-core's job handle returned by a dual-mode tool
  that ran past its soft deadline. [observed: `probe_a3.py` and
  `probe_d.py`, both pairs: `structured_content={"a": 1}` and
  `meta={"pvl": {"x": "y"}}` through the proxy; `ctx.elicit` inside a
  proxied tool answered by the client's elicitation handler; a
  `{"status": "working", "job_id": ...}` handle returned through the proxy
  identically to the direct call]
- On a modern (2026-07-28) connection with a client that declares the
  tasks extension, the proxy of a **task-capable** tool returns an empty
  result (`structured_content={}`, text `{}`) where the direct call returns
  the tool's value. [observed: `probe_d.py`, both pairs: `slow_thing`
  registered through pvl-core's `register_long_running_tool`, pinned, called
  via `call_tool` returned `{}`; called directly returned `{"n": 1}`] The
  mechanism is [unverified]: the likely cause is the tasks extension's
  `tools/call` interceptor treating the inner dispatch as a task submission,
  and reading `fastmcp_tasks/extension.py` against a trace would settle it.
  The consequence does not depend on the cause: a task-capable tool must
  not be reachable through the proxy.
- A task-augmented call of the proxy itself never runs as a task, because
  the proxy is a plain tool. [observed: `probe_b2.py`, both pairs: "Tool
  'call_tool' did not run as a task"]

### What the transform breaks: task registration

- `FastMCP.get_tasks()` applies every server-level transform's
  `list_tools()` to the task-eligible components before returning them.
  [source: fastmcp-server] The tasks extension's lifespan collects
  `server.get_tasks()`, re-filters "by the actual task config" because
  transforms "can inject non-task tools", and registers what is left with
  Docket. [source: fastmcp-tasks-lifespan] The re-filter handles injected
  tools, not removed ones: a search transform removes every hidden tool
  from that list. [observed: `probe_f.py`, both pairs: `get_tasks()`
  returned `["slow_thing"]` before `add_transform(BM25SearchTransform())`
  and `["search_tools", "call_tool"]` after]
- The consequence on a modern connection is that a hidden task-capable
  tool fails **even when called directly**, contradicting "remain fully
  functional": the call raises "Background tasks require a running tasks
  extension (Docket)". Pinning the tool in `always_visible` restores
  registration and both the plain and the native-task call. [observed:
  `probe_b2.py`, both pairs: hidden `slow_thing` failed on plain and on
  task-augmented direct calls; pinned `slow_thing` ran plain
  (`{"n": 1}`) and as a native task (`working`)]
- On a legacy connection the same hidden tool degrades instead of failing:
  the native-task path is gone, and pvl-core's job-store fallback returns a
  handle. [observed: `probe_b2.py`, both pairs: hidden `slow_thing` on a
  legacy connection returned `{"status": "working", ...}`] The fastmcp
  in-memory client did not run a native task on a legacy connection even
  without a transform, so the legacy native path is [unverified] here.

### Interaction with pvl-core's instruction finalisation

- `finalize_instructions` prunes a snippet whose `requires_tools` names a
  tool absent from the effective listing, and the effective listing is
  FastMCP's `list_tools()` with transforms applied. With a search transform
  added before finalisation, every snippet naming a hidden tool is pruned;
  a pinned tool's snippet survives. [observed: `probe_c.py`, both pairs:
  two workflow snippets, both present without the transform; none with the
  transform; only the pinned tool's with `always_visible=["read_thing"]`]
  [unverified] as a pvl-core contract until the ADR's implementation
  issue adds the test that the transform is applied after finalisation.
- `transform_tools()` receives no context argument, but FastMCP enters a
  `Context` around `list_tools()`, so the request's session is readable
  from `fastmcp.server.context._current_context` inside the hook; outside a
  request (finalisation, `get_tasks()` at lifespan) the context exists but
  `ctx.session` raises. [source: fastmcp-server] [source: fastmcp-catalog]
  [observed: `probe_a.py`, fastmcp 4.0.0: inside `transform_tools` during a
  client `list_tools`, `ctx.session.client_params.client_info` read
  `probe-client 9.9` on both protocol eras; during the no-request listing
  `ctx.session` raised "session is not available"]

### Measured on a family server

- markdown-vault-mcp (read-write, OKF on, fastmcp 4.0.5, pvl-core 9.0.0),
  listed through a FastMCP client: 48 tools, 96,925 bytes of tool JSON
  (about 24,000 tokens at four bytes per token), 34 of them `readOnlyHint:
  true`, three task-capable (`build_embeddings`, `reindex`, `summarize`),
  1,522 bytes of instructions. [observed: `measure_mvm2.py` over the
  fixture in `tests/test_client_surface_budget.py`] [source: mvm-budget]
- With every tool that is not read-only, every task-capable tool, and
  `get_server_info` and `get_job_result` pinned (17 tools) and the 31
  read-only tools hidden, the listing is 19 tools and 38,255 bytes, a 61%
  reduction; with nothing pinned it is 1,199 bytes (1,151 on CPython
  3.10, per the `call_tool` size above). [observed: `measure_mvm2.py`,
  `measure_mvm.py`, CPython 3.11 / fastmcp 4.0.5]
- One BM25 search returns five results of 8–13 kB as JSON and about 3 kB
  as markdown. Relevance is uneven: "search notes by keyword" ranked
  `search` first, but "find notes about a topic" did not return `search`
  at all. [observed: `measure_mvm2.py`]

### Prior art: annotation-split proxies

- ha-mcp subclasses `BM25SearchTransform` to emit separate
  `call_read_tool`, `call_write_tool` and `call_delete_tool` proxies,
  "each proxy carries its own MCP annotations so clients can apply
  appropriate permission policies (e.g., auto-approve reads, gate writes)",
  categorising tools by their existing `readOnlyHint` / `destructiveHint`
  and by name patterns, and pins a default set. [source: ha-mcp-categorized]
  Issues 4414 and 4418 were filed against that server's proxies, not
  FastMCP's built-in pair. [source: fastmcp-4414] [source: fastmcp-4418]

### CodeMode

- "CodeMode requires the `code-mode` extra for sandbox support"; the
  default `MontySandboxProvider` applies `max_duration_secs=30`,
  `max_memory=100_000_000`, and `max_tool_calls` of 50 per `execute`.
  "CodeMode is experimental. The core interface is stable, but the specific
  discovery tools and their parameters may evolve." [source: fastmcp-code-mode]
- The module imports without the extra, but `pydantic_monty` is absent from
  this repository's lock, so the sandbox cannot run here. [observed:
  `import fastmcp.experimental.transforms.code_mode` succeeded and
  `import pydantic_monty` failed, fastmcp 4.0.0 venv]
- There is no supported way to keep some tools direct: issue 4925 is open,
  and its proposal (`CodeMode(direct_tool_names=...)`, selecting by name
  so all versions stay direct and the discovery catalog excludes them) is
  a contributor's, not a maintainer commitment. [source: fastmcp-4925]

## Where pvl-core departs from the subject

- FastMCP's proxy reaches every catalog tool, pinned or not, and carries no
  annotations. ADR 0004 decides that pvl-core's catalog mode owns its own
  proxy, fenced to the hidden read-only set and annotated `readOnlyHint:
  true`, and never uses the built-in `call_tool` as shipped.
- FastMCP applies transforms inside `get_tasks()`. ADR 0004 decides that
  every task-capable tool is pinned unconditionally, so the transform never
  sees one to hide.

## Not covered

- Elicitation through the proxy on a modern (2026-07-28) connection: the
  fastmcp in-memory client rejected `ctx.elicit` on that era for the
  direct call too ("elicitation via server-initiated requests is
  unavailable on 2026-07-28 connections"), so the probe says nothing about
  the proxy. A modern client with MRTR-based elicitation would settle it.
- `RegexSearchTransform` and the experimental Jev transform: only BM25 was
  probed.
- Search relevance across a full family vocabulary; one server and four
  queries were measured.
