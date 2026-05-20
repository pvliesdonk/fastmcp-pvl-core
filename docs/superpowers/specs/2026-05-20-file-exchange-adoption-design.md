# File-exchange adoption — pvl-core design

- **Status:** draft, brainstorm-approved 2026-05-20
- **Targets spec:** [`pvliesdonk/mcp-file-exchange-ext`](https://github.com/pvliesdonk/mcp-file-exchange-ext) v0.1
- **Namespace:** `nl.liesdonk.file-exchange`
- **Prerequisite (already shipped):** [#121 / PR #122 — unified key-value storage factory](https://github.com/pvliesdonk/fastmcp-pvl-core/pull/122) (`build_kv_store(env_prefix, config, *, namespace)`)

## 1. Goal

Implement the `nl.liesdonk.file-exchange` v0.1 extension as the **shared, opinionated implementation** for the pvl MCP server family. Downstream servers (markdown-vault-mcp, scholar-mcp, image-generation-mcp, …) opt into one or more roles (`provider`, `fetcher`, `receiver`, `sender`) and three transports (`filesystem`, `download`, `upload`) using pvl-core helpers, with all wire-format mechanics — capability declaration, reference construction, descriptor selection, transport semantics, error normalisation, capability-URL minting — owned by pvl-core.

This is the cleanroom rewrite that follows [PR #117](https://github.com/pvliesdonk/fastmcp-pvl-core/pull/117) (the full removal of the prior v0.2–v0.6 design). The old design is not the basis; the external `mcp-file-exchange-ext` v0.1 spec is the only authority.

## 2. Driving use cases

| Flow | Roles in pvl-core | Transport |
|---|---|---|
| markdown-vault-mcp ↔ MCP peers | `provider`, `fetcher`, `receiver`, `sender` | `filesystem` primary; `download`/`upload` for non-MCP peers |
| image-generation-mcp → markdown-vault-mcp | `provider` (image-gen) + `receiver` (MV) | `filesystem` (colocated) |
| image-generation-mcp → non-MCP sandbox | `provider` (image-gen) | `download` (HTTPS) |
| scholar-mcp → markdown-vault-mcp | `provider` (scholar) + `receiver` (MV) | `filesystem` |
| non-MCP coding agent → markdown-vault-mcp | `receiver` (MV) | `upload` (HTTPS) |

All four roles are in scope from phase 1. Both filesystem and HTTPS transports are in scope, on both consumer and producer sides — the latter is necessary because non-MCP consumers/producers (coding agents, sandboxes) are first-class peers in the driving flows.

## 3. Public API surface

All symbols are top-level on `fastmcp_pvl_core`, matching the existing `build_auth` / `register_server_info_tool` convention.

### Types

```python
ArtifactMetadata
TransferHandle
IntakeTicket
SourceDescriptor       # discriminated union: filesystem | download
SinkDescriptor         # discriminated union: filesystem | upload
ExpectedConstraints    # max_size / accept_mime_types / require_digest
FileExchangeRole = Literal["provider", "fetcher", "receiver", "sender"]
FileExchangeTransport = Literal["filesystem", "download", "upload"]
FileExchangeError                # exception
FileExchangeErrorCode            # StrEnum mirroring §13
```

### Capability declaration

```python
def register_file_exchange_capability(
    server: FastMCP,
    config: ServerConfig,
    *,
    roles: Sequence[FileExchangeRole] = ("provider", "fetcher", "receiver", "sender"),
    kv_store: AsyncKeyValue,        # from build_kv_store(env_prefix, config, namespace="file-exchange")
) -> None
```

The downstream declares which **roles** it wants to play (a domain decision — does this server export, ingest, both?). pvl-core **derives the transport set per role** from what the deployment can actually satisfy. The advertised capability mirrors reality, never a declaration that runtime selection has to reject.

`maxArtifactSize` is operator-side configuration, not a kwarg — pvl-core reads `config.file_exchange_max_artifact_size` and emits it on the capability block when set. The advertised `digests` set is `("sha-256",)`, hardcoded in `_capability.py` — downstream has no domain-specific basis to vary it, and when the family converges on stronger digests later, the constant changes once in pvl-core and every downstream picks it up via `copier update`.

#### Transport-availability gating (the load-bearing detail)

Per the spec §4.2 table, role-vs-transport compatibility is asymmetric across deployment topology. pvl-core encodes the table once:

| Transport | Required deployment capability |
|---|---|
| `filesystem` (any role) | `config.file_exchange_volumes` non-empty — i.e. operator configured at least one volume in `_FILE_EXCHANGE_VOLUMES` |
| `download` (provider) | `config.transport == "http"` — the FastMCP server hosts an HTTP app on which to mount `GET /file-exchange/d/<token>` |
| `download` (fetcher) | always available (outbound HTTP) |
| `upload` (receiver) | `config.transport == "http"` — needs to host `PUT|POST /file-exchange/u/<token>` |
| `upload` (sender) | always available (outbound HTTP) |

Algorithm:

1. For each role in `roles`, compute the set of transports the deployment can satisfy.
2. Drop any role whose resulting transport set is empty (the deployment cannot play that role at all).
3. If the post-gate role set is empty, **do not advertise the capability** — `nl.liesdonk.file-exchange` is simply absent from `experimental_capabilities`. Log at INFO: `file-exchange: no satisfiable transport for any declared role — capability not advertised`.
4. Otherwise, write the gated `{role: [transports…]}` map into the `nl.liesdonk.file-exchange` block exactly as the spec requires (§5).
5. Auto-mount HTTP routes iff `provider+download` or `receiver+upload` survives the gate (the stdio-only case skips this).

This makes the call effectively zero-config for downstream — `register_file_exchange_capability(server, config, kv_store=…)` does the right thing in every deployment. Concretely:

- **stdio + no volumes** → only the consumer roles survive (`fetcher: ["download"]`, `sender: ["upload"]`) because outbound HTTPS is always available; the producer-side roles drop. The capability block IS advertised.
- **stdio + volumes** → all four roles advertised with `filesystem` (and consumer roles also keep `download`/`upload` for non-MCP peers).
- **http + no volumes** → all four roles advertised with HTTPS only.
- **http + volumes** → all four roles advertised with both transports.
- **Empty post-gate role set** (e.g. downstream passes `roles=("provider", "receiver")` on a stdio + no-volumes deployment) → capability is not advertised at all. This is the safety-net step 3, not the common case.

#### Other behaviour

- The `kv_store` parameter is the namespaced store from the existing `build_kv_store` factory. Internal sub-keyspaces: `tokens:<token>` and `intake:<artifact_id>` via `PrefixCollectionsWrapper`.
- `config` extends with new file-exchange fields (see §5 below); the call reads them rather than env-vars directly, matching the existing pvl-core pattern (`build_kv_store`, `build_oidc_proxy_auth`).

### Descriptor minting (producer side)

```python
def make_filesystem_source(
    volume: str,
    relative_path: str,
    *,
    config: ServerConfig,
) -> SourceDescriptor

async def make_filesystem_sink(
    volume: str,
    relative_path: str,
    *,
    artifact_id: str,
    config: ServerConfig,
    kv_store: AsyncKeyValue,
) -> SinkDescriptor

async def mint_download_source(
    *,
    bytes_path: Path,
    server: FastMCP,
    config: ServerConfig,
    kv_store: AsyncKeyValue,
    expires_in_s: int | None = None,
    single_use: bool = True,
) -> SourceDescriptor

async def mint_upload_sink(
    *,
    intake_path: Path,
    artifact_id: str,
    server: FastMCP,
    config: ServerConfig,
    kv_store: AsyncKeyValue,
    expires_in_s: int | None = None,
    expected: ExpectedConstraints | None = None,
) -> SinkDescriptor
```

Both sink-side minters (`make_filesystem_sink` and `mint_upload_sink`) record the `(artifact_id → resolved_intake_path)` mapping in `kv_store` at mint time — that single seam lets `resolve_intake` find the bytes after a successful transfer regardless of which transport was used. `config` supplies the volume map, the capability-URL TTL default, and (for HTTPS minters) the optional public-base-URL override. The trio `(server, config, kv_store)` is sufficient context for any helper in this section; no separate `volumes`/`ssrf`/`base_url` kwargs leak through.

`make_filesystem_source` is synchronous (just composes the URI string); the three other minters are async because they write to `kv_store`. That asymmetry is intentional — there is no kv_store interaction on the read-side filesystem minter, and forcing all four to async would be ceremony without benefit.

### Role helpers

```python
# Provider
def build_pull_response(
    artifact: ArtifactMetadata,
    sources: Sequence[SourceDescriptor],
    *,
    summary: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> CallToolResult

# Fetcher
async def pull_artifact(
    handle: TransferHandle | dict,
    *,
    dest: Path | BinaryIO,
    config: ServerConfig,
    supported_transports: Sequence[FileExchangeTransport] | None = None,
) -> ArtifactMetadata

# Receiver
def open_intake(
    *,
    sinks: Sequence[SinkDescriptor],
    expected: ExpectedConstraints | None = None,
    artifact_id: str | None = None,
) -> IntakeTicket

def build_intake_response(
    ticket: IntakeTicket,
    *,
    summary: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> CallToolResult

async def resolve_intake(
    artifact_id: str,
    *,
    kv_store: AsyncKeyValue,
) -> Path

# Sender
async def push_artifact(
    ticket: IntakeTicket | dict,
    *,
    source: Path | BinaryIO,
    config: ServerConfig,
    supported_transports: Sequence[FileExchangeTransport] | None = None,
    artifact_digest: str | None = None,
    artifact_mime: str | None = None,
    artifact_size: int | None = None,
) -> None

# Error -> envelope
def as_tool_error_result(exc: FileExchangeError) -> CallToolResult
```

Each helper validates inputs against the vendored JSON Schema, applies §17.3 version-skew + §17.4 must-understand checks, runs the §9 selection algorithm where applicable, and surfaces failures as `FileExchangeError` with one of the §13 codes.

`pull_artifact` and `push_artifact` take `config` so they can derive the volume map (`config.file_exchange_volumes`), the SSRF guard (`config.file_exchange_https_allow_loopback`/`allow_private`), and any other operator-side concern. No standalone `volumes`/`ssrf` kwargs — those are deployment state, not domain hooks. `open_intake` is pure construction: the `(artifact_id → resolved_intake_path)` mapping was already recorded by the sink-minter that produced its `sinks`, so it needs neither `config` nor `kv_store`.

`resolve_intake` always returns a `Path`. The sink minter writes the `(artifact_id → resolved_intake_path)` mapping at *mint* time — so the mapping exists from the moment the receiver returns the ticket, before any bytes arrive. To distinguish "wrong artifact_id" from "bytes not yet on disk", `resolve_intake` then asserts `path.exists()`; both cases raise `FileExchangeError(code=NOT_ACCESSIBLE)` (with distinct `detail` strings), so the caller's tool body can convert via `as_tool_error_result`. The `-> Path | None` shape was considered but creates a quiet-failure trap: callers forget the `None` branch and operate on the wrong path. The receiver-side discipline: the *processor* tool that consumes `artifact_id` is called only after the sender confirms the transfer; if it's called too early, `NOT_ACCESSIBLE` is the correct, observable failure.

When `summary` is omitted on `build_pull_response` or `build_intake_response`, pvl-core auto-synthesises a short human-readable line of the form `"file-exchange: <artifact.name or artifact.id> (<size_human>, <mime_type>)"`, falling back gracefully when fields are missing. The intent is that the model sees enough to reason about the chain without ever seeing the bytes (spec §11.1).

## 4. Internal module layout

```
src/fastmcp_pvl_core/_file_exchange/
    __init__.py                                # public-namespace assembly
    schema/
        file-exchange.schema.json              # vendored verbatim from spec repo @ pinned commit
        .expected-sha256                       # drift gate
        conformance/                           # vendored fixtures from spec repo
    _types.py                                  # Pydantic models, schema round-trip test
    _capability.py                             # register_file_exchange_capability + route mounting
    _select.py                                 # §9 selection algorithm (source + sink)
    _errors.py                                 # FileExchangeError, code constants, as_tool_error_result
    _provider.py                               # build_pull_response
    _fetcher.py                                # pull_artifact
    _receiver.py                               # open_intake, build_intake_response, resolve_intake
    _sender.py                                 # push_artifact
    _transport_filesystem.py                   # exchange:// resolution, atomic_write, confinement, mint helpers
    _transport_https.py                        # pull_download, push_upload, SSRF guard
    _url_store.py                              # capability URL minting, intake correlation; backed by kv_store
    _routes.py                                 # GET /file-exchange/d/<token>, PUT|POST /file-exchange/u/<token>
```

Public symbols are re-exported from `fastmcp_pvl_core/__init__.py`. The `_file_exchange` package itself is private (underscore-prefixed) — consumers depend on the top-level surface, not the layout.

## 5. Operator configuration

All operator-side concerns are environment variables loaded into typed fields on `ServerConfig` (per pvl-core convention — `ServerConfig.from_env` is the single read point; downstream code touches `config.foo`, not `os.environ`). The `{PREFIX}` placeholder is the consuming server's env-prefix.

| Variable | `ServerConfig` field | Meaning |
|---|---|---|
| `{PREFIX}_FILE_EXCHANGE_VOLUMES` | `file_exchange_volumes: str \| None` | `<volume-id>=<local-mount-point>` mappings, comma-separated. Parsed at registration time to gate `filesystem` transport availability per §3. |
| `{PREFIX}_FILE_EXCHANGE_MAX_ARTIFACT_SIZE` | `file_exchange_max_artifact_size: int \| None` | Hard ceiling in bytes; enforced by fetchers/receivers, pre-checked by senders/providers. |
| `{PREFIX}_FILE_EXCHANGE_HTTPS_ALLOW_LOOPBACK` | `file_exchange_https_allow_loopback: bool` | Dev-only; default `false`. Disables SSRF guard for loopback addresses. |
| `{PREFIX}_FILE_EXCHANGE_HTTPS_ALLOW_PRIVATE` | `file_exchange_https_allow_private: bool` | Dev-only; same shape, for RFC 1918 / link-local ranges. |
| `{PREFIX}_FILE_EXCHANGE_CAPABILITY_URL_TTL_DEFAULT_S` | `file_exchange_capability_url_ttl_default_s: int` | Default `expiresAt` window for minted `download`/`upload` URLs (default: 3600s). |
| `{PREFIX}_FILE_EXCHANGE_HTTPS_PUBLIC_BASE_URL` | `file_exchange_https_public_base_url: str \| None` | Override for `compute_app_domain` for capability-URL construction (reverse-proxy case). |
| `{PREFIX}_KV_STORE_URL` | `kv_store_url` (existing) | Selects the shared storage backend. File-exchange uses `namespace="file-exchange"`. |
| `config.transport` (existing, set from `{PREFIX}_TRANSPORT`) | — | Whether the server hosts an HTTP app — gates `download`-as-provider and `upload`-as-receiver per §3. |

The volumes parser in `_transport_filesystem.py` is the single source of truth for the volume map; both directions (`provider` writing, `fetcher` reading) call it. A party with no mapping for a referenced volume skips that descriptor at selection time (§9), independent of the registration-time gate (which decides what to *advertise*).

### What the gate means for operators

A deployment can rotate its file-exchange surface without code changes — just env-vars:

- **`_FILE_EXCHANGE_VOLUMES=` (empty) + `_TRANSPORT=stdio`** → only the consumer roles are advertised (`fetcher: ["download"]`, `sender: ["upload"]`). The producer-side roles (`provider`, `receiver`) drop because they need an HTTP app to host endpoints. Outbound HTTPS works regardless.
- **`_FILE_EXCHANGE_VOLUMES=vault-export=/mnt/exchange/vault` + `_TRANSPORT=stdio`** → all four roles advertised; consumer roles get both `filesystem` and outbound HTTPS, producer roles get `filesystem` only.
- **`_FILE_EXCHANGE_VOLUMES=` (empty) + `_TRANSPORT=http`** → all four roles advertised, HTTPS-only.
- **Both set + `_TRANSPORT=http`** → all four roles, both transports per role.

## 6. Transport mechanics

### 6.1 Filesystem (`exchange://` and `file://`)

- `resolve_exchange_uri(uri) -> Path` parses both URI schemes, looks up the volume (for `exchange://`), and **canonicalises** the result (resolves `.`, `..`, symlinks). The canonical path is then asserted to lie inside the resolved volume root; escapes — including via symlinks — raise `FileExchangeError(code="not-accessible")`. Used by both source (fetcher reading) and sink (sender writing) paths.
- `atomic_write(target)` context manager: temp file in the same directory, fsync, rename onto target. Guarantees consumers never see a partial write.
- Reads use `open(path, "rb")` with `O_NOFOLLOW` (defeats last-mile symlink swaps).
- Lifecycle: provider owns source-file cleanup (per spec §10.1.3); pvl-core never auto-deletes.

### 6.2 HTTPS download/upload

**Consumer side** (`pull_download` / `push_upload`):

- `https`-only; refuses non-`https` URLs and cross-origin redirects.
- No ambient credentials (no cookies, no `Authorization` header, no client certs). The capability URL is the only credential.
- **SSRF guard**: resolve hostname once; reject loopback / link-local / RFC 1918 unless the corresponding env-var override is set; connect to the pinned IP address (defeats DNS rebinding). Shared helper called by both pull and push.
- `Range` support on download for resume; verifies `Content-Length` against `artifact.size` and computed digest against `artifact.digest` if either is present.
- On upload, sends `Content-Length` and (when applicable) `Content-Digest` (RFC 9530). When the ticket's `expected.requireDigest` is set, `push_artifact`'s `artifact_digest` kwarg is mandatory.

**Producer side** (token store + sibling routes):

- 128-bit URL-safe random tokens stored in the namespaced `kv_store` under `tokens:<token>` (a single keyspace for both download and upload records, discriminated by a `kind` field; the upload record carries the `expected` constraints + `artifact_id` correlation).
- `GET /file-exchange/d/<token>` — looks up; sets `Content-Type`/`Content-Length` from stored metadata; supports `Range`; on full-bytes-served success marks the token consumed; returns 404 for expired/unknown/consumed-single-use.

  **Single-use concurrency posture**: pvl-core uses a per-key get-then-delete pattern (`kv_store.delete()` is atomic per key on every supported backend; a racing second consumer finds the key already gone and returns 404). This holds under high concurrency for Memory, FileTree, Redis, DynamoDB, and MongoDB backends. **It is best-effort if the kv_store backend lacks atomic-delete semantics**; a custom adapter must document this clearly. A future enhancement can introduce a `CompareAndSwap` primitive on the storage layer if a backend without atomic delete becomes load-bearing.

- `PUT|POST /file-exchange/u/<token>` — looks up; **streams** the request body chunk-by-chunk into a temp file via the `atomic_write` context manager (same helper as the filesystem transport). Each chunk is digested with `hashlib.sha256` (incremental), counted against `expected.maxSize` (fail-fast with HTTP 413 the moment cumulative bytes exceed the cap — never buffer beyond one chunk), and matched against `expected.acceptMimeTypes` via the request `Content-Type` header (HTTP 415 on mismatch). On body-fully-received success: verify the running digest matches any `Content-Digest` header (HTTP 422 / `digest-mismatch` on mismatch), then commit via `atomic_write`'s rename and mark the token consumed (atomic per-key `delete`). Validation failure or partial body causes the temp file to be discarded (no rename happens). The `intake:<artifact_id> → resolved_intake_path` mapping was recorded by `mint_upload_sink` at *mint* time — the route does not write it again; bytes simply arrive at the path the mapping already points to.

- All synchronous file I/O inside async handlers (chunk writes, the final fsync) is dispatched via `asyncio.to_thread` so the event loop is never blocked on disk latency. The pull route uses the same discipline for `Content-Length`-bounded streams.

- Public base URL: `compute_app_domain` (existing pvl-core helper) plus the literal `/file-exchange/{d,u}/` prefix. Overridable via `_FILE_EXCHANGE_HTTPS_PUBLIC_BASE_URL`.

- Background sweeper deletes expired tokens (default interval: 60s) to keep the store bounded.

**Logging discipline** (`_url_store.py`, `_routes.py`): capability URLs are bearer credentials per spec §12; pvl-core never logs them in full at any level. Debug logs use a token fingerprint of the form `tok=<first-8-chars>...` derived from the token's URL-safe base64 representation; the full URL or the full token never enters stdout, stderr, or any structured-log field. The `_meta` mirror under `nl.liesdonk.file-exchange/handles` / `tickets` (spec §11.1) embeds the URLs verbatim and is therefore *also* never logged. A unit test in Task E asserts that `caplog` contains no full-token substring during mint, consume, or sweep flows.

## 7. Schema vendoring & conformance

- `schema/file-exchange.schema.json` copied verbatim from `pvliesdonk/mcp-file-exchange-ext` at a pinned commit (recorded in a sibling `PINNED_AT.md`).
- CI compares the vendored file's SHA-256 against `.expected-sha256`; drift fails the build and forces an explicit re-pin commit.
- `conformance/` fixtures are vendored the same way (move together with the schema).
- `tests/file_exchange/test_conformance.py` walks the fixtures, loads each via Pydantic, asserts the documented valid/invalid outcome.
- `_types.py` includes a round-trip test asserting `model_json_schema()` output is structurally compatible with the vendored schema (catches drift between hand-written models and the spec).

## 8. Testing strategy

Three layers, all under `tests/file_exchange/`:

1. **Unit tests** per module. Notable areas: `_select.py` matrix tests for (handle, supported_transports) → expected descriptor; `_transport_filesystem.py` path-confinement (canonicalise + symlink-escape rejection on a tmp tree); `_transport_https.py` SSRF guard against a faked-DNS fixture; `_url_store.py` against an in-memory `AsyncKeyValue`.
2. **Integration tests** with two FastMCP test servers in-process (in-memory transport from `pytest-asyncio`), sharing a tmp volume. Drives the full pull and push flows for both `filesystem` and HTTPS. The HTTPS test mounts the sibling routes via Starlette's `TestClient` and exercises 128-bit token entropy, single-use enforcement under concurrency, `Range` resumption, and digest mismatch.
3. **Conformance suite** against the vendored fixtures (§7).

## 9. Tasks composition

pvl-core does not auto-declare `execution.taskSupport` on tools — that's a per-tool decision the downstream makes. The four role helpers are async and stream-friendly; they don't impose a request-timeout posture. Helper docstrings note: if a tool may handle artifacts large enough to outrun the request timeout, declare `execution.taskSupport="optional"` or `"required"` at registration. TTL sizing for capability URLs is left to the downstream (default fallback: `_CAPABILITY_URL_TTL_DEFAULT_S`); pvl-core does not auto-derive `expiresAt` from a Task's `ttl` in phase 1.

## 10. Phasing

**Phase 0** — Prerequisite. ✅ Done — [#122](https://github.com/pvliesdonk/fastmcp-pvl-core/pull/122) shipped `build_kv_store`.

**Phase 1 — Core implementation in pvl-core.** PRs filed against `pvliesdonk/fastmcp-pvl-core`, parallel-pipelined per the global PR workflow. Issue split:

| Tag | Scope | Files |
|---|---|---|
| A | Vendor schema + conformance fixtures + Pydantic types + drift gate | `_file_exchange/{schema,_types}.py` |
| B | Selection algorithm + error envelope | `_select.py`, `_errors.py` |
| C | Filesystem transport: `resolve_exchange_uri`, `atomic_write`, mint helpers | `_transport_filesystem.py` |
| D | HTTPS consumer side: `pull_download`, `push_upload`, SSRF guard | `_transport_https.py` |
| E | Capability URL store + sibling routes + intake-correlation map | `_url_store.py`, `_routes.py` |
| F | Capability declaration + role helpers + top-level re-exports | `_capability.py`, `_provider.py`, `_fetcher.py`, `_receiver.py`, `_sender.py`, `__init__.py` |
| G | Integration tests + conformance suite | `tests/file_exchange/` |

Eight items (including #122). Each PR runs the full `preflight-circus` skill locally before push; bots are merge gates.

**Phase 2 — Downstream canary: `markdown-vault-mcp`.** Filed in the MV repo, not pvl-core. Wires MV as `provider` + `fetcher` + `receiver` + `sender`, both `filesystem` and HTTPS, blocked on pvl-core issue F. Scope:

- One new tool returning `TransferHandle` for an existing note/attachment export path.
- One `open_intake`-style tool + one `ingest_attachment(artifact_id)` follow-up.
- `register_file_exchange_capability` at server build.
- Operator docs: volume map + the file-exchange volumes the deployment expects.

**Phase 3 — Follow-on canaries.** `image-generation-mcp` (provider for generated images), `scholar-mcp` (provider for fetched PDFs). Each is a small wire-up patch in its own repo once the pvl-core API is stable.

## 11. Impact on `fastmcp-server-template`

The template propagates pvl-core API to every downstream via `copier update`. Template-side work:

1. **Server-build scaffolding**: feature-gated `register_file_exchange_capability(server, config, kv_store=…)` block (off by default; copier question `enable_file_exchange: bool = false`). Note: no transport-set kwargs at the call site — they're derived from `config.file_exchange_volumes` and `config.transport` at registration, per §3.
2. **`copier.yml` questions**: `enable_file_exchange` and `file_exchange_default_roles`. Transport availability per role is *not* a template-time decision — it's deployment-derived at registration time from `ServerConfig` + env-vars.
3. **`.env.example` additions** when enabled — the env-vars from §5.
4. **`docs/file-exchange-cookbook.md`** with two minimal worked examples (provider, receiver/processor). Documentation only, not committed as live tools.
5. **`pyproject.toml` floor bump** to the pvl-core version that shipped the file-exchange APIs (after pvl-core issue F lands).
6. **A fresh template umbrella issue** filed *after* the pvl-core design + child issues are in place. Template issue `#131` (the old umbrella that referenced the dead v0.6 design) is already closed; the new one references this design doc and the pvl-core phase 1 cluster.

Template work is the **last PR of the cluster**: pvl-core's API may shift slightly under review, and the template should pin against the final shape, not chase intermediate versions.

## 12. Spec evolution (deferred, recorded)

Spec-side, not pvl-core-side — surfaced for context but not addressed by this design:

- `multiArtifact` references (spec §18) — likely a v0.2 addition. pvl-core's reference types should accept it as a backwards-compatible minor bump.
- Provider-driven revocation (spec §18) — not addressable in v0.1; pvl-core's token-store `delete()` capability is groundwork.
- IANA registration of the `exchange://` URI scheme (spec §18) — out of pvl-core's scope.

## 13. Non-goals

- No replacement for MCP `resources`. Handles are ephemeral, single-use, not listed.
- No new JSON-RPC methods or new client behaviour. The model copies a JSON object from one tool's output into another tool's input — same as any tool chain.
- No automatic Task augmentation. Composes with FastMCP's Tasks; doesn't decide for the downstream.
- No object-storage adapter (S3 presigned URLs etc.). The auto-mounted sibling endpoint is sufficient for the colocated-FS-primary deployments; a pluggable URL store is a future enhancement, not phase 1.
- No backwards-compatibility shims with the removed v0.6 design. The cleanroom rewrite stands alone.

## 14. References

- Spec: https://github.com/pvliesdonk/mcp-file-exchange-ext (v0.1 draft)
- Removed implementation: [pvliesdonk/fastmcp-pvl-core#117](https://github.com/pvliesdonk/fastmcp-pvl-core/pull/117)
- Prerequisite (shipped): [pvliesdonk/fastmcp-pvl-core#121 / #122](https://github.com/pvliesdonk/fastmcp-pvl-core/pull/122)
- Contributor framing: [/CLAUDE.md](../../../CLAUDE.md) — "Shape decisions live in pvl-core; hooks expose domain-specific behaviour only."
- FastMCP storage backends: https://gofastmcp.com/servers/storage-backends
