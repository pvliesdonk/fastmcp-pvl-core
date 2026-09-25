---
type: Log
title: Research log
description: One entry per research pass, newest first.
---

# Research log

## 2026-09-25

Sixth pass, one day after the fifth: FastMCP answered both upstream
issues. Re-ran the search-transform probes on `main` at edc991e (PR
5262 merged, closes fastmcp#5261) and on the PR 5263 branch at cf970fb
(closes fastmcp#5260), CPython 3.11.14.

- `fastmcp-search-transform.md`: `get_tasks()` now keeps hidden
  components of a catalog transform; a hidden task-capable tool runs
  plain and as a native task on `main`. On the 5263 branch the proxy
  refuses pinned names and carries least-permissive hints computed before
  the enabled filter, so a disabled write tool must be pinned by name to
  keep the proxy read-only. The empty proxy result for a task-capable
  tool on a modern connection reproduces on `main` without pvl-core and
  is filed as fastmcp#5267. Every "through v4.0.9" qualifier dates from
  this pass.
- ADR 0004 amended (§2.5): pvl-core no longer owns a proxy subclass; the
  mode depends on the FastMCP release carrying both PRs, with a pin set
  computed over registered tools.

## 2026-09-24

Fifth pass, the study for #300 (server-side tool discovery for eager
clients), on fastmcp 4.0.0 / CPython 3.10.20 (also 3.14.5) and fastmcp
4.0.5 / CPython 3.11.14. Two new pages.

- `fastmcp-search-transform.md`: probed `BM25SearchTransform` end to end.
  `always_visible` fences discovery only: the built-in `call_tool` proxy
  reaches pinned destructive tools and carries no annotations.
  `get_tasks()` applies transforms, so hidden task-capable tools are never
  registered with Docket and fail on direct calls on a modern connection;
  pinning restores them. Structured content, `_meta`, elicitation and the
  job handle pass through the proxy on a legacy connection; a proxied
  task-capable tool returns `{}` on a modern one. `finalize_instructions`
  prunes snippets for hidden tools when the transform precedes it.
  Measured markdown-vault-mcp: 97 kB direct, 38 kB with the policy pin
  set, 1.2 kB with nothing pinned (48 bytes less on 3.10, where the
  proxy's `Annotated` parameter description is lost). A denied tool is
  absent from search and refused by the proxy. CodeMode: experimental,
  extra not in the lock, no direct passthrough (fastmcp#4925).
- `mcp-client-tool-discovery.md`: drove one logging stdio server with
  Claude Code 2.1.280, OpenCode 1.18.18 and Codex CLI 0.154.0. All three
  speak a legacy version and send `clientInfo` (`claude-code`, `opencode`,
  `codex-mcp-client`); none declares a capability for deferring tool
  definitions, so eager vs. deferred is undetectable. Claude Code with
  tool search needed one MCP call against a search catalog and skipped
  `search_tools` when the instructions named the tool. The 2026-07-28
  spec makes `clientInfo` optional and says not to change behaviour on
  it. `claude-ai/0.1.0` for Desktop and claude.ai is left `[unverified]`.

## 2026-09-23

Fourth pass, while applying the `writing-model-facing-text` skill to the
tools pvl-core registers (#358), on fastmcp 4.0.0.

- New claim: on CPython 3.10 a parameter defaulting to `None` drops an
  `Annotated` string description and nests a `Field` one where a client
  does not read it; a docstring `Args:` entry lands on the property on
  every version, including alongside an explicit `description=`. First
  attributed to fastmcp 4.0.0; a probe across fastmcp 4.0.0–4.0.5 and
  CPython 3.10–3.13 showed the interpreter decides it, and the Python
  docs record the `get_type_hints` change in 3.11. pvl-core supports
  3.10, so its tools take their parameter descriptions from `Args:`.
- Pinned the docstring-section and parameter-description claims to
  `tests/test_model_facing_text.py`, which lists every tool pvl-core
  registers through a client; the "not pinned" line under "Not covered"
  is gone.

Third pass, a port rather than fresh research. Brought in
`mcp-model-facing-text.md` from `fastmcp-server-template` (PR 651), where
it was researched the same day against the MCP 2026-07-28 and 2025-11-25
schemas, SEP-2640, the Claude, OpenAI, Gemini, VS Code and Cursor
documentation, and fastmcp 4.0.5 by probe. The knowledge applies to the
tools pvl-core registers on every downstream, so it is recorded here too.

- Re-ran every FastMCP observation on the versions this repository locks
  (fastmcp 4.0.0, mcp 2.1.1, CPython 3.10): the Args-less docstring leak,
  the unparsed resource docstring, the prompt JSON-schema sentence
  (including under postponed annotations), the dropped resource
  `readOnlyHint`, instructions on legacy and modern sessions, and the
  skills provider's listing and missing extension capability. All matched
  4.0.5.
- Removed the template page's section on pvl-core's instructions builder:
  in this bundle that is pvl-core's own behaviour, recorded under
  "Where this project departs" instead. The template's test pins were
  dropped; the 2,048-unit claim is pinned to `tests/test_instructions.py`,
  and the rest wait for the change that applies the
  `writing-model-facing-text` skill to pvl-core's own descriptions.
- Not split: the page is about 30 KB against the 25 KB guidance, as in the
  template. The FastMCP-mapping facet is the natural second page.

## 2026-09-19

Second pass. Researched who decides whether a `tools/call` runs as a task, to settle
whether the jobs fallback can ever have a version-based retirement (#346).

- New page: `mcp-task-routing-is-requestor-driven.md`, against MCP
  2025-11-25, fastmcp-tasks 4.0.0 and pydocket 0.24.1.
- Outcome: the decision is the requestor's and is made before the tool body
  runs; the specification defines no operation that turns an in-flight
  foreground call into a task, and `fastmcp_tasks` creates tasks from its
  tool-call interceptor only. The fallback therefore retires per deployment,
  by observation, never on a release.
- Also recorded: Docket announces every native execution at INFO on
  `docket.worker`, which is the native half of that observation; and the
  opt-in is a session-level advertisement that FastMCP's own `Client` makes
  automatically, so a fastmcp-based probe never reaches the fallback (found
  when a first client-level test hung on a call that had silently become a
  task).

First pass. Researched what a running FastMCP native task can tell its client, to settle
whether `Jobs.defer` can hold to `run_with_deadline`'s every-mode guarantee
without losing the client-visible deferral reason (#324).

- New page: `fastmcp-native-task-signals.md`, against fastmcp 4.0.0 /
  fastmcp-tasks 4.0.0.
- Outcome: the deferral **reason** has a native channel
  (`report_progress(message=...)` → `statusMessage`, status stays `working`);
  a dynamic **retry interval** does not (`pollIntervalMs` is fixed at
  submission from static tool config, and only `creation.py` writes it).
- Both halves reproduced against an in-process `memory://` backend rather than
  inferred: the field assembly is split across submission, Redis and the
  `tasks/get` handler, so no single file settles what a client sees.
