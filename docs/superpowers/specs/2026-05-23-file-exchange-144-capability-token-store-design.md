# File-Exchange #144 — capability token minter + token store

> **Status:** Contemporaneous design record for issue #144 (6/10 of EPIC
> #138). The implementation in the same PR is the source of truth; this
> captures the shape agreed before implementation. This is **not** a wire
> spec — #144 is pvl-core's own minter/store implementation, governed by
> `CLAUDE.md` and the project's framing principle, not by
> `mcp-file-exchange-ext`. No `docs/specs/` wire-format file is touched.

**Goal:** A high-entropy capability-token minter and the token store that
backs lookup, expiry, single-use, and revocation — built on the unified
`build_kv_store` factory (#122), **no new storage abstraction**. Consumed by
the download (#145) and upload (#146) data planes, which own the routes and
assemble the full capability URLs.

## Scope (from #138's decomposition + #144's scope statement)

Per #138's issue map, **#145/#146 are the "download/upload data plane: route
+ …" and depend on #144**. The route *paths* therefore live with the data
plane. So #144 mints the **token** (the ≥128-bit unguessable credential per
§12) and runs its lifecycle in the store; the full
`https://{base_url}{route_path}/{token}` is assembled by #145/#146, which own
the paths. #144 holds the `base_url`/TTL/max-size config and offers a thin
https-enforcing join helper that takes the route path as a parameter.

The token store is a **generic primitive**: the per-token `metadata` is an
opaque `Mapping[str, Any]` the store never interprets — what goes in it
(artifact size/digest/direction/storage location) is #145/#146's concern.
This keeps #144 transport-agnostic.

## Module shape

New module `src/fastmcp_pvl_core/_file_exchange/_tokens.py`:

- `CapabilityTokenStore` — wraps an `AsyncKeyValue` (from #122) + the TTL
  ceiling; exposes the mint/lookup/consume/revoke lifecycle.
- `build_capability_token_store(config) -> CapabilityTokenStore` — factory
  that calls `build_kv_store(config, namespace="file-exchange-tokens")` and
  threads the TTL ceiling from config. Mirrors `build_event_store`.
- `MintedToken` / `TokenRecord` — small frozen dataclasses for the mint
  result and the looked-up record.
- `capability_url(base_url, path, token) -> str` — the https-enforcing join
  helper.

Expiry is delegated entirely to the KV layer: `put(..., ttl=…)` plus `get`
returning `None` after expiry. No hand-rolled expiry bookkeeping.

## API

```python
def mint(
    self,
    metadata: Mapping[str, Any],
    *,
    ttl: float,
    single_use: bool = True,
) -> MintedToken: ...

def lookup(self, token: str) -> TokenRecord | None: ...

def consume(self, token: str) -> bool: ...

def revoke(self, token: str) -> None: ...
```

(All `async def` — the `AsyncKeyValue` backend is async.)

### `mint`

1. `token = secrets.token_urlsafe(32)` — 256 bits, URL-safe (≥128 bits, §12).
2. `effective_ttl = min(ttl, ttl_ceiling)` — clamp to the operator ceiling
   (§10.2 "expiresAt SHOULD be the shortest value").
3. `await store.put(token, {"metadata": dict(metadata), "single_use": single_use}, ttl=effective_ttl)`.
4. Return `MintedToken(token=token, expires_at=now + effective_ttl)` so the
   caller builds the descriptor's `expiresAt` from the *clamped* TTL (which it
   cannot otherwise observe). `expires_at` is a tz-aware UTC `datetime`
   (matches the wire models' `AwareDatetime` `expiresAt` fields).

### `lookup`

`record = await store.get(token)`; return `TokenRecord(metadata=record["metadata"], single_use=record["single_use"])` or `None`. `None` covers
absent / expired (the KV TTL handles expiry) — no mutation.

### `consume` (single-use lifecycle)

Called *after a fully-successful transfer* (§10.2: opening a connection alone
must not invalidate the descriptor — the route calls `consume` only on
completion).

- Read the record. If absent/expired → return `False`.
- If `single_use` → `return await store.delete(token)` (the `delete` bool gives
  at-most-once under a race: exactly one concurrent caller observes `True`,
  §10.3 "at most one successful upload").
- If multi-use (`download` `singleUse: false`) → no-op, return `True` (the
  token stays valid for repeat retrieval until its TTL).

Strict cross-process atomicity rides on the backend's `delete` atomicity
(redis `DEL` is atomic; `memory`/`file` are in-process). The get-then-delete
window does not break at-most-once because the `delete` bool is the
authority, not the prior `get`.

### `revoke`

`await store.delete(token)` unconditionally (ignores `single_use`), per §15
("a provider … MAY invalidate references early when a transfer is known to be
complete"). Returns `None`.

## Configuration (env, operator axis)

Operator-side configuration is environment variables, not kwargs (per
`CLAUDE.md`). Two new `ServerConfig` fields, loaded in `from_env`:

- `<PREFIX>_FILE_EXCHANGE_TOKEN_TTL` → `file_exchange_token_ttl: float`,
  default **3600.0** (1h) — the TTL **ceiling** `mint` clamps to.
- `<PREFIX>_FILE_EXCHANGE_MAX_ARTIFACT_SIZE` → `file_exchange_max_artifact_size: int | None`,
  default **None** (unlimited) — **#144 does not itself consume this**; it is
  enforced by #146's upload route (`expected.maxSize` / endpoint rejection).
  Added here because #144's scope statement lists it as a deliverable and it
  is config-adjacent; #145/#146 read it from `ServerConfig`.
- `base_url` — **already exists**; reused by `capability_url`.

## URL helper

```python
def capability_url(base_url: str, path: str, token: str) -> str:
```

Joins `base_url` + `path` + `token` into the full capability URL and enforces
§12's `https` requirement (raises `ConfigurationError` if `base_url` is unset
or not https). `path` is the route path supplied by the caller (#145/#146,
which own their routes). This centralizes the §12 https / base-url checks in
#144 while keeping the route path with the data plane.

## Error handling

- A bad token (absent / expired / consumed) is **not** an exception:
  `lookup` returns `None`, `consume` returns `False`. Rendering a §13 error
  envelope for a bad token is the route layer's (#145/#146) responsibility —
  consistent with how `_selection` and #143's consume ops delegate envelope
  rendering to their caller.
- `capability_url` raises `ConfigurationError` (operator misconfiguration:
  `base_url` unset or non-https) — a config error, not a per-transfer §13
  failure.
- `mint`/`lookup`/`consume`/`revoke` propagate any underlying KV/IO error
  (the backend is operator-chosen infrastructure; a backend outage is the
  server's own error, surfaced by the route layer).

## Testing (`tests/_file_exchange/test_tokens.py`)

Backed by an in-process `memory://` KV store (built via `build_kv_store` with
a `memory://` `kv_store_url` config, or a `MemoryStore` directly):

- **Token entropy / URL-safety**: minted token is URL-safe and ≥128 bits
  (length check on `token_urlsafe(32)`); two mints differ.
- **TTL clamp**: `mint(ttl=10_000)` with ceiling 3600 → `MintedToken.expires_at`
  ≈ now + 3600 (clamped); `mint(ttl=60)` → ≈ now + 60 (unclamped).
- **Round-trip**: `lookup` returns the exact `metadata` and `single_use`.
- **Expiry boundary**: `mint(ttl=…tiny…)` → after expiry, `lookup` returns
  `None` (drive via a short real TTL or the store's clock; keep fast).
- **Single-use lifecycle**: mint(single_use=True) → lookup OK → `consume`
  returns `True` → `lookup` returns `None`.
- **Double-consume**: second `consume` returns `False`.
- **Multi-use**: mint(single_use=False) → `consume` returns `True` and
  `lookup` still returns the record (not invalidated).
- **Revoke**: `revoke` → `lookup` returns `None`; `revoke` on an
  absent/already-revoked token does not raise.
- **`capability_url`**: joins correctly; raises `ConfigurationError` when
  `base_url` is unset or non-https.
- **Config**: `from_env` reads the two new fields with their defaults.

## Public surface

Re-exported via `src/fastmcp_pvl_core/file_exchange.py` + the subpackage
`__init__.py` (both `__all__`s, alphabetical):

- `CapabilityTokenStore`, `build_capability_token_store`
- `MintedToken`, `TokenRecord`
- `capability_url`

(`build_capability_token_store` may also belong on the top-level
`fastmcp_pvl_core` factory surface alongside `build_event_store`/
`build_kv_store`; decide at re-export time.)

## References

- EPIC #138 (adopt mcp-file-exchange-ext v0.1); this is 6/10. Depends on #140
  (selection + error envelope, merged). Consumed by #145 (download) and #146
  (upload) data planes, which own the routes + URL assembly, and #148 (the
  `register_file_exchange_*` helpers + Tasks integration).
- #122 — the unified `build_kv_store` factory this is built on (no new
  storage abstraction).
- Wire spec (`mcp-file-exchange-ext`, pinned `5f50a4e…`): §12 (capability
  URLs — https, ≥128-bit entropy, one-artifact-one-direction, expiry), §10.2
  (download URL: ≥128-bit token, expiry, single-use-after-full-retrieval),
  §10.3 (upload URL: at-most-one-successful-upload, expiry). This document is
  **not** a wire spec.
- `CLAUDE.md` — operator config is env vars (TTL ceiling, max size), not
  kwargs; shape decisions (token entropy, namespace) live in pvl-core.
