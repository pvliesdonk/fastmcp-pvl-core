# ADR 0001 — Lift capability-link transfer, SSRF fetch, and base64 ingest into pvl-core

- **Status:** Proposed (study for [#212]; no implementation in this document)
- **Date:** 2026-07-04
- **Deciders:** pvl-core maintainers
- **Supersedes / relates to:** EPIC #138 (closed `NOT_PLANNED`; archived
  `file-exchange-archive`), image-generation-mcp#300 (shared-infra half is
  unblocked by this ADR)
- **Amended by:** #274 — `fetch_url` follows redirects, re-validating and
  re-pinning every hop. The "redirects disabled" element listed in §8 and the
  §11 #1 table describes the original implementation, not the current one; the
  security requirement it served (never dial an unvalidated address) is
  unchanged and now holds per hop.

> This is an **implementor design**, not a wire specification. It deliberately
> does **not** live under `docs/specs/`, which `CLAUDE.md` reserves for
> wire-format contracts between independently developed servers. Nothing here
> describes bytes on a protocol negotiated with a foreign implementation.

---

## 1. Context

Several servers in the `pvliesdonk/*-mcp` family need the same
ingest/transfer capabilities:

- **capability-link download** — mint a one-time HTTP URL that serves a
  server-side artifact once, then expires;
- **capability-link upload** — mint a one-time HTTP URL that accepts one
  body and commits it server-side;
- **SSRF-hardened URL fetch** — pull bytes from an operator-/caller-supplied
  URL without becoming a server-side request forgery vector;
- **base64-inline ingest** — accept bytes inline with a decode + size-cap +
  content-validation policy.

Today the implementations are **forked and drifting**:

| Capability | `markdown-vault-mcp` | `image-generation-mcp` |
|---|---|---|
| download link | `transfer/` — full state machine | `artifacts.py` — weaker fork (UUID token, **no upload, no lease/in-flight state**) |
| upload link | `transfer/` — streaming, size-cap, burn/release | — (absent) |
| SSRF fetch | `_server_tools/writer.py` — DNS-rebind pin, redaction | **not implemented** (`_input_images.py` docstring: *"Adding … URL sources later is localized here"*) |
| base64 ingest | `write` tool (decode + cap) | `_input_images.py` (decode + PIL validate + cap) |

`markdown-vault-mcp` carries the mature, repeatedly hardened copy.
`image-generation-mcp` carries a strictly weaker download-only fork and has
not yet grown the SSRF fetch at all. This is exactly the drift the family is
supposed to avoid: the next SSRF hardening in one repo silently bypasses the
other.

**Goal:** one hardened implementation in `fastmcp-pvl-core`, consumed by the
whole family, with the security-sensitive mechanics owned centrally and only
domain-specific glue left downstream.

### Cautionary precedent

EPIC #138 attempted this and was closed `NOT_PLANNED`. It over-reached into an
**external interoperable wire-format protocol** (`nl.liesdonk.file-exchange`:
4 roles × 3 transports, vendored schema, 55 conformance fixtures, version-skew
/ must-understand negotiation, `exchange://` URI scheme). The surface
ballooned and tangled in spec conformance. **This design scopes strictly to
the in-process reuse actually needed — explicitly not a wire protocol or
external-spec adoption.** §10 enumerates the guardrails that keep it there.

---

## 2. Decision (summary)

1. **Framework ownership, not a toolkit.** pvl-core exports
   `register_transfer_routes(mcp, config, *, sink, validate)` that owns the
   `/transfer/{token}` route, both link tools, the token store, and all
   size-cap / TTL / redaction mechanics, *consuming* the standalone
   `fetch_url` / `decode_base64_capped` primitives (item 2) for byte
   resolution rather than owning them. Downstream implements **only** two
   domain hooks: a `TransferSink` (where bytes land) and a
   `TransferValidator` (what bytes are acceptable).
2. **`fetch_url` and `decode_base64_capped` are also first-class standalone
   primitives.** SSRF-hardened URL fetching is a broadly useful capability,
   not only an ingest step; it is exported for direct reuse, and the transfer
   framework *consumes* it rather than owning it.
3. **The token store is backed by the existing `build_kv_store` abstraction**
   (its docstring already names *"future file-exchange token store"* as an
   intended consumer), preserving the `available ↔ in_flight` state machine
   (grace-settle on success, explicit `burn` → `consumed`) — including
   **release-on-failure** so the link survives a common transient serve failure.
4. **The token carries an opaque `sink_handle`** that pvl-core stores and
   echoes but never interprets — the structural guarantee that no domain
   logic leaks into core.
5. **New module named `_transfer`** (mirrors vault's `transfer/`); the empty
   residual `src/fastmcp_pvl_core/_file_exchange/` directory is removed.
6. **Delivered as five small, independently-shippable issues** (§11), the first
   two of which deliver reusable value before the full feature lands.

---

## 3. Q1 / Q3 — The seam: framework + opaque handle

pvl-core owns every **shape** decision; downstream provides only **domain
hooks**. The classification test from `CLAUDE.md` — *"would pvl-core be wrong
to make this decision itself?"* — resolves each element:

| Element | Owner | Why |
|---|---|---|
| Route path `/transfer/{token}`, HTTP verbs, status codes | pvl-core | shape |
| Tool names `create_download_link` / `create_upload_link` | pvl-core | shape |
| Token store, TTL clamp, `base_url`-required guard | pvl-core | shape |
| SSRF policy, scheme allowlist, redaction, size-cap enforcement | pvl-core | shape (security invariant) |
| **Where bytes land** (read/write) | downstream | domain hook (`TransferSink`) |
| **What bytes are acceptable** (ref → validated handle) | downstream | domain hook (`TransferValidator`) |

```python
class TransferSink(Protocol):
    async def read(self, handle: str) -> tuple[bytes, str, str]:
        """Return (body, media_type, filename) for a validated download handle."""

    async def write(self, handle: str, body: bytes) -> Mapping[str, Any]:
        """Commit an uploaded/ingested body; return the tool's result payload."""


# Caller-facing ref → validated OPAQUE handle. Raises to reject.
# `kind` lets a validator apply different rules to upload vs. download
# (e.g. existence check on download, extension allowlist on upload).
TransferValidator = Callable[
    [str, Literal["download", "upload"]], Awaitable[str]
]
```

### The opaque handle

The stored token carries a `sink_handle: str` that **only the sink
interprets**. `markdown-vault-mcp` puts its vault-relative `path`;
`image-generation-mcp` puts its `image://<id>` URI. pvl-core stores it,
echoes it in tool payloads, and hands it back to `sink.read` / `sink.write` —
but never parses it. This is the generalization of vault's current
`path` + `is_attachment` token fields, and it is what keeps domain branches
out of core: there is no code path in pvl-core that behaves differently based
on handle content, so there is no seam for domain logic to leak through.

Content validation (extension allowlists, existence/stat checks, MIME sniffing)
lives entirely in the `TransferValidator` — it is precisely *"what bytes are
acceptable,"* which pvl-core cannot answer for a downstream's domain.

### The sink is byte-oriented by deliberate choice (bounded, not streamed)

`TransferSink.read` returns `bytes` and `write` accepts `bytes` — the whole
body is materialized in memory, bounded by `TRANSFER_MAX_UPLOAD_BYTES` /
`TRANSFER_FETCH_MAX_BYTES` (§7). This is a **conscious decision, not an
oversight**, and it is faithful to every implementation this ADR lifts: vault's
route buffers `b"".join(chunks)` before `write` and `b64decode(...)` before
serving; vault's `fetch` buffers `b"".join(chunks)`; image-gen's `artifacts.py`
does `read_bytes()`. None stream to/from the sink today.

The alternative — a constant-memory streaming sink (async byte-iterator on
`read`, `BinaryIO`/async-chunk stream on `write`) — was the design the
**abandoned** file-exchange data plane converged on (#107 → #162 → #171), and it
dragged in precisely the generator-lifecycle and temp-file-cleanup complexity
(`aclose()`, spooled temp files, `guarded_stream`) that entangled EPIC #138.
Adopting it here would re-import that surface for a byte-buffering property none
of the lifted servers need at their current caps. **Streaming is therefore a
deliberately deferred, opt-in future extension** (§12), not a v1 shape:
the size caps are the memory bound, and a streaming sink variant can be added
non-breakingly if a downstream ever needs constant-memory transfer above its
cap. The "streaming" retained from vault (§8) is the **receive-side size-cap
loop** — reading the request/response in chunks to *abort early* past the cap —
not constant-memory handoff to the sink.

---

## 4. Q2 — Source set: two planes, three ingest front-ends, exported primitives

The four capabilities collapse cleanly:

- **Egress plane:** `download-link` → `sink.read(handle)` → serve bytes.
- **Ingest plane:** `upload-link` / `url-fetch` / `base64-inline`
  → *resolve bytes* → `validate` → `sink.write(handle, body)`.

The three inbound sources are thin front-ends onto **one**
`resolve → validate → write` core. They differ only in how bytes are
*resolved*:

- upload-link: bytes arrive as a streamed HTTP request body (size-capped);
- url-fetch: bytes are pulled by `fetch_url` (SSRF-hardened, size-capped);
- base64-inline: bytes are produced by `decode_base64_capped`.

### Primitives are exported, not buried

Per the maintainer steer, the two mechanics under the ingest plane are
**standalone public primitives**, usable independently of the transfer
feature:

- `fetch_url(url, *, max_bytes, timeout_s) -> FetchResult` — safely fetch any
  URL to bytes (SSRF guard, DNS-rebind pin, redaction). Useful anywhere a
  server needs to pull a URL, not just for ingest.
- `decode_base64_capped(data, *, max_bytes) -> bytes` — decode with a size
  cap.

`register_transfer_routes` **consumes** these; it does not own them. This is
not a contradiction of the "framework, not toolkit" decision: the *packaged
route and tools* are framework (pvl-core owns their shape), while an
SSRF-hardened fetch is its own reusable capability that happens to also be a
building block of ingest.

---

## 5. Module decomposition (`src/fastmcp_pvl_core/_transfer/`)

| File | Contents | Public export |
|---|---|---|
| `fetch.py` | scheme allowlist, `_resolve_pinned_ip` (DNS-rebind pin), blocked-host set, streaming size cap, userinfo+query redaction (logs **and** exceptions — §8), Host/SNI handling, redirects disabled | `fetch_url` |
| `base64.py` | decode + size cap | `decode_base64_capped` |
| `store.py` | `TransferStore` over `build_kv_store(config, namespace="transfer")`; opaque handle; `available ↔ in_flight` with grace-settle on success (explicit `burn` → `consumed`); TTL expiry; release-on-failure | *(internal — see correction below)* |
| `sink.py` | `TransferSink` Protocol + `TransferValidator` type — **the only things downstream implements** | `TransferSink`, `TransferValidator` |
| `routes.py` | `make_transfer_handler(store, sink)` — GET=download, POST/PUT=upload, streaming caps, RFC 6266 Content-Disposition, complete/release | *(internal)* |
| `register.py` | `register_transfer_routes(mcp, config, *, sink, validate)` — owns route + both link tools + TTL clamp + `base_url` guard, wiring one shared store | `register_transfer_routes` |
| `config.py` | `TransferConfig` (env section, §7) | `TransferConfig` |

> **Correction (post-implementation):** the `store.py` row's "Public export" originally
> read `TransferStore`. The shipped implementation never exported it — neither
> `_transfer/__init__.py`'s `__all__` nor the top-level package re-export it — and
> `_transfer/__init__.py`'s own docstring says plainly *"The store, handler, and
> route mechanics stay internal — pvl-core owns their shape."* This table cell was
> wrong from the first implementing PR; it misled a later downstream (the
> `fastmcp-server-template` DOMAIN-WIRING example) into importing
> `fastmcp_pvl_core._transfer.store` directly. Only `fetch_url` and
> `decode_base64_capped` are standalone exported primitives (§4).

All intra-package imports stay relative (`from .store import …`) so a
fold-in remains a directory rename (`CLAUDE.md` foldability rule).

---

## 6. The token store — KV-backed, release-on-failure preserved

This is the design's one genuinely hard point and its most consequential
decision, so it is spelled out in full.

### 6.1 Why not delete-on-claim

A tempting simplification over a TTL-capable KV is *delete-on-claim*: burn the
token the instant it is claimed, and let native TTL handle expiry. **Rejected.**
Operational experience with the vault implementation shows mid-serve /
transient failures are **common**, not rare. Delete-on-claim would spend the
one-time link on the *first* failed attempt, forcing a re-mint every time a
download stalls or an upload connection drops. Vault deliberately built
**release-on-failure** — a failed `in_flight` reservation returns to
`available` so the link survives — precisely because this path is hot. That
behavior is load-bearing and must be preserved.

### 6.2 The state machine over KV

The record lives in KV with the **token's expiry as the KV entry TTL**, so an
expired token (and any abandoned reservation past its own TTL) vanishes with
no sweep loop — this replaces vault's hand-rolled `_sweep_expired`. The record
value carries `status ∈ {available, in_flight, consumed}`, a per-claim `fence`
(a fresh id stamped on each claim so a superseded holder cannot mutate the
reservation that replaced it), and, while `in_flight`, a short
`lease_expires_at`:

- `claim(token, kind)` → mark `in_flight` with `lease = now + lease_seconds`
  and a fresh `fence` returned to the caller (a *crashed or slow* handler's
  reservation auto-frees once the lease lapses; the fence lets a lapsed-then-
  reclaimed holder's late `release`/`complete` no-op rather than corrupt the
  new holder);
- `release(token, fence)` → revert `in_flight → available` on transient
  failure, keeping the **full remaining TTL** (a long retry window — the link
  survives);
- `complete(token, fence)` → **grace-settle** `in_flight → available` with the
  TTL shrunk to `min(remaining, grace_seconds)` on success — **not** a hard
  burn (see below);
- `burn(token, fence)` → `consumed` (a hard, strict-one-shot burn) — the
  explicit alternative to `complete`, kept for a caller that cannot tolerate a
  grace-window replay.

**Grace-settle over hard burn (revised from the original design).** The first
cut had `complete → consumed` (burn on success). That reintroduces the exact
hazard §6.1 rejects: for a *download*, the handler must signal "done" **before**
the buffered response body is transmitted to the client (the body is sent after
the handler returns), so a burn-on-complete spends the one-time link the instant
the bytes leave the sink — a client stall or lost ack mid-transmission strands
the caller with nothing delivered (the failure `markdown-vault-mcp` hit in
practice). Since §6.3 already establishes that **the TTL is the security bound
and strict single-use is only hygiene**, the resolution is to *shrink the TTL to
a short grace* on success instead of burning: the link stays briefly reclaimable
(a stalled download simply retries within the grace window) and then ordinary
KV-TTL expiry removes it, with no sweep. `min(remaining, grace_seconds)` never
*extends* the TTL, and once `remaining ≤ grace_seconds` it keeps the shrinking
`remaining` — so the absolute expiry is pinned at the first settle and does not
slide across retries. Strict one-shot remains available as the explicit `burn`.
`grace_seconds` is operator config (`TRANSFER_GRACE_TTL_S`, §7).

### 6.3 Correctness boundary — stated honestly

`build_kv_store` returns an `AsyncKeyValue` facade exposing get / put / delete
/ ttl but **no compare-and-set**. The correctness of `claim` therefore depends
on deployment topology:

| Topology | Store URL | One-time correctness | Restart survival |
|---|---|---|---|
| single worker | `memory://` | **correct** (in-process lock) | no |
| single worker | `file://` / `redis://` / … | **correct** (in-process `asyncio.Lock` serializes claim/release/complete/burn against the shared KV) | yes |
| multiple worker **processes** / replicas sharing one backend *(not planned)* | `redis://` / `dynamodb://` / … | **best-effort by design** — a narrow same-token retry race; TTL still bounds the link (see below) | yes |

The key insight: within one process (one event loop) an `asyncio.Lock` around
the read-modify-write gives true mutual exclusion **even though the data lives
in KV** — so the deployment these servers actually run (a single
`serve --transport http` process, one FastMCP worker) gets restart survival,
correct one-time semantics, and release-on-failure together. That is the target
topology; the current in-process singleton stores (vault's `TransferStore()`
closure, image-gen's `_artifact_store` global) are already single-process, so
KV-backing is a pure upgrade over the status quo, not a new constraint.

The multi-*process* row exists only for a future that is **not planned** —
horizontally scaled replicas sharing one backend, where a token minted on one
replica is redeemed on another. That case is *why* the store is KV-backed at
all (cross-replica redemption is impossible without a shared store), but the
in-process lock would no longer serialize across replicas.

Crucially, this is not a correctness *gap* to be closed later — it is a
deliberate scoping of what "one-time" guarantees. **TTL is the
security-relevant bound**: it caps a link's lifetime regardless of topology.
Strict single-use is *hygiene*, not a security property — the token is an
unguessable 32-byte secret, so the only realistic concurrent-claim is a single
holder's own retry, which is benign (idempotent download; last-writer-wins
upload the sink's write path already tolerates). Under the current
single-process deployment single-use is exact; under a future multi-replica
deployment it degrades to best-effort, and that is **acceptable by design**. If
strict cross-replica single-use is ever wanted, it is a **non-breaking**
optional CAS-backed `claim` (redis `SET NX` / DynamoDB conditional write) —
never a regression to delete-on-claim — but it is a nicety, not a prerequisite.

---

## 7. Q4 — Configuration

Split along the `CLAUDE.md` axis: **operator config → environment variables**
(never kwargs); **domain behavior → hooks** (never config).

Already generic in `ServerConfig`: `base_url`, `kv_store_url`. New
`TransferConfig` env section, read via `from_env(prefix)` and registered in the
`domain_env_suffixes()` drift gate:

| Env var (suffix) | Purpose |
|---|---|
| `TRANSFER_TTL_DEFAULT_S` | link lifetime when the caller omits one |
| `TRANSFER_TTL_MAX_S` | ceiling; a caller-requested TTL is clamped to this |
| `TRANSFER_GRACE_TTL_S` | post-success grace window; `complete` shrinks the TTL to `min(remaining, this)` (§6.2) |
| `TRANSFER_LEASE_S` | crashed-handler reclaim window for an `in_flight` reservation (§6.2) |
| `TRANSFER_MAX_UPLOAD_BYTES` | per-upload size cap |
| `TRANSFER_FETCH_MAX_BYTES` | per-fetch size cap |
| `TRANSFER_FETCH_TIMEOUT_S` | fetch timeout |

The scheme allowlist (`http`, `https`) is **fixed = shape**, not configurable —
loosening it is an SSRF policy change pvl-core owns. Per-domain concerns (the
sink, the validator, content-type/extension policy) are **not** config; they
are the hooks.

There is **no override kwarg** for any shape element. If a downstream believes
it needs a different tool name, route, or status code, the resolution is for
pvl-core to change the shape for everyone — not to grow a kwarg.

---

## 8. Q5 — Salvage

**Lift as-is** (from `markdown-vault-mcp`, the stronger copy):

- `_resolve_pinned_ip` + `_ip_is_blocked` — DNS-rebinding-safe SSRF guard
  (validate every resolved address, pin the validated IP into the connection,
  fail closed);
- scheme allowlist; the receive-side chunked size-cap loop (reads the
  request/response in chunks to abort early once the cap is exceeded — this is
  cap enforcement, **not** constant-memory handoff to the sink, per §3);
  userinfo + query-string redaction across **all emit-paths** (log lines **and
  exception messages** — an operator-supplied URL carries credentials in
  userinfo/query, and both sinks reach process logs; #122 shipped the log path
  but missed the exception path); Host header + TLS SNI preservation
  with pinned-IP dial; redirects disabled;
- RFC 6266 Content-Disposition builder;
- the `available ↔ in_flight` state machine + lease reclaim (re-expressed over
  KV TTL, grace-settling on success, per §6);
- TTL clamp and `base_url`-required guard.

**Salvage as principles/tests** (from archived #139 / #140 / #141): path
confinement, SSRF hardening, and redaction discipline — as invariants and test
cases, **not** as code (the archived code carried wire-format baggage).

**Drop:** the `exchange://` URI scheme; 4 roles × 3 transports; the vendored
schema; the 55 conformance fixtures; version-skew / must-understand
negotiation; the external `mcp-file-exchange-ext` pin. `image-generation-mcp`'s
weaker `ArtifactStore` is **superseded, not lifted**.

---

## 9. Q6 — Adoption / migration

Per `CLAUDE.md` ("pre-existing downstream conflicts resolve by migration"),
pvl-core ships the shape and downstream migrates to it — no compatibility
shim in pvl-core.

### `markdown-vault-mcp`

- Implement `VaultTransferSink.read/write` over `vault.reader` / `vault.writer`;
  a `TransferValidator` wrapping the existing `_validate_source` /
  `_validate_destination`.
- Delete its `transfer/` package and the SSRF block in `_server_tools/writer.py`;
  re-express the `fetch` tool on the public `fetch_url` primitive.
- Migration lands in a `markdown-vault-mcp` PR; if it cannot land in the same
  cycle as the pvl-core release, a tracked downstream issue coordinates the
  cutover.

### `image-generation-mcp`

`image-generation-mcp#300` ("get external images into the gallery: base64 /
URL / upload") has **not yet settled** its shape — it lists two candidate
directions still to be brainstormed. This ADR is deliberately **agnostic to
that fork**, and being agnostic is the point: pvl-core owns the transfer
mechanics so image-gen can pick its domain shape later. Whichever #300 chooses,
the transfer lift unblocks it without prejudging it:

- **If #300 keeps ingest in the resolver** (new `data:` / `http(s)://` source
  kinds in `_input_images.py`): image-gen consumes the exported **`fetch_url` /
  `decode_base64_capped`** primitives directly — no sink, no routes. This is
  exactly *why* those primitives are exported standalone (§2.2/§4).
- **If #300 adds an add-to-gallery capability** (ingest once → stable
  `image_id`, then existing `image://` resolution takes over): image-gen
  implements a `TransferSink` over `register_transfer_routes` (download via
  `ImageService.get_image`; upload/fetch via its own gallery-write primitive)
  and retires the weaker `artifacts.py` download-only store.

The concrete hook names, provenance field, and (a)-vs-(b) choice belong to
#300, not to this ADR. What pvl-core commits to is that both paths are
supported by the same lifted mechanics; image-gen gains a real SSRF-hardened
URL fetch it has no equivalent of today.

Coexistence during transition: pvl-core releases first; each downstream cuts
over in its own PR. Nothing forces a lockstep release.

---

## 10. Q7 — Anti-scope guardrails

The constraints that keep this from re-becoming #138:

1. **No wire protocol.** No URI scheme, no cross-server negotiation, no
   external `ext` pin, no vendored schema, no conformance fixtures.
   `/transfer/{token}` is an implementation detail of a single server, **not**
   a published contract — and therefore **not** documented under `docs/specs/`.
2. **No shape-override kwargs.** Tool names, route path, status codes, and the
   scheme allowlist are pvl-core's. The kwargs are the two hooks
   (`sink`, `validate`) plus the optional `download_note` / `upload_note`, which
   *append* domain context to the generic tool descriptions without replacing
   them; the only tuning is env config. A reviewer rejects any kwarg that
   overrides a shape decision (a note does not — it adds, it cannot replace).
3. **Opaque handle.** pvl-core never interprets the sink handle, so no
   domain-aware branch can grow in core.
4. **Naming + housekeeping.** Module is `_transfer`; the empty
   `_file_exchange/` directory is removed. The word "exchange" — which dragged
   #138 toward interop — is not reused.
5. **Primitives stay primitive.** `fetch_url` / `decode_base64_capped` resolve
   bytes; they do **not** grow content-type negotiation or transforms.
   `image-generation-mcp`'s resize/convert stays in its sink, not in core.
6. **Epic capped at the five issues below**; audit between issues before
   opening the next (`CLAUDE.md` epic discipline).

---

## 11. Deliverable — decomposition into shippable issues

Each issue is small, independently testable, and ships on its own. Issues 1–2
deliver reusable value **before** the full transfer feature exists.

| # | Issue | Depends on | Notes |
|---|---|---|---|
| 1 | `fetch_url` SSRF-hardened primitive + fetch config knobs | — | standalone, no store; smallest, highest reuse; ships first. Creates the `_transfer/` package; the residual `_file_exchange/` (§2.5/§10.4) is an untracked empty dir with nothing to remove from git — this PR just confirms no residue remains |
| 2 | `decode_base64_capped` primitive | — | tiny; may ride with #1 |
| 3 | KV-backed `TransferStore` (state machine + TTL + release-on-failure) | — | store only, no routes; §6 is its spec |
| 4 | `TransferSink` / `TransferValidator` protocols + `make_transfer_handler` route | 3 | egress + ingest handler |
| 5 | `register_transfer_routes` + the two link tools + `TransferConfig` + `__init__` exports | 3, 4 | ties it together |

Downstream migrations (`markdown-vault-mcp`, `image-generation-mcp`) are
**separate follow-on trackers in their own repos**, not part of this pvl-core
epic's scope.

---

## 12. Consequences

**Positive**

- One hardened SSRF/transfer implementation; the next hardening iteration
  benefits every downstream at once.
- `image-generation-mcp` gains a real SSRF-hardened URL fetch it has no
  equivalent of today; and, should #300 adopt the add-to-gallery path, the
  upload half plus retirement of its weaker `ArtifactStore`.
- `fetch_url` becomes a reusable family-wide primitive.
- The token store finally uses the KV abstraction already built for it,
  gaining restart survival for the common single-worker deployment.

**Negative / accepted**

- Single-use is exact under the deployed single-process topology and
  best-effort-by-design under a *not-planned* multi-replica one (§6.3); **TTL,
  not single-use, is the security-relevant bound**. Strict cross-replica
  single-use is an optional future non-breaking CAS extension, explicitly not
  delete-on-claim — a nicety, not a prerequisite.
- Two downstreams must each do a migration PR (no shim); tracked if they can't
  land same-cycle.
- The sink seam is byte-materializing, bounded by the size caps (§3): a body up
  to the cap is held in RAM. The per-request bound is the cap; **peak memory is
  `concurrent-in-flight-transfers × cap`** — an operator sizing concern managed
  by the caps together with deployment-level concurrency limits, not a constant
  per-process ceiling. This matches every lifted implementation; a
  constant-memory streaming sink is a deliberately deferred, non-breaking future
  extension, explicitly not adopted in v1 to avoid re-importing the abandoned
  data plane's generator-lifecycle complexity.

**Neutral**

- The ADR names pvl-core and downstream packages in prose (allowed); it
  introduces **no** runtime self-name lookup and **no** intra-package absolute
  imports, preserving foldability.

---

## 13. Open questions for implementation time

- Exact `FetchResult` shape returned by `fetch_url` (bytes + content-type +
  final size).
- Whether `make_transfer_handler` accepts the sink directly or resolves it via
  a getter (as vault does with `get_vault_singleton`) to match pvl-core's
  dependency conventions — decided in issue #4 against the then-current code.
- Whether `TransferConfig` folds into `ServerConfig` or stays a separate
  dataclass threaded alongside it — decided in issue #5 against the
  `domain_env_suffixes()` drift-gate wiring.

[#212]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/212
