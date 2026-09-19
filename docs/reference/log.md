---
type: Log
title: Research log
description: One entry per research pass, newest first.
---

# Research log

## 2026-09-19

Researched what a running FastMCP native task can tell its client, to settle
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
