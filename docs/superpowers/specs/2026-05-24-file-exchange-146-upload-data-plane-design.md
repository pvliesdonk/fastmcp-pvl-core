# File-Exchange #146 — upload data plane (route + receiver/sender)

> **Status:** Contemporaneous design record for issue #146 (8/10 of EPIC
> #138). The implementation in the same PR is the source of truth; this
> captures the shape agreed before implementation. This is **not** a wire
> spec — #146 is pvl-core's own implementation of the `upload` transport's
> data plane, governed by `CLAUDE.md` and the project's framing principle,
> not by `mcp-file-exchange-ext`. No `docs/specs/` wire-format file is touched.

**Goal:** Implement the `upload` transport's push data plane, the mirror of the
#145 `download` pull plane: the receiver mints a capability URL backed by the
#144 token store, an HTTPS `PUT`/`POST` route on the receiver's own server
accepts the artifact bytes and deposits them through the #142 `ArtifactSink`, and
the sender pushes them through the #147 SSRF guard with a `Content-Digest`.
Everything streams — no artifact is buffered whole in memory.

## Scope (from #138's decomposition + #146's scope statement)

#146 owns the three `upload` role primitives and the serving route:
`upload_receiver_mint`, the upload `PUT`/`POST` route, and
`upload_sender_consume`. It builds on three merged dependencies:
`CapabilityTokenStore` (#144), `guarded_stream` (#147 — which already accepts a
request `content` body), and the `ArtifactSource`/`ArtifactSink` hooks (#142). It
does **not** build the top-level `register_file_exchange_*` umbrella, the shared
token-store construction, or Tasks integration — those are #148. The primitives
take their dependencies (token store / sink / source / config) as parameters;
#148 constructs and threads them.

## File structure

The EPIC's pattern is one module per transport (`_filesystem.py`, `_download.py`),
so upload gets its own module, and the cross-transport route registrar moves to a
neutral home:

- **New `_upload.py`** — `upload_receiver_mint`, `upload_sender_consume`, the
  upload route registrar (`register_upload_route`, internal), and the
  upload-specific HTTP helpers (`Content-Digest` RFC 9530 parse/format,
  `acceptMimeTypes` RFC 7231 media-range matching).
- **New `_routes.py`** — `register_file_exchange_routes` moves here from
  `_download.py` (it is cross-transport, not download-specific). It calls
  `register_download_route` (extracted from `_download.py`) and
  `register_upload_route` (from `_upload.py`). `_download` and `_upload` stay
  independent leaf modules — no circular imports.
- **New `_staging.py`** — the digest/chunk primitives shared by both transports:
  `_HASHLIB_BY_LABEL`, `_digest_verifier`, `_write_chunk`, `_CHUNK`, and a
  temp-file staging helper that writes an async byte stream to a `mkstemp` temp
  with hashing + a size bound, **preserving the `OSError → transfer-failed`
  mapping / cleanup-suppression contract established in #145** (the download
  fetcher's hard-won temp-IO error sweep must not be re-derived divergently in
  upload). The download fetcher's `Range`-resume loop stays in `_download.py`
  (download-specific); only the per-chunk write/hash/verify primitives are
  shared. The exact split of "shared helper vs. per-transport loop" is the
  plan's call; the contract (every temp-file op maps to `transfer-failed` or is
  suppressed for cleanup) is not.

This refactors merged #145 code (moving `register_file_exchange_routes`,
extracting `_staging.py`). The re-export surface is updated to import
`register_file_exchange_routes` from `_routes` (the public name is unchanged).

## register_file_exchange_routes shape

```python
def register_file_exchange_routes(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    source: ArtifactSource | None = None,
    sink: ArtifactSink | None = None,
    config: ServerConfig | None = None,
) -> None: ...
```

Mounts the download `GET` route iff `source` is given, and the upload
`PUT`/`POST` route iff `sink` is given — supporting download-only, upload-only,
or both. This makes #145's `source` parameter optional (it was required); safe
because no downstream has adopted the hooks yet (#148 threads them).
`config` carries the operator size cap (`file_exchange_max_artifact_size`) the
upload route needs to bound untrusted request bodies (§15); the download route
ignores it. **Mounting the upload route requires `config`** — `sink` given
without `config` raises `ValueError` at registration, since an upload route with
no operator cap could accept an unbounded body. `UPLOAD_PREFIX` is a pvl-core
route-shape constant (e.g. `/fx/u`), not a kwarg, not exported — mirroring
`DOWNLOAD_PREFIX`.

## Components (mirroring #145's provider/fetcher)

### `upload_receiver_mint`

```python
async def upload_receiver_mint(
    artifact_id: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    expected: ArtifactConstraints | None = None,
    method: Literal["PUT", "POST"] = "PUT",
) -> IntakeTicket: ...
```

- `minted = await token_store.mint({"artifact_id": artifact_id, "expected":
  expected.model_dump() if expected else None}, ttl=ttl, single_use=True)`. The
  token stores the `artifact_id` and the `expected` constraints so the route can
  correlate received bytes and enforce limits; the token store treats the
  metadata as opaque (#144).
- `url = capability_url(base_url, UPLOAD_PREFIX, minted.token)`.
- Returns `IntakeTicket(type=TICKET_TYPE, version=SPEC_VERSION,
  artifactId=artifact_id, expected=expected, sinks=[UploadSink(transport="upload",
  url=url, method=method, expiresAt=minted.expires_at)])`.

Minting only — no hook is called, no bytes move (mirrors
`filesystem_receiver_mint`). The receiver's `ArtifactSink` is threaded into the
route (not into mint), since the bytes arrive at the route, not at mint time.

### The upload route (`register_upload_route` → `PUT`/`POST <UPLOAD_PREFIX>/{token}`)

Mounted via `@mcp.custom_route`. Handler (Starlette `Request` → `Response`):

1. `rec = await token_store.lookup(token)`; if `None` → **`404`** (uniform for
   unknown / expired / already-consumed — leaks no token state).
2. Read `artifact_id` and `expected` (re-hydrated to `ArtifactConstraints`) from
   `rec.metadata`.
3. **`acceptMimeTypes` (RFC 7231 §3.1.1.1 media-range):** if `expected.acceptMimeTypes`
   is set, match the request `Content-Type` (media type, parameters ignored)
   against the list (`type/*` matches a subtype, `*/*` matches anything). No
   match → **`415`** (no token consumed).
4. **Stream the request body to a transient temp file** (`request.stream()` →
   temp, hashing + counting via the shared staging helper). Enforce the byte
   bound — reject with **`413`** (no consume) when the received count exceeds
   either `expected.maxSize` or `config.file_exchange_max_artifact_size` (each
   enforced when set), bounding consumption to the smaller cap + one chunk.
5. **Verify-before-use:** after the body completes, verify `Content-Digest`
   (RFC 9530) against the received bytes **if the header is present**; if
   `expected.requireDigest` is set, a **missing** or **mismatched** digest →
   **`400`** (`digest-mismatch`, §13). Uploaded bytes are untrusted, so this
   happens **before** the sink sees them.
6. Only on a clean verify, construct `meta = ArtifactMetadata(mimeType=<request
   Content-Type>, size=<received>, digest="sha-256:<hex>")` and
   `await sink.store_artifact(artifact_id, meta, <temp fd>)` — the same real sync
   fd / temp-file async→sync bridge as the download fetcher.
7. **Single-success-per-URL:** `await token_store.consume(token)` **only after a
   successful store** (§10.3: at most one successful upload). A rejected upload
   (415/413/400) or a sink failure never consumes, so the slot is not burned.
8. Success → **`204`** (body-free). The temp file is deleted on every path.
   Ambient credentials are ignored — the in-URL token is the only authorization.

### `upload_sender_consume`

```python
async def upload_sender_consume(
    sink: UploadSink,
    source: ArtifactSource,
    key: str,
    *,
    config: ServerConfig,
) -> None: ...
```

Selection (`select_sink`) is the caller's step (consistent with `_filesystem`).
The sender:

- Opens `source.open_artifact(key) -> (stream, metadata)` and **stages the bytes
  to a transient temp file** (single pass, hashing). Staging is required because
  the hook stream is non-seekable and `Content-Digest` must be computed (hashed)
  before it can be sent in the request header — a single-pass read can't both
  hash-first and stream-from-the-hook.
- Sends via `guarded_stream(sink.method, sink.url, config=config,
  transport="upload", content=<async iterator over the temp file>,
  headers={"Content-Type": metadata.mimeType (if known), "Content-Length":
  <size>, "Content-Digest": "sha-256=:<base64>:"})`. The body streams from the
  temp (not buffered in memory); the guard re-resolves/pins and strips ambient
  credentials (#147).
- Treats a non-2xx response as `FileExchangeTransferError(transfer-failed,
  transport="upload")`; a guard refusal already arrives coded (`not-accessible`)
  and propagates. The temp is deleted on every path.
- **Deferred (per §10.3 SHOULD):** the sender does **not** pre-check
  `acceptMimeTypes`. The receiver rejects a mismatch *before* consuming the
  token, so a mismatch never burns the slot — the pre-check is only a round-trip
  saving, and adding it would require threading `expected` into the sender. The
  sender always sends `Content-Digest` (it stages+hashes anyway), which satisfies
  `requireDigest` without needing `expected`.

## §10.3 specifics

- **`Content-Digest` (RFC 9530):** the wire header form is a Structured-Field
  dictionary `algo=:base64:` (e.g. `sha-256=:<base64 of the raw digest>:`) —
  **distinct** from the `ArtifactMetadata.digest` field's `sha-256:<hex>` form.
  A small hand-rolled parse (receiver) and format (sender) for the
  `sha-256/384/512` labels in `_HASHLIB_BY_LABEL` is sufficient; an unsupported
  or unparseable `Content-Digest` is a verification failure (`digest-mismatch`),
  never a silent skip (§15, mirroring `_digest_verifier`). The receiver verifies
  the header in **its declared algorithm** (hashing the received bytes with that
  algorithm) and separately records `ArtifactMetadata.digest` in pvl-core's
  `sha-256:<hex>` form; when the sender used `sha-256` (pvl-core's sender always
  does) a single hash serves both. The sender always sends a `sha-256`
  `Content-Digest`, which satisfies any `requireDigest` that lists `sha-256`.
- **HTTP status mapping** (pvl-core shape, not §13 — the route serves a possibly
  non-MCP HTTP client): `204` success; `404` unknown/expired/consumed token;
  `413` over the size bound; `415` `acceptMimeTypes` reject; `400`
  digest-mismatch / missing-required-digest; hook failure → body-free `500`
  (the sink's exception message may carry server detail — log locally, never
  echo). `upload_sender_consume` raises the §13-coded `FileExchangeTransferError`.

## #146 ↔ #148 boundary

#146 ships the three primitives + the route registrars. #148 builds the shared
token store, threads `source`/`sink`/`config` into `register_file_exchange_routes`,
registers the routes on the server, exposes the umbrella, and wires the §14 Tasks
path (large-artifact transfers). Same boundary as #145.

## Error handling

- `upload_sender_consume`: guard refusals arrive as
  `FileExchangeTransferError(not-accessible, transport="upload")` and propagate;
  a non-2xx response or any other failure maps to `transfer-failed`; the temp is
  removed on every path (`finally`), with the temp-IO `OSError` contract from
  #145 (map to `transfer-failed`, suppress cleanup-close).
- `upload_receiver_mint` is an offering-side op (§16): a token-store failure
  during minting propagates unwrapped to the offering tool's handler (matching
  `filesystem_receiver_mint` / `download_provider_mint`).
- The route maps failures to HTTP status (above); a hook/store failure mid-deposit
  does **not** consume the token, so the sender can retry or re-request a ticket.

## Testing (`tests/_file_exchange/test_upload.py`, plus an e2e module)

Unit:

1. `upload_receiver_mint` — returns an `IntakeTicket` with one `UploadSink`
   whose `url` is `capability_url(base_url, UPLOAD_PREFIX, token)`, `method`
   threads through, `expiresAt` is the minted (clamped) expiry; the token stores
   `{"artifact_id", "expected"}`; `expected` round-trips onto the ticket.
2. Route (via `httpx.ASGITransport` on `mcp.http_app()`): unknown/expired/consumed
   token → `404`; happy `PUT` deposits the bytes into the sink and consumes the
   token (second `PUT` → `404`); over `maxSize`/cap → `413` (no consume); a
   `Content-Type` not in `acceptMimeTypes` → `415` (no consume); a valid
   `Content-Digest` verifies; a mismatched `Content-Digest` → `400` and the sink
   is **not** called (verify-before-use); `requireDigest` with a missing header →
   `400`; ambient `Authorization`/`Cookie` ignored; the temp file is gone after
   every outcome.
3. `upload_sender_consume` — happy path stages and `PUT`s with `Content-Type` /
   `Content-Length` / `Content-Digest` headers asserted (mock `guarded_stream`);
   a non-2xx response → `transfer-failed`; a guard refusal → `not-accessible`;
   the temp file is gone after every outcome.

End-to-end (`test_upload_e2e.py`): two pvl-core-built mock servers. Server B
(receiver) mints via `upload_receiver_mint` and serves via the upload route
mounted on a real ASGI app; server A (sender) selects the `upload` sink and runs
`upload_sender_consume` against B's route through `guarded_stream` (pointed at
B's app) — bytes land in B's `ArtifactSink`, correlated to `artifactId`, with the
`Content-Digest` verified. Upload transport only.

Namespace re-export: each new public name reachable via
`fastmcp_pvl_core.file_exchange`.

## Public surface

Re-exported via `file_exchange.py` and the subpackage `__init__.py` (both
`__all__`s updated, alphabetical), transport-qualified beside the `filesystem_*`
and `download_*` ops:

- `upload_receiver_mint`, `upload_sender_consume` — the two role ops.
- `register_file_exchange_routes` — unchanged public name (now from `_routes`),
  with the added optional `sink` and now-optional `source`.

`UPLOAD_PREFIX` is a module constant (pvl-core route shape) — not re-exported, not
configurable. `FileExchangeTransferError`, `UploadSink`, `IntakeTicket`,
`ArtifactConstraints`, `CapabilityTokenStore`, `capability_url`,
`ArtifactSource`/`ArtifactSink`, `guarded_stream` are reused, not re-declared.

## References

- EPIC #138 (adopt mcp-file-exchange-ext v0.1); this is 8/10. Depends on #144
  (token store — merged), #147 (SSRF guard — merged), #142 (hooks — merged).
  Mirror of #145 (download data plane — merged).
- #148 (top-level `register_file_exchange_*` helpers + Tasks integration +
  adoption docs) — threads `source`/`sink`/`config` and exposes the umbrella.
- Wire spec (`mcp-file-exchange-ext`, pinned commit `5f50a4e…`): §7.4
  (`IntakeTicket`), §8.2 (push flow), §10.3 (`upload` transport obligations —
  receiver single-success/digest/constraints, sender method/Content-Digest/
  no-ambient-creds, RFC 7231 media-range, RFC 9530 `Content-Digest`), §12
  (capability URLs), §13 (error codes), §15 (security — integrity, untrusted
  bytes, resource exhaustion). This document is **not** a wire spec.
- `CLAUDE.md` — route structure is a pvl-core shape decision (constant, not
  kwarg); URL/credential-redaction discipline; the temp-IO error contract and
  verify-before-use carried over from #145.
