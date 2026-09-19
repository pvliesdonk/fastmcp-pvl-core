---
okf_version: "0.2"
---

# External behaviour references

Pages here record how something **outside** `fastmcp-pvl-core` behaves —
FastMCP internals, the MCP specification, a stdlib quirk — with the primary
sources and the date they were read. They exist so the next agent to meet the
question reads an answer instead of re-deriving one from memory.

A reference is not an ADR and not a spec: `docs/adr/` records what pvl-core
decided and why, `docs/specs/` records what bytes move between servers, and
these pages record what the world does. Where pvl-core departs from the world
deliberately, the claim says so and links the ADR.

Check `stale_after` and `subject_version` before relying on a page. A FastMCP
major bump makes every FastMCP reference stale regardless of its date.

## Pages

- [What a FastMCP native task can tell its client while it runs](fastmcp-native-task-signals.md)
  — which SEP-2663 task fields a running tool can influence, and which are
  fixed at submission. Read by `_jobs/manager.py`. Valid for FastMCP 4.x.
