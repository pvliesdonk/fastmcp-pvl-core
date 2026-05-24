# File-Exchange #145 — download data plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the `download` transport's data plane — `download_provider_mint` (token + `DownloadSource`), `register_file_exchange_routes` (the GET route that serves capability URLs), and `download_fetcher_consume` (retrieve through the #147 guard, verify, deposit) — all streaming, no in-memory buffering.

**Architecture:** New module `_file_exchange/_download.py`, free functions mirroring `_filesystem.py`. **Lazy serving:** the GET route calls the `ArtifactSource` hook on demand and streams; pvl-core holds no copy. **Fetcher** downloads to a transient temp file (hashing while writing, `Range`-resuming on a drop), verifies size+digest *before* handing the sink a real sync fd, then deletes the temp — bridging the async `guarded_stream` to the sync `ArtifactSink` and giving verify-before-use. Dependencies #144 (token store) and #147 (`guarded_stream`) are merged.

**Tech Stack:** Python 3.10+, `httpx` (the guard + ASGI test transport), Starlette (`Request`/`Response`/`StreamingResponse` for the custom route), `fastmcp` (`@mcp.custom_route`, `mcp.http_app()`), stdlib `tempfile`/`hashlib`/`asyncio`/`contextlib`, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`).

**Design reference:** `docs/superpowers/specs/2026-05-24-file-exchange-145-download-data-plane-design.md`.

**Public surface:** `download_provider_mint`, `download_fetcher_consume`, `register_file_exchange_routes` are re-exported (transport-qualified, beside `filesystem_*`). `DOWNLOAD_PREFIX` is a module constant (pvl-core route shape) — not exported, not configurable.

---

## File structure

- Create `src/fastmcp_pvl_core/_file_exchange/_download.py` — the module (constants, the three role helpers + the route handler).
- Modify `src/fastmcp_pvl_core/_file_exchange/__init__.py` — import + `__all__` the three names (alphabetical).
- Modify `src/fastmcp_pvl_core/file_exchange.py` — re-export the three names in its import block + `__all__` (alphabetical).
- Create `tests/_file_exchange/test_download.py` — unit tests (provider, fetcher, route).
- Create `tests/_file_exchange/test_download_e2e.py` — two-server pull flow.

---

### Task 1: module skeleton + `download_provider_mint`

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_download.py`
- Test: `tests/_file_exchange/test_download.py`

- [ ] **Step 1: Write the failing test**

Create `tests/_file_exchange/test_download.py`:

```python
import hashlib
import io

import httpx
import pytest

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _download
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata, DownloadSource


def _store():
    # In-memory KV-backed token store (memory backend is the default when no
    # kv_store_url is set).
    return build_capability_token_store(ServerConfig(file_exchange_token_ttl=3600.0))


async def test_provider_mint_builds_download_handle():
    store = _store()
    artifact = ArtifactMetadata(name="report.pdf", mimeType="application/pdf", size=11)
    handle = await _download.download_provider_mint(
        artifact,
        "doc-key-1",
        token_store=store,
        base_url="https://a.example",
        ttl=120.0,
        single_use=True,
    )
    assert handle.type == HANDLE_TYPE
    assert handle.version == SPEC_VERSION
    assert handle.artifact is artifact
    assert len(handle.sources) == 1
    src = handle.sources[0]
    assert isinstance(src, DownloadSource)
    assert src.transport == "download"
    assert src.url.startswith("https://a.example/fx/d/")
    assert src.singleUse is True
    # The token round-trips to the stored key.
    token = src.url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata == {"key": "doc-key-1"}
    assert rec.single_use is True


async def test_provider_mint_single_use_false_threads_through():
    store = _store()
    handle = await _download.download_provider_mint(
        ArtifactMetadata(name="x"),
        "k",
        token_store=store,
        base_url="https://a.example",
        ttl=60.0,
        single_use=False,
    )
    assert handle.sources[0].singleUse is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_download.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._download'`.

- [ ] **Step 3: Create the module with constants + `download_provider_mint`**

Create `src/fastmcp_pvl_core/_file_exchange/_download.py`:

```python
"""The ``download`` transport data plane (#145).

Three role helpers plus a serving route, free functions mirroring
``_filesystem.py``. The transport is **lazy**: the GET route calls the #142
``ArtifactSource`` hook on demand and streams the result — pvl-core holds no
copy of the artifact. The provider mints a capability URL backed by the #144
token store; the fetcher retrieves it through the #147 ``guarded_stream`` into a
transient temp file, verifies size+digest before handing the sink a real sync
fd, then deletes the temp. See
``docs/superpowers/specs/2026-05-24-file-exchange-145-download-data-plane-design.md``.

An ``ArtifactSource`` offered via ``download`` MUST yield stable bytes for the
token's lifetime: the route re-opens the hook on every GET and on each ``Range``
resume.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
from typing import TYPE_CHECKING

import httpx
from starlette.responses import Response, StreamingResponse

from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._outbound import guarded_stream
from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import DownloadSource, TransferHandle

if TYPE_CHECKING:
    from starlette.requests import Request

    from fastmcp import FastMCP

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

logger = logging.getLogger(__name__)

# pvl-core's download route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision.
DOWNLOAD_PREFIX = "/fx/d"

_CHUNK = 1024 * 1024
# Declared-digest label -> hashlib name; an unsupported label fails verification
# (cannot verify -> digest-mismatch), never silently skips. Mirrors _filesystem.
_HASHLIB_BY_LABEL = {"sha-256": "sha256", "sha-384": "sha384", "sha-512": "sha512"}
# Max mid-stream reconnects before giving up on a dropped download.
_MAX_RECONNECTS = 5


async def download_provider_mint(
    artifact: ArtifactMetadata,
    key: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    single_use: bool = True,
) -> TransferHandle:
    """Provider role (pull): mint a download token and emit a TransferHandle.

    ``artifact`` is the caller-supplied metadata for what is being offered
    (lazy serving means the source hook is untouched at mint; the route opens it
    at GET). ``key`` is the server's opaque artifact identifier, stored opaquely
    in the token for the route to read back. ``base_url`` is the server's public
    https origin; ``ttl`` is clamped by the token store's ceiling.
    """
    minted = await token_store.mint({"key": key}, ttl=ttl, single_use=single_use)
    url = capability_url(base_url, DOWNLOAD_PREFIX, minted.token)
    return TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=artifact,
        sources=[
            DownloadSource(
                transport="download",
                url=url,
                expiresAt=minted.expires_at,
                singleUse=single_use,
            )
        ],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_download.py -v`
Expected: PASS (both provider tests).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_download.py tests/_file_exchange/test_download.py
git commit -m "feat(file-exchange): download_provider_mint + module skeleton (#145)"
```

---

### Task 2: `download_fetcher_consume` — temp download + verify-before-use

Single-connection happy path, verification, size bound, temp cleanup. `Range` resume is Task 3.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_download.py`
- Test: `tests/_file_exchange/test_download.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_download.py`:

```python
def _cfg(*, max_size=None):
    return ServerConfig(file_exchange_max_artifact_size=max_size)


class _CapturingSink:
    """ArtifactSink that records the bytes it is given (proves verify-before-use:
    store_artifact is only called on a clean transfer)."""

    def __init__(self):
        self.deposited: bytes | None = None
        self.calls = 0

    async def store_artifact(self, artifact_id, metadata, stream):
        self.calls += 1
        self.deposited = stream.read()


def _handle(body: bytes, *, size=None, digest=None):
    from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata, TransferHandle

    return TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(
            name="a", size=len(body) if size is None else size, digest=digest
        ),
        sources=[
            DownloadSource(
                transport="download",
                url="https://prov.example/fx/d/tok",
                expiresAt="2099-01-01T00:00:00Z",
                singleUse=True,
            )
        ],
    )


def _install_guard(monkeypatch, responder):
    """Patch _download.guarded_stream with a MockTransport-backed guarded_stream
    that does NOT resolve/pin (we are testing the fetcher, not the guard)."""
    import contextlib as _ctx

    @_ctx.asynccontextmanager
    async def fake_guarded_stream(method, url, *, config, transport, headers=None, content=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        try:
            req = client.build_request(method, url, headers=headers or {})
            resp = await client.send(req, stream=True)
            try:
                yield resp
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_download, "guarded_stream", fake_guarded_stream)


async def test_fetcher_happy_path_verifies_and_deposits(monkeypatch):
    body = b"hello-download-bytes"
    digest = "sha-256:" + hashlib.sha256(body).hexdigest()

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    await _download.download_fetcher_consume(
        _handle(body, digest=digest), _handle(body).sources[0], sink, config=_cfg()
    )
    assert sink.deposited == body
    assert sink.calls == 1


async def test_fetcher_digest_mismatch_does_not_call_sink(monkeypatch):
    body = b"actual-bytes"
    wrong = "sha-256:" + hashlib.sha256(b"different").hexdigest()

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body, digest=wrong), _handle(body).sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.DIGEST_MISMATCH
    assert sink.calls == 0  # verify-before-use


async def test_fetcher_size_mismatch(monkeypatch):
    body = b"twelve_bytes"

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body, size=999), _handle(body).sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.SIZE_MISMATCH
    assert sink.calls == 0


async def test_fetcher_too_large(monkeypatch):
    body = b"x" * 5000

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body), _handle(body).sources[0], sink, config=_cfg(max_size=1000)
        )
    assert ei.value.code == TransferErrorCode.TOO_LARGE


async def test_fetcher_guard_refusal_propagates(monkeypatch):
    import contextlib as _ctx

    @_ctx.asynccontextmanager
    async def refusing(method, url, *, config, transport, headers=None, content=None):
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE, transport="download", detail="refused"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(_download, "guarded_stream", refusing)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(b"x"), _handle(b"x").sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE
    assert sink.calls == 0


async def test_fetcher_cleans_up_temp_on_error(monkeypatch, tmp_path):
    # The temp file must be removed on a streaming-phase error path (regression:
    # the unlink used to sit in a second sequential try that such errors skipped).
    real_mkstemp = _download.tempfile.mkstemp
    created: list[str] = []

    def spy_mkstemp(*args, **kwargs):
        kwargs.setdefault("dir", str(tmp_path))
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(_download.tempfile, "mkstemp", spy_mkstemp)

    body = b"x" * 5000

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body), _handle(body).sources[0], sink, config=_cfg(max_size=1000)
        )
    assert ei.value.code == TransferErrorCode.TOO_LARGE
    assert created  # a temp file was created
    assert all(not os.path.exists(p) for p in created)  # ...and removed on error
```

(This test needs `import os` at the top of `test_download.py` — add it if Task 1
didn't.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_download.py -k fetcher -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'download_fetcher_consume'`.

- [ ] **Step 3: Implement `download_fetcher_consume`**

Append to `src/fastmcp_pvl_core/_file_exchange/_download.py`:

```python
def _digest_verifier(declared: str | None):
    """Return (hasher | None, expected_hex | None, unverifiable).

    ``unverifiable`` is True when a digest is declared with an unsupported label
    — verification must then fail (cannot verify), never silently skip (§15).
    """
    if declared is None:
        return None, None, False
    label, _, expected_hex = declared.partition(":")
    name = _HASHLIB_BY_LABEL.get(label.lower())
    if name is None:
        return None, expected_hex, True
    return hashlib.new(name), expected_hex.lower(), False


async def download_fetcher_consume(
    handle: TransferHandle,
    descriptor: DownloadSource,
    sink: ArtifactSink,
    *,
    config: ServerConfig,
) -> None:
    """Fetcher role (pull): download ``descriptor`` and deposit into ``sink``.

    Selection (``select_source``) is the caller's step. Streams the body through
    the #147 guard into a transient temp file (hashing as it writes), verifies
    ``handle.artifact`` size+digest **before** opening the temp for the sink
    (verify-before-use), then deletes the temp. Failures map to §13 codes; a
    dropped connection is recovered with ``Range``.

    The download loop is async (it awaits ``guarded_stream``); only the blocking
    temp-file writes are off-loaded with ``asyncio.to_thread``.
    """
    expected_size = handle.artifact.size
    max_size = config.file_exchange_max_artifact_size
    hasher, expected_hex, unverifiable = _digest_verifier(handle.artifact.digest)

    fd, tmp_path = tempfile.mkstemp(prefix="fx-download-")
    tmp = os.fdopen(fd, "wb")
    try:
        try:
            received = 0
            attempts = 0
            while True:
                req_headers = {} if received == 0 else {"Range": f"bytes={received}-"}
                try:
                    async with guarded_stream(
                        "GET",
                        descriptor.url,
                        config=config,
                        transport="download",
                        headers=req_headers,
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            await asyncio.to_thread(tmp.write, chunk)
                            if hasher is not None:
                                hasher.update(chunk)
                            received += len(chunk)
                            if max_size is not None and received > max_size:
                                raise FileExchangeTransferError(
                                    TransferErrorCode.TOO_LARGE,
                                    transport="download",
                                    detail="artifact exceeds the configured max size",
                                )
                    break  # body read to completion without a connection error
                except FileExchangeTransferError:
                    raise  # guard refusal / too-large — not a resumable drop
                except (httpx.HTTPError, OSError) as exc:
                    attempts += 1
                    if attempts > _MAX_RECONNECTS:
                        raise FileExchangeTransferError(
                            TransferErrorCode.TRANSFER_FAILED,
                            transport="download",
                            detail="download interrupted and could not be resumed",
                        ) from exc
                    # loop: resume from `received` via a Range request
            await asyncio.to_thread(tmp.flush)
        finally:
            await asyncio.to_thread(tmp.close)

        # verify-before-use (computed during the single write pass)
        if expected_size is not None and received != expected_size:
            raise FileExchangeTransferError(
                TransferErrorCode.SIZE_MISMATCH,
                transport="download",
                detail="transferred byte count did not match declared size",
            )
        if handle.artifact.digest is not None and (
            unverifiable or hasher is None or hasher.hexdigest() != expected_hex
        ):
            raise FileExchangeTransferError(
                TransferErrorCode.DIGEST_MISMATCH,
                transport="download",
                detail="transferred bytes did not match declared digest",
            )
        # ingest: hand the sink a real sync fd (works whether it reads on the
        # loop or offloads — the async->sync bridge the temp file provides)
        f = await asyncio.to_thread(open, tmp_path, "rb")
        try:
            await sink.store_artifact(handle.artifact.id, handle.artifact, f)
        except FileExchangeTransferError:
            raise
        except Exception as exc:
            raise FileExchangeTransferError(
                TransferErrorCode.TRANSFER_FAILED,
                transport="download",
                detail="artifact transfer failed",
            ) from exc
        finally:
            await asyncio.to_thread(f.close)
    finally:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(os.unlink, tmp_path)
```

The outer ``try/finally`` around both phases (not two sequential ``try`` blocks)
is what guarantees the temp file is unlinked even when a streaming-phase error
(too-large, guard refusal, max-reconnect failure) propagates.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_download.py -k "fetcher or provider" -v`
Expected: PASS (provider tests + the five fetcher tests).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_download.py tests/_file_exchange/test_download.py
git commit -m "feat(file-exchange): download_fetcher_consume — temp download + verify-before-use (#145)"
```

---

### Task 3: `download_fetcher_consume` — `Range` resume on a dropped connection

**Files:**
- Test: `tests/_file_exchange/test_download.py` (the resume logic already exists from Task 2; this task proves it)

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_download.py`:

```python
def _install_guard_seq(monkeypatch, responders):
    """guarded_stream that uses a fresh MockTransport responder per call (so a
    reconnect with a Range header hits the next responder in the list)."""
    import contextlib as _ctx

    calls = {"seen_headers": []}

    @_ctx.asynccontextmanager
    async def fake(method, url, *, config, transport, headers=None, content=None):
        calls["seen_headers"].append(dict(headers or {}))
        responder = responders[min(len(calls["seen_headers"]) - 1, len(responders) - 1)]
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        try:
            req = client.build_request(method, url, headers=headers or {})
            resp = await client.send(req, stream=True)
            try:
                yield resp
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_download, "guarded_stream", fake)
    return calls


async def test_fetcher_resumes_with_range_after_drop(monkeypatch):
    body = b"0123456789abcdef" * 8  # 128 bytes
    digest = "sha-256:" + hashlib.sha256(body).hexdigest()
    split = 50

    class _DropStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body[:split]
            raise httpx.ReadError("connection dropped mid-stream")

        async def aclose(self):
            return

    def first(request):
        return httpx.Response(200, stream=_DropStream())

    def rest(request):
        # honor the resume Range: bytes=<start>-
        rng = request.headers["range"]
        start = int(rng[len("bytes=") :].split("-")[0])
        return httpx.Response(206, content=body[start:])

    calls = _install_guard_seq(monkeypatch, [first, rest])
    sink = _CapturingSink()
    await _download.download_fetcher_consume(
        _handle(body, digest=digest), _handle(body).sources[0], sink, config=_cfg()
    )
    assert sink.deposited == body
    assert calls["seen_headers"][0] == {}  # first attempt: no Range
    assert calls["seen_headers"][1] == {"Range": f"bytes={split}-"}  # resume


async def test_fetcher_gives_up_after_max_reconnects(monkeypatch):
    class _AlwaysDrop(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a"
            raise httpx.ReadError("drop")

        async def aclose(self):
            return

    def responder(request):
        return httpx.Response(200, stream=_AlwaysDrop())

    calls = _install_guard_seq(monkeypatch, [responder])
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(b"a" * 100, size=100), _handle(b"x").sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
    # initial attempt + _MAX_RECONNECTS resume attempts
    assert len(calls["seen_headers"]) == _download._MAX_RECONNECTS + 1
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `uv run pytest tests/_file_exchange/test_download.py -k resume -v` and `... -k max_reconnects -v`
Expected: PASS — the resume loop implemented in Task 2 already handles this. (If `test_fetcher_resumes_with_range_after_drop` fails because the drop exception type isn't caught, confirm the loop's `except (httpx.HTTPError, OSError)` covers `httpx.ReadError` — it does, `ReadError` is an `httpx.HTTPError`.)

- [ ] **Step 3: No new implementation** (Task 2's loop covers it). If a test exposes a gap, fix the loop in `_download.py` minimally and re-run.

- [ ] **Step 4: Run the whole fetcher suite**

Run: `uv run pytest tests/_file_exchange/test_download.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/_file_exchange/test_download.py
git commit -m "test(file-exchange): cover download fetcher Range-resume + reconnect cap (#145)"
```

---

### Task 4: `register_file_exchange_routes` — the GET route

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_download.py`
- Test: `tests/_file_exchange/test_download.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_download.py`:

```python
from fastmcp import FastMCP


class _BytesSource:
    """ArtifactSource serving fixed bytes for a single key."""

    def __init__(self, key, body, *, mime="application/octet-stream"):
        self._key, self._body, self._mime = key, body, mime
        self.opens = 0

    async def open_artifact(self, key):
        from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

        assert key == self._key
        self.opens += 1
        return io.BytesIO(self._body), ArtifactMetadata(
            name="a", mimeType=self._mime, size=len(self._body)
        )


def _route_client(store, source):
    mcp = FastMCP("test")
    _download.register_file_exchange_routes(mcp, token_store=store, source=source)
    app = mcp.http_app()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://route.test"
    )


async def _mint_token(store, key):
    minted = await store.mint({"key": key}, ttl=300.0, single_use=True)
    return minted.token


async def test_route_unknown_token_404():
    store = _store()
    async with _route_client(store, _BytesSource("k", b"x")) as client:
        r = await client.get("/fx/d/nope")
    assert r.status_code == 404


async def test_route_serves_full_body_with_headers():
    store = _store()
    body = b"the-quick-brown-fox"
    source = _BytesSource("k1", body, mime="text/plain")
    token = await _mint_token(store, "k1")
    async with _route_client(store, source) as client:
        r = await client.get(f"/fx/d/{token}")
    assert r.status_code == 200
    assert r.content == body
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-length"] == str(len(body))
    assert r.headers["accept-ranges"] == "bytes"


async def test_route_range_returns_206_partial():
    store = _store()
    body = b"0123456789"
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=3-6"})
    assert r.status_code == 206
    assert r.content == b"3456"
    assert r.headers["content-range"] == "bytes 3-6/10"
    assert r.headers["content-length"] == "4"


async def test_route_unsatisfiable_range_416():
    store = _store()
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", b"0123456789")) as client:
        r = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=99-"})
    assert r.status_code == 416


async def test_route_full_retrieval_consumes_single_use_token():
    store = _store()
    body = b"consume-me"
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r1 = await client.get(f"/fx/d/{token}")
        assert r1.status_code == 200 and r1.content == body
        r2 = await client.get(f"/fx/d/{token}")
    assert r2.status_code == 404  # single-use token consumed after full retrieval


async def test_route_middle_range_does_not_consume():
    store = _store()
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", b"0123456789")) as client:
        r1 = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=2-5"})
        assert r1.status_code == 206
        r2 = await client.get(f"/fx/d/{token}")  # still valid
    assert r2.status_code == 200


async def test_route_ignores_ambient_credentials():
    store = _store()
    body = b"data"
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r = await client.get(
            f"/fx/d/{token}",
            headers={"Authorization": "Bearer x", "Cookie": "s=1"},
        )
    assert r.status_code == 200 and r.content == body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_download.py -k route -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'register_file_exchange_routes'`.

(If a route test errors on the ASGI app lifespan, wrap the client body in
`async with LifespanManager(app):` via `asgi-lifespan`, or run the app's
`router.lifespan_context`. The custom route does not depend on MCP session
state, so `httpx.ASGITransport` should dispatch to it directly without lifespan;
adopt the lifespan wrapper only if the app rejects requests pre-startup.)

- [ ] **Step 3: Implement the route + range parsing**

Append to `src/fastmcp_pvl_core/_file_exchange/_download.py`:

```python
class _Unsatisfiable(Exception):
    """Internal: the Range header cannot be satisfied -> HTTP 416."""


def _parse_range(header: str | None, size: int | None) -> tuple[int, int | None]:
    """Parse a single-range ``Range`` header into ``(start, end_inclusive|None)``.

    ``end`` is ``None`` for "no Range" and for an open-ended ``bytes=start-``
    (the fetcher's resume form). Raises :class:`_Unsatisfiable` (-> 416) on a
    malformed or unsatisfiable range. Only a single byte range is supported.
    """
    if header is None:
        return 0, None
    if not header.startswith("bytes="):
        raise _Unsatisfiable
    spec = header[len("bytes=") :].split(",")[0].strip()
    start_s, sep, end_s = spec.partition("-")
    if sep != "-":
        raise _Unsatisfiable
    try:
        if start_s == "":  # suffix range: bytes=-N (last N bytes)
            if size is None or end_s == "":
                raise _Unsatisfiable
            suffix = int(end_s)
            if suffix <= 0:
                raise _Unsatisfiable
            return max(0, size - suffix), size - 1
        start = int(start_s)
        if start < 0 or (size is not None and start >= size):
            raise _Unsatisfiable
        if end_s == "":  # open-ended: bytes=start-
            return start, None
        end = int(end_s)
    except ValueError as exc:
        raise _Unsatisfiable from exc
    if size is not None:
        end = min(end, size - 1)
    if end < start:
        raise _Unsatisfiable
    return start, end


def register_file_exchange_routes(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    source: ArtifactSource,
) -> None:
    """Mount the ``download`` GET route on ``mcp`` (serves §12 capability URLs).

    ``GET <DOWNLOAD_PREFIX>/{token}`` looks the token up in ``token_store``,
    serves the artifact via ``source.open_artifact`` (streamed, ``Range``
    supported), and consumes a single-use token once it has been fully retrieved
    (§10.2). Ambient credentials are ignored — the in-URL token is the only
    authorization. ``token_store`` and ``source`` are threaded by #148.
    """

    @mcp.custom_route(f"{DOWNLOAD_PREFIX}/{{token}}", methods=["GET"])
    async def _serve_download(request: Request) -> Response:
        token = request.path_params["token"]
        rec = await token_store.lookup(token)
        if rec is None:
            return Response(status_code=404)
        stream, meta = await source.open_artifact(rec.metadata["key"])
        size = meta.size
        range_header = request.headers.get("range")
        try:
            start, end = _parse_range(range_header, size)
        except _Unsatisfiable:
            await asyncio.to_thread(stream.close)
            extra = {"Content-Range": f"bytes */{size}"} if size is not None else {}
            return Response(status_code=416, headers=extra)

        partial = range_header is not None
        headers = {"Accept-Ranges": "bytes"}
        if end is not None:
            headers["Content-Length"] = str(end - start + 1)
            total = str(size) if size is not None else "*"
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        elif partial and size is not None:  # open-ended bytes=start-
            headers["Content-Length"] = str(size - start)
            headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
        elif not partial and size is not None:  # full GET
            headers["Content-Length"] = str(size)

        # Consume only when the whole remaining artifact is delivered: no Range
        # or an open-ended bytes=start- streamed through source EOF. A closed
        # range never consumes (safe; the descriptor stays valid until expiry).
        consume_on_eof = end is None

        async def _body():
            try:
                to_skip = start
                while to_skip > 0:
                    chunk = await asyncio.to_thread(stream.read, min(to_skip, _CHUNK))
                    if not chunk:
                        break
                    to_skip -= len(chunk)
                remaining = None if end is None else (end - start + 1)
                hit_eof = False
                while remaining is None or remaining > 0:
                    n = _CHUNK if remaining is None else min(_CHUNK, remaining)
                    chunk = await asyncio.to_thread(stream.read, n)
                    if not chunk:
                        hit_eof = True
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                    yield chunk
                if consume_on_eof and hit_eof:
                    await token_store.consume(token)
            finally:
                await asyncio.to_thread(stream.close)

        return StreamingResponse(
            _body(),
            status_code=206 if partial else 200,
            headers=headers,
            media_type=meta.mimeType,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_download.py -v`
Expected: PASS (all download unit tests).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_download.py tests/_file_exchange/test_download.py
git commit -m "feat(file-exchange): register_file_exchange_routes download GET route (#145)"
```

---

### Task 5: public surface (re-exports)

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Test: `tests/test_file_exchange_namespace.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_file_exchange_namespace.py`:

```python
def test_download_data_plane_names_exported():
    from fastmcp_pvl_core import file_exchange as fx

    for name in (
        "download_provider_mint",
        "download_fetcher_consume",
        "register_file_exchange_routes",
    ):
        assert hasattr(fx, name), name
        assert name in fx.__all__
    # DOWNLOAD_PREFIX is internal — not part of the public surface
    assert not hasattr(fx, "DOWNLOAD_PREFIX")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_file_exchange_namespace.py::test_download_data_plane_names_exported -v`
Expected: FAIL — names not exported.

- [ ] **Step 3: Add the re-exports**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, add to the import block (with the other `_download`/transport imports; create the `from ._download import ...` line) and to `__all__` (alphabetical):

```python
from fastmcp_pvl_core._file_exchange._download import (
    download_fetcher_consume,
    download_provider_mint,
    register_file_exchange_routes,
)
```

Add to that file's `__all__`: `"download_fetcher_consume"`, `"download_provider_mint"`, `"register_file_exchange_routes"` (keep the list alphabetical).

In `src/fastmcp_pvl_core/file_exchange.py`, add the same three names to its `from fastmcp_pvl_core._file_exchange import (...)` block and to its `__all__` (alphabetical), mirroring how `filesystem_*` are re-exported.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_file_exchange_namespace.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/__init__.py src/fastmcp_pvl_core/file_exchange.py tests/test_file_exchange_namespace.py
git commit -m "feat(file-exchange): re-export download data-plane surface (#145)"
```

---

### Task 6: end-to-end two-server pull

**Files:**
- Create: `tests/_file_exchange/test_download_e2e.py`

- [ ] **Step 1: Write the failing test**

Create `tests/_file_exchange/test_download_e2e.py`:

```python
import contextlib
import hashlib
import io

import httpx
import pytest

from fastmcp import FastMCP
from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _download, _outbound
from fastmcp_pvl_core._file_exchange._selection import select_source
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _BytesSource:
    def __init__(self, key, body):
        self._key, self._body = key, body

    async def open_artifact(self, key):
        assert key == self._key
        return io.BytesIO(self._body), ArtifactMetadata(
            name="a", mimeType="application/octet-stream", size=len(self._body)
        )


class _CapturingSink:
    def __init__(self):
        self.deposited = None

    async def store_artifact(self, artifact_id, metadata, stream):
        self.deposited = stream.read()


async def test_two_server_pull_download(monkeypatch):
    body = b"end-to-end-download-payload" * 64
    digest = "sha-256:" + hashlib.sha256(body).hexdigest()

    # Server A: provider + serving route, mounted on an ASGI app.
    store = build_capability_token_store(ServerConfig(file_exchange_token_ttl=3600.0))
    mcp = FastMCP("provider")
    _download.register_file_exchange_routes(
        mcp, token_store=store, source=_BytesSource("doc", body)
    )
    app_a = mcp.http_app()

    # Route B's guard at the provider's ASGI app (no real network/SSRF check —
    # we are exercising the pull flow, not the guard).
    @contextlib.asynccontextmanager
    async def guard_to_app_a(method, url, *, config, transport, headers=None, content=None):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="http://prov.test"
        )
        try:
            path = url.split("prov.test", 1)[1] if "prov.test" in url else url
            req = client.build_request(method, path, headers=headers or {})
            resp = await client.send(req, stream=True)
            try:
                yield resp
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_download, "guarded_stream", guard_to_app_a)

    # A mints a handle (artifact carries the digest A computed out of band).
    handle = await _download.download_provider_mint(
        ArtifactMetadata(name="a", mimeType="application/octet-stream", size=len(body), digest=digest),
        "doc",
        token_store=store,
        base_url="http://prov.test",
        ttl=300.0,
        single_use=True,
    )

    # B selects the download source and fetches it into its sink.
    descriptor = select_source(handle)
    assert descriptor is not None and descriptor.transport == "download"
    sink = _CapturingSink()
    await _download.download_fetcher_consume(
        handle, descriptor, sink, config=ServerConfig()
    )
    assert sink.deposited == body
```

- [ ] **Step 2: Run test to verify it fails / passes**

Run: `uv run pytest tests/_file_exchange/test_download_e2e.py -v`
Expected: PASS (the full pull flow). If it fails on the ASGI lifespan, wrap `app_a` usage with a lifespan manager as noted in Task 4.

- [ ] **Step 3: No new implementation** — this exercises Tasks 1/2/4 together. Fix any integration gap minimally in `_download.py`.

- [ ] **Step 4: Run the full file-exchange suite**

Run: `uv run pytest tests/_file_exchange/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/_file_exchange/test_download_e2e.py
git commit -m "test(file-exchange): two-server download pull e2e (#145)"
```

---

### Task 7: full local quality gates + draft PR

**Files:** none (verification + PR).

- [ ] **Step 1: Run the full repo check suite on both min and max Python**

```bash
uv sync --all-extras
uv run --python 3.10 pytest
uv run --python 3.13 pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: all green on 3.10 and 3.13. Fix any failure and re-run before proceeding.

- [ ] **Step 2: Confirm the route prefix / public surface**

Run: `uv run python -c "import fastmcp_pvl_core.file_exchange as fx; assert hasattr(fx,'register_file_exchange_routes') and not hasattr(fx,'DOWNLOAD_PREFIX'); print('surface OK')"`
Expected: prints `surface OK`.

- [ ] **Step 3: Pre-push review (mandatory) + open the draft PR**

Per `CLAUDE.md`: invoke the `preflight-circus` skill on the cumulative `BASE..HEAD` diff and iterate until it returns `clean` (nothing ≥80). Only then push the branch and open the PR **as draft** with `Closes #145` in the body. Bot iteration after open is capped at one round; re-run `preflight-circus` before any fix-push. Flip to ready only when local review was clean, bot bodies say LGTM, and CI is fully green. Do not merge autonomously.

---

## Self-Review

**1. Spec coverage** (design doc → task):
- Lazy serving (route calls hook on GET) → Task 4 (`_serve_download` opens the hook per request; `_BytesSource.opens` reusable to confirm re-open).
- `download_provider_mint` (token + DownloadSource, artifact-not-source, digest from caller metadata) → Task 1.
- `register_file_exchange_routes` (GET `<PREFIX>/{token}`, 404, Range/206, 416, Content-* when known, consume-on-completion-to-last-byte, ambient creds ignored, PREFIX constant) → Task 4.
- `download_fetcher_consume` (temp download, hashing-while-writing, verify-before-use, Range resume, too-large, §13 mapping, temp cleanup) → Tasks 2 + 3.
- Hook-stability requirement → documented in the module docstring (Task 1 / Task 3).
- #145↔#148 boundary (deps as params) → all helpers take token_store/source/sink/config params.
- Public surface (transport-qualified re-exports; DOWNLOAD_PREFIX internal) → Task 5.
- Two-server e2e → Task 6.

No gaps found.

**2. Placeholder scan:** No `TBD`/`add error handling`/`similar to Task N`. Every code step has complete code; every run step has an expected result. (Tasks 3 and 6 have no Step 3 implementation block because they prove behavior implemented in earlier tasks — the step body says so explicitly and points at the owning task.)

**3. Type consistency:** `download_provider_mint`/`download_fetcher_consume`/`register_file_exchange_routes` signatures match across the module, tests, e2e, and the design doc. `DOWNLOAD_PREFIX`, `_CHUNK`, `_HASHLIB_BY_LABEL`, `_MAX_RECONNECTS`, `_digest_verifier`, `_parse_range`, `_Unsatisfiable` are each defined once and referenced consistently. The token metadata shape `{"key": key}` is written by `download_provider_mint`/`_mint_token` and read by `_serve_download` (`rec.metadata["key"]`). `DownloadSource(transport=..., url=..., expiresAt=..., singleUse=...)`, `TransferHandle(type=HANDLE_TYPE, version=SPEC_VERSION, artifact=..., sources=[...])`, and `CapabilityTokenStore.mint/lookup/consume` match the merged #144/#147 APIs.
