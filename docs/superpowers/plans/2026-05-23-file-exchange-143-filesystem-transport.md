# File-Exchange #143 — Filesystem Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind the mechanism-agnostic `ArtifactSource`/`ArtifactSink` hooks (#142) to the `filesystem` transport — provider/fetcher/receiver/sender — using #141's URI confinement and #142's `atomic_write`.

**Architecture:** A new free-function module `_filesystem.py` composes two private async byte primitives (`_stage` write, `_ingest` read) over the existing selection/confinement helpers and the hooks. Deposited/staged files get a fixed `0o664` (resolves #155); reads are opened `O_NOFOLLOW` + regular-file-checked (resolves #141's TOCTOU deferral). Failures raise a §13-coded `FileExchangeTransferError` that #148's middleware will render. Stops at deposit on the push side (receiver lazy-ingest is #144/#148).

**Tech Stack:** Python 3.10+, Pydantic v2 (existing wire models), `hashlib`/`os`/`shutil`/`uuid`, `asyncio.to_thread` for blocking file I/O, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"` is already configured).

**Design doc:** `docs/superpowers/specs/2026-05-23-file-exchange-143-filesystem-transport-design.md`

---

## File Structure

- **Modify** `src/fastmcp_pvl_core/_file_exchange/_paths.py` — `atomic_write` gains an optional `mode` param (fchmod the temp before `os.replace`); widen its `source` type to `SupportsRead[bytes]` so a hashing wrapper can be passed.
- **Modify** `src/fastmcp_pvl_core/_file_exchange/_errors.py` — add the `FileExchangeTransferError` exception (a §13-coded transfer failure).
- **Create** `src/fastmcp_pvl_core/_file_exchange/_filesystem.py` — `_HashingReader`, `_stage`, `_ingest`, `_open_confined_readonly`, `_verify_stream`, `_require_volume`, the four `filesystem_*` role ops, and the two accessibility predicates.
- **Modify** `src/fastmcp_pvl_core/_file_exchange/__init__.py` — re-export the new public names.
- **Modify** `src/fastmcp_pvl_core/file_exchange.py` — re-export the new public names.
- **Modify** `tests/_file_exchange/test_paths.py` — `atomic_write` mode-param tests.
- **Create** `tests/_file_exchange/test_filesystem.py` — unit tests for primitives, ops, predicates, errors, TOCTOU.
- **Create** `tests/_file_exchange/test_filesystem_e2e.py` — pull + push end-to-end across two mock servers.

**Local checks (run after each task's tests pass, and before the final commit):**
```bash
uv run pytest tests/_file_exchange/ -q
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```

---

## Task 1: `atomic_write` gains a `mode` param

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_paths.py`
- Test: `tests/_file_exchange/test_paths.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_paths.py`:

```python
def test_atomic_write_default_mode_is_0600(tmp_path):
    from io import BytesIO

    from fastmcp_pvl_core._file_exchange._paths import atomic_write

    target = tmp_path / "out.bin"
    atomic_write(target, BytesIO(b"data"))
    assert target.read_bytes() == b"data"
    assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_explicit_mode_is_applied(tmp_path):
    from io import BytesIO

    from fastmcp_pvl_core._file_exchange._paths import atomic_write

    target = tmp_path / "out.bin"
    atomic_write(target, BytesIO(b"data"), mode=0o664)
    assert target.stat().st_mode & 0o777 == 0o664
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_paths.py::test_atomic_write_explicit_mode_is_applied -v`
Expected: FAIL — `atomic_write()` got an unexpected keyword argument `mode`.

- [ ] **Step 3: Add the `mode` param and `SupportsRead` typing**

In `_paths.py`, update the imports near the top. Replace:

```python
from typing import BinaryIO, Literal
```

with:

```python
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from _typeshed import SupportsRead
```

`BinaryIO` is still referenced by `resolve_filesystem_uri`/other signatures? It is not — only `atomic_write` used it. If `ruff` flags `BinaryIO` as now-unused elsewhere, that confirms the only use was `atomic_write`. (If any other signature still needs `BinaryIO`, keep it in the import list.)

Then replace the `atomic_write` definition's signature and body. The current head is:

```python
def atomic_write(target: Path | str, source: BinaryIO) -> None:
```

Replace with:

```python
def atomic_write(
    target: Path | str, source: SupportsRead[bytes], *, mode: int | None = None
) -> None:
```

Inside the `with os.fdopen(...)` block, insert the `fchmod` between the copy and the `fsync`. The block currently reads:

```python
        with os.fdopen(fd, "wb") as tmp:
            fd = -1  # fdopen took ownership; its __exit__ closes the fd now
            shutil.copyfileobj(source, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
```

Replace with:

```python
        with os.fdopen(fd, "wb") as tmp:
            fd = -1  # fdopen took ownership; its __exit__ closes the fd now
            shutil.copyfileobj(source, tmp)
            tmp.flush()
            if mode is not None:
                # fchmod sets the exact mode (umask does not apply), so a
                # caller gets a deterministic mode with no umask raciness —
                # done before os.replace so the final file is never briefly
                # 0o600 then widened. None preserves mkstemp's 0o600.
                os.fchmod(tmp.fileno(), mode)
            os.fsync(tmp.fileno())
```

- [ ] **Step 4: Update the `atomic_write` docstring**

Replace the existing paragraph beginning "The deposited file carries `tempfile.mkstemp`'s `0o600`..." with:

```python
    By default (``mode=None``) the file carries ``tempfile.mkstemp``'s
    ``0o600`` (owner-only) mode, which ``os.replace`` preserves. Pass an
    explicit ``mode`` to ``os.fchmod`` the temp file before the rename — the
    filesystem sink passes ``0o664`` so a different-uid party on a shared
    volume can read the deposit (#155). ``fchmod`` sets the exact mode (umask
    does not apply), avoiding the umask race that reading the process umask
    under ``asyncio.to_thread`` would introduce.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_paths.py -q`
Expected: PASS (the two new tests plus the pre-existing `atomic_write` tests).

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_paths.py tests/_file_exchange/test_paths.py
git commit -m "feat(file-exchange): atomic_write optional mode param (#143)"
```

---

## Task 2: `FileExchangeTransferError` exception

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_errors.py`
- Test: `tests/_file_exchange/test_errors.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/_file_exchange/test_errors.py`:

```python
def test_transfer_error_carries_code_transport_detail():
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError

    exc = FileExchangeTransferError(
        TransferErrorCode.DIGEST_MISMATCH,
        transport="filesystem",
        detail="bytes did not match declared digest",
    )
    assert exc.code is TransferErrorCode.DIGEST_MISMATCH
    assert exc.transport == "filesystem"
    assert exc.detail == "bytes did not match declared digest"
    assert "digest-mismatch" in str(exc)
    assert isinstance(exc, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/_file_exchange/test_errors.py::test_transfer_error_carries_code_transport_detail -v`
Expected: FAIL — cannot import `FileExchangeTransferError`.

- [ ] **Step 3: Add the exception**

In `_errors.py`, after the imports (which already include `TransferErrorCode`), add:

```python
class FileExchangeTransferError(Exception):
    """A transport-level transfer failure carrying a §13 error code.

    Raised by the transport bindings (#143+) when a transfer cannot
    complete — confinement/access failure, size/digest mismatch, or an
    underlying hook/IO error. #148's fastmcp middleware maps it onto the
    wire response via :func:`build_file_exchange_error` (which needs that
    middleware to set wire ``isError`` + ``_meta`` together — see this
    module's docstring), so the bindings *raise* this rather than return an
    envelope, exactly as ``_selection`` delegates ``no-supported-transport``
    rendering to its caller.

    ``detail`` is a generic, non-sensitive message safe for the wire
    ``_meta``; the original cause is chained via ``raise ... from exc`` for
    local logs, never echoed into ``detail`` (so untrusted URIs/paths do not
    leak — see the URL/path redaction discipline).
    """

    def __init__(
        self,
        code: TransferErrorCode,
        *,
        transport: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.code = code
        self.transport = transport
        self.detail = detail
        message = code.value if detail is None else f"{code.value}: {detail}"
        super().__init__(message)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/_file_exchange/test_errors.py::test_transfer_error_carries_code_transport_detail -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_errors.py tests/_file_exchange/test_errors.py
git commit -m "feat(file-exchange): §13-coded FileExchangeTransferError (#143)"
```

---

## Task 3: `_HashingReader`

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`
- Test: `tests/_file_exchange/test_filesystem.py`

- [ ] **Step 1: Write the failing test**

Create `tests/_file_exchange/test_filesystem.py`:

```python
import hashlib
import io

from fastmcp_pvl_core._file_exchange import _filesystem


def test_hashing_reader_tracks_size_and_digest():
    payload = b"the quick brown fox" * 1000
    reader = _filesystem._HashingReader(io.BytesIO(payload))
    sink = io.BytesIO()
    # copyfileobj-style chunked drain
    while True:
        chunk = reader.read(64)
        if not chunk:
            break
        sink.write(chunk)
    assert sink.getvalue() == payload
    assert reader.size == len(payload)
    assert reader.hexdigest() == hashlib.sha256(payload).hexdigest()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py::test_hashing_reader_tracks_size_and_digest -v`
Expected: FAIL — module `_filesystem` does not exist.

- [ ] **Step 3: Create the module with `_HashingReader`**

Create `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`:

```python
"""Bind the mechanism-agnostic artifact hooks (#142) to the `filesystem`
transport.

Free functions for the four roles — provider/fetcher/receiver/sender —
composed over two private byte primitives (:func:`_stage` write,
:func:`_ingest` read), the #141 confinement helpers, and the #142 hooks.
The transport is mechanism-specific *here* on purpose; the hooks it calls
stay mechanism-agnostic. See
``docs/superpowers/specs/2026-05-23-file-exchange-143-filesystem-transport-design.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._paths import (
    VolumeMap,
    atomic_write,
    resolve_filesystem_uri,
)
from fastmcp_pvl_core._file_exchange._spec import (
    HANDLE_TYPE,
    SPEC_VERSION,
    TICKET_TYPE,
)
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    ArtifactMetadata,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    TransferHandle,
)

if TYPE_CHECKING:
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource

# pvl-core emits sha-256 (the §7.1 example) — its shape. The verifier maps a
# declared `<label>:` to a hashlib name; an unsupported label fails
# verification (cannot verify -> digest-mismatch), never silently skips.
_DIGEST_LABEL = "sha-256"
_HASHLIB_BY_LABEL = {"sha-256": "sha256", "sha-384": "sha384", "sha-512": "sha512"}

# Fixed deposit/staged-file mode (#155): owner rw, group rw, other r — so a
# different-uid party on a shared volume can read what pvl-core writes.
_DEPOSIT_MODE = 0o664

_CHUNK = 1024 * 1024


class _HashingReader:
    """Wrap a readable byte stream, tallying size + a hash as bytes are read.

    ``shutil.copyfileobj`` (inside :func:`atomic_write`) calls ``.read(n)``;
    each chunk updates the digest and byte count before being returned, so
    after the copy ``.size`` and :meth:`hexdigest` describe exactly the bytes
    that passed through. Lets pvl-core compute size+digest in the single pass
    a non-seekable source stream allows (#142).
    """

    def __init__(self, stream: SupportsRead[bytes], *, algorithm: str = "sha256") -> None:
        self._stream = stream
        self._hash = hashlib.new(algorithm)
        self.size = 0

    def read(self, size: int = -1, /) -> bytes:
        chunk = self._stream.read(size)
        self._hash.update(chunk)
        self.size += len(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._hash.hexdigest()
```

Add the `SupportsRead` import to the `TYPE_CHECKING` block (it is only used in annotations):

```python
if TYPE_CHECKING:
    from _typeshed import SupportsRead

    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
```

> Note: `ArtifactConstraints`, `ArtifactMetadata`, `FilesystemSink`, `FilesystemSource`, `IntakeTicket`, `TransferHandle`, `BinaryIO`, `Callable`, `os`, `stat`, `uuid`, `asyncio`, `Path`, `ConfigurationError`, `resolve_filesystem_uri`, `HANDLE_TYPE`, `TICKET_TYPE`, `SPEC_VERSION` are imported now but consumed by later tasks in this file. `ruff` will flag them as unused until then; that is expected mid-plan and resolved by Task 9. If you prefer a green lint after every task, add each import in the task that first uses it instead of all at once here.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py::test_hashing_reader_tracks_size_and_digest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_filesystem.py tests/_file_exchange/test_filesystem.py
git commit -m "feat(file-exchange): _HashingReader for single-pass size+digest (#143)"
```

---

## Task 4: `_stage` write primitive

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`
- Test: `tests/_file_exchange/test_filesystem.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/_file_exchange/test_filesystem.py`:

```python
import io

from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _DummySource:
    def __init__(self, payload: bytes, meta: ArtifactMetadata) -> None:
        self._payload = payload
        self._meta = meta
        self.closed = False

    async def open_artifact(self, key):
        stream = io.BytesIO(self._payload)
        original_close = stream.close

        def _close():
            self.closed = True
            original_close()

        stream.close = _close  # type: ignore[method-assign]
        return stream, self._meta


async def test_stage_writes_bytes_size_digest_and_mode(tmp_path):
    import hashlib

    from fastmcp_pvl_core._file_exchange import _filesystem

    payload = b"staged-bytes"
    meta = ArtifactMetadata(name="f.bin", mimeType="application/octet-stream")
    source = _DummySource(payload, meta)
    target = tmp_path / "vol" / "art"
    target.parent.mkdir()

    size, digest, returned_meta = await _filesystem._stage(source, "k", target)

    assert target.read_bytes() == payload
    assert size == len(payload)
    assert digest == f"sha-256:{hashlib.sha256(payload).hexdigest()}"
    assert target.stat().st_mode & 0o777 == 0o664
    assert returned_meta is meta
    assert source.closed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py::test_stage_writes_bytes_size_digest_and_mode -v`
Expected: FAIL — `_filesystem` has no attribute `_stage`.

- [ ] **Step 3: Implement `_stage` and its sync worker**

Append to `_filesystem.py`:

```python
def _write_hashed(stream: SupportsRead[bytes], target: Path) -> tuple[int, str]:
    """Sync: copy ``stream`` to ``target`` atomically at 0o664, hashing as it
    goes. Returns ``(size, "sha-256:<hex>")``."""
    reader = _HashingReader(stream)
    atomic_write(target, reader, mode=_DEPOSIT_MODE)
    return reader.size, f"{_DIGEST_LABEL}:{reader.hexdigest()}"


async def _stage(
    source: ArtifactSource, key: str, target: Path
) -> tuple[int, str, ArtifactMetadata]:
    """Pull ``source``'s bytes for ``key`` and stage them at ``target``.

    ``open_artifact`` is awaited on the loop; the blocking copy/hash/atomic
    rename runs in a worker thread. pvl-core owns the returned stream and
    closes it (per #142). Returns ``(size, digest, metadata)`` — the caller
    folds size+digest into the reference it builds.
    """
    stream, meta = await source.open_artifact(key)
    try:
        size, digest = await asyncio.to_thread(_write_hashed, stream, target)
    finally:
        stream.close()
    return size, digest, meta
```

`SupportsRead` is needed at runtime in `_write_hashed`'s annotation only under `from __future__ import annotations` (already present), so the `TYPE_CHECKING` import suffices — no runtime import needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py::test_stage_writes_bytes_size_digest_and_mode -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_filesystem.py tests/_file_exchange/test_filesystem.py
git commit -m "feat(file-exchange): _stage write primitive (#143)"
```

---

## Task 5: `filesystem_provider_mint`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`
- Test: `tests/_file_exchange/test_filesystem.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_filesystem.py`:

```python
async def test_provider_mint_builds_handle_with_computed_size_and_digest(tmp_path):
    import hashlib

    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._wire import FilesystemSource

    payload = b"report-bytes"
    meta = ArtifactMetadata(id="rep-1", name="r.bin", mimeType="application/octet-stream")
    source = _DummySource(payload, meta)
    volume_map = {"vol": tmp_path}

    handle = await _filesystem.filesystem_provider_mint(
        source, "k", volume="vol", volume_map=volume_map
    )

    assert handle.artifact.id == "rep-1"
    assert handle.artifact.size == len(payload)
    assert handle.artifact.digest == f"sha-256:{hashlib.sha256(payload).hexdigest()}"
    assert len(handle.sources) == 1
    descriptor = handle.sources[0]
    assert isinstance(descriptor, FilesystemSource)
    assert descriptor.uri.startswith("exchange://vol/")
    # the staged file actually exists at the descriptor's resolved location
    relpath = descriptor.uri.removeprefix("exchange://vol/")
    assert (tmp_path / relpath).read_bytes() == payload


async def test_provider_mint_unknown_volume_raises_configuration_error(tmp_path):
    from fastmcp_pvl_core._errors import ConfigurationError
    from fastmcp_pvl_core._file_exchange import _filesystem

    source = _DummySource(b"x", ArtifactMetadata(name="x"))
    with pytest.raises(ConfigurationError):
        await _filesystem.filesystem_provider_mint(
            source, "k", volume="missing", volume_map={"vol": tmp_path}
        )
```

Add `import pytest` to the test module's imports if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py::test_provider_mint_builds_handle_with_computed_size_and_digest -v`
Expected: FAIL — no attribute `filesystem_provider_mint`.

- [ ] **Step 3: Implement `_require_volume` and `filesystem_provider_mint`**

Append to `_filesystem.py`:

```python
def _require_volume(volume: str, volume_map: VolumeMap) -> Path:
    """Return the mount root for ``volume`` or fail loudly.

    A mint op naming a volume the server has no mapping for is a caller/config
    mistake (not a per-transfer §13 failure), so it raises
    :class:`ConfigurationError` rather than ``FileExchangeTransferError``.
    """
    root = volume_map.get(volume)
    if root is None:
        raise ConfigurationError(
            f"file-exchange: mint volume {volume!r} is not in the volume map"
        )
    return root


async def filesystem_provider_mint(
    source: ArtifactSource,
    key: str,
    *,
    volume: str,
    volume_map: VolumeMap,
) -> TransferHandle:
    """Provider role (pull): stage ``key``'s bytes onto ``volume`` and mint a
    Transfer Handle whose single ``filesystem`` source points at the staged
    file, with computed ``size``+``digest`` folded into the metadata.

    ``volume`` (hook/config) names which mapped volume to stage into; *how* a
    server picks it is #148's concern. The staged file's lifecycle/cleanup is
    the provider's (§10.1.3) and out of scope here.
    """
    root = _require_volume(volume, volume_map)
    relpath = uuid.uuid4().hex
    size, digest, meta = await _stage(source, key, root / relpath)
    artifact = meta.model_copy(update={"size": size, "digest": digest})
    descriptor = FilesystemSource(
        transport="filesystem", uri=f"exchange://{volume}/{relpath}"
    )
    return TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=artifact,
        sources=[descriptor],
    )
```

> `meta.model_copy(update=...)` does not re-validate, but `size` (int ≥ 0) and `digest` (matches `_DIGEST_PATTERN`) are valid by construction. `relpath` is a single `uuid4().hex` component, so its parent is the volume root (an existing operator mount) — no `mkdir` needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k provider_mint -v`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_filesystem.py tests/_file_exchange/test_filesystem.py
git commit -m "feat(file-exchange): filesystem_provider_mint (#143)"
```

---

## Task 6: TOCTOU-safe open + verify helpers

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`
- Test: `tests/_file_exchange/test_filesystem.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_filesystem.py`:

```python
def test_open_confined_readonly_opens_regular_file(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem

    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    f = _filesystem._open_confined_readonly(p)
    try:
        assert f.read() == b"abc"
    finally:
        f.close()


def test_open_confined_readonly_rejects_symlink_final_component(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode

    real = tmp_path / "real.bin"
    real.write_bytes(b"secret")
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._open_confined_readonly(link)
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


def test_open_confined_readonly_rejects_directory(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode

    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._open_confined_readonly(d)
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


def test_verify_stream_accepts_matching_size_and_digest(tmp_path):
    import hashlib
    import io

    from fastmcp_pvl_core._file_exchange import _filesystem

    payload = b"verify-me"
    artifact = ArtifactMetadata(
        size=len(payload),
        digest=f"sha-256:{hashlib.sha256(payload).hexdigest()}",
    )
    _filesystem._verify_stream(io.BytesIO(payload), artifact)  # no raise


def test_verify_stream_raises_size_mismatch():
    import io

    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode

    artifact = ArtifactMetadata(size=999)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._verify_stream(io.BytesIO(b"short"), artifact)
    assert ei.value.code is TransferErrorCode.SIZE_MISMATCH


def test_verify_stream_raises_digest_mismatch():
    import io

    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode

    artifact = ArtifactMetadata(digest="sha-256:" + "0" * 64)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._verify_stream(io.BytesIO(b"data"), artifact)
    assert ei.value.code is TransferErrorCode.DIGEST_MISMATCH


def test_verify_stream_unsupported_algorithm_is_digest_mismatch():
    import io

    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode

    artifact = ArtifactMetadata(digest="md5:" + "0" * 32)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._verify_stream(io.BytesIO(b"data"), artifact)
    assert ei.value.code is TransferErrorCode.DIGEST_MISMATCH
```

> `ArtifactMetadata(digest="md5:0000...")` is constructable: `_DIGEST_PATTERN` (`^[A-Za-z0-9][A-Za-z0-9-]*:[0-9a-f]+$`) matches `md5:` + hex. The verifier rejects it because `md5` is not in `_HASHLIB_BY_LABEL`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k "open_confined or verify_stream" -v`
Expected: FAIL — no attribute `_open_confined_readonly` / `_verify_stream`.

- [ ] **Step 3: Implement the helpers**

Append to `_filesystem.py`:

```python
def _open_confined_readonly(path: Path) -> BinaryIO:
    """Open an already-confined path read-only, TOCTOU-guarded.

    ``O_NOFOLLOW`` rejects a final-component symlink swapped in between
    resolution (#141) and this open; ``fstat`` rejects a non-regular target.
    Prefix-component races and full per-component ``openat`` traversal are out
    of scope — see the design doc's TOCTOU section. Any failure surfaces as
    ``not-accessible``.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport="filesystem",
            detail="source could not be opened read-only",
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FileExchangeTransferError(
                TransferErrorCode.NOT_ACCESSIBLE,
                transport="filesystem",
                detail="source is not a regular file",
            )
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _verify_stream(stream: SupportsRead[bytes], artifact: ArtifactMetadata) -> None:
    """Read ``stream`` to EOF, checking size+digest against ``artifact``.

    Verifies only the fields the metadata declares (§7.1: both optional). An
    undecodable/unsupported digest algorithm is a verification failure
    (cannot verify -> ``digest-mismatch``), not a silent skip (§15). ``detail``
    is generic — no raw bytes/paths leak to the wire.
    """
    label = expected_hex = None
    hashlib_name = None
    if artifact.digest is not None:
        label, _, expected_hex = artifact.digest.partition(":")
        hashlib_name = _HASHLIB_BY_LABEL.get(label)
    hasher = hashlib.new(hashlib_name) if hashlib_name is not None else None

    size = 0
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if hasher is not None:
            hasher.update(chunk)

    if artifact.size is not None and size != artifact.size:
        raise FileExchangeTransferError(
            TransferErrorCode.SIZE_MISMATCH,
            transport="filesystem",
            detail="transferred byte count did not match declared size",
        )
    if artifact.digest is not None:
        if hasher is None or hasher.hexdigest() != (expected_hex or "").lower():
            raise FileExchangeTransferError(
                TransferErrorCode.DIGEST_MISMATCH,
                transport="filesystem",
                detail="transferred bytes did not match declared digest",
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k "open_confined or verify_stream" -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_filesystem.py tests/_file_exchange/test_filesystem.py
git commit -m "feat(file-exchange): TOCTOU-safe open + size/digest verify (#143)"
```

---

## Task 7: `_ingest` read primitive

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`
- Test: `tests/_file_exchange/test_filesystem.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_filesystem.py`:

```python
class _RecordingSink:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}
        self.called = False

    async def store_artifact(self, artifact_id, metadata, stream):
        self.called = True
        self.stored[artifact_id] = stream.read()


async def test_ingest_verifies_then_hands_verified_bytes_to_sink(tmp_path):
    import hashlib

    from fastmcp_pvl_core._file_exchange import _filesystem

    payload = b"ingest-bytes"
    p = tmp_path / "src.bin"
    p.write_bytes(payload)
    artifact = ArtifactMetadata(
        id="a1",
        size=len(payload),
        digest=f"sha-256:{hashlib.sha256(payload).hexdigest()}",
    )
    sink = _RecordingSink()

    await _filesystem._ingest(p, artifact, sink, "a1")

    assert sink.stored["a1"] == payload


async def test_ingest_does_not_call_sink_on_digest_mismatch(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode

    p = tmp_path / "src.bin"
    p.write_bytes(b"tampered")
    artifact = ArtifactMetadata(id="a1", digest="sha-256:" + "0" * 64)
    sink = _RecordingSink()

    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem._ingest(p, artifact, sink, "a1")

    assert ei.value.code is TransferErrorCode.DIGEST_MISMATCH
    assert sink.called is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k ingest -v`
Expected: FAIL — no attribute `_ingest`.

- [ ] **Step 3: Implement `_ingest`**

Append to `_filesystem.py`:

```python
async def _ingest(
    path: Path,
    artifact: ArtifactMetadata,
    sink: ArtifactSink,
    artifact_id: str | None,
) -> None:
    """Read a confined ``path``, verify, then deposit into ``sink``.

    Two passes over one fd: pass 1 verifies size+digest (off-loop) so the
    sink never receives unverified bytes (§15 "validate before use"); pass 2
    rewinds and hands the stream to ``store_artifact`` (the sink reads, does
    not close — #142). pvl-core closes the fd. ``artifact`` is the handle's
    metadata, passed through to the sink.
    """
    f = await asyncio.to_thread(_open_confined_readonly, path)
    try:
        await asyncio.to_thread(_verify_stream, f, artifact)
        await asyncio.to_thread(f.seek, 0)
        await sink.store_artifact(artifact_id, artifact, f)
    finally:
        await asyncio.to_thread(f.close)
```

> `artifact_id` is `str | None` because §7.1 makes `ArtifactMetadata.id` optional; a well-formed provider sets it (#142). The sink's `store_artifact` declares `str`; passing through whatever the handle carries keeps pvl-core from inventing an id.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k ingest -v`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_filesystem.py tests/_file_exchange/test_filesystem.py
git commit -m "feat(file-exchange): _ingest two-pass verify-then-deposit (#143)"
```

---

## Task 8: `filesystem_fetcher_consume`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`
- Test: `tests/_file_exchange/test_filesystem.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_filesystem.py`:

```python
async def test_fetcher_consume_resolves_verifies_and_deposits(tmp_path):
    import hashlib

    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._wire import (
        FilesystemSource,
        TransferHandle,
    )
    from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION

    payload = b"fetch-bytes"
    (tmp_path / "art").write_bytes(payload)
    artifact = ArtifactMetadata(
        id="a1",
        size=len(payload),
        digest=f"sha-256:{hashlib.sha256(payload).hexdigest()}",
    )
    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=artifact,
        sources=[FilesystemSource(transport="filesystem", uri="exchange://vol/art")],
    )
    source = handle.sources[0]
    assert isinstance(source, FilesystemSource)
    sink = _RecordingSink()

    await _filesystem.filesystem_fetcher_consume(
        handle, source, sink, volume_map={"vol": tmp_path}
    )

    assert sink.stored["a1"] == payload


async def test_fetcher_consume_unresolvable_uri_is_not_accessible(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._wire import (
        FilesystemSource,
        TransferHandle,
    )
    from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(id="a1", name="x"),
        sources=[FilesystemSource(transport="filesystem", uri="exchange://other/art")],
    )
    source = handle.sources[0]
    assert isinstance(source, FilesystemSource)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_fetcher_consume(
            handle, source, _RecordingSink(), volume_map={"vol": tmp_path}
        )
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


async def test_fetcher_consume_sink_failure_is_transfer_failed(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._wire import (
        FilesystemSource,
        TransferHandle,
    )
    from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION

    (tmp_path / "art").write_bytes(b"x")

    class _BoomSink:
        async def store_artifact(self, artifact_id, metadata, stream):
            raise RuntimeError("downstream storage exploded")

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(id="a1", name="x"),
        sources=[FilesystemSource(transport="filesystem", uri="exchange://vol/art")],
    )
    source = handle.sources[0]
    assert isinstance(source, FilesystemSource)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_fetcher_consume(
            handle, source, _BoomSink(), volume_map={"vol": tmp_path}
        )
    assert ei.value.code is TransferErrorCode.TRANSFER_FAILED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k fetcher_consume -v`
Expected: FAIL — no attribute `filesystem_fetcher_consume`.

- [ ] **Step 3: Implement `filesystem_fetcher_consume`**

Append to `_filesystem.py`:

```python
async def filesystem_fetcher_consume(
    handle: TransferHandle,
    source: FilesystemSource,
    sink: ArtifactSink,
    *,
    volume_map: VolumeMap,
) -> None:
    """Fetcher role (pull): read the already-selected ``source``, verify it
    against ``handle.artifact``, and deposit into ``sink``.

    Selection (``select_source``) is the caller's step. A descriptor that
    does not resolve/confine is ``not-accessible``; size/digest mismatches
    are ``size-mismatch``/``digest-mismatch``; any other failure (e.g. the
    sink raising) is ``transfer-failed``. The original cause is chained for
    local logs; only generic detail reaches the wire.
    """
    path = resolve_filesystem_uri(source.uri, volume_map=volume_map)
    if path is None:
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport="filesystem",
            detail="source descriptor did not resolve within a configured volume",
        )
    try:
        await _ingest(path, handle.artifact, sink, handle.artifact.id)
    except FileExchangeTransferError:
        raise
    except Exception as exc:
        raise FileExchangeTransferError(
            TransferErrorCode.TRANSFER_FAILED,
            transport="filesystem",
            detail="artifact transfer failed",
        ) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k fetcher_consume -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_filesystem.py tests/_file_exchange/test_filesystem.py
git commit -m "feat(file-exchange): filesystem_fetcher_consume (#143)"
```

---

## Task 9: `filesystem_receiver_mint` + `filesystem_sender_consume`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`
- Test: `tests/_file_exchange/test_filesystem.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_filesystem.py`:

```python
def test_receiver_mint_builds_ticket_with_filesystem_sink(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints, FilesystemSink

    ticket = _filesystem.filesystem_receiver_mint(
        "intake-1",
        volume="vol",
        volume_map={"vol": tmp_path},
        expected=ArtifactConstraints(maxSize=1024),
    )

    assert ticket.artifactId == "intake-1"
    assert ticket.expected is not None and ticket.expected.maxSize == 1024
    assert len(ticket.sinks) == 1
    sink = ticket.sinks[0]
    assert isinstance(sink, FilesystemSink)
    assert sink.uri.startswith("exchange://vol/")


def test_receiver_mint_unknown_volume_raises_configuration_error(tmp_path):
    from fastmcp_pvl_core._errors import ConfigurationError
    from fastmcp_pvl_core._file_exchange import _filesystem

    with pytest.raises(ConfigurationError):
        _filesystem.filesystem_receiver_mint(
            "intake-1", volume="missing", volume_map={"vol": tmp_path}
        )


async def test_sender_consume_writes_deposit_atomically_at_0664(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._wire import FilesystemSink

    payload = b"pushed-bytes"
    source = _DummySource(payload, ArtifactMetadata(name="p.bin"))
    sink_desc = FilesystemSink(transport="filesystem", uri="exchange://vol/deposit")

    await _filesystem.filesystem_sender_consume(
        sink_desc, source, "k", volume_map={"vol": tmp_path}
    )

    deposit = tmp_path / "deposit"
    assert deposit.read_bytes() == payload
    assert deposit.stat().st_mode & 0o777 == 0o664
    assert source.closed is True


async def test_sender_consume_unresolvable_uri_is_not_accessible(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._wire import FilesystemSink

    sink_desc = FilesystemSink(transport="filesystem", uri="exchange://other/deposit")
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_sender_consume(
            sink_desc,
            _DummySource(b"x", ArtifactMetadata(name="x")),
            "k",
            volume_map={"vol": tmp_path},
        )
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


async def test_sender_consume_source_failure_is_transfer_failed(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._wire import FilesystemSink

    class _BoomSource:
        async def open_artifact(self, key):
            raise RuntimeError("cannot read domain artifact")

    sink_desc = FilesystemSink(transport="filesystem", uri="exchange://vol/deposit")
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_sender_consume(
            sink_desc, _BoomSource(), "k", volume_map={"vol": tmp_path}
        )
    assert ei.value.code is TransferErrorCode.TRANSFER_FAILED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k "receiver_mint or sender_consume" -v`
Expected: FAIL — no attributes `filesystem_receiver_mint` / `filesystem_sender_consume`.

- [ ] **Step 3: Implement both ops**

Append to `_filesystem.py`:

```python
def filesystem_receiver_mint(
    artifact_id: str,
    *,
    volume: str,
    volume_map: VolumeMap,
    expected: ArtifactConstraints | None = None,
) -> IntakeTicket:
    """Receiver role (push): allocate a deposit path on ``volume`` and mint an
    Intake Ticket whose single ``filesystem`` sink points at it.

    Minting only — no hook is called and no bytes are written. The sender
    deposits later; the receiver's lazy ingest of the deposit into its own
    ``ArtifactSink`` (correlated by ``artifact_id``) is #144/#148, not here.
    """
    root = _require_volume(volume, volume_map)
    relpath = uuid.uuid4().hex
    descriptor = FilesystemSink(
        transport="filesystem", uri=f"exchange://{volume}/{relpath}"
    )
    return IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId=artifact_id,
        expected=expected,
        sinks=[descriptor],
    )


async def filesystem_sender_consume(
    sink: FilesystemSink,
    source: ArtifactSource,
    key: str,
    *,
    volume_map: VolumeMap,
) -> None:
    """Sender role (push): write ``key``'s bytes atomically into the
    already-selected ``sink``'s resolved deposit path (at 0o664).

    Selection (``select_sink``) is the caller's step. A descriptor that does
    not resolve/confine is ``not-accessible``; any other failure (e.g. the
    source hook raising, or a missing ``file://`` parent dir) is
    ``transfer-failed``. ``expected``-constraint enforcement is the receiver's
    at ingest time (#144/#148), not the sender's.
    """
    path = resolve_filesystem_uri(sink.uri, volume_map=volume_map)
    if path is None:
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport="filesystem",
            detail="sink descriptor did not resolve within a configured volume",
        )
    try:
        await _stage(source, key, path)
    except FileExchangeTransferError:
        raise
    except Exception as exc:
        raise FileExchangeTransferError(
            TransferErrorCode.TRANSFER_FAILED,
            transport="filesystem",
            detail="artifact transfer failed",
        ) from exc
```

This task is also where every name imported in Task 3's module header is now used (`ArtifactConstraints`, `IntakeTicket`, `TICKET_TYPE`, etc.). Run `uv run ruff check src/fastmcp_pvl_core/_file_exchange/_filesystem.py` — expect no unused-import warnings now.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k "receiver_mint or sender_consume" -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_filesystem.py tests/_file_exchange/test_filesystem.py
git commit -m "feat(file-exchange): filesystem_receiver_mint + filesystem_sender_consume (#143)"
```

---

## Task 10: Accessibility predicates

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_filesystem.py`
- Test: `tests/_file_exchange/test_filesystem.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_filesystem.py`:

```python
def test_source_readable_predicate(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._wire import FilesystemSource

    (tmp_path / "art").write_bytes(b"x")
    is_readable = _filesystem.filesystem_source_readable({"vol": tmp_path})
    assert is_readable(FilesystemSource(transport="filesystem", uri="exchange://vol/art")) is True
    assert is_readable(FilesystemSource(transport="filesystem", uri="exchange://vol/missing")) is False
    assert is_readable(FilesystemSource(transport="filesystem", uri="exchange://other/art")) is False


def test_sink_writable_predicate(tmp_path):
    from fastmcp_pvl_core._file_exchange import _filesystem
    from fastmcp_pvl_core._file_exchange._wire import FilesystemSink

    is_writable = _filesystem.filesystem_sink_writable({"vol": tmp_path})
    # deposit file need not exist; its parent (the volume root) is writable
    assert is_writable(FilesystemSink(transport="filesystem", uri="exchange://vol/deposit")) is True
    assert is_writable(FilesystemSink(transport="filesystem", uri="exchange://other/deposit")) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k "readable_predicate or writable_predicate" -v`
Expected: FAIL — no attributes `filesystem_source_readable` / `filesystem_sink_writable`.

- [ ] **Step 3: Implement the predicates**

Append to `_filesystem.py`:

```python
def filesystem_source_readable(
    volume_map: VolumeMap,
) -> Callable[[FilesystemSource], bool]:
    """Build the ``is_accessible`` callback ``select_source`` consults for a
    ``FilesystemSource``: the resolved location exists and is readable."""

    def _readable(source: FilesystemSource) -> bool:
        path = resolve_filesystem_uri(source.uri, volume_map=volume_map)
        return path is not None and os.access(path, os.R_OK)

    return _readable


def filesystem_sink_writable(
    volume_map: VolumeMap,
) -> Callable[[FilesystemSink], bool]:
    """Build the ``is_accessible`` callback ``select_sink`` consults for a
    ``FilesystemSink``: the deposit's parent dir exists and is writable (the
    target file itself need not exist yet)."""

    def _writable(sink: FilesystemSink) -> bool:
        path = resolve_filesystem_uri(sink.uri, volume_map=volume_map)
        if path is None:
            return False
        parent = path.parent
        return parent.is_dir() and os.access(parent, os.W_OK)

    return _writable
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k "readable_predicate or writable_predicate" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_filesystem.py tests/_file_exchange/test_filesystem.py
git commit -m "feat(file-exchange): filesystem accessibility predicates (#143)"
```

---

## Task 11: Public re-exports

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Test: `tests/test_file_exchange_namespace.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_file_exchange_namespace.py`:

```python
def test_filesystem_transport_names_reexported():
    from fastmcp_pvl_core import file_exchange

    for name in (
        "filesystem_provider_mint",
        "filesystem_fetcher_consume",
        "filesystem_receiver_mint",
        "filesystem_sender_consume",
        "filesystem_source_readable",
        "filesystem_sink_writable",
        "FileExchangeTransferError",
    ):
        assert hasattr(file_exchange, name), name
        assert name in file_exchange.__all__, name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_file_exchange_namespace.py::test_filesystem_transport_names_reexported -v`
Expected: FAIL — names absent from `file_exchange`.

- [ ] **Step 3: Add re-exports in the subpackage `__init__.py`**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, add an import block (alphabetically among the existing `from ... import` groups, after the `_errors` import group):

```python
from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeTransferError,
    build_file_exchange_error,
)
from fastmcp_pvl_core._file_exchange._filesystem import (
    filesystem_fetcher_consume,
    filesystem_provider_mint,
    filesystem_receiver_mint,
    filesystem_sender_consume,
    filesystem_sink_writable,
    filesystem_source_readable,
)
```

(The `_errors` group already imports `build_file_exchange_error`; add `FileExchangeTransferError` to it as shown.)

Then add these names to `__all__`, keeping it alphabetical:

```python
    "FileExchangeTransferError",
    ...
    "filesystem_fetcher_consume",
    "filesystem_provider_mint",
    "filesystem_receiver_mint",
    "filesystem_sender_consume",
    "filesystem_sink_writable",
    "filesystem_source_readable",
```

- [ ] **Step 4: Mirror the re-exports in the namespace module**

In `src/fastmcp_pvl_core/file_exchange.py`, add to the `from fastmcp_pvl_core._file_exchange import (...)` block (alphabetical):

```python
    FileExchangeTransferError,
    filesystem_fetcher_consume,
    filesystem_provider_mint,
    filesystem_receiver_mint,
    filesystem_sender_consume,
    filesystem_sink_writable,
    filesystem_source_readable,
```

and the same seven names to that file's `__all__` (alphabetical).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_file_exchange_namespace.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/__init__.py src/fastmcp_pvl_core/file_exchange.py tests/test_file_exchange_namespace.py
git commit -m "feat(file-exchange): re-export filesystem transport surface (#143)"
```

---

## Task 12: End-to-end pull + push across two mock servers

**Files:**
- Create: `tests/_file_exchange/test_filesystem_e2e.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/_file_exchange/test_filesystem_e2e.py`:

```python
"""End-to-end: two pvl-core-built mock servers exchange a file over the
filesystem transport only, sharing one volume. Pull (provider->fetcher,
both hooks) is a full round-trip; push (receiver->sender) goes to deposit
(receiver lazy-ingest is #144/#148, out of scope)."""

import hashlib
import io

from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactMetadata,
    FilesystemSink,
    FilesystemSource,
)


class _MockServer:
    """A server that can both produce (ArtifactSource) and store (ArtifactSink)."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.meta: dict[str, ArtifactMetadata] = {}
        self.received: dict[str, bytes] = {}

    async def open_artifact(self, key):
        return io.BytesIO(self.blobs[key]), self.meta[key]

    async def store_artifact(self, artifact_id, metadata, stream):
        self.received[artifact_id] = stream.read()


async def test_pull_round_trip(tmp_path):
    volume_map = {"shared": tmp_path}
    a, b = _MockServer(), _MockServer()
    payload = b"quarterly-report-bytes"
    a.blobs["rep"] = payload
    a.meta["rep"] = ArtifactMetadata(id="rep-1", name="rep.bin", mimeType="application/octet-stream")

    # provider (A) mints a handle; the bytes are staged on the shared volume
    handle = await file_exchange.filesystem_provider_mint(
        a, "rep", volume="shared", volume_map=volume_map
    )
    assert handle.artifact.digest == f"sha-256:{hashlib.sha256(payload).hexdigest()}"

    # fetcher (B) selects the source and pulls it into its own storage
    from fastmcp_pvl_core.file_exchange import select_source

    source = select_source(
        handle, is_accessible=file_exchange.filesystem_source_readable(volume_map)
    )
    assert isinstance(source, FilesystemSource)
    await file_exchange.filesystem_fetcher_consume(handle, source, b, volume_map=volume_map)

    assert b.received["rep-1"] == payload


async def test_push_to_deposit(tmp_path):
    volume_map = {"shared": tmp_path}
    a, b = _MockServer(), _MockServer()
    payload = b"uploaded-dataset-bytes"
    a.blobs["ds"] = payload
    a.meta["ds"] = ArtifactMetadata(id="ds-1", name="ds.bin")

    # receiver (B) mints a ticket with a deposit sink
    ticket = file_exchange.filesystem_receiver_mint(
        "intake-9", volume="shared", volume_map=volume_map
    )

    # sender (A) selects the sink and pushes its bytes to the deposit path
    from fastmcp_pvl_core.file_exchange import select_sink

    sink = select_sink(
        ticket, is_accessible=file_exchange.filesystem_sink_writable(volume_map)
    )
    assert isinstance(sink, FilesystemSink)
    await file_exchange.filesystem_sender_consume(sink, a, "ds", volume_map=volume_map)

    relpath = sink.uri.removeprefix("exchange://shared/")
    deposit = tmp_path / relpath
    assert deposit.read_bytes() == payload
    assert deposit.stat().st_mode & 0o777 == 0o664
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `uv run pytest tests/_file_exchange/test_filesystem_e2e.py -v`
Expected: PASS — every op already exists from Tasks 1–11; this proves they compose. If any import fails, fix the re-export in Task 11.

- [ ] **Step 3: Commit**

```bash
git add tests/_file_exchange/test_filesystem_e2e.py
git commit -m "test(file-exchange): filesystem pull+push e2e across two mock servers (#143)"
```

---

## Task 13: Full local gate + close-out

**Files:** none (verification only).

- [ ] **Step 1: Sync deps to match CI**

Run: `uv sync --all-extras`
Expected: environment resolves cleanly.

- [ ] **Step 2: Run the full suite on the minimum interpreter**

Run: `uv run pytest -q`
Expected: all green (new + pre-existing).

- [ ] **Step 3: Run the full suite on the maximum interpreter**

Run: `uv run --python 3.13 pytest -q`
Expected: all green (catches version-dependent behavior, e.g. `Path.resolve` symlink-loop differences — see prior PR #152).

- [ ] **Step 4: Format, lint, type-check**

Run:
```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```
Expected: no diffs, no lint errors, no type errors.

- [ ] **Step 5: Confirm #155's accidental-0o600 deposit is gone**

Run: `uv run pytest tests/_file_exchange/test_filesystem.py -k "0664" -v`
Expected: PASS — deposits/staged files are `0o664`, not the accidental `0o600`.

> PR is opened separately (after the mandatory `preflight-circus` local review per the repo workflow). The PR body must include `Closes #143` and `Closes #155` (this plan resolves both). Do not open the PR as part of plan execution.

---

## Self-Review

**Spec coverage:**
- Two byte primitives (`_stage`, `_ingest`) → Tasks 4, 7. ✓
- Four role ops → Tasks 5 (provider), 8 (fetcher), 9 (receiver + sender). ✓
- Accessibility predicates → Task 10. ✓
- Fixed `0o664` deposit/staged mode + `atomic_write` `mode` param (#155) → Tasks 1, 4, 9 (asserted in 4/9/12). ✓
- `uuid4` opaque path; never name/artifactId → Tasks 5, 9 (relpath = `uuid.uuid4().hex`). ✓
- Explicit `volume=` param + `ConfigurationError` on unmapped → Tasks 5, 9. ✓
- sha-256 emit + sha-256/384/512 verify, unknown algo → digest-mismatch → Tasks 4, 6. ✓
- TOCTOU `O_NOFOLLOW` + regular-file check (#141) → Task 6. ✓
- `FileExchangeTransferError` (§13 codes), #148 renders → Task 2; raised in Tasks 6, 8, 9. ✓
- Re-exports → Task 11. ✓
- E2E pull round-trip + push to deposit, filesystem only → Task 12. ✓
- Stop at deposit (no receiver ingest) → enforced by scope; Task 12 push asserts deposit, not ArtifactSink. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✓

**Type consistency:** `_stage -> (int, str, ArtifactMetadata)` consumed correctly in Task 5; `_ingest(path, artifact, sink, artifact_id)` matches Task 8's call; predicate factory return types `Callable[[FilesystemSource|Sink], bool]` match `select_source`/`select_sink`'s `is_accessible=` params; `FileExchangeTransferError(code, *, transport, detail)` constructed identically everywhere; `_DEPOSIT_MODE`/`_DIGEST_LABEL`/`_HASHLIB_BY_LABEL`/`_CHUNK` defined once in Task 3, used in 4/6. ✓
