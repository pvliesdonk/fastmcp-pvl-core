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
- [Who decides whether a tool call runs as a task](mcp-task-routing-is-requestor-driven.md)
  — the MCP tasks utility is requestor-driven and FastMCP applies that before
  the tool body runs; nothing promotes a foreground call to a task later. Read
  by `_jobs/manager.py` and by the jobs fallback's retirement criterion. Valid
  for MCP 2025-11-25 / FastMCP 4.x.
- [MCP model-facing text](mcp-model-facing-text.md) — who reads each
  description field, how clients cut and index it, how FastMCP builds it
  from a docstring, and what the vendors say it should carry; the evidence
  behind the `writing-model-facing-text` skill. Read by the modules that
  register tools on a downstream's server (`_server_info.py`,
  `_jobs/register.py`, `_transfer/register.py`). Valid for MCP 2026-07-28
  / FastMCP 4.x.
- [FastMCP tool-search transform and CodeMode](fastmcp-search-transform.md)
  — what the search transforms change on the wire, that `always_visible`
  fenced discovery but not the proxy through v4.0.9 (PR 5263 fences and
  annotates it), that hidden task-capable tools dropped out of Docket
  registration through v4.0.9 (PR 5262 fixes it), what the proxy
  preserves, and catalog sizes measured on markdown-vault-mcp. Read by ADR 0004 and the catalog-mode
  implementation it proposes. Valid for FastMCP 4.x.
- [How MCP clients discover tools and identify themselves](mcp-client-tool-discovery.md)
  — deferral is not observable server-side, what Claude Code, OpenCode and
  Codex send as `clientInfo`, and what the specification lets a server do
  with it. Read by ADR 0004. Valid for MCP 2026-07-28 / 2025-11-25.
