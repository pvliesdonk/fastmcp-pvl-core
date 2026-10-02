---
type: Reference
title: Where a request's protocol revision and client identity come from
description: How the MCP Python SDK and FastMCP expose the negotiated protocol revision and the client's name and version per request, in the handshake era and the 2026-07-28 era.
subject_version: "mcp 2.2.0 / mcp-types 2.2.0 / fastmcp 4.0.10"
valid_for: "MCP 2026-07-28 (handshake era up to 2025-11-25); FastMCP 4.x; mcp 2.x"
generated:
  by: process:researching-references
  at: 2026-10-02
stale_after: 2027-04-02
status: stable
verified:
  - by: process:researching-references
    at: 2026-10-02
sources:
  - id: sdk-session
    title: "mcp.server.session.ServerSession — client_params, protocol_version"
    resource: file:///.venv/lib/python3.14/site-packages/mcp/server/session.py
    accessed: 2026-10-02
  - id: sdk-connection
    title: "mcp.server.connection.Connection — from_envelope, for_loop"
    resource: file:///.venv/lib/python3.14/site-packages/mcp/server/connection.py
    accessed: 2026-10-02
  - id: sdk-runner
    title: "mcp.server.runner — initialize negotiation, server/discover"
    resource: file:///.venv/lib/python3.14/site-packages/mcp/server/runner.py
    accessed: 2026-10-02
  - id: sdk-modern-http
    title: "mcp.server._streamable_http_modern — _meta envelope"
    resource: file:///.venv/lib/python3.14/site-packages/mcp/server/_streamable_http_modern.py
    accessed: 2026-10-02
  - id: sdk-versions
    title: "mcp_types.version — protocol revision registry"
    resource: file:///.venv/lib/python3.14/site-packages/mcp_types/version.py
    accessed: 2026-10-02
  - id: fastmcp-request-context
    title: "fastmcp.server.dependencies.FastMCPRequestContext"
    resource: file:///.venv/lib/python3.14/site-packages/fastmcp/server/dependencies.py
    accessed: 2026-10-02
---

# Where a request's protocol revision and client identity come from

MCP `2026-07-28` replaced the `initialize` handshake with a per-request
envelope: a modern request carries its protocol revision, client
capabilities and, optionally, client info in `_meta`, and there is no
protocol session. Handshake-era clients (`2025-11-25` and earlier) still
negotiate once through `initialize`. A server that wants to say which
revision and client a request came from has to read both shapes. This page
records where the SDK and FastMCP put those values.

## Scope

- Covers: the per-request protocol revision, the client's name and version,
  and the set of revisions the SDK serves, as a FastMCP middleware or tool
  can read them.
- Does not cover: capability negotiation, the era's effect on elicitation or
  log delivery (#349), or HTTP header handling.
- Depended on by: `src/fastmcp_pvl_core/_connection.py`, read by
  `_logging_middleware.py` and `_server_info.py`.

## Claims

### Protocol revision

- `ServerSession.protocol_version` is the connection's revision and is never
  `None`: "Populated at `Connection` construction and overwritten once the
  handshake commits on the loop path". [source: sdk-session] (`session.py:85`)
- FastMCP copies it onto `FastMCPRequestContext.protocol_version`, reachable
  as `ctx.request_context.protocol_version`. [source: fastmcp-request-context]
  (`dependencies.py:81`)
- While `initialize` itself is in flight the value is a seed, not the
  negotiated revision: `Connection.for_loop` starts from a hint or
  `LATEST_HANDSHAKE_VERSION`. [source: sdk-connection] (`connection.py:300-316`)
  pvl-core's request log therefore omits the revision on `initialize`.
- `initialize` negotiates the client's requested revision when it is a
  handshake revision, and otherwise answers `LATEST_HANDSHAKE_VERSION`.
  [source: sdk-runner] (`runner.py:420-426`)
- `server/discover` advertises `MODERN_PROTOCOL_VERSIONS` only.
  [source: sdk-runner] (`runner.py:524-531`) The revisions the SDK serves are
  the union of `HANDSHAKE_PROTOCOL_VERSIONS` and `MODERN_PROTOCOL_VERSIONS`,
  both re-exported as `mcp.types.version`. [source: sdk-versions]

### Client identity

- `ServerSession.client_params` holds the `initialize` params on a
  handshake-era connection. [source: sdk-session] (`session.py:65`)
- On a modern connection, `Connection.from_envelope` synthesizes
  `client_params` only when the envelope carried both client info and
  capabilities; client info is optional per the spec, so a modern client may
  leave `client_params` at `None`. [source: sdk-connection]
  (`connection.py:260-298`, the test is at `:288`)
- The raw client info stays in the request's `_meta` under
  `io.modelcontextprotocol/clientInfo` when the client sent it.
  [source: sdk-modern-http] (`_streamable_http_modern.py:282-284`) FastMCP
  lifts `_meta` unparsed onto `FastMCPRequestContext.meta`.
  [source: fastmcp-request-context] (`dependencies.py:78`)
- Therefore the client's name and version are read from `client_params`
  first and from `_meta` second, and may be absent on a modern request.

### Reachability from middleware

- `MiddlewareContext.fastmcp_context.request_context` is set for every
  inbound message type, notifications included, in both eras; on
  `initialize`, `client_params` is still `None`.
  [observed: a middleware overriding `on_message`, an in-process
  `fastmcp.Client` with `mode="legacy"`, `mode="2026-07-28"` and
  `mode="auto"`, and `client_info=Implementation(name="probe-client",
  version="9.9")`. Legacy: `initialize` saw `2025-11-25` and no client;
  `notifications/initialized`, `tools/list`, `tools/call` saw `2025-11-25`
  and `probe-client 9.9`. Modern and auto: `server/discover` (auto only),
  `tools/list`, `tools/call` saw `2026-07-28`, `probe-client 9.9`, and the
  same pair in `_meta`. fastmcp 4.0.10, mcp 2.2.0, CPython 3.14.]
  [pins: tests/test_logging_middleware.py::test_started_line_carries_protocol_and_client]
- A tool invoked in-process through `FastMCP.call_tool` gets a context whose
  `request_context` is `None`. [observed: same environment.]
  [pins: tests/test_server_info.py::TestRegisterServerInfoTool::test_required_fields_only]
