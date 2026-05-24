# File-Exchange #146 — Upload Data Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the `upload` transport's push data plane — a receiver that mints a capability URL + the `PUT`/`POST` route that deposits the pushed bytes through the `ArtifactSink`, and a sender that pushes an artifact through the SSRF guard with an RFC 9530 `Content-Digest`.

**Architecture:** Mirror of the merged #145 download plane. First refactor merged #145 code: extract shared digest/chunk primitives into `_staging.py`, and move the cross-transport route registrar `register_file_exchange_routes` into a new `_routes.py` (extracting `register_download_route` from `_download.py`). Then add `_upload.py` with `upload_receiver_mint`, the upload route, `upload_sender_consume`, and the RFC 9530 (`Content-Digest`) / RFC 7231 (media-range) helpers. The route streams the request body to a transient temp file (hashing + size-bounded), verifies digest/constraints **before** the sink sees the bytes, then consumes the single-use token only on a successful store.

**Tech Stack:** Python 3.10–3.13, FastMCP (`@mcp.custom_route`), Starlette `Request`/`Response`, httpx (via `guarded_stream`), Pydantic v2 wire models, `pytest` + `pytest-asyncio`, `httpx.ASGITransport` for route/e2e tests.

**Design doc:** `docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md`

---

## File Structure

- **Create `src/fastmcp_pvl_core/_file_exchange/_staging.py`** — shared primitives moved out of `_download.py`: `_CHUNK`, `_HASHLIB_BY_LABEL`, `_digest_verifier`, `_write_chunk`. Imported by both `_download.py` and `_upload.py` (independent leaf modules; no circular import).
- **Modify `src/fastmcp_pvl_core/_file_exchange/_download.py`** — import the four primitives from `_staging`; remove their local definitions and the now-unused `import hashlib`. Rename `register_file_exchange_routes` → `register_download_route` (signature `register_download_route(mcp, *, token_store, source)`; body unchanged).
- **Create `src/fastmcp_pvl_core/_file_exchange/_routes.py`** — `register_file_exchange_routes(mcp, *, token_store, source=None, sink=None, config=None)`; mounts the download route iff `source`, the upload route iff `sink` (which requires `config`).
- **Create `src/fastmcp_pvl_core/_file_exchange/_upload.py`** — `UPLOAD_PREFIX`, `upload_receiver_mint`, `register_upload_route`, `upload_sender_consume`, and the helpers `_format_content_digest`, `_parse_content_digest`, `_media_type_accepted`.
- **Modify `src/fastmcp_pvl_core/_file_exchange/__init__.py`** and **`src/fastmcp_pvl_core/file_exchange.py`** — re-export `register_file_exchange_routes` from `._routes` (was `._download`); add `upload_receiver_mint`, `upload_sender_consume`.
- **Create `tests/_file_exchange/test_upload.py`** — unit tests (receiver mint, helpers, route, sender).
- **Create `tests/_file_exchange/test_upload_e2e.py`** — two-server push end-to-end.
- **Modify `tests/test_file_exchange_namespace.py`** — assert the upload names are re-exported.

---

### Task 1: Extract `_staging.py` (refactor merged #145)

This is a pure move refactor — no behaviour change. The existing download test suite is the regression gate.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_staging.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/_download.py`
- Test (regression): `tests/_file_exchange/test_download.py`

- [ ] **Step 1: Create `_staging.py` with the moved primitives**

```python
"""Shared temp-file staging + digest primitives for the HTTP transports.

The download fetcher (#145) and the upload route/sender (#146) both buffer a
byte stream to a transient temp file with hashing and a size bound, and verify a
declared digest. These primitives factor out the common parts. Each transport
keeps its own read loop (download resumes via ``Range``; upload reads
``request.stream()`` or a hook stream) and applies the
``OSError -> transfer-failed`` mapping / cleanup-suppression contract around
them.
"""

from __future__ import annotations

import hashlib
from typing import IO

# Streaming chunk size for temp-file staging (1 MiB).
_CHUNK = 1024 * 1024

# Declared-digest label -> hashlib name; an unsupported label fails verification
# (cannot verify -> digest-mismatch), never silently skips (§15).
_HASHLIB_BY_LABEL = {"sha-256": "sha256", "sha-384": "sha384", "sha-512": "sha512"}


def _digest_verifier(
    declared: str | None,
) -> tuple[hashlib._Hash | None, str | None, bool]:
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


def _write_chunk(tmp: IO[bytes], hasher: hashlib._Hash | None, chunk: bytes) -> None:
    """Write a body chunk to the temp file and fold it into the running hash.

    Both ops run off the event loop in a single ``asyncio.to_thread`` dispatch.
    """
    tmp.write(chunk)
    if hasher is not None:
        hasher.update(chunk)
```

- [ ] **Step 2: Point `_download.py` at `_staging`**

In `src/fastmcp_pvl_core/_file_exchange/_download.py`:

1. Delete the local definitions of `_CHUNK` (line ~54), `_HASHLIB_BY_LABEL` (line ~57), `_digest_verifier` (lines ~96–110), and `_write_chunk` (lines ~113–120). Leave `DOWNLOAD_PREFIX` and `_MAX_RECONNECTS` in place.
2. Remove `import hashlib` (now unused in `_download.py`).
3. Add this import alongside the other `_file_exchange` imports:

```python
from fastmcp_pvl_core._file_exchange._staging import (
    _CHUNK,
    _HASHLIB_BY_LABEL,
    _digest_verifier,
    _write_chunk,
)
```

`_HASHLIB_BY_LABEL` is still referenced by `_download.py`'s fetcher status-check path, so keep it in the import even though `_digest_verifier` also uses it.

- [ ] **Step 3: Run the download suite + gates to verify no behaviour change**

Run: `uv run pytest tests/_file_exchange/test_download.py tests/_file_exchange/test_download_e2e.py -q`
Expected: PASS (same counts as before the refactor).

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: clean (in particular, no "imported but unused" for `hashlib`).

- [ ] **Step 4: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_staging.py src/fastmcp_pvl_core/_file_exchange/_download.py
git commit -m "refactor(file-exchange): extract shared staging primitives (#146)"
```

---

### Task 2: Move `register_file_exchange_routes` to `_routes.py`

Extract the download route registrar into `_download.py` as `register_download_route`, and create `_routes.py` to own the public cross-transport `register_file_exchange_routes` (download-only for now; the upload branch is added in Task 5). Pure move — existing tests are the gate.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_download.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_routes.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Modify: `tests/_file_exchange/test_download_e2e.py`

- [ ] **Step 1: Rename the registrar in `_download.py`**

In `src/fastmcp_pvl_core/_file_exchange/_download.py`, rename the function `register_file_exchange_routes` (line ~349) to `register_download_route`. Its body (the `@mcp.custom_route` GET handler `_serve_download`) is unchanged. The signature stays `(mcp: FastMCP, *, token_store: CapabilityTokenStore, source: ArtifactSource)` — `source` stays required here; optionality lives in `_routes.py`. Update the first docstring line to read:

```python
    """Mount the ``download`` GET route on ``mcp`` (serves §12 capability URLs).
```

(unchanged — only the function name changes).

- [ ] **Step 2: Create `_routes.py`**

```python
"""Cross-transport FastMCP route registration for the file-exchange HTTP transports.

``register_file_exchange_routes`` mounts the ``download`` GET route (when a
``source`` hook is given) and the ``upload`` PUT/POST route (when a ``sink`` hook
is given). Each transport's registrar lives in its own leaf module
(``_download``/``_upload``); this module is the single public entry point #148
threads ``token_store``/``source``/``sink``/``config`` into.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp_pvl_core._file_exchange._download import register_download_route

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore


def register_file_exchange_routes(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    source: ArtifactSource | None = None,
    sink: ArtifactSink | None = None,
    config: ServerConfig | None = None,
) -> None:
    """Mount the file-exchange HTTP routes on ``mcp``.

    Mounts the ``download`` GET route iff ``source`` is given. (The ``upload``
    PUT/POST route — mounted iff ``sink`` is given — is added in #146 Task 5.)
    ``token_store``/``source``/``sink``/``config`` are threaded by #148.
    """
    if source is not None:
        register_download_route(mcp, token_store=token_store, source=source)
```

Note: `sink`/`config` are accepted now so the public signature is stable across Task 5 (which adds the upload branch). They are intentionally unused until then.

- [ ] **Step 3: Re-export `register_file_exchange_routes` from `_routes`**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, change the `_download` import block (lines ~19–23) to drop `register_file_exchange_routes`:

```python
from fastmcp_pvl_core._file_exchange._download import (
    download_fetcher_consume,
    download_provider_mint,
)
```

and add a new import block (keep import blocks grouped by module, alphabetical by module name — `_routes` sorts after `_paths`/`_selection`? place it after the `_paths` block and before `_selection`):

```python
from fastmcp_pvl_core._file_exchange._routes import register_file_exchange_routes
```

`__all__` already lists `register_file_exchange_routes` — leave it.

In `src/fastmcp_pvl_core/file_exchange.py`, the names are imported in one combined block from `fastmcp_pvl_core._file_exchange` (the subpackage), so no per-module change is needed there — `register_file_exchange_routes` still resolves. Leave `file_exchange.py` unchanged in this task.

- [ ] **Step 4: Update the download e2e import**

`tests/_file_exchange/test_download_e2e.py` calls `_download.register_file_exchange_routes(...)` (line ~67). It must now use the public registrar. Change the import (line ~18) and the call:

```python
from fastmcp_pvl_core._file_exchange import _download, _routes
```

```python
    _routes.register_file_exchange_routes(
        mcp, token_store=store, source=_BytesSource("doc", body)
    )
```

(`_download` is still imported — the test monkeypatches `_download.guarded_stream` and calls `_download.download_provider_mint`/`download_fetcher_consume`.)

- [ ] **Step 5: Run download tests + namespace test + gates**

Run: `uv run pytest tests/_file_exchange/test_download.py tests/_file_exchange/test_download_e2e.py tests/test_file_exchange_namespace.py -q`
Expected: PASS. `test_download_data_plane_names_reexported` still passes (the public name is unchanged).

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_download.py src/fastmcp_pvl_core/_file_exchange/_routes.py src/fastmcp_pvl_core/_file_exchange/__init__.py tests/_file_exchange/test_download_e2e.py
git commit -m "refactor(file-exchange): move route registrar to _routes, source optional (#146)"
```

---

### Task 3: `upload_receiver_mint` + `UPLOAD_PREFIX`

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_upload.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Test: `tests/_file_exchange/test_upload.py`
- Modify: `tests/test_file_exchange_namespace.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/_file_exchange/test_upload.py`:

```python
"""Tests for the ``upload`` transport data plane (#146)."""

from __future__ import annotations

import base64
import contextlib
import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _routes, _upload
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    ArtifactMetadata,
    IntakeTicket,
    UploadSink,
)

pytestmark = pytest.mark.anyio


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_receiver_mint_returns_ticket_with_upload_sink():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://recv.test", ttl=300.0
    )
    assert isinstance(ticket, IntakeTicket)
    assert ticket.artifactId == "art-1"
    assert len(ticket.sinks) == 1
    sink = ticket.sinks[0]
    assert isinstance(sink, UploadSink)
    assert sink.transport == "upload"
    assert sink.method == "PUT"
    assert sink.url.startswith("https://recv.test/fx/u/")
    token = sink.url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["artifact_id"] == "art-1"
    assert rec.metadata["expected"] is None


async def test_receiver_mint_threads_method_and_expected():
    store = _store()
    expected = ArtifactConstraints(maxSize=1024, acceptMimeTypes=["text/*"])
    ticket = await _upload.upload_receiver_mint(
        "art-2",
        token_store=store,
        base_url="https://recv.test",
        ttl=300.0,
        expected=expected,
        method="POST",
    )
    assert ticket.expected == expected
    assert ticket.sinks[0].method == "POST"
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["expected"] == {
        "maxSize": 1024,
        "acceptMimeTypes": ["text/*"],
        "requireDigest": None,
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/_file_exchange/test_upload.py -q`
Expected: FAIL with `ModuleNotFoundError`/`AttributeError` (`_upload` has no `upload_receiver_mint`).

- [ ] **Step 3: Create `_upload.py` with the constant + receiver mint**

```python
"""The ``upload`` transport data plane (#146).

Three role helpers (receiver mint, the PUT/POST serving route, sender) plus the
RFC 9530 / RFC 7231 HTTP helpers the route and sender need, free functions
mirroring ``_filesystem.py`` / ``_download.py``. The receiver mints a capability
URL backed by the #144 token store; the route streams the pushed body to a
transient temp file, verifies the declared ``Content-Digest`` and the ticket's
``expected`` constraints **before** handing the #142 ``ArtifactSink`` a real sync
fd, then consumes the single-use token only on a successful store; the sender
stages the artifact, computes a ``Content-Digest``, and pushes it through the
#147 ``guarded_stream``. See
``docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import IntakeTicket, UploadSink

if TYPE_CHECKING:
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints

logger = logging.getLogger(__name__)

# pvl-core's upload route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision (mirrors DOWNLOAD_PREFIX).
UPLOAD_PREFIX = "/fx/u"


async def upload_receiver_mint(
    artifact_id: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    expected: ArtifactConstraints | None = None,
    method: Literal["PUT", "POST"] = "PUT",
) -> IntakeTicket:
    """Receiver role (push): mint an upload token and emit an IntakeTicket.

    ``artifact_id`` is the server's opaque identifier for the artifact slot,
    stored in the token for the route to correlate the pushed bytes; ``expected``
    is the §7.4 constraint set the route enforces at ingest (stored opaquely too).
    ``base_url`` is the server's public https origin; ``ttl`` is clamped by the
    token store's ceiling. Minting only — no hook runs and no bytes move (the
    sink is threaded into the route, where the bytes actually arrive).
    """
    minted = await token_store.mint(
        {
            "artifact_id": artifact_id,
            "expected": expected.model_dump(mode="json") if expected else None,
        },
        ttl=ttl,
        single_use=True,
    )
    url = capability_url(base_url, UPLOAD_PREFIX, minted.token)
    return IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId=artifact_id,
        expected=expected,
        sinks=[
            UploadSink(
                transport="upload",
                url=url,
                method=method,
                expiresAt=minted.expires_at,
            )
        ],
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/_file_exchange/test_upload.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Re-export `upload_receiver_mint` + update namespace test**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, add an import block for `_upload` (place after the `_tokens` block, before `_validation`):

```python
from fastmcp_pvl_core._file_exchange._upload import (
    upload_receiver_mint,
)
```

and add `"upload_receiver_mint"` to `__all__` (alphabetical — after `select_source`, before `validate_wire`).

In `src/fastmcp_pvl_core/file_exchange.py`, add `upload_receiver_mint` to the combined import (alphabetical, after `select_source`) and to `__all__` (after `select_source`).

In `tests/test_file_exchange_namespace.py`, add a new test:

```python
def test_upload_data_plane_names_reexported():
    from fastmcp_pvl_core import file_exchange

    for name in (
        "upload_receiver_mint",
        "upload_sender_consume",
        "register_file_exchange_routes",
    ):
        assert hasattr(file_exchange, name), name
        assert name in file_exchange.__all__, name
    # UPLOAD_PREFIX is internal route shape, not part of the public surface.
    assert not hasattr(file_exchange, "UPLOAD_PREFIX")
```

(`upload_sender_consume` is added in Task 6 — this test will fail until then; that is expected and acceptable for the incremental sequence, but to keep every task green, add `upload_sender_consume` to both `__all__`s and the imports as a forward reference now is NOT possible since the symbol doesn't exist. Instead: in THIS task, write the test referencing only `upload_receiver_mint` and `register_file_exchange_routes`; Task 6 extends it to include `upload_sender_consume`.)

Replace the test body above with the Task-3 version:

```python
def test_upload_data_plane_names_reexported():
    from fastmcp_pvl_core import file_exchange

    for name in ("upload_receiver_mint", "register_file_exchange_routes"):
        assert hasattr(file_exchange, name), name
        assert name in file_exchange.__all__, name
    # UPLOAD_PREFIX is internal route shape, not part of the public surface.
    assert not hasattr(file_exchange, "UPLOAD_PREFIX")
```

- [ ] **Step 6: Run namespace test + gates**

Run: `uv run pytest tests/test_file_exchange_namespace.py tests/_file_exchange/test_upload.py -q`
Expected: PASS.

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_upload.py src/fastmcp_pvl_core/_file_exchange/__init__.py src/fastmcp_pvl_core/file_exchange.py tests/_file_exchange/test_upload.py tests/test_file_exchange_namespace.py
git commit -m "feat(file-exchange): add upload_receiver_mint + IntakeTicket (#146)"
```

---

### Task 4: `Content-Digest` (RFC 9530) + media-range (RFC 7231) helpers

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_upload.py`
- Test: `tests/_file_exchange/test_upload.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_upload.py`:

```python
def test_format_content_digest_rfc9530():
    raw = hashlib.sha256(b"abc").digest()
    out = _upload._format_content_digest("sha-256", raw)
    assert out == "sha-256=:" + base64.b64encode(raw).decode("ascii") + ":"


def test_parse_content_digest_roundtrip():
    raw = hashlib.sha256(b"abc").digest()
    header = _upload._format_content_digest("sha-256", raw)
    assert _upload._parse_content_digest(header) == ("sha-256", raw)


def test_parse_content_digest_picks_first_supported():
    raw = hashlib.sha512(b"abc").digest()
    header = "md5=:" + base64.b64encode(b"x").decode() + ":, sha-512=:" + (
        base64.b64encode(raw).decode() + ":"
    )
    assert _upload._parse_content_digest(header) == ("sha-512", raw)


def test_parse_content_digest_rejects_unparseable():
    assert _upload._parse_content_digest("sha-256=not-a-byte-sequence") is None
    assert _upload._parse_content_digest("sha-256=:!!!notbase64!!!:") is None
    assert _upload._parse_content_digest("md5=:" + base64.b64encode(b"x").decode() + ":") is None


@pytest.mark.parametrize(
    ("content_type", "accept", "ok"),
    [
        ("text/plain", ["text/plain"], True),
        ("text/plain; charset=utf-8", ["text/plain"], True),
        ("text/plain", ["text/*"], True),
        ("text/plain", ["*/*"], True),
        ("application/json", ["text/*"], False),
        ("application/json", ["text/*", "application/json"], True),
        (None, ["text/*"], False),
        ("not-a-media-type", ["*/*"], False),
    ],
)
def test_media_type_accepted(content_type, accept, ok):
    assert _upload._media_type_accepted(content_type, accept) is ok
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/_file_exchange/test_upload.py -k "content_digest or media_type" -q`
Expected: FAIL (`AttributeError`: helpers not defined).

- [ ] **Step 3: Implement the helpers**

Add to the imports at the top of `_upload.py`:

```python
import base64
import binascii
```

Add to the constants section of `_upload.py` (after `UPLOAD_PREFIX`):

```python
# Default digest algorithm for the receiver's recorded ArtifactMetadata.digest
# and the sender's Content-Digest (pvl-core's shape). Members of _HASHLIB_BY_LABEL
# are also accepted on an inbound Content-Digest.
_DEFAULT_DIGEST_LABEL = "sha-256"
```

Add `_HASHLIB_BY_LABEL` to the `_staging` import (the helpers need it):

```python
from fastmcp_pvl_core._file_exchange._staging import _HASHLIB_BY_LABEL
```

Add the helpers:

```python
def _format_content_digest(label: str, raw: bytes) -> str:
    """Format a raw digest as an RFC 9530 ``Content-Digest`` member: ``label=:b64:``.

    The Structured-Field byte-sequence form (base64 wrapped in colons) — distinct
    from the wire ``ArtifactMetadata.digest`` field's ``label:hex`` form.
    """
    return f"{label}=:{base64.b64encode(raw).decode('ascii')}:"


def _parse_content_digest(header: str) -> tuple[str, bytes] | None:
    """Parse the first supported RFC 9530 ``Content-Digest`` member.

    Returns ``(label, raw_digest_bytes)`` for the first member whose algorithm is
    in :data:`_HASHLIB_BY_LABEL` and whose value is a well-formed byte sequence,
    or ``None`` when the header has no supported, well-formed member. A present
    header that parses to ``None`` is unverifiable -> the route rejects it
    (digest-mismatch), never silently skips (§15).
    """
    for member in header.split(","):
        label, sep, value = member.strip().partition("=")
        if sep != "=":
            continue
        label = label.strip().lower()
        value = value.strip()
        if label not in _HASHLIB_BY_LABEL:
            continue
        if len(value) < 2 or value[0] != ":" or value[-1] != ":":
            continue
        try:
            raw = base64.b64decode(value[1:-1], validate=True)
        except (binascii.Error, ValueError):
            continue
        return label, raw
    return None


def _media_type_accepted(content_type: str | None, accept: list[str]) -> bool:
    """Match a request media type against RFC 7231 §3.1.1.1 media-ranges.

    ``type/subtype`` matches exactly; ``type/*`` matches any subtype of ``type``;
    ``*/*`` matches anything. Parameters (``; charset=...``) are ignored. A
    missing or malformed ``Content-Type`` matches nothing (the route rejects).
    """
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if "/" not in media:
        return False
    main, sub = media.split("/", 1)
    for entry in accept:
        candidate = entry.split(";", 1)[0].strip().lower()
        if candidate == "*/*":
            return True
        if "/" not in candidate:
            continue
        cand_main, cand_sub = candidate.split("/", 1)
        if cand_main == main and cand_sub in ("*", sub):
            return True
    return False
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/_file_exchange/test_upload.py -k "content_digest or media_type" -q`
Expected: PASS.

- [ ] **Step 5: Gates**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_upload.py tests/_file_exchange/test_upload.py
git commit -m "feat(file-exchange): add Content-Digest (RFC 9530) + media-range helpers (#146)"
```

---

### Task 5: The upload route + wire `sink`/`config` into `register_file_exchange_routes`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_upload.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/_routes.py`
- Test: `tests/_file_exchange/test_upload.py`

- [ ] **Step 1: Write the failing route tests**

Append to `tests/_file_exchange/test_upload.py`:

```python
class _CapturingSink:
    def __init__(self):
        self.calls = []

    async def store_artifact(self, artifact_id, metadata, stream):
        self.calls.append((artifact_id, metadata, stream.read()))


class _BoomSink:
    async def store_artifact(self, artifact_id, metadata, stream):
        raise RuntimeError("sink failure with /secret/path detail")


def _mount(sink, *, config=None):
    store = _store()
    mcp = FastMCP("receiver")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, sink=sink, config=config or ServerConfig()
    )
    return store, mcp


def _client(mcp):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp.http_app()), base_url="http://up.test"
    )


async def _mint_token(store, *, artifact_id="art-1", expected=None):
    minted = await store.mint(
        {
            "artifact_id": artifact_id,
            "expected": expected.model_dump(mode="json") if expected else None,
        },
        ttl=300.0,
        single_use=True,
    )
    return minted.token


async def test_upload_route_unknown_token_404():
    _store_, mcp = _mount(_CapturingSink())
    async with _client(mcp) as client:
        resp = await client.put("/fx/u/nonexistent", content=b"x")
    assert resp.status_code == 404


async def test_upload_route_happy_deposits_and_consumes():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store)
    body = b"upload-payload" * 100
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=body)
    assert resp.status_code == 204
    assert len(sink.calls) == 1
    artifact_id, meta, deposited = sink.calls[0]
    assert artifact_id == "art-1"
    assert deposited == body
    assert meta.size == len(body)
    assert meta.digest == "sha-256:" + hashlib.sha256(body).hexdigest()
    # Single-use token consumed -> a replay is 404.
    assert await store.lookup(token) is None
    async with _client(mcp) as client:
        resp2 = await client.put(f"/fx/u/{token}", content=body)
    assert resp2.status_code == 404


async def test_upload_route_oversize_413_no_consume():
    store, mcp = _mount(
        sink := _CapturingSink(),
        config=ServerConfig(file_exchange_max_artifact_size=16),
    )
    token = await _mint_token(store)
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=b"x" * 64)
    assert resp.status_code == 413
    assert sink.calls == []
    assert await store.lookup(token) is not None  # not consumed


async def test_upload_route_maxsize_constraint_413():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store, expected=ArtifactConstraints(maxSize=8))
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=b"x" * 64)
    assert resp.status_code == 413
    assert sink.calls == []


async def test_upload_route_mime_reject_415_no_consume():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(
        store, expected=ArtifactConstraints(acceptMimeTypes=["text/*"])
    )
    async with _client(mcp) as client:
        resp = await client.put(
            f"/fx/u/{token}", content=b"{}", headers={"content-type": "application/json"}
        )
    assert resp.status_code == 415
    assert sink.calls == []
    assert await store.lookup(token) is not None


async def test_upload_route_valid_content_digest_verifies():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store)
    body = b"digest-checked-body"
    cd = _upload._format_content_digest("sha-256", hashlib.sha256(body).digest())
    async with _client(mcp) as client:
        resp = await client.put(
            f"/fx/u/{token}", content=body, headers={"content-digest": cd}
        )
    assert resp.status_code == 204
    assert sink.calls[0][2] == body


async def test_upload_route_digest_mismatch_400_no_sink_call():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store)
    body = b"the-real-body"
    wrong = _upload._format_content_digest("sha-256", hashlib.sha256(b"other").digest())
    async with _client(mcp) as client:
        resp = await client.put(
            f"/fx/u/{token}", content=body, headers={"content-digest": wrong}
        )
    assert resp.status_code == 400
    assert sink.calls == []  # verify-before-use: sink never saw the bytes
    assert await store.lookup(token) is not None  # not consumed


async def test_upload_route_require_digest_missing_header_400():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(
        store, expected=ArtifactConstraints(requireDigest=["sha-256"])
    )
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=b"no-digest-header")
    assert resp.status_code == 400
    assert sink.calls == []


async def test_upload_route_ambient_credentials_ignored():
    store, mcp = _mount(sink := _CapturingSink())
    token = await _mint_token(store)
    async with _client(mcp) as client:
        resp = await client.put(
            f"/fx/u/{token}",
            content=b"ok",
            headers={"authorization": "Bearer bogus", "cookie": "x=y"},
        )
    assert resp.status_code == 204
    assert sink.calls[0][2] == b"ok"


async def test_upload_route_sink_failure_500_no_consume():
    store, mcp = _mount(_BoomSink())
    token = await _mint_token(store)
    async with _client(mcp) as client:
        resp = await client.put(f"/fx/u/{token}", content=b"data")
    assert resp.status_code == 500
    assert resp.content == b""  # body-free: no hook detail echoed
    assert await store.lookup(token) is not None  # not consumed -> retryable


async def test_register_routes_upload_requires_config():
    store = _store()
    mcp = FastMCP("receiver")
    with pytest.raises(ValueError, match="config"):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, sink=_CapturingSink(), config=None
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/_file_exchange/test_upload.py -k "route or requires_config" -q`
Expected: FAIL (route + `register_upload_route` not implemented; `register_file_exchange_routes` does not mount upload).

- [ ] **Step 3: Implement the upload route in `_upload.py`**

Add to the imports at the top of `_upload.py`:

```python
import asyncio
import contextlib
import hashlib
import os
import tempfile
```

(extend the existing TYPE_CHECKING block):

```python
if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints
```

Add the runtime imports (these are used at runtime, not just typing):

```python
from starlette.responses import Response

from fastmcp_pvl_core._file_exchange._staging import _CHUNK, _HASHLIB_BY_LABEL, _write_chunk
from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints, ArtifactMetadata
```

(Consolidate the `_wire` / `_staging` imports — do not import a name twice. `ArtifactConstraints` moves from the TYPE_CHECKING block to the runtime block because the route calls `ArtifactConstraints.model_validate`.)

Add the registrar:

```python
def register_upload_route(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    sink: ArtifactSink,
    config: ServerConfig,
) -> None:
    """Mount the ``upload`` PUT/POST route on ``mcp`` (serves §12 capability URLs).

    ``PUT``/``POST <UPLOAD_PREFIX>/{token}`` looks the token up, streams the
    request body to a transient temp file (hashing, bounded by the operator cap
    ``config.file_exchange_max_artifact_size`` and the ticket's
    ``expected.maxSize``), enforces ``acceptMimeTypes`` and verifies a declared
    ``Content-Digest`` **before** depositing through ``sink.store_artifact``, and
    consumes the single-use token only on a successful store (§10.3). Ambient
    credentials are ignored — the in-URL token is the only authorization.
    ``token_store``/``sink``/``config`` are threaded by #148.
    """
    max_artifact = config.file_exchange_max_artifact_size

    @mcp.custom_route(f"{UPLOAD_PREFIX}/{{token}}", methods=["PUT", "POST"])
    async def _serve_upload(request: Request) -> Response:
        token = request.path_params["token"]
        rec = await token_store.lookup(token)
        if rec is None:
            return Response(status_code=404)
        artifact_id = rec.metadata["artifact_id"]
        expected_raw = rec.metadata.get("expected")
        expected: ArtifactConstraints | None = (
            ArtifactConstraints.model_validate(expected_raw)
            if expected_raw is not None
            else None
        )

        content_type = request.headers.get("content-type")
        if (
            expected is not None
            and expected.acceptMimeTypes
            and not _media_type_accepted(content_type, expected.acceptMimeTypes)
        ):
            return Response(status_code=415)

        cd_header = request.headers.get("content-digest")
        require_digest = bool(expected is not None and expected.requireDigest)
        cd = _parse_content_digest(cd_header) if cd_header is not None else None
        if cd_header is not None and cd is None:
            # A present but unverifiable Content-Digest is a verification
            # failure, never a silent skip (§15).
            return Response(status_code=400)
        if cd is None and require_digest:
            return Response(status_code=400)
        algo_label = cd[0] if cd is not None else _DEFAULT_DIGEST_LABEL

        # Smaller of the operator cap and the per-ticket cap, each when set.
        size_cap = max_artifact
        if expected is not None and expected.maxSize is not None:
            size_cap = (
                expected.maxSize
                if size_cap is None
                else min(size_cap, expected.maxSize)
            )

        try:
            fd, tmp_path = tempfile.mkstemp(prefix="fx-upload-")
        except OSError:
            logger.exception("file-exchange: upload temp create failed")
            return Response(status_code=500)
        try:
            try:
                tmp = os.fdopen(fd, "wb")
            except OSError:
                with contextlib.suppress(OSError):
                    os.close(fd)
                logger.exception("file-exchange: upload temp open failed")
                return Response(status_code=500)
            hasher = hashlib.new(_HASHLIB_BY_LABEL[algo_label])
            received = 0
            try:
                try:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        received += len(chunk)
                        if size_cap is not None and received > size_cap:
                            return Response(status_code=413)
                        await asyncio.to_thread(_write_chunk, tmp, hasher, chunk)
                    await asyncio.to_thread(tmp.flush)
                except OSError:
                    logger.exception("file-exchange: upload temp write failed")
                    return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    tmp.close()

            # Verify-before-use: an uploaded body is untrusted, so the digest is
            # checked before the sink ever sees the bytes.
            if cd is not None and hasher.digest() != cd[1]:
                return Response(status_code=400)

            meta = ArtifactMetadata(
                mimeType=content_type,
                size=received,
                digest=f"{algo_label}:{hasher.hexdigest()}",
            )
            try:
                ingest = await asyncio.to_thread(open, tmp_path, "rb")
            except OSError:
                logger.exception("file-exchange: upload staged open failed")
                return Response(status_code=500)
            try:
                await sink.store_artifact(artifact_id, meta, ingest)
            except Exception:
                # The hook may carry server paths in its message — never echo it;
                # log locally, body-free 500. The token is not consumed (nothing
                # stored), so the sender can re-request a ticket and retry.
                logger.exception("file-exchange: upload sink store_artifact failed")
                return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    ingest.close()

            # Single-success-per-URL: consume only after a successful store.
            try:
                await token_store.consume(token)
            except Exception:
                # The artifact was stored; a consume failure must not fail the
                # request. Log it (no token — redaction): the single-use token
                # may remain usable until it expires.
                logger.warning(
                    "file-exchange: upload token consume failed after store; "
                    "token may remain usable to TTL"
                )
            return Response(status_code=204)
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, tmp_path)
```

- [ ] **Step 4: Wire the upload branch into `register_file_exchange_routes`**

In `src/fastmcp_pvl_core/_file_exchange/_routes.py`, add the `_upload` import and the upload branch:

```python
from fastmcp_pvl_core._file_exchange._download import register_download_route
from fastmcp_pvl_core._file_exchange._upload import register_upload_route
```

Replace the body of `register_file_exchange_routes` and its docstring tail:

```python
    """Mount the file-exchange HTTP routes on ``mcp``.

    Mounts the ``download`` GET route iff ``source`` is given and the ``upload``
    PUT/POST route iff ``sink`` is given. Mounting the upload route requires
    ``config`` (the operator size cap bounds untrusted request bodies). All of
    ``token_store``/``source``/``sink``/``config`` are threaded by #148.
    """
    if source is not None:
        register_download_route(mcp, token_store=token_store, source=source)
    if sink is not None:
        if config is None:
            raise ValueError(
                "register_file_exchange_routes: mounting the upload route "
                "requires `config` for the operator size cap"
            )
        register_upload_route(mcp, token_store=token_store, sink=sink, config=config)
```

- [ ] **Step 5: Run the route tests**

Run: `uv run pytest tests/_file_exchange/test_upload.py -q`
Expected: PASS (all unit + route tests).

- [ ] **Step 6: Gates**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_upload.py src/fastmcp_pvl_core/_file_exchange/_routes.py tests/_file_exchange/test_upload.py
git commit -m "feat(file-exchange): add upload PUT/POST route with verify-before-use (#146)"
```

---

### Task 6: `upload_sender_consume`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_upload.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Test: `tests/_file_exchange/test_upload.py`
- Modify: `tests/test_file_exchange_namespace.py`

- [ ] **Step 1: Write the failing sender tests**

Append to `tests/_file_exchange/test_upload.py`:

```python
class _BytesSource:
    def __init__(self, key, body, *, mime="application/octet-stream"):
        self._key, self._body, self._mime = key, body, mime

    async def open_artifact(self, key):
        import io

        assert key == self._key
        return io.BytesIO(self._body), ArtifactMetadata(
            name="a", mimeType=self._mime, size=len(self._body)
        )


class _FakeGuarded:
    def __init__(self, status):
        self.status = status


def _upload_sink(method="PUT"):
    return UploadSink(
        transport="upload",
        url="https://up.test/fx/u/tok",
        method=method,
        expiresAt=datetime.now(timezone.utc) + timedelta(hours=1),
    )


async def test_sender_stages_and_sends_with_headers(monkeypatch):
    body = b"sender-payload" * 50
    captured: dict = {}

    @contextlib.asynccontextmanager
    async def fake_guard(method, url, *, config, transport, headers=None, content=None):
        captured["method"] = method
        captured["url"] = url
        captured["transport"] = transport
        captured["headers"] = dict(headers or {})
        captured["body"] = b"".join([chunk async for chunk in content])
        yield _FakeGuarded(204)

    monkeypatch.setattr(_upload, "guarded_stream", fake_guard)
    await _upload.upload_sender_consume(
        _upload_sink(), _BytesSource("k", body, mime="text/plain"), "k",
        config=ServerConfig(),
    )
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://up.test/fx/u/tok"
    assert captured["transport"] == "upload"
    assert captured["body"] == body
    assert captured["headers"]["Content-Length"] == str(len(body))
    assert captured["headers"]["Content-Type"] == "text/plain"
    assert captured["headers"]["Content-Digest"] == (
        "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"
    )


async def test_sender_omits_content_type_when_unknown(monkeypatch):
    captured: dict = {}

    @contextlib.asynccontextmanager
    async def fake_guard(method, url, *, config, transport, headers=None, content=None):
        async for _ in content:
            pass
        captured["headers"] = dict(headers or {})
        yield _FakeGuarded(201)

    monkeypatch.setattr(_upload, "guarded_stream", fake_guard)

    class _NoMimeSource:
        async def open_artifact(self, key):
            import io

            return io.BytesIO(b"x"), ArtifactMetadata(size=1)

    await _upload.upload_sender_consume(
        _upload_sink(), _NoMimeSource(), "k", config=ServerConfig()
    )
    assert "Content-Type" not in captured["headers"]


async def test_sender_non_2xx_raises_transfer_failed(monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_guard(method, url, *, config, transport, headers=None, content=None):
        async for _ in content:
            pass
        yield _FakeGuarded(500)

    monkeypatch.setattr(_upload, "guarded_stream", fake_guard)
    with pytest.raises(FileExchangeTransferError) as exc:
        await _upload.upload_sender_consume(
            _upload_sink(), _BytesSource("k", b"data"), "k", config=ServerConfig()
        )
    assert exc.value.code == TransferErrorCode.TRANSFER_FAILED
    assert exc.value.transport == "upload"


async def test_sender_guard_refusal_propagates(monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_guard(method, url, *, config, transport, headers=None, content=None):
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE, transport="upload", detail="blocked"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(_upload, "guarded_stream", fake_guard)
    with pytest.raises(FileExchangeTransferError) as exc:
        await _upload.upload_sender_consume(
            _upload_sink(), _BytesSource("k", b"data"), "k", config=ServerConfig()
        )
    assert exc.value.code == TransferErrorCode.NOT_ACCESSIBLE
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/_file_exchange/test_upload.py -k sender -q`
Expected: FAIL (`AttributeError`: `upload_sender_consume` not defined).

- [ ] **Step 3: Implement `upload_sender_consume`**

Add the runtime imports to `_upload.py` (consolidate with the existing import lines — add the new names, do not duplicate):

```python
from collections.abc import AsyncIterator

from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._outbound import guarded_stream
```

Extend the runtime TYPE_CHECKING / `_wire` imports so `UploadSink` and `ArtifactSource` are available — `UploadSink` is already imported at runtime; add `ArtifactSource` to the TYPE_CHECKING block:

```python
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
```

Add the function:

```python
async def upload_sender_consume(
    sink: UploadSink,
    source: ArtifactSource,
    key: str,
    *,
    config: ServerConfig,
) -> None:
    """Sender role (push): stage ``source[key]`` and push it to ``sink``.

    Selection (``select_sink``) is the caller's step. Stages the artifact to a
    transient temp file (single pass, hashing), computes an RFC 9530
    ``Content-Digest``, and streams the temp through the #147 ``guarded_stream``
    (which strips ambient credentials and refuses redirects on a bodied request).
    A non-2xx response maps to ``transfer-failed``; a guard refusal arrives coded
    and propagates. The temp is deleted on every path.

    Staging is required: the hook stream is non-seekable and the ``Content-Digest``
    must be hashed before it can be sent in the request header, so a single hook
    read cannot both hash-first and stream-from-the-hook.
    """
    stream, meta = await source.open_artifact(key)
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="fx-upload-send-")
    except OSError as exc:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.close)
        raise FileExchangeTransferError(
            TransferErrorCode.TRANSFER_FAILED,
            transport="upload",
            detail="failed to create a temporary file",
        ) from exc
    try:
        try:
            tmp = os.fdopen(fd, "wb")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        hasher = hashlib.sha256()
        size = 0
        try:
            try:
                while True:
                    chunk = await asyncio.to_thread(stream.read, _CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    await asyncio.to_thread(_write_chunk, tmp, hasher, chunk)
                await asyncio.to_thread(tmp.flush)
            except OSError as exc:
                raise FileExchangeTransferError(
                    TransferErrorCode.TRANSFER_FAILED,
                    transport="upload",
                    detail="failed to stage the artifact for upload",
                ) from exc
            finally:
                with contextlib.suppress(OSError):
                    tmp.close()
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)

        headers = {
            "Content-Length": str(size),
            "Content-Digest": _format_content_digest(
                _DEFAULT_DIGEST_LABEL, hasher.digest()
            ),
        }
        if meta.mimeType is not None:
            headers["Content-Type"] = meta.mimeType

        async def _content() -> AsyncIterator[bytes]:
            handle = await asyncio.to_thread(open, tmp_path, "rb")
            try:
                while True:
                    chunk = await asyncio.to_thread(handle.read, _CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(handle.close)

        try:
            async with guarded_stream(
                sink.method,
                sink.url,
                config=config,
                transport="upload",
                headers=headers,
                content=_content(),
            ) as resp:
                if not 200 <= resp.status < 300:
                    raise FileExchangeTransferError(
                        TransferErrorCode.TRANSFER_FAILED,
                        transport="upload",
                        detail="upload endpoint returned a non-success status",
                    )
        except OSError as exc:
            # A temp read surfacing from the content generator during send.
            raise FileExchangeTransferError(
                TransferErrorCode.TRANSFER_FAILED,
                transport="upload",
                detail="failed to read the staged artifact during upload",
            ) from exc
    finally:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(os.unlink, tmp_path)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/_file_exchange/test_upload.py -k sender -q`
Expected: PASS.

- [ ] **Step 5: Re-export `upload_sender_consume` + extend namespace test**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, extend the `_upload` import block:

```python
from fastmcp_pvl_core._file_exchange._upload import (
    upload_receiver_mint,
    upload_sender_consume,
)
```

and add `"upload_sender_consume"` to `__all__` (alphabetical — after `upload_receiver_mint`).

In `src/fastmcp_pvl_core/file_exchange.py`, add `upload_sender_consume` to the combined import (after `upload_receiver_mint`) and `__all__` (after `upload_receiver_mint`).

In `tests/test_file_exchange_namespace.py`, extend `test_upload_data_plane_names_reexported`:

```python
def test_upload_data_plane_names_reexported():
    from fastmcp_pvl_core import file_exchange

    for name in (
        "upload_receiver_mint",
        "upload_sender_consume",
        "register_file_exchange_routes",
    ):
        assert hasattr(file_exchange, name), name
        assert name in file_exchange.__all__, name
    # UPLOAD_PREFIX is internal route shape, not part of the public surface.
    assert not hasattr(file_exchange, "UPLOAD_PREFIX")
```

- [ ] **Step 6: Run namespace + full upload suite + gates**

Run: `uv run pytest tests/_file_exchange/test_upload.py tests/test_file_exchange_namespace.py -q`
Expected: PASS.

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_upload.py src/fastmcp_pvl_core/_file_exchange/__init__.py src/fastmcp_pvl_core/file_exchange.py tests/_file_exchange/test_upload.py tests/test_file_exchange_namespace.py
git commit -m "feat(file-exchange): add upload_sender_consume (#146)"
```

---

### Task 7: Two-server push end-to-end

**Files:**
- Create: `tests/_file_exchange/test_upload_e2e.py`

- [ ] **Step 1: Write the failing e2e test**

```python
"""End-to-end upload push: server A sends, server B mints + receives.

Exercises the whole ``upload`` data plane together — ``upload_receiver_mint``,
``register_file_exchange_routes`` (upload route mounted on a real ASGI app),
``select_sink``, and ``upload_sender_consume`` — over a loopback ASGI transport.
The SSRF guard is replaced with one that routes to server B's app, because we are
exercising the push flow, not the guard (which has its own tests).
"""

import contextlib

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _routes, _upload
from fastmcp_pvl_core._file_exchange._selection import select_sink
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

pytestmark = pytest.mark.anyio


class _BytesSource:
    def __init__(self, key, body):
        self._key, self._body = key, body

    async def open_artifact(self, key):
        import io

        assert key == self._key
        return io.BytesIO(self._body), ArtifactMetadata(
            name="a", mimeType="application/octet-stream", size=len(self._body)
        )


class _CapturingSink:
    def __init__(self):
        self.calls = []

    async def store_artifact(self, artifact_id, metadata, stream):
        self.calls.append((artifact_id, metadata, stream.read()))


class _FakeGuarded:
    def __init__(self, status):
        self.status = status


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_two_server_push_upload(monkeypatch):
    body = b"end-to-end-upload-payload" * 64

    # Server B: receiver mint + upload route on a real ASGI app.
    store = _store()
    sink = _CapturingSink()
    recv = FastMCP("receiver")
    _routes.register_file_exchange_routes(
        recv, token_store=store, sink=sink, config=ServerConfig()
    )
    app_b = recv.http_app()

    # Server A's guard, redirected at B's ASGI app (no real network / SSRF check).
    @contextlib.asynccontextmanager
    async def guard_to_app_b(
        method, url, *, config, transport, headers=None, content=None
    ):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://recv.test"
        )
        try:
            path = url.split("recv.test", 1)[1]
            req = client.build_request(
                method, path, headers=headers or {}, content=content
            )
            resp = await client.send(req)
            try:
                yield _FakeGuarded(resp.status_code)
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_upload, "guarded_stream", guard_to_app_b)

    # B mints an intake ticket; A selects the upload sink and pushes.
    ticket = await _upload.upload_receiver_mint(
        "art-e2e", token_store=store, base_url="https://recv.test", ttl=300.0
    )
    descriptor = select_sink(ticket)
    assert descriptor is not None and descriptor.transport == "upload"

    await _upload.upload_sender_consume(
        descriptor, _BytesSource("k", body), "k", config=ServerConfig()
    )

    # The bytes landed in B's sink, correlated to artifactId.
    assert len(sink.calls) == 1
    assert sink.calls[0][0] == "art-e2e"
    assert sink.calls[0][2] == body
    # The single-use token was consumed by the completed upload.
    assert await store.lookup(descriptor.url.rsplit("/", 1)[1]) is None
```

- [ ] **Step 2: Run to verify it passes**

Run: `uv run pytest tests/_file_exchange/test_upload_e2e.py -q`
Expected: PASS (the implementation already exists from Tasks 3–6; this is an integration check, so it should pass on the first run — if it fails, the failure is a real integration gap to fix before committing).

- [ ] **Step 3: Gates**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/_file_exchange/test_upload_e2e.py
git commit -m "test(file-exchange): two-server push e2e for upload data plane (#146)"
```

---

### Task 8: Full suite, quality gates, preflight-circus, draft PR

**Files:** none (verification + PR).

- [ ] **Step 1: Full local check matrix (matches CI's dependency state)**

```bash
uv sync --all-extras
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```
Expected: all green; the new `test_upload.py` + `test_upload_e2e.py` tests are included; download tests unchanged.

- [ ] **Step 2: Cross-version check (min + max interpreter)**

```bash
uv run --python 3.10 pytest tests/_file_exchange/test_upload.py tests/_file_exchange/test_upload_e2e.py -q
uv run --python 3.13 pytest tests/_file_exchange/test_upload.py tests/_file_exchange/test_upload_e2e.py -q
```
Expected: PASS on both (no version-dependent behaviour in the upload path; this guards against the 3.10/3.13 split that bit #152).

- [ ] **Step 3: Preflight-circus on the cumulative diff**

Invoke the `preflight-circus` skill against `BASE..HEAD` (`BASE = $(git merge-base HEAD origin/main)`). It is a normative-content change (the design doc cites RFC 9530 / RFC 7231 / spec §10.3 but is **informative**, not a wire spec — lens 6 fires only if a `docs/specs/` wire-format file changes, which it does not here). Resolve every finding surviving the ≥80 filter before pushing.

- [ ] **Step 4: Open the draft PR**

Confirm an issue exists (#146). Push the branch and open as draft:

```bash
git push -u origin feat/146-upload-data-plane
gh pr create --draft --title "feat(file-exchange): upload transport data plane (#146)" --body "$(cat <<'EOF'
## Summary
- Adds the `upload` transport data plane (EPIC #138, 8/10): `upload_receiver_mint` (token + IntakeTicket/UploadSink), the PUT/POST route (stream-to-temp, `acceptMimeTypes` RFC 7231, `Content-Digest` RFC 9530 verify, single-success-per-URL, verify-before-use into the `ArtifactSink`), and `upload_sender_consume` (stage + `Content-Digest` + `guarded_stream`).
- Refactor: extracts shared digest/chunk primitives into `_staging.py`; moves `register_file_exchange_routes` into `_routes.py` (now mounts download iff `source`, upload iff `sink`+`config`).
- #148 threads `source`/`sink`/`config` + adds Tasks integration; this PR ships the primitives + route.

## Test plan
- [ ] `uv run pytest` green (new `test_upload.py` + `test_upload_e2e.py`; download suite unchanged)
- [ ] `uv run ruff format --check . && uv run ruff check . && uv run mypy src` clean
- [ ] Cross-version: `uv run --python 3.10/3.13 pytest tests/_file_exchange/test_upload*.py`
- [ ] Receiver mint, route happy+consume / replay-404 / oversize-413 / mime-415 / digest-verify+mismatch-400 / requireDigest-missing-400 / ambient-creds-ignored / sink-failure-500, sender headers+non-2xx+guard-refusal, two-server push e2e

Closes #146.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Bot iteration (capped) → flip ready**

Read the `claude-review` body (not just the check status). Address any finding per the iteration cap (re-run the full `preflight-circus` before each fix push). Once local circus was clean, bot bodies LGTM, and CI is green, flip ready: `gh pr ready <N>`.

---

## Self-Review

**1. Spec coverage** (design doc → task):
- `upload_receiver_mint` (token stores `artifact_id`+`expected`; IntakeTicket+UploadSink) → Task 3. ✓
- `register_file_exchange_routes` optional `source`/`sink`/`config`; download-only / upload-only / both; upload requires `config` → Task 2 (download + optional source) + Task 5 (sink/config + ValueError guard). ✓
- Upload route: 404 / acceptMimeTypes-415 / stream-to-temp + size-cap-413 / Content-Digest verify (present→verify, requireDigest→required) / verify-before-use / store + 204 / consume-only-on-success / ambient-creds-ignored / hook-fail-500 → Task 5. ✓
- `upload_sender_consume`: stage + Content-Digest + `guarded_stream` content body + non-2xx→transfer-failed + guard-refusal propagates + temp cleanup + omit Content-Type when unknown → Task 6. ✓
- §10.3 Content-Digest RFC 9530 (`label=:b64:`, verify in declared algo, metadata `label:hex`) + acceptMimeTypes RFC 7231 media-range → Task 4 (helpers) + Task 5 (route use). ✓
- File structure: `_staging.py` (Task 1), `_routes.py` (Task 2), `_upload.py` (Tasks 3–6); temp-IO OSError contract from #145 preserved (route→body-free 500; sender→transfer-failed) → Tasks 5/6. ✓
- Public surface re-exports + namespace test → Tasks 3/6. ✓
- Testing (unit + route + sender + e2e) → Tasks 3–7. ✓

**2. Placeholder scan:** No "TBD"/"implement later"/"add error handling"/"write tests for the above". Every code/test step shows complete code. The only deferred items are explicitly out of scope (#148 threading; sender-side mime pre-check deferred per the design's §10.3 rationale).

**3. Type consistency:**
- `register_download_route(mcp, *, token_store, source)` — defined Task 2, called from `_routes.py` Task 2. ✓
- `register_upload_route(mcp, *, token_store, sink, config)` — defined Task 5, called from `_routes.py` Task 5. ✓
- `register_file_exchange_routes(mcp, *, token_store, source=None, sink=None, config=None)` — Task 2 (download branch + sink/config params present) → Task 5 (upload branch added). Signature stable across both. ✓
- `upload_receiver_mint(artifact_id, *, token_store, base_url, ttl, expected=None, method="PUT")` — Task 3 def; same shape in e2e (Task 7) and unit (Task 3). ✓
- `upload_sender_consume(sink, source, key, *, config)` — Task 6 def; same call in sender tests (Task 6) + e2e (Task 7). ✓
- Helpers `_format_content_digest(label, raw)`, `_parse_content_digest(header) -> (label, raw)|None`, `_media_type_accepted(content_type, accept) -> bool` — Task 4 def; used in Task 5 route + Task 6 sender + tests. ✓
- `_staging` exports `_CHUNK`, `_HASHLIB_BY_LABEL`, `_digest_verifier`, `_write_chunk` — Task 1; imported by `_download` (Task 1) and `_upload` (Tasks 4–6). ✓
- Token metadata keys `"artifact_id"` / `"expected"` — written by `upload_receiver_mint` (Task 3) and the route's `_mint_token` helper (Task 5), read by the route (Task 5). Consistent. ✓
- `ArtifactMetadata(mimeType=, size=, digest=)` with all-optional fields (≥1 required) — the route always sets size+digest, so the invariant holds. ✓
