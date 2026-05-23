# File-Exchange #142 — mechanism-agnostic byte-source / byte-sink hook contracts

> **Status:** Contemporaneous design record for issue #142 (4/10 of EPIC
> #138). The implementation in the same PR is the source of truth; this
> captures the shape agreed before implementation. This is **not** a wire
> spec — #142 is pvl-core's own internal abstraction, governed by
> `CLAUDE.md` and the project's framing principle, not by
> `mcp-file-exchange-ext`. No `docs/specs/` wire-format file is touched.

**Goal:** Define the two public hook protocols downstream servers implement
to produce and deposit artifact bytes — `ArtifactSource` and
`ArtifactSink` — plus a transport-agnostic atomic write-then-rename helper,
and a test that statically proves no transport name leaks into a hook
signature.

## The load-bearing principle: hooks are mechanism-agnostic

A downstream server implements these hooks to answer one domain question
each: *"where do the bytes for an artifact I offer come from?"* and *"where
do the bytes for an artifact I receive go?"* The **transport** that carries
those bytes between two parties — a shared filesystem volume (#143), an
HTTPS download/upload (#145/#146) — lives **entirely behind** the hook and
**MUST NOT** appear in its signature. A hook cannot tell which transport is
in use; everything pvl-core does to bridge a hook to a transport (staging a
volume file, serving an HTTP route, spooling a body, computing a digest) is
pvl-core-internal.

The two hooks are exact mirrors over a single byte carrier.

## Byte carrier and sync/async shape (decided up front)

- **Byte carrier:** one type — a synchronous `typing.BinaryIO`. Streaming
  (not in-memory `bytes`) so large artifacts never fully materialise, and so
  pvl-core can compute size+digest while copying.
- **Hook methods:** `async def`. pvl-core's data plane is async (FastMCP);
  always-await is the clean shape and keeps the protocol typing simple. This
  is a contract-cleanliness choice for an async-native server, **not**
  transport projection — the byte payload itself stays a *sync* `BinaryIO`;
  only the hook *method* is async. (This supersedes an earlier
  sync-or-async stance from the now-removed v0.x design.)

## The two contracts

New module `src/fastmcp_pvl_core/_file_exchange/_hooks.py`.

```python
@runtime_checkable
class ArtifactSource(Protocol):
    """Downstream hook: produce the bytes for an artifact this server offers.

    Mechanism-agnostic. pvl-core bridges this to whatever transport carries
    the bytes; the transport never appears here.
    """

    async def open_artifact(self, key: str) -> tuple[BinaryIO, ArtifactMetadata]:
        """Return a readable byte stream plus the metadata the server knows.

        ``key`` is the server's own opaque identifier for the artifact it is
        offering (a domain key, not a wire field). The caller (pvl-core)
        reads the stream to completion and closes it, and computes/records
        size+digest itself — so the returned ``ArtifactMetadata`` need only
        carry what the server knows (e.g. name, mimeType). Raise on failure.
        """


@runtime_checkable
class ArtifactSink(Protocol):
    """Downstream hook: deposit the bytes for an artifact this server receives.

    The exact mirror of :class:`ArtifactSource`. Mechanism-agnostic.
    """

    async def store_artifact(
        self, artifact_id: str, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        """Read ``stream`` to completion and deposit its bytes durably.

        ``artifact_id`` is the wire id of the artifact being received (an
        ``IntakeTicket.artifactId`` on the push side, or a
        ``TransferHandle.artifact.id`` on the pull side). The caller
        (pvl-core) owns ``stream`` — it may hand the sink a counting/hashing
        wrapper so it can verify size+digest as the sink reads — so the sink
        reads but does **not** close it. Return ``None`` on success; raise
        on failure.
        """
```

**Notes on the shape:**

- **Mirrors over one stream.** Source produces `(stream, metadata)`; sink
  consumes `(id, metadata, stream)`. The source returns metadata *with* the
  stream because pvl-core uses both together at provide-time (copy bytes
  into the transport while computing size+digest for the handle), so no
  separate `describe()` method is needed.
- **Naming asymmetry is intentional.** The source takes the server's own
  *domain* `key`; the sink takes the *wire* `artifact_id`. They are
  different namespaces, and the names say so.
- **Stream ownership.** Source: pvl-core reads and **closes** the returned
  stream. Sink: pvl-core owns the passed stream (the sink reads, does not
  close); pvl-core may wrap it to compute size+digest.
- **size/digest are pvl-core's job**, computed once via stream-wrapping —
  not reimplemented per downstream or per transport.

## Atomic write-then-rename helper

A single-purpose utility for any transport that deposits bytes to a local
path (the filesystem sink in #143; a download cache later). Added to
`_paths.py` — the existing filesystem-utilities home
(`canonicalize_and_confine`, URI resolution) — to co-locate filesystem
operations rather than spawn a one-function module.

```python
def atomic_write(target: Path, source: BinaryIO) -> None:
    """Write ``source``'s bytes to ``target`` atomically.

    Streams into a temp file in ``target``'s own directory (so the final
    ``os.replace`` is a same-filesystem atomic rename), flushes +
    ``os.fsync``es the temp file, then ``os.replace``s it into place — so a
    concurrent reader never observes a partial file (§10.1.3 "made visible
    atomically: write to a temporary path, then rename into place"). The
    parent directory must already exist. On any error the temp file is
    removed, leaving ``target`` untouched.

    Sync (pure file I/O); an async transport hook runs it via
    ``asyncio.to_thread`` so it never blocks the event loop.
    """
```

- **Behavior:** `NamedTemporaryFile(dir=target.parent, delete=False)` →
  `shutil.copyfileobj(source, tmp)` → `tmp.flush()` +
  `os.fsync(tmp.fileno())` → `os.replace(tmp.name, target)`; a
  `try/except` unlinks the temp on any failure and re-raises.
- **Directory-fsync** (crash-durability of the rename itself) is a
  reasonable hardening left to the implementation plan; the spec mandates
  atomic *visibility*, which `os.replace` provides.
- **Single purpose:** it does **not** compute size/digest — that stays in
  pvl-core's stream-wrapping, so digest logic is not duplicated per
  transport.

## Error handling

The hooks **raise on failure** (any exception). #142 deliberately defines
**no new exception types**: mapping a hook exception to the §13
`TransferError` envelope is the data plane's responsibility (#143+), and
`_errors.py` already owns that mapping. `atomic_write` is fail-safe — it
cleans up the temp file and re-raises, never leaving a partial deposit.

## Testing (`tests/_file_exchange/test_hooks.py`)

1. **No-transport-name introspection test (the headline guard).** Iterate
   the `Protocol` classes defined in `_hooks.py` (so a *future* hook is
   auto-covered, not just today's two); for each public async method,
   collect parameter names, each parameter's annotation repr, and the
   return annotation; assert none contains a forbidden token
   (case-insensitive): `filesystem`, `download`, `upload`, `http`, `https`,
   `exchange`, `url`, `volume`. A **negative control** — checking a
   deliberately-misnamed dummy signature (e.g. a `http_url` parameter) does
   fire — proves the check can't silently pass.
2. **`@runtime_checkable` behavior.** A conforming dummy `isinstance`-matches
   each Protocol; a non-conforming object does not.
3. **Implementability round-trip.** A dummy `ArtifactSource` returns a
   `BytesIO` + `ArtifactMetadata`; a dummy `ArtifactSink` reads a stream and
   records its bytes — both awaited — proving the async contracts are
   implementable as written.
4. **`atomic_write`.** Correct content end-to-end; on a source that raises
   mid-copy, the temp is cleaned up and `target` is absent; parent-must-exist.
5. **Namespace re-export.** `ArtifactSource`, `ArtifactSink`, `atomic_write`
   reachable via `fastmcp_pvl_core.file_exchange` (the established
   convention; both `__all__` lists updated, mirrored and alphabetical).

## Public surface

Re-exported via `src/fastmcp_pvl_core/file_exchange.py` and the subpackage
`__init__.py` (both `__all__`s updated):

- `ArtifactSource` — Protocol
- `ArtifactSink` — Protocol
- `atomic_write` — helper

## References

- EPIC #138 (adopt mcp-file-exchange-ext v0.1); this is 4/10.
- #143 (5/10) — the first consumer: binds these hooks to the filesystem
  transport and uses `atomic_write` for the sink.
- `CLAUDE.md` — "Hooks expose domain-specific behaviour only"; the framing
  principle that governs this abstraction.
- Wire types consumed: `ArtifactMetadata` (§7.1) from
  `_file_exchange/_wire.py`. This document is **not** a wire spec.
