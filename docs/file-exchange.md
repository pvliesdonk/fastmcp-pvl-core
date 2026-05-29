# File-exchange extension — pvl-core implementation notes

This document describes pvl-core's *implementation* of the
[`mcp-file-exchange-ext`](https://github.com/pvliesdonk/mcp-file-exchange-ext)
v0.1 protocol. The wire spec lives upstream and is vendored as
`src/fastmcp_pvl_core/_file_exchange/_schema/file-exchange.json`. **This
file is not a wire spec** — anything that affects byte-on-the-wire
behaviour belongs upstream.

## Architecture

Four roles, two HTTP routes, one token store, four hook-mounted entry
points.

| Role     | Direction       | What it does                                |
|----------|-----------------|---------------------------------------------|
| Provider | Server → Peer   | Offer an artifact via a tool; response = `TransferHandle` |
| Fetcher  | Peer → Server   | Pull a peer's artifact handed to a fetcher tool |
| Receiver | Server → Peer   | Accept an artifact via a tool; response = `IntakeTicket` |
| Sender   | Peer → Server   | Push a local artifact to a peer's receiver  |

`register_file_exchange(mcp, ...)` mounts the two HTTP routes
(`/fx/d/{token}` GET and `/fx/u/{token}` PUT) and builds the
`CapabilityTokenStore` backed by the KV factory. The per-role helpers
register MCP tools on top.

## Setup walkthrough

```python
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig

mcp = FastMCP("my-server")
config = ServerConfig(
    kv_store_url="memory://",          # or redis://, etc.
    file_exchange_token_ttl=3600.0,
    file_exchange_max_artifact_size=10 * 1024 * 1024,
    file_exchange_allowed_networks=("10.0.0.0/8",),
    file_exchange_http_timeout=30.0,
)

fxctx = file_exchange.register_file_exchange(
    mcp,
    config=config,
    base_url="https://my-server.example",
    source=my_source,    # required if any provider or sender helper used
    sink=my_sink,        # required if any receiver or fetcher helper used
    # volume_map=...,    # optional; required only if you want filesystem
                         # transport support in fetcher/sender. Build via
                         # `load_volume_map(env_prefix="FILE_EXCHANGE_")`
                         # or pass a `Mapping[str, Path]` directly.
)

# Per-role registrations. See the adoption guide for one example per role.
```

## Operator knobs (`ServerConfig.file_exchange_*`)

| Field | Default | What it bounds |
|---|---|---|
| `file_exchange_token_ttl` | `3600.0` | Maximum lifetime of a capability token. Per-mint TTL is clamped to this ceiling. |
| `file_exchange_max_artifact_size` | `None` | Operator cap on body size (bytes); applied alongside per-mint `expected.maxSize`. |
| `file_exchange_allowed_networks` | `()` | CIDR allow-list for outbound fetcher/sender HTTP. Empty tuple denies all outbound — opt-in by design. |
| `file_exchange_http_timeout` | `30.0` | Connect/read/write timeout for outbound HTTP, in seconds. |

## Tasks integration (§14)

`register_file_exchange` adds the server-level capability declaration
`tasks.requests.tools.call = True` to `mcp.experimental_capabilities`
so peers know this server accepts `tools/call` as a task submission.

Every tool the four role helpers register carries the per-tool
annotation `taskSupport="optional"`. pvl-core does **not** itself
schedule tasks — that's FastMCP's responsibility via the `fastmcp[tasks]`
extra (docket). The umbrella helpers only declare the capability and
the per-tool hint; they intentionally do not flip
`FastMCP(tasks=True)` or set `task=True` on individual tools.

## Filesystem transport (optional)

The filesystem transport requires a `VolumeMap` at
`register_file_exchange` time. Without one, a fetcher / sender tool
that selects a filesystem descriptor raises
`FileExchangeTransferError(NO_SUPPORTED_TRANSPORT)` at call time.
Build a `VolumeMap` from environment variables via
`fastmcp_pvl_core._file_exchange._paths.load_volume_map(env_prefix="FILE_EXCHANGE_")`
or pass a `Mapping[str, Path]` directly.

## Error model

All transport-layer failures raise
`fastmcp_pvl_core._file_exchange._errors.FileExchangeTransferError`
with a `code` from the §13 envelope (`TransferErrorCode`). The
umbrella helpers do not wrap or swallow these — they propagate
through the FastMCP tool layer and reach the MCP client as a typed
error response.

The §13 codes pvl-core emits:

- `TRANSFER_FAILED` — generic transport failure (network drop, disk
  full, sink raised).
- `NOT_ACCESSIBLE` — SSRF guard refusal; peer-side authz failure.
- `TOO_LARGE` — body exceeds the operator or per-mint cap.
- `SIZE_MISMATCH` — declared size vs. observed bytes disagree.
- `DIGEST_MISMATCH` — `Content-Digest` did not match the received bytes,
  or `requireDigest` lists a different algorithm than the client sent.
- `NO_SUPPORTED_TRANSPORT` — selection found no descriptor whose
  transport pvl-core supports (or filesystem was selected but no
  `volume_map` was configured).
- `DESCRIPTOR_EXPIRED` — capability URL's `expiresAt` has passed.
- `MIME_TYPE_REJECTED` — receiver's `acceptMimeTypes` does not include
  the upload's Content-Type.
- `UNSUPPORTED_REQUIREMENT` — wire-level `requires` includes a feature
  pvl-core does not implement.

## See also

- `docs/file-exchange-adoption.md` — one worked example per role.
- `docs/superpowers/specs/2026-05-27-file-exchange-146-failure-modes.md`
  — the failure-mode matrix that drove the upload data plane.
- `docs/superpowers/specs/2026-05-28-content-digest-restructure.md` —
  the Content-Digest pipeline architecture.
- Upstream wire spec: <https://github.com/pvliesdonk/mcp-file-exchange-ext>.
