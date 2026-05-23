# File-Exchange #142 — Artifact Source/Sink Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define the mechanism-agnostic `ArtifactSource` / `ArtifactSink` hook protocols downstream servers implement, plus a transport-agnostic `atomic_write` helper, plus a test that statically proves no transport name leaks into a hook signature.

**Architecture:** Two `@runtime_checkable` `typing.Protocol` classes in a new `_file_exchange/_hooks.py`, each with one `async def` method over a single sync `BinaryIO`; an `atomic_write(target, source)` helper added to the existing filesystem-utilities module `_file_exchange/_paths.py`; both re-exported through `file_exchange.py` and the subpackage `__init__.py`. The transport (filesystem in #143, HTTP later) lives entirely behind the hooks.

**Tech Stack:** Python 3.10+, `typing.Protocol`, `pytest` (`asyncio_mode = "auto"`), the existing `_file_exchange._wire.ArtifactMetadata`.

**Design doc:** `docs/superpowers/specs/2026-05-23-file-exchange-142-hooks-design.md`

---

## File structure

- **Create** `src/fastmcp_pvl_core/_file_exchange/_hooks.py` — the two Protocol contracts (`ArtifactSource`, `ArtifactSink`). Sole responsibility: the hook contracts.
- **Modify** `src/fastmcp_pvl_core/_file_exchange/_paths.py` — add the `atomic_write` helper (co-located with the other filesystem utilities).
- **Modify** `src/fastmcp_pvl_core/_file_exchange/__init__.py` and `src/fastmcp_pvl_core/file_exchange.py` — re-export `ArtifactSource`, `ArtifactSink`, `atomic_write`.
- **Create** `tests/_file_exchange/test_hooks.py` — protocol behavior + the no-transport-name introspection guard.
- **Modify** `tests/_file_exchange/test_paths.py` — `atomic_write` tests.
- **Modify** `tests/test_file_exchange_namespace.py` — assert the three new symbols are exposed.

Run the full local gate after each task: `uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run mypy src`. The repo supports Python 3.10–3.13; if a change is version-sensitive, also run `uv run --python 3.13 pytest tests/_file_exchange -q`.

---

### Task 1: The two hook protocols

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_hooks.py`
- Test: `tests/_file_exchange/test_hooks.py`

- [ ] **Step 1: Write the failing tests** (`tests/_file_exchange/test_hooks.py`)

```python
"""Tests for the mechanism-agnostic artifact source/sink hook protocols."""

from __future__ import annotations

import asyncio
from io import BytesIO

from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _DummySource:
    async def open_artifact(self, key):  # noqa: ANN001, ANN202
        return BytesIO(b"hello"), ArtifactMetadata(name="a.bin")


class _DummySink:
    def __init__(self):
        self.stored: bytes | None = None

    async def store_artifact(self, artifact_id, metadata, stream):  # noqa: ANN001, ANN202
        self.stored = stream.read()


def test_source_runtime_checkable_matches_conforming():
    assert isinstance(_DummySource(), ArtifactSource)


def test_sink_runtime_checkable_matches_conforming():
    assert isinstance(_DummySink(), ArtifactSink)


def test_runtime_checkable_rejects_nonconforming():
    assert not isinstance(object(), ArtifactSource)
    assert not isinstance(object(), ArtifactSink)


async def test_source_returns_stream_and_metadata():
    stream, meta = await _DummySource().open_artifact("k")
    assert stream.read() == b"hello"
    assert meta.name == "a.bin"


async def test_sink_reads_stream_and_returns_none():
    sink = _DummySink()
    result = await sink.store_artifact("id-1", ArtifactMetadata(name="x"), BytesIO(b"payload"))
    assert result is None
    assert sink.stored == b"payload"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_hooks.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._hooks'`.

- [ ] **Step 3: Create the protocols** (`src/fastmcp_pvl_core/_file_exchange/_hooks.py`)

```python
"""Mechanism-agnostic artifact byte-source / byte-sink hook protocols.

Downstream servers implement these to produce and deposit artifact bytes.
The transport that carries the bytes (a shared filesystem volume, an HTTPS
download/upload, ...) lives entirely behind the hook and MUST NOT appear in
its signature — a hook cannot tell which transport is in use. The two
protocols are exact mirrors over one synchronous BinaryIO.
"""

from __future__ import annotations

from typing import BinaryIO, Protocol, runtime_checkable

from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


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
        size + digest itself — so the returned ``ArtifactMetadata`` need
        only carry what the server knows (e.g. name, mimeType). Raise on
        failure.
        """
        ...


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
        wrapper so it can verify size + digest as the sink reads — so the
        sink reads but does **not** close it. Return ``None`` on success;
        raise on failure.
        """
        ...
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_hooks.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_hooks.py tests/_file_exchange/test_hooks.py
git commit -m "feat(_hooks): ArtifactSource/ArtifactSink mechanism-agnostic hook protocols"
```

---

### Task 2: No-transport-name introspection guard

The headline #142 deliverable: a test that statically proves no transport/mechanism name leaks into any hook signature, with a negative control proving the check has teeth. It iterates the Protocol classes *defined in* `_hooks.py` so a future hook is auto-covered.

**Files:**
- Modify: `tests/_file_exchange/test_hooks.py`

- [ ] **Step 1: Add the introspection guard tests** (append to `tests/_file_exchange/test_hooks.py`)

```python
import inspect

import pytest

from fastmcp_pvl_core._file_exchange import _hooks

# Transport / mechanism names that must never appear in a hook signature.
_FORBIDDEN_TOKENS = (
    "filesystem",
    "download",
    "upload",
    "http",
    "https",
    "exchange",
    "url",
    "volume",
)

# Protocol classes defined in _hooks (not imported ones like ArtifactMetadata):
_HOOK_PROTOCOLS = [
    obj
    for _name, obj in inspect.getmembers(_hooks, inspect.isclass)
    if obj.__module__ == _hooks.__name__
]


def _signature_tokens(method) -> list[str]:  # noqa: ANN001
    """Param names + annotation reprs + return annotation, as strings."""
    sig = inspect.signature(method)
    tokens: list[str] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        tokens.append(name)
        tokens.append(str(param.annotation))
    tokens.append(str(sig.return_annotation))
    return tokens


def _public_methods(cls):  # noqa: ANN001, ANN202
    return [
        m
        for name, m in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    ]


def test_hook_protocols_discovered():
    # Guard the guard: if discovery breaks (0 protocols), the test below
    # would pass vacuously. Pin that both hooks are found.
    names = {p.__name__ for p in _HOOK_PROTOCOLS}
    assert names == {"ArtifactSource", "ArtifactSink"}


@pytest.mark.parametrize("proto", _HOOK_PROTOCOLS, ids=lambda p: p.__name__)
def test_no_transport_name_in_hook_signatures(proto):
    for method in _public_methods(proto):
        for token in _signature_tokens(method):
            low = token.lower()
            for forbidden in _FORBIDDEN_TOKENS:
                assert forbidden not in low, (
                    f"{proto.__name__}.{method.__name__}: transport token "
                    f"{forbidden!r} leaked into the signature ({token!r})"
                )


def test_introspection_guard_has_teeth():
    # Negative control: a signature carrying a transport token is detectable
    # by the same token extraction, so a real leak could not pass silently.
    def bad(self, http_url: str) -> None: ...  # noqa: ANN001

    tokens = [t.lower() for t in _signature_tokens(bad)]
    assert any("http" in t for t in tokens)
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/_file_exchange/test_hooks.py -q`
Expected: PASS — `test_hook_protocols_discovered`, both parametrized `test_no_transport_name_in_hook_signatures[ArtifactSource]` / `[ArtifactSink]`, and `test_introspection_guard_has_teeth` all pass.

- [ ] **Step 3: Commit**

```bash
git add tests/_file_exchange/test_hooks.py
git commit -m "test(_hooks): static no-transport-name guard for hook signatures (+ negative control)"
```

---

### Task 3: `atomic_write` helper

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_paths.py`
- Test: `tests/_file_exchange/test_paths.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/_file_exchange/test_paths.py`)

```python
# --- atomic_write ---


def test_atomic_write_writes_content(tmp_path):
    target = tmp_path / "out.bin"
    from io import BytesIO

    atomic_write(target, BytesIO(b"hello"))
    assert target.read_bytes() == b"hello"


def test_atomic_write_overwrites_existing(tmp_path):
    target = tmp_path / "out.bin"
    target.write_bytes(b"old")
    from io import BytesIO

    atomic_write(target, BytesIO(b"new"))
    assert target.read_bytes() == b"new"


def test_atomic_write_cleans_up_and_leaves_target_absent_on_source_error(tmp_path):
    class _Boom:
        def read(self, *_a):
            raise RuntimeError("boom")

    target = tmp_path / "out.bin"
    with pytest.raises(RuntimeError, match="boom"):
        atomic_write(target, _Boom())
    assert not target.exists()
    # no temp file left behind in the directory
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_does_not_clobber_existing_on_source_error(tmp_path):
    target = tmp_path / "out.bin"
    target.write_bytes(b"keep")

    class _Boom:
        def read(self, *_a):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        atomic_write(target, _Boom())
    assert target.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_requires_existing_parent(tmp_path):
    from io import BytesIO

    target = tmp_path / "missing" / "out.bin"
    with pytest.raises(FileNotFoundError):
        atomic_write(target, BytesIO(b"x"))
```

Add `atomic_write` to the existing top-of-file import in `tests/_file_exchange/test_paths.py`:

```python
from fastmcp_pvl_core._file_exchange._paths import (
    _parse_fs_uri,
    _parse_volume_map,
    atomic_write,
    canonicalize_and_confine,
    load_volume_map,
    resolve_filesystem_uri,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_paths.py -k atomic_write -q`
Expected: FAIL — `ImportError: cannot import name 'atomic_write'`.

- [ ] **Step 3: Add the helper** (`src/fastmcp_pvl_core/_file_exchange/_paths.py`)

Extend the imports at the top of the module:

```python
import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.parse import urlsplit
```

(Keep the existing `from fastmcp_pvl_core._env import env` / `from fastmcp_pvl_core._errors import ConfigurationError` lines.)

Add the helper (place it after `canonicalize_and_confine`, before `resolve_filesystem_uri`):

```python
def atomic_write(target: Path, source: BinaryIO) -> None:
    """Write ``source``'s bytes to ``target`` atomically.

    Streams into a temp file in ``target``'s own directory (so the final
    ``os.replace`` is a same-filesystem atomic rename), flushes + fsyncs it,
    then ``os.replace``s it into place — a concurrent reader never observes
    a partial file (§10.1.3 "made visible atomically: write to a temporary
    path, then rename into place"). The parent directory must already exist.
    On any error the temp file is removed, leaving ``target`` untouched.

    Sync (pure file I/O); an async transport hook runs it via
    ``asyncio.to_thread`` so it never blocks the event loop.
    """
    target = Path(target)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as tmp:
            shutil.copyfileobj(source, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # copy/fsync/replace failed — the temp may still exist (it is only
        # gone after a successful os.replace). Remove it so no partial
        # deposit and no orphan temp is left; target is untouched.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_paths.py -k atomic_write -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_paths.py tests/_file_exchange/test_paths.py
git commit -m "feat(_paths): atomic_write helper (temp + fsync + os.replace), fail-safe"
```

---

### Task 4: Public re-exports + namespace test

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Test: `tests/test_file_exchange_namespace.py`

- [ ] **Step 1: Write the failing namespace test** (append to `tests/test_file_exchange_namespace.py`)

```python
def test_hook_helpers_exposed():
    from fastmcp_pvl_core import file_exchange

    # Protocols are classes; atomic_write is callable.
    assert isinstance(file_exchange.ArtifactSource, type)
    assert isinstance(file_exchange.ArtifactSink, type)
    assert callable(file_exchange.atomic_write)
    for name in ("ArtifactSource", "ArtifactSink", "atomic_write"):
        assert name in file_exchange.__all__
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_file_exchange_namespace.py::test_hook_helpers_exposed -q`
Expected: FAIL — `AttributeError: module 'fastmcp_pvl_core.file_exchange' has no attribute 'ArtifactSource'`.

- [ ] **Step 3: Add the re-exports**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`: import `ArtifactSink`, `ArtifactSource` from `._hooks` and `atomic_write` from `._paths` (the module already imports other names from `._paths`; add `atomic_write` to that import, keeping it alphabetical), and add `"ArtifactSink"`, `"ArtifactSource"`, `"atomic_write"` to `__all__` in alphabetical position.

In `src/fastmcp_pvl_core/file_exchange.py`: mirror the same three imports and the same three `__all__` entries, keeping both lists alphabetical and identical in membership to the subpackage `__init__.py` (the established convention).

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_file_exchange_namespace.py -q`
Expected: PASS.

- [ ] **Step 5: Full gate + commit**

Run: `uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: all green.

```bash
git add src/fastmcp_pvl_core/_file_exchange/__init__.py src/fastmcp_pvl_core/file_exchange.py tests/test_file_exchange_namespace.py
git commit -m "feat(file_exchange): expose ArtifactSource/ArtifactSink/atomic_write in public namespace"
```

---

## Notes for the implementer

- **`from __future__ import annotations`** is required at the top of `_hooks.py` and `test_hooks.py` — it makes the introspection guard read string annotations directly (no runtime resolution), and matches the repo's style.
- **Do not** add new exception types, a `describe()` method, a `DepositResult`, or any transport-aware branching — the contracts are deliberately minimal (YAGNI; see the design doc's error-handling and shape notes).
- **mypy:** the `Protocol` methods with `...` bodies and the `tuple[BinaryIO, ArtifactMetadata]` return type are valid; if mypy flags an unused-annotation or self-type issue, do not "fix" it by widening the signature — the signatures are load-bearing for the introspection guard.
