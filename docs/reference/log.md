---
type: Log
title: Research log
description: One entry per research pass, newest first.
---

# Research log

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
