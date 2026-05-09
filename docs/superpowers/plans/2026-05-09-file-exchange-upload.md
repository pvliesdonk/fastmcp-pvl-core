# File Exchange Upload Direction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `register_file_exchange_upload` — a symmetric inbound mirror of `register_file_exchange` — and extend the File Exchange spec to v0.4 (Amendments 10 + 11).

**Architecture:** Fold into the existing `file_exchange` module. Extract token-lifecycle primitives into a shared internal `_BaseTokenStore[T]`. Add an `UploadStore`/`UploadRecord` pair, an `UploadHandle`, a `POST /<ns>/uploads/{token}` route, and a public `register_file_exchange_upload` registrar. Both registrars cooperate on capability emission via a shared module-level builder so download-only, upload-only, and both-direction servers all advertise correctly. `consumes` keeps its existing meaning; an optional per-method filter `transfer_methods.http.upload.accepts` tightens the upload route's MIME check.

**Tech Stack:** Python 3.12+, FastMCP, Starlette, pytest, httpx (for ASGI integration tests). Project conventions: `from __future__ import annotations`, frozen dataclasses, module-level `logger = logging.getLogger(__name__)`, public API stable via re-exports in `src/fastmcp_pvl_core/__init__.py`.

**Spec reference:** `docs/superpowers/specs/2026-05-09-file-exchange-upload-design.md`.

---

## File Structure

| Path | Status | Responsibility |
|---|---|---|
| `src/fastmcp_pvl_core/_token_store.py` | new | Generic `_BaseTokenStore[T]` + concrete `ArtifactStore` (moved) + `UploadStore` (new). `TokenRecord` (existing) and `UploadRecord` (new) live here. Module-level singletons for both. |
| `src/fastmcp_pvl_core/_artifacts.py` | shrunk to re-export shim | Re-exports `ArtifactStore`, `TokenRecord`, `get_artifact_store`, `set_artifact_store` from `_token_store` for backward compatibility. Marked deprecated in module docstring. |
| `src/fastmcp_pvl_core/_file_exchange_runtime.py` | extended | New `register_upload_route(mcp, store, *, path, receiver, stream_receiver, accepts)` helper that mounts `POST /<ns>/uploads/{token}` with the documented status-code contract. |
| `src/fastmcp_pvl_core/_file_exchange_protocol.py` | edited | `SPEC_VERSION` bumps `"0.2"` → `"0.4"`. `FileExchangeCapability.to_capability_dict()` learns the nested-`http` shape and a `legacy_capability_shape` flag. New `_FileExchangeCapabilityBuilder` accumulates download- and upload-side contributions. |
| `src/fastmcp_pvl_core/file_exchange.py` | extended | New `register_file_exchange_upload(...)` public function. New `UploadHandle` dataclass. The existing `register_file_exchange` is refactored to push its capability contribution through the shared builder rather than calling `register_file_exchange_capability` directly. |
| `src/fastmcp_pvl_core/__init__.py` | edited | Export `UploadRecord`, `UploadStore`, `UploadHandle`, `register_file_exchange_upload`, `get_upload_store`, `set_upload_store`. Update `__all__`. |
| `docs/specs/file-exchange.md` | edited | Append Amendments 10 and 11 to the v0.4.0 amendments draft. Update header to indicate v0.4 amendments now include direction tagging and inbound HTTP. |
| `CHANGELOG.md` | edited | New section for the upcoming minor describing the upload addition + spec bump. |
| `README.md` | edited | One-line mention of `register_file_exchange_upload` next to the existing `register_file_exchange` reference. |
| `tests/test_token_store.py` | new | Lifecycle tests for `_BaseTokenStore[T]` parameterised over `TokenRecord` and `UploadRecord`. |
| `tests/test_artifacts.py` | kept; shim re-export verified | Existing artifact-payload-specific tests remain. |
| `tests/test_uploads.py` | new | `UploadStore` and `UploadRecord` direct tests. |
| `tests/test_file_exchange_upload_route.py` | new | Integration tests for the POST route via `httpx.AsyncClient`. |
| `tests/test_file_exchange_upload_facade.py` | new | Tests for `register_file_exchange_upload`: tool registration, validator, env vars, mutual exclusion, TTL clamp. |
| `tests/test_file_exchange_capability_merge.py` | new | Capability-merge tests across download-only / upload-only / both / legacy-flat-shape. |

---

### Task 1: Extract `_BaseTokenStore[T]` + move `ArtifactStore` to new module

**Files:**
- Create: `src/fastmcp_pvl_core/_token_store.py`
- Create: `tests/test_token_store.py`
- Modify: `src/fastmcp_pvl_core/_artifacts.py` (becomes re-export shim)

This is the first refactor. We lift the lifecycle bits of `ArtifactStore` into a generic base class while moving the concrete store to a new module. The existing `_artifacts.py` becomes a 5-line re-export shim so all current import paths keep working.

- [ ] **Step 1: Write the failing test** in `tests/test_token_store.py`:

```python
"""Tests for the generic token-store base class."""

from __future__ import annotations

import dataclasses
import time

from fastmcp_pvl_core._token_store import _BaseTokenStore


@dataclasses.dataclass(frozen=True)
class _DummyRecord:
    expires_at: float
    payload: str


def test_base_token_store_create_returns_unique_tokens() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    t1 = store._mint_token()
    t2 = store._mint_token()
    assert t1 != t2
    assert isinstance(t1, str) and len(t1) == 32


def test_base_token_store_atomic_consume_returns_record_once() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    token = store._mint_token()
    rec = _DummyRecord(expires_at=time.time() + 60, payload="hi")
    store._records[token] = rec
    out = store._atomic_consume(token)
    assert out is rec
    assert store._atomic_consume(token) is None


def test_base_token_store_atomic_consume_treats_expired_as_missing() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    token = store._mint_token()
    store._records[token] = _DummyRecord(expires_at=time.time() - 1, payload="x")
    assert store._atomic_consume(token) is None
    # Expired record must be removed even though the consume returned None.
    assert token not in store._records


def test_base_token_store_purge_expired_removes_only_expired() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    fresh = store._mint_token()
    stale = store._mint_token()
    now = time.time()
    store._records[fresh] = _DummyRecord(expires_at=now + 60, payload="fresh")
    store._records[stale] = _DummyRecord(expires_at=now - 1, payload="stale")
    store._purge_expired()
    assert fresh in store._records
    assert stale not in store._records


def test_base_token_store_atomic_consume_unknown_returns_none() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    assert store._atomic_consume("does-not-exist") is None
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
uv run pytest tests/test_token_store.py -v
```

Expected: `ImportError` / module not found.

- [ ] **Step 3: Implement `_BaseTokenStore` in `src/fastmcp_pvl_core/_token_store.py`**

```python
"""Generic token-lifecycle helpers.

This module hosts the shared one-time-token machinery used by both the
artifact-download direction (``ArtifactStore``) and the upload direction
(``UploadStore``). The generic :class:`_BaseTokenStore` provides UUID4
minting, lazy expiry sweep, and the atomic consume-and-remove primitive
both directions need.

The concrete stores layer their direction-specific data on top:

- ``ArtifactStore`` keeps the existing ``add(content, ...)`` / ``pop(token)``
  shape — it stores the bytes inline, since downloads serve them on
  demand from a single record.
- ``UploadStore`` reserves a slot at link-creation time and consumes it
  when the POST arrives — bytes do not live in the record.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Generic, Protocol, TypeVar

logger = logging.getLogger(__name__)


class _HasExpiresAt(Protocol):
    """Structural type for any record the base store can hold."""

    @property
    def expires_at(self) -> float: ...


T = TypeVar("T", bound=_HasExpiresAt)


class _BaseTokenStore(Generic[T]):
    """In-memory keyed store with TTL and atomic one-time consume.

    Tokens are UUID4 hex strings (cryptographically unguessable). Each
    public access path triggers a lazy expiry sweep.

    Subclasses provide the public mutation API (``add``/``pop`` for
    artifacts; ``reserve``/``consume`` for uploads). The base only owns
    token minting, expiry tracking, and the consume-and-remove
    primitive.
    """

    def __init__(self) -> None:
        self._records: dict[str, T] = {}

    def _mint_token(self) -> str:
        """Return a fresh UUID4 hex token."""
        return uuid.uuid4().hex

    def _atomic_consume(self, token: str) -> T | None:
        """Pop ``token`` if present and unexpired, else ``None``.

        The token is always removed from the store (even when expired)
        so a subsequent caller cannot retry with the same token.
        """
        self._purge_expired()
        record = self._records.pop(token, None)
        if record is None:
            return None
        # Defense in depth: a record can tip past expires_at between
        # _purge_expired's internal now() and this post-pop check.
        if time.time() > record.expires_at:
            return None
        return record

    def _purge_expired(self) -> None:
        """Drop any record whose ``expires_at`` is in the past."""
        now = time.time()
        expired = [t for t, r in self._records.items() if now > r.expires_at]
        for t in expired:
            del self._records[t]
        if expired:
            logger.debug("token_store_purge count=%d", len(expired))
```

- [ ] **Step 4: Run the test, verify it passes**

```bash
uv run pytest tests/test_token_store.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_token_store.py tests/test_token_store.py
git commit -m "refactor(file-exchange): extract _BaseTokenStore generic for shared lifecycle"
```

---

### Task 2: Move `ArtifactStore` + `TokenRecord` into `_token_store.py`; reduce `_artifacts.py` to a shim

**Files:**
- Modify: `src/fastmcp_pvl_core/_token_store.py` (add `TokenRecord` + `ArtifactStore` + singletons)
- Modify: `src/fastmcp_pvl_core/_artifacts.py` (becomes re-export shim)
- Verify: `tests/test_artifacts.py` and `tests/test_artifacts_ext.py` still pass unchanged

The goal is to move code without changing behavior. After this task, every existing import path (`from fastmcp_pvl_core._artifacts import ArtifactStore`, `from fastmcp_pvl_core import ArtifactStore`) keeps working.

- [ ] **Step 1: Run the existing artifact tests to capture the baseline**

```bash
uv run pytest tests/test_artifacts.py tests/test_artifacts_ext.py -v
```

Expected: all current tests pass. Note the test count.

- [ ] **Step 2: Append `TokenRecord` + `ArtifactStore` + singletons to `_token_store.py`**

Add the following at the end of `src/fastmcp_pvl_core/_token_store.py` (after the existing `_BaseTokenStore` class):

```python
# ---------------------------------------------------------------------------
# Artifact direction (downloads)
# ---------------------------------------------------------------------------


from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.responses import Response

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request


@dataclass(frozen=True)
class TokenRecord:
    """A one-time downloadable artifact (download direction).

    Attributes:
        content: The raw bytes to serve.
        filename: Suggested filename for ``Content-Disposition``.
        mime_type: MIME type served in ``Content-Type``.
        expires_at: Unix timestamp after which the record is expired.
    """

    content: bytes
    filename: str
    mime_type: str
    expires_at: float


def _sanitize_filename(filename: str) -> str:
    cleaned = filename.replace("\r", "").replace("\n", "")
    cleaned = cleaned.replace('"', "_").replace("\\", "_")
    return cleaned or "download"


class ArtifactStore(_BaseTokenStore[TokenRecord]):
    """In-memory one-time artifact store with TTL expiry."""

    def __init__(
        self,
        ttl_seconds: float = 3600.0,
        *,
        base_url: str | None = None,
        route_path: str = "/artifacts/{token}",
    ) -> None:
        super().__init__()
        if "{token}" not in route_path:
            raise ValueError(
                f"route_path must contain '{{token}}' placeholder; got {route_path!r}"
            )
        self._ttl = float(ttl_seconds)
        self._base_url = base_url
        self._route_path = route_path

    def add(
        self,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
        ttl_seconds: float | None = None,
    ) -> str:
        self._purge_expired()
        token = self._mint_token()
        ttl = self._ttl if ttl_seconds is None else float(ttl_seconds)
        self._records[token] = TokenRecord(
            content=content,
            filename=filename,
            mime_type=mime_type,
            expires_at=time.time() + ttl,
        )
        logger.debug(
            "artifact_add token_prefix=%s size=%d mime=%s ttl=%.1fs",
            token[:8], len(content), mime_type, ttl,
        )
        return token

    def pop(self, token: str) -> TokenRecord | None:
        return self._atomic_consume(token)

    @property
    def has_base_url(self) -> bool:
        return self._base_url is not None

    def build_url(self, token: str) -> str:
        if self._base_url is None:
            raise RuntimeError("ArtifactStore.base_url is required for URL construction")
        base = self._base_url.rstrip("/")
        path = "/" + self._route_path.lstrip("/")
        return f"{base}{path}".replace("{token}", token)

    def put_ephemeral(
        self,
        content: bytes,
        *,
        content_type: str,
        filename: str,
        ttl_seconds: float | None = None,
    ) -> str:
        token = self.add(
            content, filename=filename, mime_type=content_type, ttl_seconds=ttl_seconds
        )
        return self.build_url(token)

    @staticmethod
    def register_route(
        mcp: FastMCP,
        store: ArtifactStore,
        *,
        path: str = "/artifacts/{token}",
    ) -> None:
        @mcp.custom_route(path, methods=["GET"])
        async def _artifact_handler(request: Request) -> Response:
            token = request.path_params.get("token", "")
            record = store.pop(token)
            if record is None:
                logger.debug("artifact_handler_miss token_prefix=%s", (token or "")[:8])
                return Response(content="Not Found", status_code=404)
            safe_filename = _sanitize_filename(record.filename)
            logger.info(
                "artifact_handler_serve token_prefix=%s size=%d mime=%s",
                token[:8], len(record.content), record.mime_type,
            )
            return Response(
                content=record.content,
                media_type=record.mime_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{safe_filename}"',
                },
            )


# Module-level singleton accessor (kept here so HTTP route handlers and
# tool bodies share the same store without DI/lifespan plumbing).

_artifact_store: ArtifactStore | None = None


def set_artifact_store(store: ArtifactStore | None) -> None:
    global _artifact_store
    _artifact_store = store


def get_artifact_store() -> ArtifactStore:
    if _artifact_store is None:
        raise RuntimeError(
            "ArtifactStore singleton is not set — call set_artifact_store(...) "
            "during server startup (HTTP/SSE transports only)"
        )
    return _artifact_store
```

- [ ] **Step 3: Replace the body of `src/fastmcp_pvl_core/_artifacts.py` with a re-export shim**

```python
"""Backward-compatibility shim.

The real implementation now lives in :mod:`fastmcp_pvl_core._token_store`.
This module re-exports the public artifact-direction surface so existing
``from fastmcp_pvl_core._artifacts import ...`` imports keep working
during the deprecation window. Slated for removal one minor version
after introduction.
"""

from __future__ import annotations

from fastmcp_pvl_core._token_store import (
    ArtifactStore,
    TokenRecord,
    get_artifact_store,
    set_artifact_store,
)

__all__ = [
    "ArtifactStore",
    "TokenRecord",
    "get_artifact_store",
    "set_artifact_store",
]
```

- [ ] **Step 4: Run all artifact tests + the new token-store tests, verify all pass**

```bash
uv run pytest tests/test_token_store.py tests/test_artifacts.py tests/test_artifacts_ext.py -v
```

Expected: same count as Step 1, plus the 5 from Task 1, all green.

- [ ] **Step 5: Run the full suite to catch unrelated breakage**

```bash
uv run pytest -q
```

Expected: full suite passes.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_token_store.py src/fastmcp_pvl_core/_artifacts.py
git commit -m "refactor(file-exchange): move ArtifactStore into _token_store; _artifacts becomes shim"
```

---

### Task 3: Add `UploadRecord` dataclass

**Files:**
- Modify: `src/fastmcp_pvl_core/_token_store.py`
- Create: `tests/test_uploads.py`

- [ ] **Step 1: Write the failing test** in `tests/test_uploads.py`:

```python
"""Tests for upload direction records and store."""

from __future__ import annotations

import dataclasses
import time

import pytest

from fastmcp_pvl_core._token_store import UploadRecord


class TestUploadRecord:
    def test_is_frozen(self) -> None:
        record = UploadRecord(
            target_id="vault/foo.md",
            max_bytes=1024,
            extra={},
            expires_at=time.time() + 60,
        )
        assert dataclasses.is_dataclass(record)
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.target_id = "x"  # type: ignore[misc]

    def test_default_extra_is_empty_dict_via_factory(self) -> None:
        # Two records must not share the default mutable.
        a = UploadRecord(
            target_id="a", max_bytes=10, extra={}, expires_at=0.0
        )
        b = UploadRecord(
            target_id="b", max_bytes=10, extra={}, expires_at=0.0
        )
        # Records frozen, so we can't mutate; check identity differs only
        # if the caller supplied distinct dicts.
        assert a.extra is not b.extra or (a.extra == {} and b.extra == {})

    def test_required_fields(self) -> None:
        with pytest.raises(TypeError):
            UploadRecord()  # type: ignore[call-arg]
```

- [ ] **Step 2: Run, verify it fails**

```bash
uv run pytest tests/test_uploads.py -v
```

Expected: ImportError on `UploadRecord`.

- [ ] **Step 3: Add `UploadRecord` to `_token_store.py`** (Place the new section AFTER `class ArtifactStore` and AFTER the artifact singleton accessor block, BEFORE the end of the file. The artifact section (record → store → singleton) stays self-contained; the upload section sits below it, parallel-shaped, and will gain `UploadStore` + its singleton in Task 4.):

```python
# ---------------------------------------------------------------------------
# Upload direction (intake)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadRecord:
    """A reservation slot for an in-flight upload (intake direction).

    Unlike :class:`TokenRecord`, an ``UploadRecord`` does **not** carry
    bytes — bytes arrive over the wire when the agent ``POST``s to
    ``/<ns>/uploads/{token}``. The record carries only the metadata the
    receiver needs to commit the bytes to its domain (``target_id``,
    ``extra``) plus the runtime guards (``max_bytes``, ``expires_at``).

    Attributes:
        target_id: Opaque identifier for the upload destination, chosen
            by the tool caller. The receiver decides what it means
            (path, document id, etc.). Same character rules as
            ``origin_id`` and ``exchange://`` segments — see spec.
        max_bytes: Hard size cap for the POST body, enforced at the
            HTTP route before dispatch.
        extra: Caller-supplied dict passed verbatim to the receiver.
        expires_at: Unix timestamp after which the reservation is
            invalid; consumed reservations are removed atomically by
            ``UploadStore.consume``.
    """

    target_id: str
    max_bytes: int
    extra: dict
    expires_at: float
```

- [ ] **Step 4: Run, verify it passes**

```bash
uv run pytest tests/test_uploads.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_token_store.py tests/test_uploads.py
git commit -m "feat(file-exchange): add UploadRecord dataclass for intake reservations"
```

---

### Task 4: Add `UploadStore` and singleton accessors

**Files:**
- Modify: `src/fastmcp_pvl_core/_token_store.py`
- Modify: `tests/test_uploads.py`

- [ ] **Step 1: Add tests for `UploadStore`** to `tests/test_uploads.py`:

```python
from fastmcp_pvl_core._token_store import UploadStore  # noqa: E402  (re-grouped at top in real edit)


class TestUploadStore:
    def test_reserve_returns_token_and_url(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(target_id="vault/foo.md", max_bytes=1024)
        assert isinstance(token, str) and len(token) == 32
        url = store.build_url(token)
        assert url == f"https://srv.test/uploads/{token}"

    def test_reserve_with_explicit_ttl_and_extra(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(
            target_id="vault/x.md", max_bytes=10, ttl_seconds=42, extra={"k": 1}
        )
        record = store.peek(token)
        assert record is not None
        assert record.extra == {"k": 1}
        assert record.expires_at - time.time() == pytest.approx(42, abs=2)

    def test_consume_returns_record_then_none(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(target_id="x", max_bytes=10)
        first = store.consume(token)
        assert first is not None and first.target_id == "x"
        assert store.consume(token) is None

    def test_consume_returns_none_for_expired(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(target_id="x", max_bytes=10, ttl_seconds=-1)
        assert store.consume(token) is None

    def test_consume_returns_none_for_unknown(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        assert store.consume("not-a-real-token") is None

    def test_build_url_requires_base_url(self) -> None:
        store = UploadStore()
        token = store.reserve(target_id="x", max_bytes=10)
        with pytest.raises(RuntimeError, match="base_url"):
            store.build_url(token)


def test_upload_store_singleton_accessors() -> None:
    from fastmcp_pvl_core._token_store import (
        get_upload_store, set_upload_store,
    )
    set_upload_store(None)
    with pytest.raises(RuntimeError, match="set_upload_store"):
        get_upload_store()
    s = UploadStore(base_url="https://srv.test")
    set_upload_store(s)
    assert get_upload_store() is s
    set_upload_store(None)  # leave clean
```

- [ ] **Step 2: Run, verify failures**

```bash
uv run pytest tests/test_uploads.py -v
```

Expected: ImportError on `UploadStore` / accessors.

- [ ] **Step 3: Add `UploadStore` and singletons to `_token_store.py`** below `UploadRecord`:

```python
class UploadStore(_BaseTokenStore[UploadRecord]):
    """In-memory reservation store for inbound uploads.

    Mirrors :class:`ArtifactStore` lifecycle (UUID4 token, lazy expiry,
    atomic consume) but inverts the data flow: the tool reserves a slot
    and the bytes arrive over HTTP later. The bytes themselves never
    live in the record.
    """

    def __init__(
        self,
        ttl_seconds: float = 300.0,
        *,
        base_url: str | None = None,
        route_path: str = "/uploads/{token}",
    ) -> None:
        super().__init__()
        if "{token}" not in route_path:
            raise ValueError(
                f"route_path must contain '{{token}}' placeholder; got {route_path!r}"
            )
        self._ttl = float(ttl_seconds)
        self._base_url = base_url
        self._route_path = route_path

    def reserve(
        self,
        *,
        target_id: str,
        max_bytes: int,
        ttl_seconds: float | None = None,
        extra: dict | None = None,
    ) -> str:
        """Mint a token reserving a one-shot upload slot."""
        self._purge_expired()
        token = self._mint_token()
        ttl = self._ttl if ttl_seconds is None else float(ttl_seconds)
        self._records[token] = UploadRecord(
            target_id=target_id,
            max_bytes=int(max_bytes),
            extra=dict(extra or {}),
            expires_at=time.time() + ttl,
        )
        logger.debug(
            "upload_reserve token_prefix=%s target_id=%s max_bytes=%d ttl=%.1fs",
            token[:8], target_id, max_bytes, ttl,
        )
        return token

    def consume(self, token: str) -> UploadRecord | None:
        """Atomic: returns record + marks consumed, or ``None`` if missing/expired/consumed."""
        return self._atomic_consume(token)

    def peek(self, token: str) -> UploadRecord | None:
        """Inspect without consuming. Test-only utility; callers MUST use :meth:`consume` in production."""
        record = self._records.get(token)
        if record is None or time.time() > record.expires_at:
            return None
        return record

    @property
    def has_base_url(self) -> bool:
        return self._base_url is not None

    def build_url(self, token: str) -> str:
        if self._base_url is None:
            raise RuntimeError(
                "UploadStore.base_url is required for URL construction"
            )
        base = self._base_url.rstrip("/")
        path = "/" + self._route_path.lstrip("/")
        return f"{base}{path}".replace("{token}", token)


# ---------------------------------------------------------------------------
# Upload singleton accessor
# ---------------------------------------------------------------------------

_upload_store: UploadStore | None = None


def set_upload_store(store: UploadStore | None) -> None:
    global _upload_store
    _upload_store = store


def get_upload_store() -> UploadStore:
    if _upload_store is None:
        raise RuntimeError(
            "UploadStore singleton is not set — call set_upload_store(...) "
            "during server startup (HTTP/SSE transports only)"
        )
    return _upload_store
```

- [ ] **Step 4: Run, verify all pass**

```bash
uv run pytest tests/test_uploads.py -v
```

Expected: all upload-store tests green.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_token_store.py tests/test_uploads.py
git commit -m "feat(file-exchange): add UploadStore with reserve/consume/peek API"
```

---

### Task 5: Add `register_upload_route` runtime helper — happy path with buffered receiver

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange_runtime.py`
- Create: `tests/test_file_exchange_upload_route.py`

The helper mounts the POST handler. We build it incrementally — happy path first, error cases in subsequent tasks.

- [ ] **Step 1: Write the happy-path failing test** in `tests/test_file_exchange_upload_route.py`:

```python
"""Integration tests for the POST /<ns>/uploads/{token} route."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._file_exchange_runtime import register_upload_route
from fastmcp_pvl_core._token_store import UploadStore


def _build_app(
    receiver, *, accepts: tuple[str, ...] = ("*/*",)
) -> tuple[FastMCP, UploadStore]:
    """Construct a FastMCP with the upload route mounted."""
    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(
        mcp, store=store, namespace="ns", receiver=receiver, accepts=accepts
    )
    return mcp, store


@pytest.mark.asyncio
async def test_post_happy_path_returns_receiver_dict() -> None:
    captured: dict[str, Any] = {}

    def recv(record, body: bytes) -> dict[str, Any]:
        captured["target_id"] = record.target_id
        captured["body"] = body
        return {"path": record.target_id, "size_bytes": len(body)}

    mcp, store = _build_app(recv)
    token = store.reserve(target_id="hello.txt", max_bytes=1024)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"hello world",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"path": "hello.txt", "size_bytes": 11}
    assert captured["target_id"] == "hello.txt"
    assert captured["body"] == b"hello world"
```

- [ ] **Step 2: Run, verify it fails**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -v
```

Expected: ImportError on `register_upload_route`.

- [ ] **Step 3: Implement `register_upload_route` in `src/fastmcp_pvl_core/_file_exchange_runtime.py`**

Append a new section at end of file:

```python
# ---------------------------------------------------------------------------
# Upload route (POST /<ns>/uploads/{token}) — spec §"Inbound HTTP transfer"
# ---------------------------------------------------------------------------

import json as _json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from starlette.requests import Request as _StarletteRequest
from starlette.responses import JSONResponse, Response as _StarletteResponse

if TYPE_CHECKING:
    from fastmcp_pvl_core._token_store import UploadRecord, UploadStore


BufferedReceiver = Callable[["UploadRecord", bytes], "dict[str, Any] | Awaitable[dict[str, Any]]"]
StreamReceiver = Callable[
    ["UploadRecord", AsyncIterator[bytes]], "Awaitable[dict[str, Any]]"
]


def _accepts_match(content_type: str, accepts: tuple[str, ...]) -> bool:
    if "*/*" in accepts:
        return True
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    for entry in accepts:
        e = entry.strip().lower()
        if e == ct:
            return True
        if e.endswith("/*") and ct.startswith(e[:-1]):
            return True
    return False


def register_upload_route(
    mcp: FastMCP,
    *,
    store: UploadStore,
    namespace: str,
    receiver: BufferedReceiver | None = None,
    stream_receiver: StreamReceiver | None = None,
    accepts: tuple[str, ...] = ("*/*",),
) -> None:
    """Mount ``POST /<namespace>/uploads/{token}`` on ``mcp``.

    Exactly one of ``receiver`` (buffered) or ``stream_receiver``
    (chunked) MUST be supplied. The route handles token lookup, size
    enforcement, MIME filtering, atomic one-time consumption, and
    receiver dispatch with the documented status-code mapping.
    """
    if (receiver is None) == (stream_receiver is None):
        raise ValueError(
            "register_upload_route requires exactly one of receiver= or "
            "stream_receiver="
        )

    path = f"/{namespace}/uploads/{{token}}"

    @mcp.custom_route(path, methods=["POST"])
    async def _upload_handler(request: _StarletteRequest) -> _StarletteResponse:
        token = request.path_params.get("token", "")
        record = store.consume(token)
        if record is None:
            logger.debug("upload_handler_miss token_prefix=%s", (token or "")[:8])
            return _StarletteResponse(content="Not Found", status_code=404)
        body = await request.body()
        if receiver is not None:
            result = receiver(record, body)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[assignment]
        else:
            assert stream_receiver is not None  # narrow

            async def _single_chunk() -> AsyncIterator[bytes]:
                yield body
            result = await stream_receiver(record, _single_chunk())
        if not isinstance(result, dict):
            logger.error(
                "upload_receiver returned non-dict (%s); coercing to {} for response",
                type(result).__name__,
            )
            result = {}
        return JSONResponse(result, status_code=200)
```

- [ ] **Step 4: Run, verify it passes**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange_runtime.py tests/test_file_exchange_upload_route.py
git commit -m "feat(file-exchange): mount POST /<ns>/uploads/{token} happy path"
```

---

### Task 6: Token-error status codes (404 missing/consumed, 410 expired)

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange_runtime.py`
- Modify: `tests/test_file_exchange_upload_route.py`

Today the handler returns 404 in all token-failure cases. The spec requires distinguishing expired (410) from missing/consumed (404 with no leak about prior existence).

- [ ] **Step 1: Add tests** for the three failure modes:

```python
@pytest.mark.asyncio
async def test_post_unknown_token_returns_404() -> None:
    mcp, _ = _build_app(lambda rec, body: {})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/ns/uploads/bogus", content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_already_consumed_token_returns_404() -> None:
    mcp, store = _build_app(lambda rec, body: {"ok": True})
    token = store.reserve(target_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        first = await client.post(f"/ns/uploads/{token}", content=b"x")
        second = await client.post(f"/ns/uploads/{token}", content=b"x")
    assert first.status_code == 200
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_post_expired_token_returns_410() -> None:
    mcp, store = _build_app(lambda rec, body: {})
    token = store.reserve(target_id="x", max_bytes=10, ttl_seconds=-1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}", content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 410
```

- [ ] **Step 2: Run, the 410 case fails** (the others pass — current handler already 404s on consume() returning None).

```bash
uv run pytest tests/test_file_exchange_upload_route.py -v
```

Expected: `test_post_expired_token_returns_410` fails (returns 404).

- [ ] **Step 3: Distinguish expired from missing in `_upload_handler`**

This requires `UploadStore.consume_or_status(token) -> tuple[UploadRecord | None, str]` returning `("ok", record)`, `("expired", None)`, `("missing", None)`. Add it to `UploadStore`:

In `src/fastmcp_pvl_core/_token_store.py`, add a method on `UploadStore`:

```python
    def consume_or_status(
        self, token: str
    ) -> tuple[UploadRecord | None, str]:
        """Atomic consume that distinguishes expired from missing/consumed.

        Returns one of:
            (record, "ok")       — token was valid; record consumed.
            (None,   "expired")  — token existed but had passed expires_at; removed.
            (None,   "missing")  — token unknown (never existed, or already consumed).
        """
        # Pop the requested token BEFORE purging. ``_purge_expired`` would
        # otherwise erase a record whose ``expires_at`` is already in the
        # past, collapsing the expired-vs-missing distinction this method
        # exists to provide.
        record = self._records.pop(token, None)
        # Sweep the rest of the table for tidiness so the lazy-purge
        # invariant ("each access path triggers a purge") still holds.
        self._purge_expired()
        if record is None:
            return None, "missing"
        if time.time() > record.expires_at:
            return None, "expired"
        return record, "ok"
```

(Implementations MAY tighten the second tuple element to `Literal["ok", "expired", "missing"]` for caller-side narrowing; the plan keeps `str` for readability.)

> Note: this method's pop-then-purge ordering is intentionally inverted from `_atomic_consume`'s purge-then-pop. `_atomic_consume` conflates expired and missing into `None`, so a pre-pop purge is fine there; `consume_or_status` distinguishes the two states, which requires capturing the record before any sweep removes it. The post-pop `time.time() > record.expires_at` check is the standard defense-in-depth for the microsecond-window race between the pop and the comparison.

In `src/fastmcp_pvl_core/_file_exchange_runtime.py`, update the handler to use it:

```python
        record, status = store.consume_or_status(token)
        if status == "expired":
            logger.debug("upload_handler_expired token_prefix=%s", token[:8])
            return _StarletteResponse(content="Gone", status_code=410)
        if record is None:
            logger.debug("upload_handler_miss token_prefix=%s", (token or "")[:8])
            return _StarletteResponse(content="Not Found", status_code=404)
```

- [ ] **Step 4: Run all upload tests, verify all pass**

```bash
uv run pytest tests/test_file_exchange_upload_route.py tests/test_uploads.py -v
```

Expected: green across the board.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_token_store.py src/fastmcp_pvl_core/_file_exchange_runtime.py tests/test_file_exchange_upload_route.py
git commit -m "feat(file-exchange): distinguish expired (410) from missing (404) for upload tokens"
```

---

### Task 7: Size enforcement (413 by Content-Length and by mid-stream overrun)

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange_runtime.py`
- Modify: `tests/test_file_exchange_upload_route.py`

- [ ] **Step 1: Add tests** for both size-failure paths:

```python
@pytest.mark.asyncio
async def test_post_oversize_by_content_length_returns_413() -> None:
    mcp, store = _build_app(lambda rec, body: {"ok": True})
    token = store.reserve(target_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=b"x" * 11,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "11",
            },
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_post_oversize_via_chunk_overrun_returns_413() -> None:
    """Defense-in-depth: client lies about Content-Length, real body is bigger."""
    captured: dict[str, Any] = {"called": False}

    def recv(record, body: bytes) -> dict[str, Any]:
        captured["called"] = True
        return {"ok": True}

    mcp, store = _build_app(recv)
    token = store.reserve(target_id="x", max_bytes=10)

    async def chunk_iter():
        yield b"x" * 8
        yield b"x" * 8  # total 16 > 10

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}",
            content=chunk_iter(),
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 413
    assert captured["called"] is False
```

- [ ] **Step 2: Run, verify both fail**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -k oversize -v
```

Expected: both fail (no current size enforcement).

- [ ] **Step 3: Add Content-Length precheck and per-chunk running total** in `_upload_handler`:

Replace the body-reading section of `_upload_handler` with:

```python
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                cl_int = int(cl)
            except ValueError:
                cl_int = -1
            if cl_int > record.max_bytes:
                logger.debug(
                    "upload_handler_oversize_cl token_prefix=%s declared=%d max=%d",
                    token[:8], cl_int, record.max_bytes,
                )
                return _StarletteResponse(
                    content="Payload Too Large", status_code=413
                )

        # Defense in depth — the client may have lied. Read in chunks
        # tracking the running total ourselves; bail as soon as we
        # exceed the cap rather than after a full buffer fill.
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > record.max_bytes:
                logger.debug(
                    "upload_handler_oversize_chunk token_prefix=%s total=%d max=%d",
                    token[:8], total, record.max_bytes,
                )
                return _StarletteResponse(
                    content="Payload Too Large", status_code=413
                )
            chunks.append(chunk)
        body = b"".join(chunks)
```

(The streaming-receiver branch will get its own non-buffering implementation in Task 9; for now this code path is buffered-only.)

- [ ] **Step 4: Run, verify both pass**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -v
```

Expected: all upload-route tests green.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange_runtime.py tests/test_file_exchange_upload_route.py
git commit -m "feat(file-exchange): enforce upload max_bytes via Content-Length + chunk running total (413)"
```

---

### Task 8: Content-Type filter (415 mismatch; `*/*` disables)

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange_runtime.py`
- Modify: `tests/test_file_exchange_upload_route.py`

- [ ] **Step 1: Add tests**:

```python
@pytest.mark.asyncio
async def test_post_unaccepted_content_type_returns_415() -> None:
    def recv(record, body: bytes) -> dict[str, Any]:
        return {"ok": True}

    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(
        mcp, store=store, namespace="ns", receiver=recv,
        accepts=("application/octet-stream", "image/png"),
    )
    token = store.reserve(target_id="x", max_bytes=1024)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}", content=b"x",
            headers={"Content-Type": "text/plain"},
        )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_post_wildcard_accepts_disables_check() -> None:
    mcp, store = _build_app(lambda rec, body: {"ok": True}, accepts=("*/*",))
    token = store.reserve(target_id="x", max_bytes=1024)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}", content=b"x",
            headers={"Content-Type": "audio/weird"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_glob_accepts_matches_subtype() -> None:
    def recv(record, body): return {"ok": True}
    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(
        mcp, store=store, namespace="ns", receiver=recv,
        accepts=("image/*",),
    )
    token = store.reserve(target_id="x", max_bytes=1024)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}", content=b"x",
            headers={"Content-Type": "image/png"},
        )
    assert resp.status_code == 200
```

- [ ] **Step 2: Run, verify the 415 test fails**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -k content_type -v
```

Expected: `test_post_unaccepted_content_type_returns_415` fails (returns 200).

- [ ] **Step 3: Insert the MIME check** in `_upload_handler` between the token lookup and the size precheck:

```python
        ct_header = request.headers.get("content-type", "")
        if not _accepts_match(ct_header, accepts):
            logger.debug(
                "upload_handler_unsupported_media_type token_prefix=%s ct=%r",
                token[:8], ct_header,
            )
            return _StarletteResponse(
                content="Unsupported Media Type", status_code=415,
            )
```

- [ ] **Step 4: Run, verify all upload-route tests pass**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange_runtime.py tests/test_file_exchange_upload_route.py
git commit -m "feat(file-exchange): enforce upload Content-Type filter with */* and globbing (415)"
```

---

### Task 9: Receiver exception → status code mapping (400, 409, 500)

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange_runtime.py`
- Modify: `tests/test_file_exchange_upload_route.py`

- [ ] **Step 1: Add tests** for each exception class:

```python
@pytest.mark.asyncio
async def test_post_receiver_value_error_returns_400() -> None:
    def recv(record, body):
        raise ValueError("bad path")
    mcp, store = _build_app(recv)
    token = store.reserve(target_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}", content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 400
    assert "bad path" in resp.text


@pytest.mark.asyncio
async def test_post_receiver_file_exists_returns_409() -> None:
    def recv(record, body):
        raise FileExistsError("already there")
    mcp, store = _build_app(recv)
    token = store.reserve(target_id="x", max_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}", content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_post_receiver_other_exception_returns_500(caplog) -> None:
    import logging
    def recv(record, body):
        raise RuntimeError("kaboom")
    mcp, store = _build_app(recv)
    token = store.reserve(target_id="x", max_bytes=10)
    with caplog.at_level(logging.ERROR):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp.http_app()),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                f"/ns/uploads/{token}", content=b"x",
                headers={"Content-Type": "application/octet-stream"},
            )
    assert resp.status_code == 500
    assert any("kaboom" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run, verify the three tests fail**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -k receiver -v
```

Expected: ValueError/FileExistsError/RuntimeError currently propagate as 500 (or break the request). All three new tests fail.

- [ ] **Step 3: Wrap the receiver call** in `_upload_handler`:

```python
        try:
            if receiver is not None:
                result = receiver(record, body)
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[assignment]
            else:
                assert stream_receiver is not None
                async def _single_chunk() -> AsyncIterator[bytes]:
                    yield body
                result = await stream_receiver(record, _single_chunk())
        except ValueError as exc:
            logger.info("upload_receiver_value_error token_prefix=%s: %s", token[:8], exc)
            return _StarletteResponse(content=str(exc), status_code=400)
        except FileExistsError as exc:
            logger.info("upload_receiver_conflict token_prefix=%s: %s", token[:8], exc)
            return _StarletteResponse(content=str(exc), status_code=409)
        except Exception:
            logger.exception("upload_receiver_failure token_prefix=%s", token[:8])
            return _StarletteResponse(content="Internal Server Error", status_code=500)
```

- [ ] **Step 4: Run, verify all upload-route tests pass**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange_runtime.py tests/test_file_exchange_upload_route.py
git commit -m "feat(file-exchange): map receiver exceptions to upload HTTP status codes"
```

---

### Task 10: Streaming receiver path (no buffering)

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange_runtime.py`
- Modify: `tests/test_file_exchange_upload_route.py`

The streaming branch should not allocate the full body in memory. We pass the chunks straight from `request.stream()` to the receiver, with the running-total cap applied via a small async generator wrapper.

- [ ] **Step 1: Add tests** for streaming receiver:

```python
@pytest.mark.asyncio
async def test_post_stream_receiver_sees_chunks_live() -> None:
    seen: list[bytes] = []

    async def recv(record, body):
        async for chunk in body:
            seen.append(chunk)
        return {"chunks": len(seen), "total": sum(len(c) for c in seen)}

    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(
        mcp, store=store, namespace="ns", stream_receiver=recv,
    )
    token = store.reserve(target_id="x", max_bytes=1024)

    async def chunk_iter():
        yield b"abc"
        yield b"defg"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}", content=chunk_iter(),
            headers={"Content-Type": "application/octet-stream"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"chunks": len(seen), "total": 7}
    assert b"".join(seen) == b"abcdefg"


@pytest.mark.asyncio
async def test_post_stream_receiver_oversize_aborts_before_completion() -> None:
    received_chunks: list[bytes] = []

    async def recv(record, body):
        async for chunk in body:
            received_chunks.append(chunk)
        return {"ok": True}

    mcp = FastMCP(name="test-upload")
    store = UploadStore(base_url="http://test.invalid")
    register_upload_route(
        mcp, store=store, namespace="ns", stream_receiver=recv,
    )
    token = store.reserve(target_id="x", max_bytes=5)

    async def chunk_iter():
        yield b"abcd"
        yield b"efgh"  # cumulative 8 > 5

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/ns/uploads/{token}", content=chunk_iter(),
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 413
```

- [ ] **Step 2: Run, verify the streaming test fails**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -k stream -v
```

Expected: streaming test fails (the current streaming branch in Task 5 buffers via `_single_chunk`).

- [ ] **Step 3: Replace the body-reading branch** in `_upload_handler` so streaming bypasses buffering:

```python
        # Common precheck (Content-Length).
        cl = request.headers.get("content-length")
        if cl is not None:
            try: cl_int = int(cl)
            except ValueError: cl_int = -1
            if cl_int > record.max_bytes:
                return _StarletteResponse(content="Payload Too Large", status_code=413)

        async def _bounded_chunks() -> AsyncIterator[bytes]:
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > record.max_bytes:
                    raise _OversizeError()
                yield chunk

        try:
            if receiver is not None:
                # Buffered: collect chunks via the bounded generator.
                buf: list[bytes] = []
                async for c in _bounded_chunks():
                    buf.append(c)
                body = b"".join(buf)
                result = receiver(record, body)
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[assignment]
            else:
                assert stream_receiver is not None
                result = await stream_receiver(record, _bounded_chunks())
        except _OversizeError:
            logger.debug("upload_handler_oversize_chunk token_prefix=%s", token[:8])
            return _StarletteResponse(content="Payload Too Large", status_code=413)
        except ValueError as exc:
            ...    # (same as before)
        except FileExistsError as exc:
            ...
        except Exception:
            ...
```

And add the helper exception class above `register_upload_route`:

```python
class _OversizeError(Exception):
    """Internal: signals the upload exceeded its cap mid-stream."""
```

- [ ] **Step 4: Run all upload-route tests**

```bash
uv run pytest tests/test_file_exchange_upload_route.py -v
```

Expected: all green, including the two new streaming tests.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange_runtime.py tests/test_file_exchange_upload_route.py
git commit -m "feat(file-exchange): streaming upload receiver path with bounded chunk iterator"
```

---

### Task 11: Add `UploadHandle` + `register_file_exchange_upload` (basic registration)

**Files:**
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Create: `tests/test_file_exchange_upload_facade.py`

- [ ] **Step 1: Write the basic-registration failing test** in `tests/test_file_exchange_upload_facade.py`:

```python
"""Tests for register_file_exchange_upload public facade."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import register_file_exchange_upload, UploadRecord


@pytest.mark.asyncio
async def test_registration_adds_create_upload_link_tool(monkeypatch) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    def recv(record: UploadRecord, body: bytes) -> dict[str, Any]:
        return {"target_id": record.target_id}

    mcp = FastMCP(name="test")
    handle = register_file_exchange_upload(
        mcp, namespace="ns", env_prefix="TEST_UPLOAD",
        receiver=recv,
    )
    assert handle.namespace == "ns"
    assert handle.tool_name == "create_upload_link"

    # Tool should be registered.
    tools = await mcp.get_tools()
    assert "create_upload_link" in tools


@pytest.mark.asyncio
async def test_create_upload_link_returns_url_and_ttl(monkeypatch) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp, namespace="ns", env_prefix="TEST_UPLOAD",
        receiver=lambda rec, body: {"ok": True},
    )
    tool = (await mcp.get_tools())["create_upload_link"]
    result = await tool.run(arguments={"target_id": "vault/foo.md"})
    payload = result.structured_content or {}
    assert payload["target_id"] == "vault/foo.md"
    assert payload["upload_url"].startswith("http://srv.test/ns/uploads/")
    assert payload["expires_in_seconds"] > 0
```

- [ ] **Step 2: Run, verify failures (ImportError on the public name)**

```bash
uv run pytest tests/test_file_exchange_upload_facade.py -v
```

Expected: ImportError on `register_file_exchange_upload`.

- [ ] **Step 3: Add `UploadHandle` and `register_file_exchange_upload`** at the end of `src/fastmcp_pvl_core/file_exchange.py` (before the `__all__`).

```python
# ---------------------------------------------------------------------------
# Upload direction (intake)
# ---------------------------------------------------------------------------

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastmcp_pvl_core._token_store import (
    UploadRecord, UploadStore, set_upload_store,
)
from fastmcp_pvl_core._file_exchange_runtime import register_upload_route
from fastmcp_pvl_core._env import env, parse_bool

_DEFAULT_UPLOAD_TOOL = "create_upload_link"
_DEFAULT_UPLOAD_TTL_SECONDS = 300.0
_DEFAULT_UPLOAD_TTL_MAX_SECONDS = 3600.0
_DEFAULT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024

BufferedUploadReceiver = Callable[[UploadRecord, bytes], "dict[str, Any] | Awaitable[dict[str, Any]]"]
StreamUploadReceiver = Callable[[UploadRecord, AsyncIterator[bytes]], "Awaitable[dict[str, Any]]"]
PreLinkValidator = Callable[[str, "dict[str, Any] | None"], None]


@dataclass(frozen=True)
class UploadHandle:
    """Handle returned by :func:`register_file_exchange_upload`."""

    namespace: str
    tool_name: str
    enabled: bool
    upload_store: UploadStore | None
    ttl_default: float
    ttl_max: float
    max_bytes_default: int

    def create_link(
        self,
        *,
        target_id: str,
        ttl_seconds: float | None = None,
        max_bytes: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[str, float]:
        """Mint an upload reservation directly. Escape valve for advanced wraps.

        Returns:
            (upload_url, effective_ttl_seconds).
        """
        if self.upload_store is None:
            raise RuntimeError("upload not enabled (transport=stdio or disabled)")
        ttl = float(self.ttl_default if ttl_seconds is None else ttl_seconds)
        ttl = min(ttl, self.ttl_max)
        cap = int(self.max_bytes_default if max_bytes is None else max_bytes)
        token = self.upload_store.reserve(
            target_id=target_id, max_bytes=cap, ttl_seconds=ttl, extra=extra,
        )
        return self.upload_store.build_url(token), ttl


def register_file_exchange_upload(
    mcp: FastMCP,
    *,
    namespace: str,
    env_prefix: str,
    receiver: BufferedUploadReceiver | None = None,
    stream_receiver: StreamUploadReceiver | None = None,
    pre_link_validator: PreLinkValidator | None = None,
    transport: Literal["http", "stdio", "auto"] = "auto",
    upload_tool_name: str = _DEFAULT_UPLOAD_TOOL,
    tool_tags: frozenset[str] = frozenset({"write"}),
    accepts: tuple[str, ...] = ("*/*",),
    max_bytes_default: int = _DEFAULT_UPLOAD_MAX_BYTES,
    ttl_default: float = _DEFAULT_UPLOAD_TTL_SECONDS,
    ttl_max: float = _DEFAULT_UPLOAD_TTL_MAX_SECONDS,
) -> UploadHandle:
    """Wire MCP File Exchange upload direction onto ``mcp``.

    Mirrors :func:`register_file_exchange` for the inbound half. Exactly
    one of ``receiver`` / ``stream_receiver`` MUST be supplied.
    """
    if (receiver is None) == (stream_receiver is None):
        raise ValueError(
            "register_file_exchange_upload requires exactly one of "
            "receiver= or stream_receiver="
        )

    resolved_transport = _resolve_transport(env_prefix, transport)
    enabled = (
        resolved_transport != "stdio"
        and parse_bool(env(env_prefix, "UPLOAD_ENABLED", "true"))
    )
    if not enabled:
        return UploadHandle(
            namespace=namespace, tool_name=upload_tool_name, enabled=False,
            upload_store=None,
            ttl_default=ttl_default, ttl_max=ttl_max,
            max_bytes_default=max_bytes_default,
        )

    base_url = env(env_prefix, "BASE_URL")
    if not base_url:
        logger.warning(
            "register_file_exchange_upload: %s_BASE_URL not set; upload "
            "endpoint disabled (would mint links without a public URL)",
            env_prefix,
        )
        return UploadHandle(
            namespace=namespace, tool_name=upload_tool_name, enabled=False,
            upload_store=None,
            ttl_default=ttl_default, ttl_max=ttl_max,
            max_bytes_default=max_bytes_default,
        )

    # Env-driven knob overrides.
    mb_raw = env(env_prefix, "UPLOAD_MAX_BYTES")
    if mb_raw:
        max_bytes_default = int(mb_raw)
    ttl_raw = env(env_prefix, "UPLOAD_TTL")
    if ttl_raw:
        ttl_default = float(ttl_raw)

    store = UploadStore(
        ttl_seconds=ttl_default,
        base_url=base_url,
        route_path=f"/{namespace}/uploads/{{token}}",
    )
    set_upload_store(store)
    register_upload_route(
        mcp, store=store, namespace=namespace,
        receiver=receiver, stream_receiver=stream_receiver,
        accepts=accepts,
    )

    handle = UploadHandle(
        namespace=namespace, tool_name=upload_tool_name, enabled=True,
        upload_store=store,
        ttl_default=ttl_default, ttl_max=ttl_max,
        max_bytes_default=max_bytes_default,
    )

    @mcp.tool(name=upload_tool_name, tags=tool_tags)
    async def create_upload_link(  # noqa: F811 — defined inside facade
        target_id: str,
        ttl_seconds: int = int(ttl_default),
        max_bytes: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mint a one-time HTTPS POST URL for an inbound upload."""
        if pre_link_validator is not None:
            pre_link_validator(target_id, extra)
        url, eff = handle.create_link(
            target_id=target_id,
            ttl_seconds=ttl_seconds, max_bytes=max_bytes, extra=extra,
        )
        return {
            "upload_url": url,
            "expires_in_seconds": int(eff),
            "target_id": target_id,
        }

    return handle
```

Then export the new names from `src/fastmcp_pvl_core/__init__.py`:

```python
from fastmcp_pvl_core._token_store import (
    ArtifactStore, TokenRecord, UploadRecord, UploadStore,
    get_artifact_store, set_artifact_store,
    get_upload_store, set_upload_store,
)
...
from fastmcp_pvl_core.file_exchange import (
    ConsumerSink, FetchContext, FetchResult, FileExchangeHandle,
    UploadHandle, register_file_exchange, register_file_exchange_upload,
)
```

And add the names to `__all__`.

- [ ] **Step 4: Run, verify all pass**

```bash
uv run pytest tests/test_file_exchange_upload_facade.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/file_exchange.py src/fastmcp_pvl_core/__init__.py tests/test_file_exchange_upload_facade.py
git commit -m "feat(file-exchange): add register_file_exchange_upload public facade"
```

---

### Task 12: `pre_link_validator` runs before token mint

**Files:**
- (already wired in Task 11)
- Modify: `tests/test_file_exchange_upload_facade.py`

- [ ] **Step 1: Add tests** for the validator path:

```python
@pytest.mark.asyncio
async def test_pre_link_validator_blocks_invalid_target(monkeypatch) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    def reject(target_id: str, extra) -> None:
        if ".." in target_id:
            raise ValueError(f"path traversal rejected: {target_id}")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp, namespace="ns", env_prefix="TEST_UPLOAD",
        receiver=lambda rec, body: {"ok": True},
        pre_link_validator=reject,
    )
    tool = (await mcp.get_tools())["create_upload_link"]
    with pytest.raises(Exception, match="path traversal rejected"):
        await tool.run(arguments={"target_id": "../../etc/passwd"})


@pytest.mark.asyncio
async def test_pre_link_validator_passes_extra_through(monkeypatch) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    seen: dict[str, Any] = {}

    def vlog(target_id: str, extra) -> None:
        seen["target_id"] = target_id
        seen["extra"] = extra

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp, namespace="ns", env_prefix="TEST_UPLOAD",
        receiver=lambda rec, body: {"ok": True},
        pre_link_validator=vlog,
    )
    tool = (await mcp.get_tools())["create_upload_link"]
    await tool.run(arguments={"target_id": "x.md", "extra": {"k": 1}})
    assert seen == {"target_id": "x.md", "extra": {"k": 1}}
```

- [ ] **Step 2: Run, verify they pass** (this is already wired by Task 11; the tests are validation):

```bash
uv run pytest tests/test_file_exchange_upload_facade.py -v
```

Expected: all pass.

- [ ] **Step 3: Commit (test-only commit if the implementation already covers it)**

```bash
git add tests/test_file_exchange_upload_facade.py
git commit -m "test(file-exchange): cover pre_link_validator path in upload facade"
```

---

### Task 13: TTL clamp + env-var overrides + mutual exclusion

**Files:**
- Modify: `tests/test_file_exchange_upload_facade.py` (additional tests)
- Modify: `src/fastmcp_pvl_core/file_exchange.py` (only if a test fails)

- [ ] **Step 1: Add tests** for clamp, env overrides, and mutual exclusion:

```python
@pytest.mark.asyncio
async def test_ttl_clamped_to_ttl_max(monkeypatch) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")

    mcp = FastMCP(name="test")
    register_file_exchange_upload(
        mcp, namespace="ns", env_prefix="TEST_UPLOAD",
        receiver=lambda rec, body: {"ok": True},
        ttl_default=300.0, ttl_max=600.0,
    )
    tool = (await mcp.get_tools())["create_upload_link"]
    result = await tool.run(arguments={"target_id": "x", "ttl_seconds": 99999})
    payload = result.structured_content or {}
    assert payload["expires_in_seconds"] == 600  # clamped


@pytest.mark.asyncio
async def test_env_overrides_max_bytes_and_ttl(monkeypatch) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UPLOAD_BASE_URL", "http://srv.test")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_MAX_BYTES", "5000000")
    monkeypatch.setenv("TEST_UPLOAD_UPLOAD_TTL", "120")

    mcp = FastMCP(name="test")
    h = register_file_exchange_upload(
        mcp, namespace="ns", env_prefix="TEST_UPLOAD",
        receiver=lambda rec, body: {"ok": True},
    )
    assert h.max_bytes_default == 5_000_000
    assert h.ttl_default == 120.0


def test_mutual_exclusion_of_receivers() -> None:
    mcp = FastMCP(name="test")
    with pytest.raises(ValueError, match="exactly one"):
        register_file_exchange_upload(
            mcp, namespace="ns", env_prefix="TEST_X",
            receiver=lambda rec, body: {},
            stream_receiver=lambda rec, body: {},  # type: ignore[arg-type]
        )


def test_neither_receiver_raises() -> None:
    mcp = FastMCP(name="test")
    with pytest.raises(ValueError, match="exactly one"):
        register_file_exchange_upload(
            mcp, namespace="ns", env_prefix="TEST_X",
        )


def test_stdio_transport_returns_disabled_handle(monkeypatch) -> None:
    monkeypatch.setenv("TEST_UPLOAD_TRANSPORT", "stdio")
    mcp = FastMCP(name="test")
    h = register_file_exchange_upload(
        mcp, namespace="ns", env_prefix="TEST_UPLOAD",
        receiver=lambda rec, body: {"ok": True},
    )
    assert h.enabled is False
    assert h.upload_store is None
```

- [ ] **Step 2: Run, verify all pass** (Task 11 wiring should cover all of these — running confirms there are no gaps).

```bash
uv run pytest tests/test_file_exchange_upload_facade.py -v
```

Expected: all green. If any fail, fix the facade in `file_exchange.py` and rerun.

- [ ] **Step 3: Commit**

```bash
git add tests/test_file_exchange_upload_facade.py src/fastmcp_pvl_core/file_exchange.py
git commit -m "test(file-exchange): cover upload TTL clamp, env overrides, mutual exclusion, stdio noop"
```

---

### Task 14: Capability-emission cooperation — introduce `_FileExchangeCapabilityBuilder`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange_protocol.py`
- Create: `tests/test_file_exchange_capability_merge.py`

The current download facade calls `register_file_exchange_capability` directly with a fully-formed `FileExchangeCapability`. To let upload contribute its `transfer_methods.http.upload` block, we introduce an accumulator and route both registrars through it.

- [ ] **Step 1: Write the failing test** in `tests/test_file_exchange_capability_merge.py`:

```python
"""Tests for capability-merge across download/upload registrars."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._file_exchange_protocol import (
    _FileExchangeCapabilityBuilder,
)


def test_builder_download_only_emits_flat_http_in_legacy_shape() -> None:
    b = _FileExchangeCapabilityBuilder(
        namespace="ns", legacy_capability_shape=True,
    )
    b.set_download(tool_name="create_download_link")
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["version"] == "0.2"
    assert d["transfer_methods"]["http"] == {"tool": "create_download_link"}


def test_builder_download_only_emits_nested_http_in_v04_shape() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_download(tool_name="create_download_link")
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["version"] == "0.4"
    assert d["transfer_methods"]["http"] == {
        "download": {"tool": "create_download_link"},
    }


def test_builder_upload_only_emits_nested_http_with_upload_only() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_upload(
        tool_name="create_upload_link", max_bytes=10_000_000, max_ttl_seconds=300,
    )
    d = b.build().to_capability_dict()
    assert d["transfer_methods"]["http"] == {
        "upload": {
            "tool": "create_upload_link",
            "max_bytes": 10_000_000,
            "max_ttl_seconds": 300,
        },
    }


def test_builder_both_directions_merge_under_single_http_block() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_download(tool_name="create_download_link")
    b.set_upload(tool_name="create_upload_link", max_bytes=10, max_ttl_seconds=60)
    d = b.build().to_capability_dict()
    http = d["transfer_methods"]["http"]
    assert set(http) == {"download", "upload"}
    assert http["download"]["tool"] == "create_download_link"
    assert http["upload"]["tool"] == "create_upload_link"


def test_builder_both_directions_in_legacy_shape_keeps_only_download_http() -> None:
    b = _FileExchangeCapabilityBuilder(
        namespace="ns", legacy_capability_shape=True,
    )
    b.set_download(tool_name="create_download_link")
    b.set_upload(tool_name="create_upload_link", max_bytes=10, max_ttl_seconds=60)
    d = b.build().to_capability_dict()
    assert d["version"] == "0.2"
    # In legacy shape, the http block is the flat tool: <name>; upload
    # cannot ride along (there's no nested upload key in v0.2). The
    # builder logs a warning but still emits a download-only flat shape.
    assert d["transfer_methods"]["http"] == {"tool": "create_download_link"}


def test_builder_with_neither_direction_returns_none() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    assert b.build() is None
```

- [ ] **Step 2: Run, verify they fail**

```bash
uv run pytest tests/test_file_exchange_capability_merge.py -v
```

Expected: ImportError on `_FileExchangeCapabilityBuilder`.

- [ ] **Step 3: Bump `SPEC_VERSION` and add the builder** in `_file_exchange_protocol.py`:

Change line 36:

```python
SPEC_VERSION = "0.4"
```

And insert before the `# Capability advertisement` section:

```python
# ---------------------------------------------------------------------------
# Capability builder — merges download + upload contributions
# ---------------------------------------------------------------------------


@dataclass
class _FileExchangeCapabilityBuilder:
    """Accumulates per-direction contributions into one capability dict.

    Both ``register_file_exchange`` (download) and
    ``register_file_exchange_upload`` (upload) push their entries into a
    shared module-level instance keyed by namespace; the actual
    capability dict is materialised when both registrars have run.
    """

    namespace: str
    exchange_id: str | None = None
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    legacy_capability_shape: bool = False
    _exchange_present: bool = False
    _download_tool: str | None = None
    _upload_tool: str | None = None
    _upload_max_bytes: int | None = None
    _upload_max_ttl_seconds: int | None = None
    _upload_accepts: tuple[str, ...] | None = None

    def set_exchange(self, present: bool = True) -> None:
        self._exchange_present = present

    def set_download(self, *, tool_name: str) -> None:
        self._download_tool = tool_name

    def set_upload(
        self,
        *,
        tool_name: str,
        max_bytes: int,
        max_ttl_seconds: int,
        accepts: tuple[str, ...] | None = None,
    ) -> None:
        self._upload_tool = tool_name
        self._upload_max_bytes = max_bytes
        self._upload_max_ttl_seconds = max_ttl_seconds
        self._upload_accepts = accepts

    def build(self) -> "FileExchangeCapability | None":
        transfer_methods: dict[str, dict[str, Any]] = {}
        if self._exchange_present:
            transfer_methods["exchange"] = {}
        http_block = self._build_http_block()
        if http_block is not None:
            transfer_methods["http"] = http_block
        if not transfer_methods:
            return None
        version = "0.2" if self.legacy_capability_shape else "0.4"
        return FileExchangeCapability(
            namespace=self.namespace,
            exchange_id=self.exchange_id,
            produces=self.produces,
            consumes=self.consumes,
            transfer_methods=transfer_methods,
            version=version,
        )

    def _build_http_block(self) -> dict[str, Any] | None:
        if self.legacy_capability_shape:
            if self._upload_tool is not None:
                logger.warning(
                    "legacy_capability_shape=True drops the upload entry "
                    "(no nested http.upload key in v0.2 spec); upgrade clients "
                    "to v0.4 or unset legacy_capability_shape to advertise upload."
                )
            if self._download_tool is None:
                return None
            return {"tool": self._download_tool}
        block: dict[str, Any] = {}
        if self._download_tool is not None:
            block["download"] = {"tool": self._download_tool}
        if self._upload_tool is not None:
            up: dict[str, Any] = {
                "tool": self._upload_tool,
                "max_bytes": self._upload_max_bytes,
                "max_ttl_seconds": self._upload_max_ttl_seconds,
            }
            if self._upload_accepts is not None:
                up["accepts"] = list(self._upload_accepts)
            block["upload"] = up
        return block or None
```

Also: extend `FileExchangeCapability.to_capability_dict()` so version is read from the dataclass field rather than the module constant. **Already correct** — it reads `self.version` (line 433). No change needed.

- [ ] **Step 4: Run, verify all pass**

```bash
uv run pytest tests/test_file_exchange_capability_merge.py -v
```

Expected: all 6 green.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange_protocol.py tests/test_file_exchange_capability_merge.py
git commit -m "feat(file-exchange): bump spec version 0.2->0.4 and add capability builder for merge"
```

---

### Task 15: Wire `register_file_exchange` and `register_file_exchange_upload` through the builder

**Files:**
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Verify: full test suite

The two registrars currently emit (or don't emit) capabilities independently. Replace direct `register_file_exchange_capability(...)` calls with `_FileExchangeCapabilityBuilder` accumulation, and emit the merged dict via the existing middleware on first `initialize`.

The builder lives per-server. Because `mcp` instances are parameter-passed into both registrars, we keep a `WeakKeyDictionary` mapping `id(mcp)` → builder. (Note: FastMCP instances are not weakref-able generally; use a regular dict keyed by `id(mcp)` and clear via a public test-helper.)

- [ ] **Step 1: Run the existing capability tests to capture baseline**

```bash
uv run pytest tests/test_file_exchange_protocol.py tests/test_file_exchange_facade.py -v
```

Expected: all pass.

- [ ] **Step 2: Add the per-server builder lookup and rewire both registrars** in `src/fastmcp_pvl_core/file_exchange.py`:

```python
# Module-level — one builder per FastMCP instance.
_capability_builders: dict[int, _FileExchangeCapabilityBuilder] = {}


def _get_or_create_builder(
    mcp: FastMCP, *, namespace: str, exchange_id: str | None = None,
    produces: tuple[str, ...] = (), consumes: tuple[str, ...] = (),
    legacy_capability_shape: bool = False,
) -> _FileExchangeCapabilityBuilder:
    key = id(mcp)
    builder = _capability_builders.get(key)
    if builder is None:
        builder = _FileExchangeCapabilityBuilder(
            namespace=namespace,
            exchange_id=exchange_id,
            produces=produces,
            consumes=consumes,
            legacy_capability_shape=legacy_capability_shape,
        )
        _capability_builders[key] = builder
    else:
        # Subsequent registrars on the same mcp may extend produces/consumes.
        builder.exchange_id = builder.exchange_id or exchange_id
        builder.produces = tuple(set(builder.produces) | set(produces))
        builder.consumes = tuple(set(builder.consumes) | set(consumes))
    return builder


def _emit_capability(mcp: FastMCP) -> FileExchangeCapability | None:
    builder = _capability_builders.get(id(mcp))
    if builder is None:
        return None
    cap = builder.build()
    if cap is not None:
        register_file_exchange_capability(mcp, cap)
    return cap


def reset_capability_builders_for_test() -> None:
    """Test-only: clear the per-server builder registry.

    Required because builders are keyed by ``id(mcp)`` and
    test fixtures recycle FastMCP instances across the process.
    """
    _capability_builders.clear()
```

In `register_file_exchange`, replace the section that builds & registers the capability (around lines 580–599 — `if enabled: ... register_file_exchange_capability(mcp, capability)`):

```python
    if enabled:
        builder = _get_or_create_builder(
            mcp,
            namespace=namespace,
            exchange_id=exchange.exchange_id if exchange is not None else None,
            produces=tuple(produces) if produce else (),
            consumes=tuple(consumes) if consume else (),
        )
        if exchange is not None:
            builder.set_exchange(True)
        if produce and store is not None and base_url is not None:
            builder.set_download(tool_name=download_tool_name)
        # Note: consumer-side fetch tool retains its v0.2-style flat name
        # in the existing _build_transfer_methods path. Until consumer-side
        # is also direction-tagged (separate change), we don't add a
        # builder.set_consumer(...) here.
        capability = _emit_capability(mcp)
        # ... rest of function uses `capability` as before for the handle
```

In `register_file_exchange_upload`, after the route is registered, contribute to the builder and re-emit:

```python
    builder = _get_or_create_builder(mcp, namespace=namespace)
    builder.set_upload(
        tool_name=upload_tool_name,
        max_bytes=max_bytes_default,
        max_ttl_seconds=int(ttl_max),
        accepts=accepts if accepts != ("*/*",) else None,
    )
    _emit_capability(mcp)
```

Add `reset_capability_builders_for_test` to the conftest's auto-reset fixtures (so cross-test bleed doesn't happen):

In `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _reset_capability_builders() -> Iterator[None]:
    from fastmcp_pvl_core.file_exchange import reset_capability_builders_for_test
    reset_capability_builders_for_test()
    yield
    reset_capability_builders_for_test()
```

- [ ] **Step 3: Run the full file-exchange test set**

```bash
uv run pytest tests/test_file_exchange_protocol.py tests/test_file_exchange_facade.py tests/test_file_exchange_runtime.py tests/test_file_exchange_coverage.py tests/test_file_exchange_upload_route.py tests/test_file_exchange_upload_facade.py tests/test_file_exchange_capability_merge.py -v
```

Expected: all green. (The existing facade tests now exercise the builder path; spec version reads "0.4".) If any test asserts `version == "0.2"` literal, update it (the existing protocol test at `tests/test_file_exchange_protocol.py` is the most likely candidate — adjust to `"0.4"`.

- [ ] **Step 4: Run the entire suite**

```bash
uv run pytest -q
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/file_exchange.py tests/conftest.py tests/test_file_exchange_protocol.py
git commit -m "refactor(file-exchange): emit capability via builder; download+upload cooperate on merge"
```

---

### Task 16: Add an end-to-end facade test that registers both directions

**Files:**
- Modify: `tests/test_file_exchange_capability_merge.py`

- [ ] **Step 1: Add an end-to-end test** that runs both real registrars and confirms the merged capability:

```python
@pytest.mark.asyncio
async def test_register_both_directions_emits_merged_http(monkeypatch) -> None:
    monkeypatch.setenv("TEST_DUAL_TRANSPORT", "http")
    monkeypatch.setenv("TEST_DUAL_BASE_URL", "http://srv.test")
    monkeypatch.setenv("MCP_EXCHANGE_DIR", "")  # explicitly disable exchange volume

    from fastmcp_pvl_core import register_file_exchange, register_file_exchange_upload

    mcp = FastMCP(name="dual")
    register_file_exchange(
        mcp, namespace="ns", env_prefix="TEST_DUAL",
        produces=["image/png"],
    )
    register_file_exchange_upload(
        mcp, namespace="ns", env_prefix="TEST_DUAL",
        receiver=lambda rec, body: {"ok": True},
    )

    from fastmcp_pvl_core.file_exchange import _capability_builders
    builder = _capability_builders[id(mcp)]
    cap = builder.build()
    d = cap.to_capability_dict()

    assert d["version"] == "0.4"
    http = d["transfer_methods"]["http"]
    assert "download" in http and http["download"]["tool"] == "create_download_link"
    assert "upload" in http and http["upload"]["tool"] == "create_upload_link"
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/test_file_exchange_capability_merge.py -v
```

Expected: green.

- [ ] **Step 3: Commit**

```bash
git add tests/test_file_exchange_capability_merge.py
git commit -m "test(file-exchange): end-to-end merge of register_file_exchange + register_file_exchange_upload"
```

---

### Task 17: Update `docs/specs/file-exchange.md` — Amendments 10 and 11

**Files:**
- Modify: `docs/specs/file-exchange.md`

- [ ] **Step 1: Read the v0.4.0 amendments header** at the bottom of `docs/specs/file-exchange.md` to understand the existing pattern (Amendments 1–9 are already there).

- [ ] **Step 2: Update the file header** at line 3 — change the version line:

```markdown
**Version:** 0.2.5 (with proposed v0.4.0 amendments — see end of document; the v0.4 wire bump bundles these into a single minor version once accepted)
```

(No actual change needed if it already says this; verify the doc still reads accurately given that we now have 11 amendments.)

- [ ] **Step 3: Append Amendment 10** at the end of the amendments section (after Amendment 9):

```markdown
### Amendment 10: HTTP method gains direction tagging

**Where:** §"Transfer Methods / `http`" + §"Discovery / Capability declaration".

**Status today:** v0.2.5 has `transfer_methods.http: { tool: "create_download_link" }` (or `{ tool: "fetch" }` for consumers). Direction is implicit — only download exists.

**Amendment:** the `http` method nests by direction:

```json
"http": {
  "download": { "tool": "create_download_link" },
  "upload":   { "tool": "create_upload_link", "max_bytes": 10485760, "max_ttl_seconds": 3600 }
}
```

A server may declare either, both, or (for the consumer-of-downloads case) just `download.tool: "fetch"`. The `exchange` method stays direction-agnostic — the URI scheme already works in either direction (an agent writes to its own namespace under `$MCP_EXCHANGE_DIR` and passes the URI to a server tool, mirroring today's download flow). This direction-agnosticism of `exchange` is documented as a non-amendment in the table at the end of this section.

**Migration:** servers advertising `version: "0.2"` keep the flat shape; `version: "0.4"` uses the nested shape. Implementations SHOULD support reading the flat shape from older peers for one minor version. Upstream tooling (`register_file_exchange[_upload]`) accepts a `legacy_capability_shape: bool = False` flag during the migration window.

**Rationale:** keeps a single `transfer_methods` block (no parallel `intake_methods`) while making both directions explicit. Forces a wire bump, but folds into the v0.4.0 bump cleanly.
```

- [ ] **Step 4: Append Amendment 11**:

```markdown
### Amendment 11: Inbound HTTP transfer (upload)

**Where:** new §"Server Requirements / Server accepting uploads"; addition to §"Transfer Methods / `http`".

**Status today:** v0.2.5 defines no inbound mechanism. `consumes` describes MIME types accepted via `file_ref` pull only.

**Amendment:** a server that supports direct upload:

- MUST register a tool named `create_upload_link` (or whatever name is advertised in `transfer_methods.http.upload.tool`).
- Tool MUST accept `target_id` (opaque to client/consumer), `ttl_seconds`, `max_bytes`, optional `extra` dict; MUST return `{ upload_url, expires_in_seconds, target_id }`.
- MUST expose `POST /<namespace>/uploads/{token}` with the documented status-code contract (404 for missing/consumed token; 410 for expired; 413 by `Content-Length` precheck and by per-chunk overrun; 415 for unaccepted `Content-Type`; 400 / 409 / 500 from receiver per the receiver-exception mapping).
- MUST consume tokens atomically before dispatching to the receiver (one-time guarantee).
- MAY clamp `ttl_seconds` to a server ceiling and SHOULD return the effective value (mirror of Amendment 7).

`consumes` keeps its existing meaning: MIME types the server can ingest, regardless of mechanism. The presence of `transfer_methods.http.upload` advertises that direct upload is one available intake mechanism for those types. An optional **per-method filter** `transfer_methods.http.upload.accepts: [mime types]` MAY tighten the subset for the upload path specifically — for the case where a server consumes broadly via `fetch` but only accepts a narrower subset via direct upload. Absent → route inherits the full `consumes` list. `*/*` in this filter explicitly disables MIME checking at the route layer.

`target_id` follows the same character rules as `origin_id` and `exchange://` segments (no `/`, `\`, `.`, `..`, control bytes, leading/trailing whitespace, `?`, `#`).

**Authorization:** the `/uploads/{token}` route is intentionally outside the MCP authorization middleware — possession of a fresh, unconsumed, cryptographically unguessable token IS the authorization, exactly as the existing `/artifacts/{token}` GET route works for downloads. The auth gate is the `create_upload_link` tool, which IS subject to standard MCP tool-tag authorization.

**Rationale:** completes the symmetric story for HTTP transfer. Reuses the existing one-time-token pattern, status-code conventions, and capability-declaration shape.
```

- [ ] **Step 5: Update the "Non-amendments" section** at the bottom of the spec to record one more entry:

```markdown
- The `exchange` method's direction-agnosticism: an agent writing to its own namespace under `$MCP_EXCHANGE_DIR` and passing the resulting `exchange://` URI to a server tool is the upload analogue of the existing download flow. The URI scheme requires no new code — segment validation, atomic write, and the read path all work in either direction. No amendment text needed; this is documented here so future authors do not propose a redundant "amendment" for it.
```

- [ ] **Step 6: Run a quick sanity check** that the spec doc still passes any markdown linting the project uses (no project-level markdown lint is wired in this repo as of this writing — the check is "does it open and render in your editor without obvious breakage").

```bash
test -f docs/specs/file-exchange.md && wc -l docs/specs/file-exchange.md
```

- [ ] **Step 7: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): add v0.4 amendments 10 and 11 (direction tagging + inbound HTTP)"
```

---

### Task 18: Update `__init__.py` exports + `CHANGELOG.md` + `README.md`

**Files:**
- Modify: `src/fastmcp_pvl_core/__init__.py` (verify all new names exported and in `__all__`)
- Modify: `CHANGELOG.md`
- Modify: `README.md`

- [ ] **Step 1: Verify `__init__.py` exports** — open and confirm the import block from Task 11 plus `__all__` entries:

```bash
grep -n "UploadRecord\|UploadStore\|UploadHandle\|register_file_exchange_upload\|get_upload_store\|set_upload_store" src/fastmcp_pvl_core/__init__.py
```

Expected: each of `UploadRecord`, `UploadStore`, `UploadHandle`, `register_file_exchange_upload`, `get_upload_store`, `set_upload_store` appears at least twice (once in import, once in `__all__`).

If any are missing, add them. The `__all__` list is alphabetised in the existing file; insert each new name in the correct slot.

- [ ] **Step 2: Add a new section to `CHANGELOG.md`** at the top (under the existing "Unreleased" or whatever the project convention is — check the file first):

```bash
head -20 CHANGELOG.md
```

Then add the section. Example wording:

```markdown
## [Unreleased]

### Added

- `register_file_exchange_upload` — symmetric inbound mirror of `register_file_exchange`. Mints one-time POST URLs via a registered `create_upload_link` tool; receiver callable handles domain-specific commit. Buffered (`receiver=`) or streaming (`stream_receiver=`) variants. Optional `pre_link_validator=` runs at link creation so invalid `target_id`s surface as in-band tool errors. Closes #64.
- `UploadRecord`, `UploadStore`, `UploadHandle`, `get_upload_store`, `set_upload_store` exported from the public API.

### Changed

- File Exchange capability `version` advertised by `register_file_exchange` bumps from `"0.2"` to `"0.4"` to reflect the v0.4.0 amendments draft (now including Amendments 10 and 11). A `legacy_capability_shape=False` flag opt-in keeps the v0.2 flat `http` block for one minor version of overlap.
- Internal: `ArtifactStore` and `TokenRecord` move from `_artifacts.py` to a new `_token_store.py` that hosts both directions. `_artifacts.py` becomes a deprecation shim re-exporting the public names. Slated for removal one minor version after release.
```

- [ ] **Step 3: Add a one-paragraph mention to `README.md`** in whatever section currently mentions `register_file_exchange`:

```bash
grep -n "register_file_exchange" README.md
```

Append a short snippet next to the existing reference, e.g.:

```markdown
For the inbound direction (local agent pushes a file *into* the server), use the symmetric helper:

```python
from fastmcp_pvl_core import register_file_exchange_upload

register_file_exchange_upload(
    mcp,
    namespace="vault",
    env_prefix="MARKDOWN_VAULT_MCP",
    receiver=_my_upload_receiver,
    pre_link_validator=_validate_target_path,
)
```

This mints one-time `POST` URLs via a `create_upload_link` tool and dispatches the bytes to your receiver. See File Exchange spec §"Inbound HTTP transfer" (v0.4 amendments) for the wire contract.
```

- [ ] **Step 4: Run the suite a final time**

```bash
uv run pytest -q
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/__init__.py CHANGELOG.md README.md
git commit -m "docs: add register_file_exchange_upload to changelog and README"
```

---

### Task 19: Coverage check

**Files:**
- (no changes unless coverage gaps are found)

- [ ] **Step 1: Run coverage**

```bash
uv run pytest --cov=fastmcp_pvl_core --cov-branch --cov-report=term-missing -q
```

Expected: 95%+ line / 90%+ branch on new code (`_token_store.py`, the upload bits of `_file_exchange_runtime.py`, the new section of `file_exchange.py`, the builder in `_file_exchange_protocol.py`).

- [ ] **Step 2: For any gap on new code,** add a focused test in the appropriate test file. Common gaps to expect:
  - The `consume_or_status` "missing" branch (already covered by Task 6; verify).
  - The legacy-shape upload-warning log line (test by configuring `legacy_capability_shape=True` AND `set_upload(...)` and asserting a warning is logged).
  - The `peek` method on `UploadStore` (covered by Task 4, verify).
  - The "no base_url, upload disabled" path (Task 13's coverage; verify).

- [ ] **Step 3: Re-run coverage** to confirm targets hit:

```bash
uv run pytest --cov=fastmcp_pvl_core --cov-branch -q
```

- [ ] **Step 4: Commit (only if any gap-filler tests were added)**

```bash
git add tests/
git commit -m "test(file-exchange): close coverage gaps in upload direction"
```

---

### Task 20: Final verification — local-review circus before opening the PR

**Files:**
- (no changes)

This is the pre-flight checklist from your project's `CLAUDE.md`. Both reviewer subagents run on the cumulative diff before the PR opens.

- [ ] **Step 1: Refresh from main**

```bash
git fetch origin main
git log --oneline origin/main..HEAD
```

Expected: commits from this plan only; no surprise drift.

- [ ] **Step 2: Run lint + format + type-check + full suite (CI parity)**

```bash
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy src/
uv run pytest -q
```

Expected: each green. (This mirrors the `feedback_local_checks_match_ci.md` memory.)

- [ ] **Step 3: Dispatch the primary reviewer subagent** via the Agent tool with `subagent_type: "pr-review-toolkit:code-reviewer"` on the cumulative diff (`git diff origin/main..HEAD`). Address findings to clean.

- [ ] **Step 4: Dispatch the second-opinion reviewer** via the Agent tool with `subagent_type: "superpowers:code-reviewer"` on the same diff. Address findings to clean.

- [ ] **Step 5: Open the PR as draft, with body referencing #64 and the spec doc.** Bot iteration is capped at one round per CLAUDE.md.

- [ ] **Step 6: After bot LGTM and CI green, mark the PR ready for human review.**

```bash
gh pr ready <PR-number>
```

- [ ] **Step 7: File the three downstream `fastmcp-server-template` issues** described in `docs/superpowers/specs/2026-05-09-file-exchange-upload-design.md` §"Downstream impact". They do not block this PR.

---

## Self-Review Summary

(Run by the plan author before handoff. Inline notes — no need to re-review after fixes.)

**1. Spec coverage** — every section of the design doc maps to one or more tasks:

| Spec section | Task(s) |
|---|---|
| Architecture & module layout | 1, 2 |
| Public API (UploadHandle, registrar) | 11, 12, 13 |
| HTTP route & runtime | 5, 6, 7, 8, 9, 10 |
| Token store refactor | 1, 2, 3, 4 |
| Spec extension (Amendments 10–11) | 14, 17 |
| Capability merge | 14, 15, 16 |
| Tests | 1, 3, 4, 5–10, 11–13, 14, 16, 19 |
| Downstream impact (template issues) | 20 step 7 |

**2. Placeholder scan** — no "TBD"/"TODO" in the steps. Every code block is complete and runnable.

**3. Type consistency** — names used across tasks:
- `UploadRecord`, `UploadStore`, `UploadHandle`, `register_file_exchange_upload`, `register_upload_route`, `pre_link_validator`, `_FileExchangeCapabilityBuilder`, `_BaseTokenStore` are spelled consistently.
- `TokenRecord` (existing) is preserved for backward compat; `UploadRecord` (new) is the upload-direction record. The plan does not rename `TokenRecord` to `ArtifactRecord` despite the spec doc using that latter name informally — this avoids a breaking export change. Any future spec wording can use `TokenRecord` consistently or rename later in its own PR.
- `consume_or_status` returns `(record, status)` in tests and in the implementation in Task 6 (Step 3).

**4. Ambiguity check** — locked in:
- `accepts=("*/*",)` is the route-level disable signal; the per-method capability filter is omitted from the dict when the registrar is configured with `("*/*",)` (Task 15 Step 2 implementation note in `register_file_exchange_upload`'s builder push).
- `pre_link_validator(target_id, extra)` raising `ValueError` surfaces as a tool error to the LLM, not an HTTP status.
- `legacy_capability_shape=True` AND `set_upload(...)` → warning log + download-only flat http; upload silently dropped from the wire (clients on v0.2 wouldn't understand it anyway).

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-09-file-exchange-upload.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
