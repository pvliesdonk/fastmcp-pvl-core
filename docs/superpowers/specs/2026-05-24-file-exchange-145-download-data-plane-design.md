# File-Exchange #145 — download data plane (route + provider + fetcher)

> **Status:** Contemporaneous design record for issue #145 (7/10 of EPIC
> #138). The implementation in the same PR is the source of truth; this
> captures the shape agreed before implementation. This is **not** a wire
> spec — #145 is pvl-core's own implementation of the `download` transport's
> data plane, governed by `CLAUDE.md` and the project's framing principle,
> not by `mcp-file-exchange-ext`. No `docs/specs/` wire-format file is touched.

**Goal:** Implement the `download` transport's data plane: the provider mints a
capability URL backed by the #144 token store, an HTTPS `GET` route on the
provider's own server serves the artifact bytes for that URL, and the fetcher
retrieves them through the #147 SSRF guard with size/digest verification and
`Range`-based recovery. Everything streams — no artifact is buffered whole.

## Scope (from #138's decomposition + #145's scope statement)

#145 owns the three `download` role primitives and the serving route:
`download_provider_mint`, `register_file_exchange_routes`, and
`download_fetcher_consume`. It builds on two merged dependencies:
`CapabilityTokenStore` (#144) and `guarded_stream` (#147). It does **not** build
the top-level `register_file_exchange_*` umbrella, the shared token-store
construction, or Tasks integration — those are #148. The three primitives take
their dependencies (token store / source hook / sink / config) as parameters;
#148 constructs and threads them.

## Serving model: lazy (the route calls the hook on GET)

The capability URL points at the provider's own route. When a fetcher `GET`s it,
the route produces the bytes by calling the #142 `ArtifactSource` hook on demand
and streaming the result — pvl-core holds no copy of the artifact.

The alternative (eager: stage the artifact to a pvl-core-managed local spool at
mint time, like the `filesystem` transport's `_stage`) was rejected. It would
re-create a TTL'd local blob store (create-at-mint, delete-on-consume,
delete-on-expiry, orphan reaping, disk bounds), read the whole artifact before
any fetch, and hold a full copy — contradicting the issue's "streaming
end-to-end, never buffered whole" and "`Content-Length` *when known*" (the
"when known" only makes sense if the provider isn't staging-and-measuring). Its
only real wins — always-populated `digest` and a seekable spool for free
`Range` — are either approximated under lazy (digest from the hook's metadata
when known; `Range` by re-opening the hook) or defend a non-use-case
(generate-fresh-on-every-open hooks, a misuse for a capability URL fetched
possibly after the session ends).

**Consequence — the hook-stability contract.** Because lazy re-opens the hook
(at every `GET`, and again on each `Range` resume), an `ArtifactSource` offered
via `download` **MUST yield stable bytes for the lifetime of the token** (until
`expiresAt`). For the real use cases (a file at rest) this is automatic; it is
documented as the per-transport requirement.

**Consequence — digest.** pvl-core does not read the artifact at mint, so it
cannot compute a digest. `handle.artifact.digest` is whatever the offering
caller's `ArtifactMetadata` already carries — the #142 hook contract explicitly
permits a server to supply "what it knows." §15's "providers SHOULD populate
digest" is a SHOULD (the fetcher verifies *if present*), and `https` covers
in-transit integrity. Omitting digest when the caller doesn't know it is
compliant.

## Module shape

New module `src/fastmcp_pvl_core/_file_exchange/_download.py`. **Free
functions**, mirroring `_filesystem.py` — no service-object pattern. A
pvl-core-chosen route-path constant `DOWNLOAD_PREFIX` (e.g. `/fx/d`) — the route
structure is a pvl-core **shape** decision, not a kwarg.

### `download_provider_mint`

```python
async def download_provider_mint(
    artifact: ArtifactMetadata,
    key: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    single_use: bool = True,
) -> TransferHandle: ...
```

- `minted = await token_store.mint({"key": key}, ttl=ttl, single_use=single_use)`
  (`mint` is async, so the helper is `async def`).
- `url = capability_url(base_url, DOWNLOAD_PREFIX, minted.token)`.
- Returns `TransferHandle(type=HANDLE_TYPE, version=SPEC_VERSION, artifact=artifact,
  sources=[DownloadSource(transport="download", url=url,
  expiresAt=minted.expires_at, singleUse=single_use)])`.

It takes `artifact: ArtifactMetadata` (caller-supplied), **not** the `source`
hook: lazy means the source is untouched at mint, and the offering tool already
knows the metadata it advertises. The token's stored metadata is the opaque
`{"key": key}` — the route reads it back to know which artifact to open. (The
token store treats the metadata as opaque, per #144.)

### `register_file_exchange_routes`

```python
def register_file_exchange_routes(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    source: ArtifactSource,
) -> None: ...
```

Mounts, via `@mcp.custom_route(f"{DOWNLOAD_PREFIX}/{{token}}", methods=["GET"])`,
an async handler (Starlette `Request` → `Response`):

1. `rec = await token_store.lookup(token)`; if `None` → **`404`** (covers
   unknown / expired / already-consumed — a uniform 404 leaks nothing about
   token state).
2. `stream, meta = await source.open_artifact(rec.metadata["key"])`.
3. Parse the `Range` request header (single `bytes=start-[end]`; a syntactically
   invalid range → `416`). Set status `200` (no/whole range) or `206` (partial),
   with `Content-Type` from `meta.mimeType`, and `Content-Length` /
   `Content-Range` / `Accept-Ranges: bytes` set **when `meta.size` is known**.
4. Body: a `StreamingResponse` over an async generator that reads `stream` in
   chunks (blocking reads off-loop via `asyncio.to_thread`, as `_filesystem`
   does). For a `Range` with a non-zero `start`, read-and-discard `start` bytes
   from the opened stream to reach the offset (the #142 stream is non-seekable),
   then stream the remainder.
5. **Single-use consume:** at the *end* of the generator — reached only if the
   client received every chunk — call `await token_store.consume(token)` **iff
   the served range reached the final byte** (`200`, or a `206` whose end is
   `size-1`). A dropped connection (generator closed early) or a middle `Range`
   never consumes; the descriptor stays valid for `Range` recovery until
   `expiresAt` (§10.2). `consume` is a no-op for a multi-use token.
6. Ambient credentials are ignored — the in-URL token is the only authorization;
   the handler reads no cookies/`Authorization`.

`source.open_artifact` is the only place the hook is touched on the serving side.
The route owns no bytes; it streams the hook's output straight to the response.

### `download_fetcher_consume`

```python
async def download_fetcher_consume(
    handle: TransferHandle,
    descriptor: DownloadSource,
    sink: ArtifactSink,
    *,
    config: ServerConfig,
) -> None: ...
```

Selection (`select_source`) is the caller's step (consistent with
`_filesystem`). The fetcher composes a **verifying, reconnecting reader** and
hands it to the sink:

- **Transport:** `guarded_stream("GET", descriptor.url, config=config,
  transport="download")` (#147) yields the streamed response. The fetcher reads
  `aiter_bytes()` and, on a mid-stream drop (an `httpx` error before
  `handle.artifact.size` bytes have arrived), re-issues `guarded_stream` with a
  `Range: bytes=<received>-` header and continues the *same* running hash/count.
  Each reconnect is a fresh guarded call (re-resolved + re-pinned — correct per
  #147's per-request mitigation).
- **Verify-at-EOF-raises:** the reader wraps the byte flow in a SHA-style hasher
  + byte counter (reusing the `_filesystem._HashingReader` shape). Download is
  single-pass and non-seekable, so it cannot two-pass like `_filesystem._ingest`.
  Instead, at the underlying EOF the wrapper verifies `size` (vs
  `handle.artifact.size` if present) and `digest` (vs `handle.artifact.digest` if
  present, algorithm parsed from the `<label>:` prefix) and **raises**
  `FileExchangeTransferError(size-mismatch | digest-mismatch)` *instead of*
  signalling EOF when wrong. The sink (`store_artifact`), reading the wrapper,
  therefore never reaches a clean EOF on corrupt bytes — an atomic sink (temp →
  rename, per #142/#143) discards them, so bad bytes are never committed. This is
  the streaming analog of `_filesystem`'s verify-before-use.
- **Size bound:** while streaming, if the byte count exceeds
  `config.file_exchange_max_artifact_size` (when set), raise `too-large` —
  bounding consumption to `max + one chunk` rather than reading an
  attacker-sized body to completion (§15 resource exhaustion).
- The wrapped reader is passed to `await sink.store_artifact(handle.artifact.id,
  handle.artifact, reader)`; pvl-core does not buffer the artifact.

## Error handling

`download_fetcher_consume` raises the §13-coded `FileExchangeTransferError` (the
shared type from #143):

- Guard refusals/connection failures already arrive as
  `FileExchangeTransferError(not-accessible, transport="download")` from
  `guarded_stream` — propagated unchanged (it already carries the label).
- `size-mismatch` / `digest-mismatch` from the verify-at-EOF wrapper;
  `too-large` from the size bound.
- Any other underlying failure (e.g. the sink raising for a non-verification
  reason) is wrapped as `transfer-failed`, with the original cause chained for
  local logs and a generic, non-leaking wire `detail` (URL redaction discipline
  carried forward).

`download_provider_mint` is an offering-side op: per §16 the offering roles emit
well-formed references rather than reporting §13 transfer errors, so a
token-store failure during minting propagates unwrapped to the offering tool's
own handler (matching `filesystem_provider_mint`).

The **route** maps failures to HTTP status, not §13 (it serves bytes to a
possibly-non-MCP client over HTTP): `404` (unknown/expired/consumed token),
`416` (unsatisfiable range), `200`/`206` on success. A hook failure mid-stream
tears down the response (the client sees a truncated body / reset); the staged
single-use token is **not** consumed (consume only fires on clean completion),
so the fetcher can retry via `Range` or re-request a fresh reference.

## Testing (`tests/_file_exchange/test_download.py`, plus an e2e module)

Unit:

1. `download_provider_mint` — returns a `TransferHandle` with one `DownloadSource`
   whose `url` is `capability_url(base_url, DOWNLOAD_PREFIX, token)`, `expiresAt`
   is the minted (clamped) expiry, `singleUse` threads through; the token stores
   `{"key": key}`; `artifact` is the caller's metadata verbatim.
2. Route (via FastMCP test client / `httpx` against the mounted app):
   unknown/expired/consumed token → `404`; happy `GET` streams the hook bytes
   with `Content-Type`/`Content-Length` from the hook metadata; `Range` →
   `206` + `Content-Range`; invalid range → `416`; a completed full/`to-EOF`
   retrieval consumes a single-use token (second `GET` → `404`); a middle-range
   or simulated client-disconnect does **not** consume; ambient
   `Authorization`/cookie headers are ignored.
3. `download_fetcher_consume` — happy path streams into a sink with size+digest
   verified; a tampered body raises `digest-mismatch` and the sink's
   `store_artifact` never sees a clean EOF (verify-before-commit); a short/long
   body raises `size-mismatch`; exceeding `max_artifact_size` raises `too-large`;
   a guard refusal propagates `not-accessible`; a dropped connection is recovered
   via `Range` (mock `guarded_stream` to fail once mid-stream, assert the resume
   `Range` header and the completed transfer).

End-to-end (`test_download_e2e.py`): two pvl-core-built mock servers. A mints via
`download_provider_mint` and serves via `register_file_exchange_routes` (mounted
on a real ASGI test transport); B selects the `download` source and runs
`download_fetcher_consume` against A's route through `guarded_stream` (its SSRF
checks satisfied for the loopback test target via the allowlist) — bytes land in
B's `ArtifactSink` with size+digest verified. Download transport only.

Namespace re-export: each new public name reachable via
`fastmcp_pvl_core.file_exchange`.

## Public surface

Re-exported via `src/fastmcp_pvl_core/file_exchange.py` and the subpackage
`__init__.py` (both `__all__`s updated, alphabetical), transport-qualified to sit
beside the `filesystem_*` ops:

- `download_provider_mint`, `download_fetcher_consume` — two of the role ops.
- `register_file_exchange_routes` — the route registrar (the only `download`
  role with no `filesystem` analog).

`DOWNLOAD_PREFIX` is a module constant (pvl-core's route shape); it is **not**
re-exported or configurable. `FileExchangeTransferError`, `DownloadSource`,
`CapabilityTokenStore`, `capability_url`, `ArtifactSource`/`ArtifactSink`,
`guarded_stream` are reused, not re-declared.

## References

- EPIC #138 (adopt mcp-file-exchange-ext v0.1); this is 7/10. Depends on #144
  (capability token minter + store — merged) and #147 (SSRF + DNS-rebind guard —
  merged).
- #146 (upload data plane) — the symmetric push side; shares the token store and
  `guarded_stream`, and the same route-registration entry point will host the
  upload `PUT`/`POST` route (whether `register_file_exchange_routes` grows the
  upload route or #146 adds a sibling registrar is #146's call).
- #148 (top-level `register_file_exchange_*` helpers + Tasks integration +
  adoption docs) — builds the shared token store, threads source/sink/config into
  these primitives, registers the routes, and exposes the umbrella. The §14
  Tasks path (large-artifact transfers) is #148, not here.
- Wire spec (`mcp-file-exchange-ext`, pinned commit `5f50a4e…`): §7.2.2
  (`download` source descriptor), §8.1 (pull flow), §9 (selection), §10.2
  (`download` transport obligations — provider Range/single-use/Content-*,
  fetcher no-ambient-creds/verify), §12 (capability URLs), §13 (error codes),
  §15 (security — integrity, resource exhaustion). This document is **not** a
  wire spec.
- `CLAUDE.md` — route structure is a pvl-core shape decision (constant, not
  kwarg); the URL-redaction discipline carried forward; the lazy hook-stability
  requirement is a per-transport contract pvl-core documents.
