---
type: Log
title: Research log
description: One entry per research pass, newest first.
---

# Research log

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
  `docket.worker`, which is the native half of that observation.

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
