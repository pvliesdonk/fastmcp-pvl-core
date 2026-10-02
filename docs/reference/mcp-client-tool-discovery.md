---
type: Reference
title: How MCP clients discover tools and identify themselves
description: Whether a server can tell an eager client from one that defers tool definitions, what each client sends as clientInfo, and what the specification allows a server to do with that.
subject_version: "MCP 2026-07-28 and 2025-11-25; Claude Code 2.1.280; OpenCode 1.18.18; Codex CLI 0.154.0; mcp 2.1.1"
valid_for: "MCP 2026-07-28 / 2025-11-25; client behaviour as of 2026-09"
generated:
  by: process:researching-references
  at: 2026-09-24
stale_after: 2027-03-24
verified:
  - by: process:researching-references-refute
    at: 2026-09-24
status: stable
sources:
  - id: mcp-basic
    title: MCP specification 2026-07-28, Overview (statelessness, _meta per-request protocol fields)
    resource: https://modelcontextprotocol.io/specification/2026-07-28/basic/index
    accessed: 2026-09-24
  - id: mcp-versioning
    title: MCP specification 2026-07-28, Versioning and Compatibility
    resource: https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
    accessed: 2026-09-24
  - id: mcp-lifecycle-legacy
    title: MCP specification 2025-11-25, Lifecycle
    resource: https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
    accessed: 2026-09-24
  - id: claude-code-mcp
    title: Claude Code docs, Connect Claude Code to tools via MCP
    resource: https://code.claude.com/docs/en/mcp
    accessed: 2026-09-24
  - id: claude-code-41836
    title: anthropics/claude-code issue 41836, no session identifier sent to MCP servers
    resource: https://github.com/anthropics/claude-code/issues/41836
    accessed: 2026-09-24
  - id: opencode-mcp
    title: OpenCode docs, MCP servers
    resource: https://opencode.ai/docs/mcp-servers/
    accessed: 2026-09-24
  - id: opencode-client
    title: OpenCode source, packages/opencode/src/mcp/index.ts
    resource: https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/mcp/index.ts
    accessed: 2026-09-24
  - id: opencode-codemode
    title: OpenCode source, packages/codemode/codemode.md (CodeMode design and status)
    resource: https://github.com/anomalyco/opencode/blob/dev/packages/codemode/codemode.md
    accessed: 2026-09-24
  - id: opencode-registry
    title: OpenCode source, packages/opencode/src/tool/registry.ts
    resource: https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/registry.ts
    accessed: 2026-09-24
  - id: mcp-sdk-session
    title: MCP Python SDK, mcp/server/session.py (ServerSession.client_params, client_capabilities)
    resource: https://github.com/modelcontextprotocol/python-sdk/blob/main/src/mcp/server/session.py
    accessed: 2026-09-24
---

# How MCP clients discover tools and identify themselves

Issue #300 asks whether pvl-core can tell, from the server side, that a
connected client is about to send every tool description to its model, so
that a server-side search catalog could switch on only for such clients.
This page separates that into two questions with different answers: whether
*deferral* is observable (it is not) and whether *identity* is observable
(it is, with limits the specification sets). The evidence is one throwaway
stdio server that logged every request's session data, driven by the three
coding agents installed on the research machine, plus the specification
text. The Claude Code tool-search claims that already live in
[`mcp-model-facing-text.md`](mcp-model-facing-text.md) are linked, not
repeated.

## Scope

- Covers: what Claude Code, OpenCode and Codex CLI send at connect
  (protocol version, `clientInfo`, capabilities, the listing calls); what
  the two protocol eras say a server may learn about a client and may do
  with it; how the Python SDK exposes it; how Claude Code behaves against a
  server whose catalog is a search pair.
- Does not cover: Claude Desktop, claude.ai, VS Code, Cursor or Gemini CLI
  (not run; one secondary claim is marked); OpenCode with its experimental
  code mode switched on; HTTP-transport headers.
- Depended on by: `docs/adr/0004-tool-catalog-mode.md` (the decision not
  to switch catalog mode on client identity); the future `_catalog.py`.

## Claims

### Deferral is not observable from the server

- Claude Code with tool search active sent the same `initialize`,
  `tools/list`, `prompts/list` and `resources/list` at connect as the two
  eager clients, and no capability that names deferral. [observed:
  `idserver.py` (a FastMCP 4.0.0 stdio server logging
  `ctx.session.client_params` and `client_capabilities` from a middleware)
  driven by `claude -p --mcp-config ... --strict-mcp-config`, Claude Code
  2.1.280, 2026-09-24; the same binary and flags, traced in the
  search-catalog run below, used the client-local `ToolSearch`, so tool
  search was active]
- Neither era defines a client capability for "I defer tool definitions":
  the 2025-11-25 client capability table lists `roots`, `sampling`,
  `elicitation`, `tasks` and `experimental` [source: mcp-lifecycle-legacy];
  the capability sets observed were exactly subsets of those (Claude Code:
  `elicitation`, `roots.listChanged`; OpenCode: `roots`; Codex:
  `elicitation.form`, `elicitation.url`, `experimental."codex/auth-change"`).
  [observed: the same logs]
- Claude Code keeps a discovery cache of a server's tool list across
  sessions: "Claude Code loaded the server's tool list from its discovery
  cache, saved in a previous session, instead of connecting at startup, and
  Claude Code connects the server the first time Claude calls one of the
  server's tools." [source: claude-code-mcp] A server whose catalog shape
  changed between sessions under the same configured name therefore meets
  a client that still holds the old shape. Which shape the model then sees
  is [unverified]: the study's cached-name run was not traced, and its
  server log was indistinguishable from the fresh-name run; a traced run
  under a reused name would settle it.
- Against a search-catalog server Claude Code needed **one** MCP call: the
  model loaded the proxy's definition with its local `ToolSearch`, then
  called `call_tool` with the hidden tool's name taken from the server
  instructions, without ever calling `search_tools`. [observed: the
  `--output-format stream-json --verbose` transcript of the fresh-name run:
  `ToolSearch {"query": "select:mcp__idprobe3__call_tool"}` then
  `mcp__idprobe3__call_tool {"name": "probe_read", ...}`; the server log's
  further `tools/list` and `tools/call probe_read` lines were the proxy's
  internal dispatch through the logging middleware, not client requests]

### What each client sends as identity

- Claude Code 2.1.280: `clientInfo` `{"name": "claude-code", "title":
  "Claude Code", "version": "2.1.280", "description": "Anthropic's agentic
  coding tool", "websiteUrl": "https://claude.com/claude-code"}`, protocol
  `2025-11-25`, via a legacy `initialize`. [observed: `idserver.py` log]
- OpenCode 1.18.18: `clientInfo` `{"name": "opencode", "version":
  "1.18.18"}`, protocol `2025-11-25`, via `initialize`; the source
  constructs `new Client({ name: "opencode", version: InstallationVersion
  })`. [observed: `idserver.py` log driven by `opencode run` with
  `OPENCODE_CONFIG` pointing at a config that registers the server; the
  model turn itself failed on an expired provider token after the listing]
  [source: opencode-client]
- Codex CLI 0.154.0: `clientInfo` `{"name": "codex-mcp-client", "title":
  "Codex", "version": "0.154.0"}`, protocol `2025-06-18`, via
  `initialize`. [observed: `idserver.py` log driven by `codex exec -c
  mcp_servers...`; the model turn failed on account limits after the
  listing]
- All three spoke a legacy version to a server that supports 2026-07-28,
  so today `clientInfo` reaches a FastMCP server through `initialize` and
  is complete. [observed: the three logs]
- Claude Desktop and claude.ai report `{"name": "claude-ai", "version":
  "0.1.0"}` [unverified]: issue 41836 states it for "Claude Code, Claude
  Desktop, or claude.ai" over HTTP, but the Claude Code value observed
  above differs, so the claim is at most about the other two; one logged
  connection from either would settle it. [source: claude-code-41836]

### What OpenCode does with a catalog

- OpenCode's documentation warns that "MCP servers add to your context, so
  you want to be careful with which ones you enable" and that some servers
  "tend to add a lot of tokens and can easily exceed the context limit";
  the only controls it documents are per-server `enabled` and a `timeout`
  for "fetching tools from the MCP server". It documents no deferred
  loading. [source: opencode-mcp]
- OpenCode carries an experimental client-side CodeMode: MCP tools
  "register as grouped tools and are deferred while CodeMode is enabled",
  the model sees "a token-budgeted catalog" plus a `$codemode.search`
  tool, and "Direct Core tools remain direct". [source: opencode-codemode]
  It is gated by an `experimentalCodeMode` runtime flag.
  [source: opencode-registry] With the flag off, which is the default,
  OpenCode is an eager client.

### What the specification lets a server do with identity

- Legacy (2025-11-25 and earlier): the `initialize` request "MUST" contain
  the protocol version, client capabilities and "client implementation
  information"; the server "MUST respond with its own capabilities and
  information". [source: mcp-lifecycle-legacy]
- Modern (2026-07-28 and later): "there is no negotiation handshake"; every
  request carries `io.modelcontextprotocol/protocolVersion` (required),
  `io.modelcontextprotocol/clientCapabilities` (required) and
  `io.modelcontextprotocol/clientInfo` (optional) in `_meta`. "Clients
  SHOULD include `io.modelcontextprotocol/clientInfo` on every request
  unless specifically configured not to do so." [source: mcp-basic]
  [source: mcp-versioning]
- The modern spec says of `clientInfo` and `serverInfo`: they "are
  self-reported by the sender and are not verified by the protocol. They
  are intended for display, logging, and debugging. Implementations SHOULD
  NOT use them to change the behavior of the client or server, and SHOULD
  NOT rely on them for security decisions." [source: mcp-basic]
- The modern spec's statelessness rule: "Servers MUST NOT rely on prior
  requests over the same connection to establish context (e.g.,
  capabilities, protocol version, client identity). Every request supplies
  this metadata in its `_meta` field." [source: mcp-basic]
- A dual-era server "selects its behavior from how the client opens": an
  `initialize` request selects legacy semantics for the process or
  session; a request carrying modern `_meta` is served statelessly.
  [source: mcp-versioning]

### How the SDK and FastMCP expose it

- mcp 2.1.1 `ServerSession.client_params` is "the client's `initialize`
  request params; `None` when no client info was supplied", and
  `client_capabilities` is preferred because "on 2026-07-28+ the request
  envelope declares capabilities while client info stays optional, so
  capabilities can be present without `client_params`". [source:
  mcp-sdk-session] [observed: the installed `mcp/server/session.py`, mcp
  2.1.1]
- FastMCP's `Context.session` returns that session, and the context is
  live inside a transform's `transform_tools()` during a client's
  `tools/list`, on both eras. [observed: `probe_a.py`, fastmcp 4.0.0; the
  claim and its evidence are recorded under "Interaction with pvl-core's
  instruction finalisation" in
  [`fastmcp-search-transform.md`](fastmcp-search-transform.md)] A
  per-client catalog is therefore mechanically possible.

## Where pvl-core departs from the subject

- Nothing here is departed from. ADR 0004 follows the modern spec's
  "SHOULD NOT use them to change the behavior" and makes catalog mode an
  operator setting rather than an identity switch.

## Not covered

- Identity and connect behaviour of Claude Desktop, claude.ai, VS Code,
  Cursor and Gemini CLI: not run.
- OpenCode with `experimentalCodeMode` on: not run; the design document is
  the only source.
- Whether Claude Code re-fetches `tools/list` when a server sends
  `notifications/tools/list_changed` after its discovery cache was used:
  not probed.
- HTTP-transport request headers (`user-agent`, `MCP-Protocol-Version`):
  only stdio was driven.
