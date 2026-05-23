# File-Exchange #143 — filesystem transport binding

> **Status:** Contemporaneous design record for issue #143 (5/10 of EPIC
> #138). The implementation in the same PR is the source of truth; this
> captures the shape agreed before implementation. This is **not** a wire
> spec — #143 is pvl-core's own internal binding of the mechanism-agnostic
> hooks (#142) to the `filesystem` transport, governed by `CLAUDE.md` and
> the project's framing principle, not by `mcp-file-exchange-ext`. No
> `docs/specs/` wire-format file is touched.

**Goal:** Bind the mechanism-agnostic `ArtifactSource`/`ArtifactSink` hooks
(#142) to actual filesystem reads and writes, using the `exchange://`/`file://`
resolution + path confinement (#141) and the `atomic_write` helper (#142).
Implements the filesystem path for all four roles — `provider`, `fetcher`,
`receiver`, `sender` — and resolves the two items earlier issues deferred to
here: the open-time TOCTOU defence (#141) and the deposit-file permission
policy (#155).

## Scope (from #138's decomposition + #143's scope statement)

The shared volume is a **staging area**; the #142 hooks bridge it to each
server's real storage. #143 implements exactly the four mint/consume ops in
#143's scope statement and **stops at deposit** on the push side. After the
sender writes bytes to the deposit path, the receiver only ingests them into
its `ArtifactSink` lazily, on a later tool call (§8.2 step 6). That lazy
ingest needs `artifactId`→path correlation state, which #138's issue map
places in #144 (token/correlation store on the #122 KV factory) and #148
(register helpers + Tasks integration) — **not** here. `ArtifactSink` is still
exercised within #143 through the pull/fetcher path, so the end-to-end test is
meaningful.

A pvl-core-built server can play any of the four roles depending on the tool,
so a downstream server implements **both** hooks; the mock servers in the
end-to-end test do the same.

## Module shape

New module `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`. **Free
functions**, mirroring the rest of the package (`select_source`,
`resolve_filesystem_uri`) — no service-object pattern is introduced. The four
role ops are thin compositions over two private byte primitives, the existing
selection/confinement helpers, and the hooks.

## Two private byte primitives

These factor every byte-moving concern (hashing, sizing, atomic write,
TOCTOU-safe read, verify-before-use) into two functions the role ops compose.

### `_stage(source, key, target) -> tuple[int, str]`

The write primitive — used by **provider mint** (stage a source artifact) and
**sender consume** (write a push deposit).

1. `stream, meta = await source.open_artifact(key)`.
2. Wrap `stream` in a chunked counting + SHA-256 reader (`_HashingReader`).
3. `atomic_write(target, wrapped, mode=0o664)` under `asyncio.to_thread` (it
   is sync, blocking file I/O).
4. pvl-core closes the source stream (per #142: the source returns the stream,
   pvl-core reads and closes it).
5. Return `(size, "sha-256:" + hexdigest)`.

Single-pass: the source stream may be a non-seekable pipe/socket (#142), so
size+digest are computed *while copying*, never by a second read of the
source.

### `_ingest(path, artifact, sink, artifact_id) -> None`

The read primitive — used by **fetcher consume**. (`artifact` is the handle's
`ArtifactMetadata`; named to avoid colliding with the `IntakeTicket.expected`
*constraints* concept. Two passes over one fd.)

1. TOCTOU-safe open (see below) → a regular-file read-only fd.
2. **Pass 1:** chunked read → size + SHA-256. Verify against
   `artifact.size`/`artifact.digest` *when present* (§7.1 says both are
   optional but SHOULD be set). Raise `size-mismatch`/`digest-mismatch` on
   mismatch — *before the sink sees any bytes* (§15 "validate … before use").
3. `seek(0)`.
4. **Pass 2:** `await sink.store_artifact(artifact_id, artifact, stream)` — the
   sink reads to completion and does not close (per #142). pvl-core closes the
   fd afterwards.

Two passes over a single fd keep memory constant (chunked, never buffering the
whole artifact) and guarantee the sink only ever receives verified bytes. The
local confined file is seekable, so a second open (and second TOCTOU window) is
avoided. (The future `download` transport (#145) cannot seek a single-use HTTP
body and will verify *while* streaming; that is #145's concern, not a
constraint on this primitive.)

## The four role ops

All take `volume_map: VolumeMap` (the #141 type). The consume ops take the
**already-selected** descriptor — selection (`select_source`/`select_sink`)
stays the caller's step, consistent with `_selection` returning `None` so the
caller renders `no-supported-transport`.

| Op | Signature | Behaviour |
|---|---|---|
| `filesystem_provider_mint` | `(source, key, *, volume, volume_map) -> TransferHandle` | allocate opaque relpath under `volume` → `_stage` → `FilesystemSource(uri="exchange://{volume}/{relpath}")` + `TransferHandle(type, version=SPEC_VERSION, artifact=…, sources=[…])` with computed `size`+`digest` folded into the artifact metadata |
| `filesystem_fetcher_consume` | `(handle, source, sink, *, volume_map) -> None` | confine `source.uri` → `_ingest(path, handle.artifact, sink, handle.artifact.id)` |
| `filesystem_receiver_mint` | `(artifact_id, *, volume, volume_map, expected=None) -> IntakeTicket` | allocate opaque deposit relpath under `volume` → `FilesystemSink(uri=…)` + `IntakeTicket(type, version, artifactId=artifact_id, expected, sinks=[…])`. No hook is called and no bytes are written — minting only |
| `filesystem_sender_consume` | `(sink, source, key, *, volume_map) -> None` | confine `sink.uri` → `_stage(source, key, path)` (writes the deposit atomically at `0o664`) |

Plus two accessibility predicates that produce the `is_accessible=` callbacks
the existing selection functions accept:

- `filesystem_source_readable(volume_map) -> Callable[[FilesystemSource], bool]`
  — resolve+confine the descriptor `uri`, return `os.access(path, R_OK)`.
- `filesystem_sink_writable(volume_map) -> Callable[[FilesystemSink], bool]`
  — resolve+confine, return whether the target's parent dir is writable.

An unmapped volume or a confinement failure resolves to `None` (#141) and the
predicate returns `False` → selection skips the descriptor (§9).

## Decisions

### Deposit / staged file permissions (resolves #155)

A **fixed `0o664`** (owner rw, group rw, other r) for every file pvl-core
writes onto the shared exchange volume — both a provider-staged source file and
a sender-written deposit. §10.1.3 requires a source file to be readable, and a
sink's target writable, by the *other party's* OS identity; `mkstemp`'s
accidental `0o600` breaks that on a shared volume. This is a single pvl-core
**shape** (no env, no kwarg): downstream conforms.

Applied by `fchmod`-ing the temp fd **before** `os.replace`, so the final file
is never briefly `0o600` and the rename stays atomic. Mechanically:
`atomic_write` gains `mode: int | None = None` — `None` preserves today's
`mkstemp` `0o600` (so the generic primitive and its existing test are
untouched); `_stage` passes `0o664`.

### Path allocation

`uuid4().hex` opaque relpath under the chosen volume root. The artifact `name`
(§7.1: "MUST NOT be used as a filesystem path") and the receiver-chosen
`artifactId` (an untrusted wire string that may contain `/` or `..`) are
**never** used to build a path. The parent directory is created with
`mkdir(parents=True, exist_ok=True)` before the write (`atomic_write` requires
an existing parent).

### Mint target volume

An explicit `volume=` parameter names which mapped volume to stage into. *How*
a real server chooses it (single designated outbound volume, per-peer
selection, …) is #148's register-helper concern and out of scope here. Minting
into a volume absent from `volume_map` is a `ConfigurationError`-class caller
mistake.

### Digest

pvl-core emits `sha-256` (the §7.1 example) — its shape. The fetcher parses the
declared `algo:` prefix of `expected.digest` and verifies it, supporting
`sha-256`/`sha-384`/`sha-512` (mapped to `hashlib`); an unknown algorithm or
any mismatch raises `digest-mismatch` (an undecodable/unsupported algorithm is
treated as a verification failure, not a silent skip — §15 "MUST verify and
fail").

### TOCTOU-safe open (resolves #141's deferral)

The confinement check in #141 is resolution-time; the residual race is a
symlink swapped on the final component between confine and open. Defence:
`os.open(path, os.O_RDONLY | os.O_NOFOLLOW)`, then re-confine the opened path
and assert via `os.fstat` that it is a regular file. This rejects the
final-component symlink swap. Full per-component `openat` walking is
deliberately **not** done — §10.1's own closing line ("sharing a volume
already implies a trust boundary") makes it overkill for this transport.

## Error handling

A typed exception **`FileExchangeTransferError(code: TransferErrorCode, *,
transport="filesystem", detail=None)`** carries a §13 code. The consume ops
raise it: `not-accessible` (confinement/access failure), `size-mismatch`,
`digest-mismatch`, and `transfer-failed` for an underlying hook/IO error.
`#148`'s fastmcp middleware maps it onto the wire response via
`build_file_exchange_error` (which, per its own docstring, needs that
middleware to set wire `isError`+`_meta` together — so #143 raises rather than
returns an envelope, exactly as `_selection` delegates `no-supported-transport`
rendering to its caller).

`_stage`/`atomic_write` stay fail-safe: on any error the temp file is removed
and the target is left untouched — no partial deposit.

## Testing (`tests/_file_exchange/test_filesystem.py`, plus an e2e module)

Unit:

1. `atomic_write` mode param — default (`None`) still yields `0o600` (existing
   `test_paths.py` assertion holds); explicit `0o664` is asserted on the
   written file.
2. `_stage` — size+digest computed correctly from a `BytesIO` and from a
   non-seekable stream; resulting file is `0o664`; atomic (a source raising
   mid-copy leaves no target and no orphan temp).
3. `_ingest` — verifies size+digest; on a tampered file the sink's
   `store_artifact` is **never called** and `size-mismatch`/`digest-mismatch`
   is raised (proves verify-before-use); a matching file reaches the sink
   intact.
4. TOCTOU — a symlink as the final path component is rejected at open
   (`O_NOFOLLOW`); a non-regular target is rejected.
5. Accessibility predicates — readable/writable detection; unmapped volume and
   confinement escape → `False`.
6. Confinement escape on a consume op → `not-accessible`.

End-to-end (`test_filesystem_e2e.py`): two pvl-core-built mock servers, each
implementing **both** hooks, sharing a `tmp_path` volume. **Pull** flow full
round-trip — `filesystem_provider_mint` on A → `select_source` →
`filesystem_fetcher_consume` on B → bytes land in B's `ArtifactSink`, with
size+digest verified. **Push** flow to deposit — `filesystem_receiver_mint` on
B → `select_sink` → `filesystem_sender_consume` on A → assert the deposited
file's content and `0o664` mode. Filesystem transport only.

Namespace re-export: each new public name reachable via
`fastmcp_pvl_core.file_exchange`.

## Public surface

Re-exported via `src/fastmcp_pvl_core/file_exchange.py` and the subpackage
`__init__.py` (both `__all__`s updated, alphabetical). Names are
transport-qualified so #145/#146's `download`/`upload` ops won't collide:

- `filesystem_provider_mint`, `filesystem_fetcher_consume`,
  `filesystem_receiver_mint`, `filesystem_sender_consume` — the four role ops
- `filesystem_source_readable`, `filesystem_sink_writable` — selection
  predicates
- `FileExchangeTransferError` — the §13-coded exception

`atomic_write` is already exported (it gains the `mode` param). The two byte
primitives (`_stage`, `_ingest`, `_HashingReader`) stay private.

## References

- EPIC #138 (adopt mcp-file-exchange-ext v0.1); this is 5/10. Depends on #141
  (`exchange://` + confinement) and #142 (hooks + `atomic_write`).
- #155 — deposit file permissions; resolved here as the fixed `0o664` shape.
- #144 (token/correlation store) and #148 (register helpers + Tasks
  integration) — own the receiver lazy-ingest + `artifactId` correlation that
  this issue stops short of.
- Wire spec (`mcp-file-exchange-ext`, pinned commit `5f50a4e…`): §7.2.1/§7.2.3
  (descriptors), §8.1/§8.2 (pull/push flows), §9 (selection), §10.1
  (filesystem semantics + obligations), §15 (untrusted content, integrity),
  §16 (per-role conformance). This document is **not** a wire spec.
- `CLAUDE.md` — "Hooks expose domain-specific behaviour only"; the framing
  principle that keeps the transport binding mechanism-specific and the hooks
  mechanism-agnostic.
