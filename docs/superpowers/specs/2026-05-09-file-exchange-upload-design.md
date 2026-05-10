# File Exchange — Upload Direction (Design)

**Date:** 2026-05-09
**Status:** design, awaiting plan
**Tracks:** [pvliesdonk/fastmcp-pvl-core#64](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/64), downstream consumer [pvliesdonk/markdown-vault-mcp#443](https://github.com/pvliesdonk/markdown-vault-mcp/issues/443)
**Spec impact:** File Exchange v0.4.0 amendments (this draft folds two new amendments into the pending v0.4.0 set)

## Problem

`register_file_exchange` and `create_download_link` cover the **outbound** half of file transfer between an MCP server and the world: the server mints a one-time HTTPS URL, a client or another server `GET`s the bytes. There is no symmetric **inbound** primitive. When a local agent (claude-code, cursor, a script) wants to push a file *into* a server-managed area, the only options today are:

- Round-trip through MCP context as base64 — context-bound, broken above ~1 MB.
- The agent posts to its own out-of-band endpoint — every server reinvents the same wheel.

The downstream proposal in #64 is non-binding: this design accepts the goal but reshapes the helper to fold cleanly into the existing `file_exchange` module rather than living alongside it as a parallel system.

## Goal

Add a symmetric helper, `register_file_exchange_upload`, that:

1. Mints a one-time HTTPS POST URL via a registered `create_upload_link` tool.
2. Accepts a domain-specific receiver callable that commits the bytes.
3. Validates the target identifier *before* the link is minted, so a misbehaving agent gets a clean tool-error in-band instead of after wasting an upload.
4. Plays nicely with PR #61's authorization (the upload-link-minting tool runs through standard tool-tag authz; the POST route's auth is the unguessable token, mirroring how `/artifacts/{token}` works for downloads).
5. Extends the File Exchange spec rather than living parallel to it — one URI scheme, one capability declaration, one set of conventions for both directions.

## Non-goals

- `exchange://` reverse for co-deployed-server pushes (already works — the URI scheme is direction-agnostic; documented as a non-amendment, not new code).
- Resumable uploads (multi-part). Single-shot POST is v1.
- Per-token Content-Type *override* by the POSTer; v1 trusts the link-creation-time `accepts` filter.
- Rate limiting at the `/uploads/` route. Operators wire that at their reverse proxy.

## Architecture & module layout

Folded into the existing `file_exchange` module rather than added as a sibling. Concrete shape:

| Existing | New / changed |
|---|---|
| `_artifacts.py` (`ArtifactStore`, `ArtifactRecord`) | → `_token_store.py` (`TokenStore[T]` generic, `ArtifactRecord`, `UploadRecord`); `_artifacts.py` becomes a deprecation shim re-exporting for one minor version. |
| `_file_exchange_runtime.py` (download GET route, producer side) | Gains an `UploadHandle`, the POST route, and a streaming/buffered receiver dispatcher. |
| `file_exchange.py` (public facade, `register_file_exchange(...)`) | Gains a sibling registrar `register_file_exchange_upload(...)`. Both registrars cooperate on the capability declaration via a shared module-level `_FileExchangeCapabilityBuilder`. |

Two registrars rather than one combined call. `register_file_exchange` already takes a lot of arguments; collapsing both directions into a single call would balloon a signature that is already at the edge of comfort. The two are independent on the call site, cooperate internally on the capability dict.

## Public API

```python
from fastmcp_pvl_core import register_file_exchange_upload, UploadRecord

handle = register_file_exchange_upload(
    mcp,
    namespace="vault",
    env_prefix="MARKDOWN_VAULT_MCP",
    transport="auto",                       # auto|http|sse|stdio  (stdio = no-op)
    receiver=_my_receiver,                  # buffered: (record, body: bytes) -> dict
    # OR
    stream_receiver=_my_stream_receiver,    # streaming: (record, body: AsyncIterator[bytes]) -> dict
    # exactly one of receiver / stream_receiver MUST be set
    pre_link_validator=_validate_target,    # optional: (target_id, extra) -> None, raises ValueError
    upload_tool_name="create_upload_link",  # override on collision
    tool_tags=frozenset({"write"}),         # configurable so PR #61 authz mapping wires correctly
    accepts=("application/octet-stream",),  # per-route MIME filter; "*/*" disables check
    max_bytes_default=10 * 1024 * 1024,
    ttl_default=300,
    ttl_max=3600,                           # operator ceiling, clamps with disclosure (mirror amendment 7)
    legacy_capability_shape=False,          # opt-in to v0.2 flat shape during migration
)
```

### Receiver contracts

```python
def receiver(record: UploadRecord, body: bytes) -> dict[str, Any]:
    """Commit the uploaded bytes (buffered).

    Args:
        record: token metadata — target_id, content_type, size_bytes, max_bytes, extra dict.
        body: raw bytes from the POST, already validated against record.max_bytes.

    Returns:
        Dict serialised as the HTTP 200 response body. Conventional keys: path, size_bytes, content_type.

    Raises:
        ValueError: invalid input (path traversal, disallowed extension, etc.) — HTTP 400.
        FileExistsError: receiver enforces no-overwrite — HTTP 409.
        Any other exception logs at ERROR and surfaces as HTTP 500.
    """

async def stream_receiver(record: UploadRecord, body: AsyncIterator[bytes]) -> dict[str, Any]:
    """Same contract; bytes arrive chunk-by-chunk."""
```

Buffered is the obvious default for small files. Streaming opt-in for receivers that pipe to disk or to S3 without double-allocating. Mutually exclusive: passing both raises `ValueError` at registration.

### `pre_link_validator`

Runs inside `create_upload_link` *before* `TokenStore.create(...)`. Raising `ValueError` surfaces as a tool-error to the LLM in-band ("invalid path: ..."), so the LLM never wastes an upload round-trip on an invalid target. This replaces the `mcp.disable + re-register` pattern proposed in #64 — that pattern stays available via `handle.create_link(...)` for advanced consumers, but the validator covers the 95% case in one line.

### `UploadHandle`

```python
class UploadHandle:
    namespace: str           # read-only
    tool_name: str           # read-only
    def create_link(
        self,
        target_id: str,
        ttl_seconds: int = 300,
        max_bytes: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> UploadRecord: ...   # bypass-the-tool escape valve for advanced wraps
```

### Tool registered

```python
@mcp.tool(tags=tool_tags)
async def create_upload_link(
    target_id: str,
    ttl_seconds: int = 300,
    max_bytes: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a one-time HTTPS POST URL for pushing bytes into the server.

    Returns:
        upload_url (str): the one-time URL to POST to.
        expires_in_seconds (int): effective TTL after server-side clamping.
        target_id (str): echo of the input.
    """
```

## HTTP route & runtime

`POST /<namespace>/uploads/{token}`. In order:

1. **Lookup** the token in `UploadStore`. Missing → 404. Expired → 410. Already consumed → 404 (do **not** leak that the token *did* exist).
2. **Content-Length check** against `record.max_bytes` → 413 *before* reading any body bytes.
3. **Content-Type filter** against `accepts` (when set) → 415 on mismatch. `*/*` explicitly disables the check.
4. **Atomic consume** — flip the `consumed` flag and pull the record in one operation, *before* the receiver runs. A slow receiver cannot be re-fired by a racing client retransmit.
5. **Read the body** with a per-chunk running total checked against `max_bytes` (defense-in-depth: clients can lie about Content-Length). Streaming receiver gets chunks live; buffered receiver gets the assembled `bytes` once complete. Limit exceeded mid-stream → 413, partial bytes discarded, no receiver invocation.
6. **Dispatch** to receiver. `ValueError` → 400. `FileExistsError` → 409. Any other exception → 500 with full traceback at ERROR.
7. **Return** the receiver's dict as JSON, HTTP 200.

### Security

- **Token-as-bearer is the auth.** `/uploads/{token}` does NOT go through PR #61's authorization middleware. Possession of a fresh, unconsumed token IS the authorization, exactly the way `/artifacts/{token}` works for downloads. The auth gate is `create_upload_link`, which IS a tool subject to tag-based authz.
- **`pre_link_validator` runs at link creation** so an LLM gets a clean tool-error in-band rather than wasting an upload round-trip on an invalid target.
- **Path-traversal validation runs both at link creation and at receiver dispatch** — defense-in-depth, since `target_id` could be marked invalid by the time the link is consumed (rare, but cheap to re-check).
- **No `?` or `#` in `target_id`** — same rule as v0.4.0 amendment 9 for `exchange://` URIs, applied to JSON-RPC `target_id` strings.
- **Rate limiting** is operator concern at the reverse-proxy layer.

## Token store refactor

`_artifacts.py` (346 lines) becomes `_token_store.py`. Lifecycle bits — UUID4 keying, TTL, atomic one-time consume, GC-on-create — live on a generic `TokenStore[T]`. Records are typed payloads.

```python
@dataclass(frozen=True)
class ArtifactRecord:
    token: str
    created_at: datetime
    expires_at: datetime
    consumed: bool
    content_type: str
    data: bytes

@dataclass(frozen=True)
class UploadRecord:
    token: str
    created_at: datetime
    expires_at: datetime
    consumed: bool
    target_id: str
    max_bytes: int
    extra: dict[str, Any]

ArtifactStore = TokenStore[ArtifactRecord]
UploadStore = TokenStore[UploadRecord]
```

`atomic_consume(token) -> T | None` does lookup-and-mark in one critical section. The bytes-or-no-bytes asymmetry (download stores bytes inline; upload reserves a slot, bytes arrive over the wire) lives in the record type — the generic store doesn't care.

`_artifacts.py` keeps existing imports working under a deprecation shim that re-exports from `_token_store.py` for one minor version, then drops.

## Spec extension — File Exchange v0.4.0 amendments

Two new amendments fold into the existing v0.4.0 amendments draft (so a single wire bump from `"0.2"` → `"0.4"` covers all amendments together).

### Amendment 10 — HTTP method gains direction tagging

**Where:** §"Transfer Methods / `http`" + §"Discovery / Capability declaration".

**Status today:** v0.2.5 has `transfer_methods.http: { tool: "create_download_link" }` (or `{ tool: "fetch" }` for consumers). Direction is implicit — only download exists.

**Amendment:** the `http` method nests by direction:

```json
"http": {
  "download": { "tool": "create_download_link" },
  "upload":   { "tool": "create_upload_link", "max_bytes": 10485760, "max_ttl_seconds": 3600 }
}
```

A server may declare either, both, or (for the consumer-of-downloads case) just `download.tool: "fetch"`. The `exchange` method stays direction-agnostic — the URI scheme already works in either direction (an agent writes to its own namespace under `$MCP_EXCHANGE_DIR` and passes the URI to a server tool, mirroring today's download flow). This is documented as a non-amendment.

**Migration:** servers advertising `version: "0.2"` keep the flat shape; `version: "0.4"` uses the nested shape. Implementations SHOULD support reading the flat shape from older peers for one minor version. `register_file_exchange[_upload]` accepts a `legacy_capability_shape: bool = False` flag during the migration window.

**Rationale:** keeps one transfer_methods block (no parallel `intake_methods`) while making both directions explicit. Forces a wire bump, but folds into v0.4.0's existing bump cleanly.

### Amendment 11 — Inbound HTTP transfer (upload)

**Where:** new §"Server Requirements / Server accepting uploads"; addition to §"Transfer Methods / `http`".

**Status today:** v0.2.5 defines no inbound mechanism. `consumes` describes MIME types accepted via `file_ref` pull only.

**Amendment:** a server that supports direct upload:

- MUST register a tool named `create_upload_link` (or whatever name is advertised in `transfer_methods.http.upload.tool`).
- Tool MUST accept `target_id` (opaque to client/consumer), `ttl_seconds`, `max_bytes`, optional `extra` dict; MUST return `{ upload_url, expires_in_seconds, target_id }`.
- MUST expose `POST /<namespace>/uploads/{token}` with the status-code contract documented above.
- MUST consume tokens atomically before dispatching to the receiver (one-time guarantee).
- MAY clamp `ttl_seconds` to a server ceiling and SHOULD return the effective value (mirror of amendment 7).

`consumes` keeps its existing meaning: MIME types the server can ingest, regardless of mechanism. The presence of `transfer_methods.http.upload` advertises that direct upload is one available intake mechanism for those types. An optional **per-method filter** `transfer_methods.http.upload.accepts: [mime types]` MAY tighten the subset for the upload path specifically — for the case where a server consumes broadly via fetch but only accepts a narrower subset via direct upload. Absent → route inherits the full `consumes` list. `*/*` in this filter explicitly disables MIME checking at the route layer.

`target_id` follows the same character rules as `origin_id` and `exchange://` segments (no `/`, `\`, `.`, `..`, control bytes, leading/trailing whitespace, `?`, `#`).

**Rationale:** completes the symmetric story for HTTP transfer. Reuses the existing one-time-token pattern, status-code conventions, and capability-declaration shape.

## Capability merge

When a host calls both `register_file_exchange(...)` and `register_file_exchange_upload(...)`, the capability dict merges:

```json
"file_exchange": {
  "version": "0.4",
  "namespace": "vault-mcp",
  "exchange_id": "hades-01",
  "produces": [],
  "consumes": ["application/pdf", "image/*", "text/markdown"],
  "transfer_methods": {
    "exchange": {},
    "http": {
      "download": { "tool": "create_download_link" },
      "upload":   { "tool": "create_upload_link", "max_bytes": 10485760, "max_ttl_seconds": 3600 }
    }
  }
}
```

A shared module-level `_FileExchangeCapabilityBuilder` accumulates direction-specific contributions; the dict materialises lazily on first `initialize`, by which time both registrars have run. Tests cover download-only, upload-only, both, the legacy flat shape, and the version-string assertion.

## Tests

| File | Status | Coverage |
|---|---|---|
| `tests/test_token_store.py` | new | Generic `TokenStore[T]` lifecycle, parameterised over `ArtifactRecord` and `UploadRecord`. Token creation, expiry, atomic consume, GC-on-create race, double-consume rejection. |
| `tests/test_artifacts.py` | kept, slimmed | Only artifact-payload-specific tests (bytes, content_type) remain. Deprecation-shim re-export verified. |
| `tests/test_file_exchange_upload.py` | new | Buffered receiver, streaming receiver, mutual exclusion of receiver/stream_receiver, `pre_link_validator` raising, exception → status mapping, in-band tool-error case. |
| `tests/test_upload_route.py` | new (integration) | Full HTTP exercise via `httpx.AsyncClient` against the registered ASGI app: 404 missing, 404 already-consumed (no token-existence leak), 410 expired, 413 by Content-Length, 413 by chunk overrun mid-stream (Content-Length lied), 415 MIME mismatch, 415 with `*/*` filter (must NOT 415), 200 happy path, 500 on receiver exception. |
| `tests/test_capability_merge.py` | new | Download-only, upload-only, both, legacy flat-shape, version-string assertion. |
| `tests/test_file_exchange_runtime.py` | existing | Add `exchange://` reverse cases: agent writes to its own namespace, server reads via the same path-resolution code as today's download direction. Documentation/test of already-working behaviour. |
| `tests/integration/test_markdown_vault_sketch.py` | new (optional) | Minimal end-to-end with an in-memory receiver, verifying the helper's docstring example. |

Coverage target: same 95% line / 90% branch the project holds elsewhere. New code does not regress.

## Downstream impact — `pvliesdonk/fastmcp-server-template`

Three issues to file in the template repo (issue numbers will be assigned at filing time and are referenced as `template#A/B/C` here as placeholders). None block this PR; the template adopts asynchronously.

1. **`template#A` — Add upload-receiver scaffolding to `server.py`** — extend the existing `DOMAIN-WIRING-START`/`DOMAIN-WIRING-END` sentinel block with a commented-out `register_file_exchange_upload(...)` call alongside the existing `register_file_exchange(...)`. Include stubbed `_my_upload_receiver(record, body) -> dict[str, Any]` and `_my_pre_link_validator(target_id, extra) -> None` adjacent so a downstream `copier update` reveals the seams. Stays commented by default — fresh-scaffolded server is download-only unless the maintainer opts in.
2. **`template#B` — Document `register_file_exchange_upload` in template README and configuration docs** — README MCP-Tools-table row for `create_upload_link` (commented as opt-in). New configuration-table rows for `<PREFIX>_UPLOAD_ENABLED`, `<PREFIX>_UPLOAD_MAX_BYTES`, `<PREFIX>_UPLOAD_TTL`. Short "Local agent uploads" section in the deployment guide showing the agent-side `curl -X POST <url> --data-binary @file.pdf` mechanic.
3. **`template#C` — Bump fastmcp-pvl-core minimum version** in template's `pyproject.toml` to whatever minor ships this. Mechanical chore; may be folded into `template#B` to keep the template-repo issue list to two if preferred at filing time.

## Open questions / deferred

- `exchange://` reverse for upload is documented as already-working but no new code or tests beyond the new cases in `test_file_exchange_runtime.py`. If a future deployment exposes a corner case, file then.
- Resumable / multi-part uploads (chunked POST or `tus.io`-style) — not in v1. If a downstream needs >100 MB single uploads, file a follow-up.
- Per-token `accepts` override at link creation — not in v1; the route-level filter is enough. If consumer needs fine-grained per-link MIME constraints, add a parameter to `create_upload_link` later.
- Bot reviewers running on the spec-extension PR will want fresh CI fixture servers that exercise both directions; lined up with the implementation plan.

## References

- pvliesdonk/fastmcp-pvl-core#64 — original proposal
- pvliesdonk/fastmcp-pvl-core#61 — authorization submodule (PR), context for tool-tag authz
- pvliesdonk/fastmcp-pvl-core#28, #29 — pending FastMCP `experimental_capabilities` migration; out of scope here but capability merge will need to follow whichever wiring is current at landing time
- pvliesdonk/markdown-vault-mcp#443 — first downstream consumer; wires this helper directly, no local POC
- `docs/specs/file-exchange.md` — current File Exchange specification with v0.4.0 amendments draft (this design adds Amendments 10 and 11 to that draft)
