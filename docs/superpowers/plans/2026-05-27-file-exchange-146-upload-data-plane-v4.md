# File-Exchange #146 Upload Data Plane — TDD-First Implementation Plan (v4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `upload` transport's data plane (route + receiver mint + sender consume) as the mirror of the merged #145 download data plane, driven test-by-test by the 2026-05-27 failure-mode matrix, on a fresh branch with no carry-over sediment from the three abandoned attempts (#163 / #164 / #165).

**Architecture:** One new private module per concern — `_staging.py` for the digest+chunk primitives shared with the download fetcher, `_upload.py` for the upload role helpers + route registrar + RFC 9530/7231 helpers, `_routes.py` for the cross-transport `register_file_exchange_routes` registrar. Existing `_download.py` is refactored in place to import the shared primitives and to expose its own `register_download_route`; `register_file_exchange_routes` moves to `_routes.py`. Public re-exports in `file_exchange.py` and the subpackage `__init__.py` stay alphabetical, with the public name `register_file_exchange_routes` unchanged.

**Tech Stack:** Python 3.10–3.13, `pydantic`, `starlette` (via `fastmcp.custom_route`), `httpx` (via `_outbound.guarded_stream`), `asyncio.to_thread` for blocking I/O off-loading, `hashlib` for digest, `pytest` (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md`
**Failure-mode matrix:** `docs/superpowers/specs/2026-05-27-file-exchange-146-failure-modes.md`
**Mirror reference:** `src/fastmcp_pvl_core/_file_exchange/_download.py` on `main`.

**PR-shape rule:** After each Task, decide whether the diff so far is independently mergeable with a small review surface. If yes, push; if not, continue. Do **not** pre-commit to "one PR" or "five PRs" — let the work shape its packaging. The three-abandon memory (`feedback_three_abandons_means_scope`) warns against grinding sediment; the abandon-rule (`feedback_high_recall_bot_iteration_limits`) warns against ignoring iteration spirals. Both apply.

**Discipline at every Task boundary:**

- `uv run pytest tests/_file_exchange/test_download.py tests/_file_exchange/test_download_e2e.py` — the download suite must stay green.
- `uv run ruff format .` then `uv run ruff check .` then `uv run mypy src` — every commit.
- The matrix row IDs (A1–G2) are referenced in each step's test docstring so reviewers can trace each test back to its enumerated failure mode.

---

## Task 1 — Extract shared staging primitives (matrix rows G1, B1 mirror, B2 mirror, B3 mirror)

**Goal:** Move the chunk-write / digest-verifier / `_CHUNK` / `_HASHLIB_BY_LABEL` primitives out of `_download.py` into `_staging.py` without changing observable behaviour. This unblocks `_upload.py` from re-deriving them. It is a pure refactor — every existing download test must remain green.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_staging.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/_download.py`
- Test: `tests/_file_exchange/test_staging.py`

- [ ] **Step 1.1: Write the failing test for `_staging` re-exports.**

```python
# tests/_file_exchange/test_staging.py
"""Matrix row G1: extracted staging primitives match the download originals."""

import hashlib

from fastmcp_pvl_core._file_exchange import _staging


def test_chunk_constant_is_one_megabyte():
    assert _staging._CHUNK == 1024 * 1024


def test_hashlib_label_map_covers_sha_256_384_512():
    assert _staging._HASHLIB_BY_LABEL == {
        "sha-256": "sha256",
        "sha-384": "sha384",
        "sha-512": "sha512",
    }


def test_digest_verifier_unknown_label_unverifiable():
    hasher, expected_hex, unverifiable = _staging._digest_verifier("md5:abcd")
    assert hasher is None
    assert expected_hex == "abcd"
    assert unverifiable is True


def test_digest_verifier_known_label_returns_hasher():
    hasher, expected_hex, unverifiable = _staging._digest_verifier(
        "sha-256:" + "0" * 64
    )
    assert isinstance(hasher, type(hashlib.sha256()))
    assert expected_hex == "0" * 64
    assert unverifiable is False


def test_digest_verifier_none_declared_no_op():
    assert _staging._digest_verifier(None) == (None, None, False)


def test_write_chunk_writes_and_hashes(tmp_path):
    target = tmp_path / "buf.bin"
    with target.open("wb") as fh:
        h = hashlib.new("sha256")
        _staging._write_chunk(fh, h, b"abc")
        _staging._write_chunk(fh, h, b"def")
    assert target.read_bytes() == b"abcdef"
    assert h.hexdigest() == hashlib.sha256(b"abcdef").hexdigest()


def test_write_chunk_no_hasher_writes_only(tmp_path):
    target = tmp_path / "buf.bin"
    with target.open("wb") as fh:
        _staging._write_chunk(fh, None, b"abc")
    assert target.read_bytes() == b"abc"
```

- [ ] **Step 1.2: Run the test — confirm it fails.**

```bash
uv run pytest tests/_file_exchange/test_staging.py -v
```
Expected: `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._staging'`.

- [ ] **Step 1.3: Create `_staging.py` with the extracted primitives.**

```python
# src/fastmcp_pvl_core/_file_exchange/_staging.py
"""Shared digest + chunk-write primitives for the file-exchange data planes.

Both ``_download`` (fetcher: write incoming HTTP body to a temp) and
``_upload`` (route: write incoming PUT body to a temp; sender: write outgoing
hook stream to a temp before hashing) share the same per-chunk
write-and-hash pattern and the same declared-digest verifier semantics.
Centralising the primitives here keeps the contract — every temp-file op
maps to ``transfer-failed`` or is suppressed for cleanup, and an
unsupported digest label fails verification rather than silently skipping
— from being re-derived divergently between transports (matrix row G1,
spec §15, mirror ``_download.py`` lines 47–302).
"""

from __future__ import annotations

import hashlib
from typing import IO

_CHUNK = 1024 * 1024

# Declared-digest label -> hashlib name; an unsupported label fails
# verification (cannot verify -> digest-mismatch), never silently skips.
_HASHLIB_BY_LABEL = {"sha-256": "sha256", "sha-384": "sha384", "sha-512": "sha512"}


def _digest_verifier(
    declared: str | None,
) -> tuple[hashlib._Hash | None, str | None, bool]:
    """Return ``(hasher | None, expected_hex | None, unverifiable)``.

    ``unverifiable`` is True when a digest is declared with an unsupported
    label — verification must then fail (cannot verify), never silently
    skip (§15).
    """
    if declared is None:
        return None, None, False
    label, _, expected_hex = declared.partition(":")
    name = _HASHLIB_BY_LABEL.get(label.lower())
    if name is None:
        return None, expected_hex, True
    return hashlib.new(name), expected_hex.lower(), False


def _write_chunk(
    tmp: IO[bytes], hasher: hashlib._Hash | None, chunk: bytes
) -> None:
    """Write a body chunk to the temp file and fold it into the running hash.

    Both ops run off the event loop in a single ``asyncio.to_thread`` dispatch.
    """
    tmp.write(chunk)
    if hasher is not None:
        hasher.update(chunk)
```

- [ ] **Step 1.4: Run the staging test — confirm it passes.**

```bash
uv run pytest tests/_file_exchange/test_staging.py -v
```
Expected: PASS.

- [ ] **Step 1.5: Rewire `_download.py` to import the primitives.**

In `src/fastmcp_pvl_core/_file_exchange/_download.py`:

1. Delete the local `_CHUNK = 1024 * 1024`, `_HASHLIB_BY_LABEL = {...}`, `_digest_verifier(...)`, and `_write_chunk(...)` definitions.
2. Add the import near the existing imports:

```python
from fastmcp_pvl_core._file_exchange._staging import (
    _CHUNK,
    _HASHLIB_BY_LABEL,
    _digest_verifier,
    _write_chunk,
)
```

3. Leave every other line of `_download.py` untouched. The names are unchanged; only their source module is.

- [ ] **Step 1.6: Run the download suite — confirm it still passes.**

```bash
uv run pytest tests/_file_exchange/test_download.py tests/_file_exchange/test_download_e2e.py -v
```
Expected: PASS, identical to pre-refactor.

- [ ] **Step 1.7: Format, lint, type-check, commit.**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_staging.py \
        src/fastmcp_pvl_core/_file_exchange/_download.py \
        tests/_file_exchange/test_staging.py
git commit -m "$(cat <<'EOF'
refactor(file-exchange): extract _staging.py from _download.py (#146)

Move _CHUNK, _HASHLIB_BY_LABEL, _digest_verifier, _write_chunk out of
_download.py into a new _staging.py. Pure refactor: no observable
behaviour change; download tests stay green. Unblocks #146's upload
data plane from re-deriving the primitives.

Matrix row G1. Refs #146.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Task 1 PR-shape checkpoint:** This refactor is independently reviewable in ~50 lines. If you want to push and open a PR for it alone, do so now and pause for review before Task 2. Otherwise continue.

---

## Task 2 — `upload_receiver_mint` (matrix rows E1, E4)

**Goal:** The smallest upload primitive: token mint + `IntakeTicket` build. No I/O.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_upload.py`
- Test: `tests/_file_exchange/test_upload_mint.py`

- [ ] **Step 2.1: Write the failing tests for mint.**

```python
# tests/_file_exchange/test_upload_mint.py
"""Matrix row E1, E4: upload_receiver_mint builds an IntakeTicket."""

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints, UploadSink


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_mint_returns_intake_ticket_with_one_upload_sink():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="https://b.example",
        ttl=120.0,
    )
    assert ticket.type == TICKET_TYPE
    assert ticket.version == SPEC_VERSION
    assert ticket.artifactId == "art-1"
    assert ticket.expected is None
    assert len(ticket.sinks) == 1
    sink = ticket.sinks[0]
    assert isinstance(sink, UploadSink)
    assert sink.transport == "upload"
    assert sink.url.startswith("https://b.example/fx/u/")
    assert sink.method == "PUT"
    token = sink.url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata == {"artifact_id": "art-1", "expected": None}
    assert rec.single_use is True


async def test_mint_method_post_threads_through():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "art-2",
        token_store=store,
        base_url="https://b.example",
        ttl=120.0,
        method="POST",
    )
    assert ticket.sinks[0].method == "POST"


async def test_mint_expected_round_trips_onto_ticket_and_metadata():
    store = _store()
    expected = ArtifactConstraints(
        maxSize=1024, acceptMimeTypes=["application/json"], requireDigest=["sha-256"]
    )
    ticket = await _upload.upload_receiver_mint(
        "art-3",
        token_store=store,
        base_url="https://b.example",
        ttl=120.0,
        expected=expected,
    )
    assert ticket.expected == expected
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["artifact_id"] == "art-3"
    assert rec.metadata["expected"] == expected.model_dump()


async def test_mint_calls_do_not_collide_for_same_artifact_id():
    """E1: minting twice yields two distinct tokens, both valid."""
    store = _store()
    t1 = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://b.example", ttl=120.0
    )
    t2 = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://b.example", ttl=120.0
    )
    tok1 = t1.sinks[0].url.rsplit("/", 1)[1]
    tok2 = t2.sinks[0].url.rsplit("/", 1)[1]
    assert tok1 != tok2
    assert (await store.lookup(tok1)) is not None
    assert (await store.lookup(tok2)) is not None
```

- [ ] **Step 2.2: Run the tests — confirm they fail.**

```bash
uv run pytest tests/_file_exchange/test_upload_mint.py -v
```
Expected: `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._upload'`.

- [ ] **Step 2.3: Create `_upload.py` with `upload_receiver_mint` and `UPLOAD_PREFIX`.**

```python
# src/fastmcp_pvl_core/_file_exchange/_upload.py
"""The ``upload`` transport data plane (#146).

Three free helpers — ``upload_receiver_mint``, ``upload_sender_consume``,
and ``register_upload_route`` — that mirror ``_download.py``'s pull plane.
The route accepts an HTTPS ``PUT``/``POST`` capability URL, streams the
request body to a transient temp file with size + digest verification,
and on a clean verify deposits the bytes into the receiver's
``ArtifactSink`` (#142). The sender stages the offered bytes (hook stream
→ temp + hash) and ``PUT``s them through the #147 SSRF guard.

See ``docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md``
for the contract and ``…2026-05-27-file-exchange-146-failure-modes.md`` for
the enumerated failure modes each test in this module exercises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import IntakeTicket, UploadSink

if TYPE_CHECKING:
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints

# pvl-core's upload route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision.
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

    Mint-only — no hook is called, no bytes move. The token stores the
    ``artifact_id`` and the ``expected`` constraints so the route can
    correlate received bytes back to a wire id and enforce limits at
    deposit time. The token store treats the metadata as opaque (#144).
    """
    minted = await token_store.mint(
        {
            "artifact_id": artifact_id,
            "expected": expected.model_dump() if expected is not None else None,
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

- [ ] **Step 2.4: Run the mint tests — confirm they pass.**

```bash
uv run pytest tests/_file_exchange/test_upload_mint.py -v
```
Expected: PASS.

- [ ] **Step 2.5: Format, lint, type-check, commit.**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_upload.py \
        tests/_file_exchange/test_upload_mint.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): upload_receiver_mint (#146)

Mint a single-use capability token and build an IntakeTicket carrying
one UploadSink. No I/O, no hook calls — pure token-store mint plus
descriptor construction. The token's opaque metadata carries the
artifact_id and the optional ArtifactConstraints so the route (next
task) can correlate received bytes and enforce limits.

Matrix rows E1, E4. Refs #146.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — RFC 9530 `Content-Digest` parser + formatter (matrix rows F1, F2, D4, D5)

**Goal:** Hand-rolled parse/format for the structured-field dictionary form `algo=:base64:`. Used by both the route (parse incoming header) and the sender (format outgoing header).

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_upload.py`
- Test: `tests/_file_exchange/test_upload_helpers.py`

- [ ] **Step 3.1: Write the failing tests.**

```python
# tests/_file_exchange/test_upload_helpers.py
"""Matrix rows F1, F2, F3, F4, D4, D5: pure-function helpers in _upload."""

import base64
import hashlib

import pytest

from fastmcp_pvl_core._file_exchange._upload import (
    _content_digest_format,
    _content_digest_parse,
    _media_range_matches,
)


# --- Content-Digest parse (F1, D4, D5) ---


def test_content_digest_parse_sha256_well_formed():
    payload = b"hello"
    raw = hashlib.sha256(payload).digest()
    b64 = base64.b64encode(raw).decode("ascii")
    header = f"sha-256=:{b64}:"
    algo, decoded = _content_digest_parse(header)
    assert algo == "sha-256"
    assert decoded == raw


def test_content_digest_parse_sha512_well_formed():
    raw = hashlib.sha512(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    algo, decoded = _content_digest_parse(f"sha-512=:{b64}:")
    assert algo == "sha-512"
    assert decoded == raw


def test_content_digest_parse_tolerates_whitespace():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    algo, decoded = _content_digest_parse(f"  sha-256 = :{b64}:  ")
    assert algo == "sha-256"
    assert decoded == raw


@pytest.mark.parametrize(
    "header",
    [
        "garbage",
        "sha-256:abcd",  # missing structured-field colons
        "md5=:YWJjZA==:",  # unsupported algo label
        "sha-256=::",  # empty value
        "sha-256=:not-base64!:",
        "sha-256=:YWJjZA",  # missing trailing colon
        "",
    ],
)
def test_content_digest_parse_malformed_or_unknown_returns_none(header):
    """D4 + D5: present-but-unparseable / unsupported-algo -> None."""
    assert _content_digest_parse(header) is None


# --- Content-Digest format (F2) ---


def test_content_digest_format_round_trips():
    raw = hashlib.sha256(b"hello").digest()
    header = _content_digest_format("sha-256", raw)
    parsed = _content_digest_parse(header)
    assert parsed == ("sha-256", raw)


# --- Media-range matching (F3, F4) ---


@pytest.mark.parametrize(
    "content_type,accept,expected",
    [
        ("application/json", ["application/json"], True),
        ("application/json; charset=utf-8", ["application/json"], True),
        ("image/png", ["image/*"], True),
        ("text/plain", ["image/*"], False),
        ("application/octet-stream", ["*/*"], True),
        ("APPLICATION/JSON", ["application/json"], True),  # case-insensitive
        ("application/json", ["text/plain", "application/json"], True),
        ("application/json", ["text/plain", "text/html"], False),
        ("", ["application/json"], False),
        ("application/json", [], False),
    ],
)
def test_media_range_matches_table(content_type, accept, expected):
    """F3."""
    assert _media_range_matches(content_type, accept) is expected
```

- [ ] **Step 3.2: Run — confirm failure.**

```bash
uv run pytest tests/_file_exchange/test_upload_helpers.py -v
```
Expected: `ImportError: cannot import name '_content_digest_parse' from 'fastmcp_pvl_core._file_exchange._upload'`.

- [ ] **Step 3.3: Add the helpers to `_upload.py`.**

Add to the bottom of `_upload.py` (above the existing `upload_receiver_mint`, or below — order doesn't matter for behaviour; module-internal helpers conventionally near the top):

```python
import base64 as _b64
import binascii

from fastmcp_pvl_core._file_exchange._staging import _HASHLIB_BY_LABEL


def _content_digest_parse(header: str) -> tuple[str, bytes] | None:
    """Parse an RFC 9530 ``Content-Digest`` structured-field dictionary entry.

    Returns ``(algo_label, raw_digest_bytes)`` on success, or ``None`` for any
    malformed input or unsupported algorithm — the caller treats ``None`` as a
    verification failure (``digest-mismatch``), never a silent skip
    (matrix rows D4, D5; spec §10.3).

    Only a single-algorithm dictionary entry is accepted (the form pvl-core's
    sender produces); ``sha-256``/``sha-384``/``sha-512`` are the supported
    labels.
    """
    if not header:
        return None
    entry = header.split(",", 1)[0].strip()
    label, sep, rest = entry.partition("=")
    if sep != "=":
        return None
    label = label.strip().lower()
    if label not in _HASHLIB_BY_LABEL:
        return None
    rest = rest.strip()
    if len(rest) < 2 or not rest.startswith(":") or not rest.endswith(":"):
        return None
    b64 = rest[1:-1]
    if not b64:
        return None
    try:
        raw = _b64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    return label, raw


def _content_digest_format(label: str, raw: bytes) -> str:
    """Format ``(label, raw_digest_bytes)`` as ``algo=:base64:`` per RFC 9530."""
    return f"{label}=:{_b64.b64encode(raw).decode('ascii')}:"


def _media_range_matches(content_type: str, accept: list[str]) -> bool:
    """RFC 7231 §3.1.1.1 media-range match: parameters ignored, case-insensitive.

    ``type/*`` matches any subtype; ``*/*`` matches anything. An empty
    ``content_type`` or an empty ``accept`` list never matches
    (matrix rows F3, F4).
    """
    if not content_type or not accept:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    if "/" not in main:
        return False
    main_type, main_sub = main.split("/", 1)
    for entry in accept:
        if not entry:
            continue
        entry_main = entry.split(";", 1)[0].strip().lower()
        if "/" not in entry_main:
            continue
        e_type, e_sub = entry_main.split("/", 1)
        if (e_type == "*" or e_type == main_type) and (
            e_sub == "*" or e_sub == main_sub
        ):
            return True
    return False
```

- [ ] **Step 3.4: Run — confirm pass.**

```bash
uv run pytest tests/_file_exchange/test_upload_helpers.py -v
```
Expected: PASS.

- [ ] **Step 3.5: Format, lint, type-check, commit.**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_upload.py \
        tests/_file_exchange/test_upload_helpers.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): RFC 9530 Content-Digest + RFC 7231 media-range helpers (#146)

Hand-rolled parse/format for the structured-field dictionary form
`algo=:base64:` (sha-256/384/512 only) plus an RFC 7231 §3.1.1.1
media-range matcher. All three are pure functions, exhaustively
covered with table-driven tests. Present-but-unparseable headers and
unsupported algorithm labels return None / False — never a silent
skip (spec §10.3, matrix rows F1–F4, D4, D5).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4 — `register_upload_route` happy path (matrix row A1 success arm, F4)

**Goal:** The route handler. Start with the happy PUT — body streams to a temp, digest verifies, sink stores, token consumes, 204 returned. Edge modes (A2–A6, B1–B6, C1–C2, D4–D8) are added in **Task 5** one by one; this task lands the skeleton so each subsequent edge-mode test attaches to a working baseline.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_upload.py`
- Test: `tests/_file_exchange/test_upload_route.py`

- [ ] **Step 4.1: Write the failing happy-path test.**

```python
# tests/_file_exchange/test_upload_route.py
"""Matrix rows A1–A6, B1, B4, B6, C1–C2, D4–D8, F4, F5: upload route."""

import hashlib
import os
from typing import BinaryIO

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints, ArtifactMetadata


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


class _RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, ArtifactMetadata, bytes]] = []

    async def store_artifact(
        self, artifact_id: str | None, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        data = stream.read()
        self.calls.append((artifact_id, metadata, data))


async def _mount(sink, *, config=None):
    cfg = config or ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=10 * 1024 * 1024,
    )
    store = build_capability_token_store(cfg)
    mcp = FastMCP("test")
    _upload.register_upload_route(mcp, token_store=store, sink=sink, config=cfg)
    return mcp, store


async def _client(mcp):
    transport = httpx.ASGITransport(app=mcp.http_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_route_happy_put_deposits_and_consumes():
    """A1 success: PUT 204 -> sink called once, token consumed."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="http://test",
        ttl=120.0,
    )
    url = ticket.sinks[0].url
    token = url.rsplit("/", 1)[1]
    async with await _client(mcp) as c:
        resp = await c.put(
            url, content=b"hello world", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 204
    assert len(sink.calls) == 1
    aid, meta, body = sink.calls[0]
    assert aid == "art-1"
    assert body == b"hello world"
    assert meta.mimeType == "text/plain"
    assert meta.size == len(b"hello world")
    assert meta.digest == "sha-256:" + hashlib.sha256(b"hello world").hexdigest()
    # Token consumed
    assert await store.lookup(token) is None


async def test_route_unknown_token_404():
    """A1 prelude: lookup miss -> 404."""
    sink = _RecordingSink()
    mcp, _ = await _mount(sink)
    async with await _client(mcp) as c:
        resp = await c.put(
            "/fx/u/does-not-exist", content=b"x", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 404
    assert sink.calls == []
```

- [ ] **Step 4.2: Run — confirm failure.**

```bash
uv run pytest tests/_file_exchange/test_upload_route.py -v
```
Expected: `ImportError: cannot import name 'register_upload_route'`.

- [ ] **Step 4.3: Implement the route in `_upload.py`.**

Add the imports and the registrar. Place this after the helpers (`_content_digest_parse`, `_media_range_matches`) added in Task 3:

```python
import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
from typing import TYPE_CHECKING, cast

from fastmcp_pvl_core._file_exchange._staging import _CHUNK, _write_chunk
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink

logger = logging.getLogger(__name__)


def register_upload_route(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    sink: ArtifactSink,
    config: ServerConfig,
) -> None:
    """Mount ``PUT``/``POST <UPLOAD_PREFIX>/{token}`` on ``mcp``.

    The route serves §12 capability URLs minted by ``upload_receiver_mint``;
    ambient credentials are ignored — the in-URL token is the only
    authorization. ``config.file_exchange_max_artifact_size`` is the operator
    body-size cap; per-mint ``expected.maxSize`` is the smaller of the two when
    set. See the failure-mode matrix for the full per-status-code contract.
    """
    from starlette.responses import Response

    async def _handle(request: Request) -> Response:
        token = request.path_params["token"]
        rec = await token_store.lookup(token)
        if rec is None:
            return Response(status_code=404)
        artifact_id = cast("str", rec.metadata["artifact_id"])
        expected_raw = rec.metadata.get("expected")
        expected = (
            ArtifactConstraints.model_validate(expected_raw)
            if expected_raw is not None
            else None
        )

        content_type = request.headers.get("content-type", "")
        if expected is not None and expected.acceptMimeTypes is not None:
            if not _media_range_matches(content_type, expected.acceptMimeTypes):
                return Response(status_code=415)

        cap_per_mint = expected.maxSize if expected is not None else None
        cap_operator = config.file_exchange_max_artifact_size
        # The smaller-of-two cap is the effective cap; if neither is set, no cap.
        cap: int | None
        if cap_per_mint is None:
            cap = cap_operator
        elif cap_operator is None:
            cap = cap_per_mint
        else:
            cap = min(cap_per_mint, cap_operator)

        # Stage to a transient temp file (hash on the fly).
        try:
            fd, tmp_path = tempfile.mkstemp(prefix="fx-upload-")
        except OSError:
            logger.exception("file-exchange: upload mkstemp failed")
            return Response(status_code=500)
        try:
            try:
                tmp = os.fdopen(fd, "wb")
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(fd)
                raise
            hasher = hashlib.new("sha256")
            received = 0
            too_large = False
            try:
                try:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        if cap is not None and received + len(chunk) > cap:
                            too_large = True
                            break
                        await asyncio.to_thread(_write_chunk, tmp, hasher, chunk)
                        received += len(chunk)
                except OSError:
                    logger.exception("file-exchange: upload temp write failed")
                    return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(tmp.close)

            if too_large:
                return Response(status_code=413)

            # Verify Content-Digest BEFORE the sink sees the bytes
            cd_header = request.headers.get("content-digest")
            if cd_header is not None:
                parsed = _content_digest_parse(cd_header)
                if parsed is None:
                    return Response(status_code=400)
                cd_algo, cd_raw = parsed
                if cd_algo == "sha-256":
                    if hasher.digest() != cd_raw:
                        return Response(status_code=400)
                else:
                    # Re-hash the temp in the declared algorithm.
                    rehash = hashlib.new(_HASHLIB_BY_LABEL[cd_algo])
                    try:
                        with open(tmp_path, "rb") as fh:
                            while True:
                                buf = await asyncio.to_thread(fh.read, _CHUNK)
                                if not buf:
                                    break
                                rehash.update(buf)
                    except OSError:
                        logger.exception(
                            "file-exchange: upload rehash read failed"
                        )
                        return Response(status_code=500)
                    if rehash.digest() != cd_raw:
                        return Response(status_code=400)
            elif expected is not None and expected.requireDigest is not None:
                # require-digest with missing header
                return Response(status_code=400)

            meta = ArtifactMetadata(
                mimeType=content_type or None,
                size=received,
                digest="sha-256:" + hasher.hexdigest(),
            )
            try:
                f = await asyncio.to_thread(open, tmp_path, "rb")
            except OSError:
                logger.exception("file-exchange: upload temp re-open failed")
                return Response(status_code=500)
            try:
                try:
                    await sink.store_artifact(artifact_id, meta, f)
                except Exception:
                    logger.exception("file-exchange: upload sink store_artifact failed")
                    return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(f.close)

            # Single-success-per-URL: consume ONLY after the sink succeeds.
            with contextlib.suppress(Exception):
                await token_store.consume(token)
            return Response(status_code=204)
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, tmp_path)

    mcp.custom_route(f"{UPLOAD_PREFIX}/{{token}}", methods=["PUT", "POST"])(_handle)
```

Also add to the existing imports at the top of `_upload.py`:

```python
from fastmcp_pvl_core._file_exchange._staging import _HASHLIB_BY_LABEL
```

(If it was already added in Task 3, leave it.)

- [ ] **Step 4.4: Run the two happy-path tests — confirm they pass.**

```bash
uv run pytest tests/_file_exchange/test_upload_route.py -v
```
Expected: `test_route_happy_put_deposits_and_consumes` PASS; `test_route_unknown_token_404` PASS.

- [ ] **Step 4.5: Format, lint, type-check, commit.**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_upload.py \
        tests/_file_exchange/test_upload_route.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): upload route happy path + 404 lookup miss (#146)

Mount PUT/POST /fx/u/{token}. The handler streams the body to a temp
file with sha-256 hashing, verifies an optional Content-Digest header
BEFORE the sink sees the bytes (verify-before-use), deposits to the
sink, and consumes the single-use token only after a successful store.
This commit lands the skeleton; edge modes (415/413/400/sink-raises,
fd-leak guards, concurrency) are added one matrix row per commit in
the next task.

Matrix rows A1 (success arm), F4 (no acceptMimeTypes constraint).
Refs #146.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5 — Upload route edge modes (matrix rows A2–A6, B1, B4, B6, C1, C2, D4–D8, F5)

**Goal:** Add one TDD micro-cycle per edge mode. Each adds a test, runs it (fail), adds whatever code change makes it pass, commits.

> Pattern for every micro-cycle below: (a) add the test to `tests/_file_exchange/test_upload_route.py`, (b) run it to confirm a meaningful failure, (c) make the minimal code change in `_upload.py`'s `_handle`, (d) re-run, (e) `ruff/mypy`, (f) commit with a message naming the matrix row.

The route skeleton from Task 4 already covers most edge behaviour structurally (415/413/400 short-circuits, sink-raise → 500, temp unlink in `finally`). The cycles here exist to **prove** that coverage with a test before declaring the row done.

- [ ] **Step 5.1: A1 — second PUT after success returns 404 (test only; no code change expected).**

```python
async def test_route_second_put_after_success_returns_404():
    """A1 ordering: token consumed after first success; second PUT -> 404."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url
    async with await _client(mcp) as c:
        r1 = await c.put(url, content=b"a", headers={"Content-Type": "text/plain"})
        r2 = await c.put(url, content=b"b", headers={"Content-Type": "text/plain"})
    assert r1.status_code == 204
    assert r2.status_code == 404
    assert len(sink.calls) == 1
```

Run → expect PASS (the skeleton already does this). Commit: `test(file-exchange): cover A1 second-PUT-after-success (#146)`.

- [ ] **Step 5.2: A2 — sink raise does NOT consume the token.**

```python
class _RaisingSink:
    def __init__(self) -> None:
        self.attempts = 0

    async def store_artifact(self, artifact_id, metadata, stream):
        self.attempts += 1
        stream.read()
        if self.attempts == 1:
            raise RuntimeError("boom")


async def test_route_sink_raise_does_not_consume_token():
    """A2: sink raises -> 500, token still valid, retry succeeds."""
    sink = _RaisingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url
    async with await _client(mcp) as c:
        r1 = await c.put(url, content=b"a", headers={"Content-Type": "text/plain"})
        r2 = await c.put(url, content=b"b", headers={"Content-Type": "text/plain"})
    assert r1.status_code == 500
    assert r2.status_code == 204
    assert sink.attempts == 2
```

Run → expect PASS (skeleton consumes only after a successful store). Commit.

- [ ] **Step 5.3: A3 — acceptMimeTypes mismatch returns 415, no consume, no sink call.**

```python
async def test_route_accept_mime_mismatch_415():
    """A3: Content-Type not in acceptMimeTypes -> 415, no consume, no sink."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="http://test",
        ttl=120.0,
        expected=ArtifactConstraints(acceptMimeTypes=["application/json"]),
    )
    url = ticket.sinks[0].url
    token = url.rsplit("/", 1)[1]
    async with await _client(mcp) as c:
        resp = await c.put(url, content=b"hi", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert sink.calls == []
    assert await store.lookup(token) is not None  # not consumed
```

Run → expect PASS. Commit.

- [ ] **Step 5.4: A4 — too-large returns 413 (per-mint cap and operator cap).**

```python
async def test_route_too_large_per_mint_cap_413():
    """A4: body exceeds expected.maxSize -> 413, no consume, no sink."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="http://test",
        ttl=120.0,
        expected=ArtifactConstraints(maxSize=4),
    )
    url = ticket.sinks[0].url
    token = url.rsplit("/", 1)[1]
    async with await _client(mcp) as c:
        resp = await c.put(
            url, content=b"too-many-bytes", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 413
    assert sink.calls == []
    assert await store.lookup(token) is not None


async def test_route_too_large_operator_cap_413():
    """A4: body exceeds operator cap -> 413."""
    sink = _RecordingSink()
    cfg = ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=4,
    )
    mcp, store = await _mount(sink, config=cfg)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url
    async with await _client(mcp) as c:
        resp = await c.put(
            url, content=b"too-many", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 413
```

Run → expect PASS. Commit.

- [ ] **Step 5.5: A5 — Content-Digest mismatch returns 400, no consume, no sink.**

```python
async def test_route_content_digest_mismatch_400():
    """A5: wrong Content-Digest -> 400, no sink call (verify-before-use)."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url
    token = url.rsplit("/", 1)[1]
    bad = "sha-256=:" + "A" * 44 + ":"  # wrong digest
    async with await _client(mcp) as c:
        resp = await c.put(
            url,
            content=b"hello",
            headers={"Content-Type": "text/plain", "Content-Digest": bad},
        )
    assert resp.status_code == 400
    assert sink.calls == []
    assert await store.lookup(token) is not None
```

Run → expect PASS. Commit.

- [ ] **Step 5.6: A6, D4, D5 — require-digest missing, unparseable, unsupported algo.**

```python
async def test_route_require_digest_but_missing_header_400():
    """A6: requireDigest set + no Content-Digest -> 400."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="http://test",
        ttl=120.0,
        expected=ArtifactConstraints(requireDigest=["sha-256"]),
    )
    url = ticket.sinks[0].url
    async with await _client(mcp) as c:
        resp = await c.put(url, content=b"hi", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 400
    assert sink.calls == []


async def test_route_content_digest_unparseable_400():
    """D4: present-but-unparseable Content-Digest -> 400."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url
    async with await _client(mcp) as c:
        resp = await c.put(
            url,
            content=b"hi",
            headers={"Content-Type": "text/plain", "Content-Digest": "garbage"},
        )
    assert resp.status_code == 400


async def test_route_content_digest_unsupported_algo_400():
    """D5: unsupported algo label -> 400."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url
    async with await _client(mcp) as c:
        resp = await c.put(
            url,
            content=b"hi",
            headers={
                "Content-Type": "text/plain",
                "Content-Digest": "md5=:YWJjZA==:",
            },
        )
    assert resp.status_code == 400
```

Run → expect PASS. Commit.

- [ ] **Step 5.7: B1 — fd leak between `mkstemp` and the staging `try` (monkey-patch hashlib).**

```python
async def test_route_hashlib_failure_does_not_leak_fd(monkeypatch):
    """B1: simulated post-fdopen pre-staging failure -> fd closed, temp unlinked."""
    import fastmcp_pvl_core._file_exchange._upload as up

    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url

    # Track temp paths created so we can assert they are gone.
    created: list[str] = []
    real_mkstemp = up.tempfile.mkstemp

    def spy_mkstemp(**kw):
        fd, path = real_mkstemp(**kw)
        created.append(path)
        return fd, path

    monkeypatch.setattr(up.tempfile, "mkstemp", spy_mkstemp)

    # Force hashlib.new("sha256") (the route's running hasher) to raise.
    real_hashlib_new = up.hashlib.new

    def boom(name):
        if name == "sha256":
            raise RuntimeError("FIPS")
        return real_hashlib_new(name)

    monkeypatch.setattr(up.hashlib, "new", boom)

    async with await _client(mcp) as c:
        resp = await c.put(
            url, content=b"hi", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 500
    # Temp file must have been unlinked despite the early failure.
    for p in created:
        assert not os.path.exists(p), f"temp leaked: {p}"
```

If this fails (it likely will), restructure the route to move `hasher = hashlib.new("sha256")` **inside** the staging `try:` so the `finally` (tmp.close) and the outer `finally` (os.unlink) both fire. Re-run → PASS. Commit.

- [ ] **Step 5.8: B4 + B6 — sink that reads but doesn't close; temp re-open failure.**

```python
async def test_route_sink_does_not_close_fd_route_closes_it(monkeypatch):
    """B4: route owns the fd handed to the sink and closes it."""
    closes: list[bool] = []

    class _SpySink:
        async def store_artifact(self, artifact_id, metadata, stream):
            stream.read()
            original_close = stream.close

            def tracked():
                closes.append(True)
                original_close()

            stream.close = tracked  # type: ignore[method-assign]

    mcp, store = await _mount(_SpySink())
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url
    async with await _client(mcp) as c:
        resp = await c.put(url, content=b"x", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 204
    assert closes == [True]  # route closed the stream after the sink returned


async def test_route_temp_reopen_failure_500(monkeypatch):
    """B6: open(tmp_path, 'rb') raises -> 500, sink not called, temp unlinked."""
    import fastmcp_pvl_core._file_exchange._upload as up

    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url

    original_open = up.open if hasattr(up, "open") else __builtins__["open"]
    # The route uses `open(tmp_path, "rb")` via the module's namespace —
    # patch builtins.open and key on the prefix.
    import builtins
    real_open = builtins.open

    def fake_open(path, mode="r", *a, **kw):
        if isinstance(path, str) and "fx-upload-" in path and "rb" in mode:
            raise OSError("simulated")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    async with await _client(mcp) as c:
        resp = await c.put(url, content=b"x", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 500
    assert sink.calls == []
```

Run → expect PASS (skeleton already handles both; the spy test asserts the contract). Commit.

- [ ] **Step 5.9: D6, D7, D8 — sink raises `FileExchangeTransferError`; sink raises plain `Exception`; temp write `OSError`.**

```python
class _FxFailSink:
    async def store_artifact(self, artifact_id, metadata, stream):
        from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
        from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
        stream.read()
        raise FileExchangeTransferError(
            TransferErrorCode.TRANSFER_FAILED, transport="upload", detail="x"
        )


async def test_route_sink_raises_file_exchange_error_500():
    """D6."""
    mcp, store = await _mount(_FxFailSink())
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    async with await _client(mcp) as c:
        resp = await c.put(
            ticket.sinks[0].url, content=b"x", headers={"Content-Type": "text/plain"}
        )
    assert resp.status_code == 500
    assert resp.content == b""


async def test_route_temp_write_oserror_500(monkeypatch):
    """D8: OSError mid-stream -> 500, sink not called, temp unlinked."""
    import fastmcp_pvl_core._file_exchange._upload as up

    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )

    def boom(tmp, hasher, chunk):
        raise OSError("disk full")

    monkeypatch.setattr(up, "_write_chunk", boom)
    async with await _client(mcp) as c:
        resp = await c.put(
            ticket.sinks[0].url,
            content=b"hello",
            headers={"Content-Type": "text/plain"},
        )
    assert resp.status_code == 500
    assert sink.calls == []
```

Run → expect PASS. Commit.

- [ ] **Step 5.10: F5 — ambient `Authorization`/`Cookie` headers are ignored.**

```python
async def test_route_ambient_authorization_ignored():
    """F5: ambient Authorization on a valid token is ignored (still 204)."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    async with await _client(mcp) as c:
        resp = await c.put(
            ticket.sinks[0].url,
            content=b"x",
            headers={
                "Content-Type": "text/plain",
                "Authorization": "Bearer fake",
                "Cookie": "session=abc",
            },
        )
    assert resp.status_code == 204
    assert len(sink.calls) == 1


async def test_route_ambient_authorization_on_invalid_token_still_404():
    """F5: ambient Authorization does not rescue an invalid token."""
    sink = _RecordingSink()
    mcp, _ = await _mount(sink)
    async with await _client(mcp) as c:
        resp = await c.put(
            "/fx/u/nope",
            content=b"x",
            headers={
                "Content-Type": "text/plain",
                "Authorization": "Bearer admin",
            },
        )
    assert resp.status_code == 404
```

Run → expect PASS. Commit.

- [ ] **Step 5.11: C1 — two concurrent PUTs, at most one consumes.**

```python
import asyncio as _asyncio


async def test_route_concurrent_puts_at_most_one_consumes():
    """C1: two concurrent PUTs -> at-most-one consume, both may store."""
    sink = _RecordingSink()
    mcp, store = await _mount(sink)
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="http://test", ttl=120.0
    )
    url = ticket.sinks[0].url
    token = url.rsplit("/", 1)[1]
    async with await _client(mcp) as c:
        r1, r2 = await _asyncio.gather(
            c.put(url, content=b"a", headers={"Content-Type": "text/plain"}),
            c.put(url, content=b"b", headers={"Content-Type": "text/plain"}),
        )
    statuses = sorted([r1.status_code, r2.status_code])
    # Either (204, 204) — both raced past lookup before consume — or
    # (204, 404) — second one's lookup landed after consume. Never both 404.
    assert statuses in ([204, 204], [204, 404])
    assert await store.lookup(token) is None  # consumed
```

Run → expect PASS. Commit.

- [ ] **Step 5.12: Re-run the whole route suite + download suite. Format, lint, type-check. Final route commit if any cleanup landed.**

```bash
uv run pytest tests/_file_exchange/test_upload_route.py tests/_file_exchange/test_download.py -v
uv run ruff format .
uv run ruff check .
uv run mypy src
```

**Task 5 PR-shape checkpoint:** Tasks 1–5 plus mint + helpers are ~600 lines of code + tests. The route is now feature-complete for matrix rows A1–A6, B1, B4, B6, C1, D4–D8, F4, F5. **Push and open a draft PR here.** Wait for a clean preflight-circus before adding Tasks 6+. This is the natural decomposition the matrix's work order points at.

---

## Task 6 — `register_file_exchange_routes` cross-transport registrar (matrix rows A7, A8, E2, E3)

**Goal:** Move the public name `register_file_exchange_routes` out of `_download.py` (where it lives on `main`) into a new `_routes.py`, and add the upload-route arm with strict precondition validation.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_routes.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/_download.py` (rename the registrar to `register_download_route`)
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py` (re-export `register_file_exchange_routes` from `_routes`)
- Test: `tests/_file_exchange/test_routes_registrar.py`

- [ ] **Step 6.1: Write the failing tests for the cross-transport registrar.**

```python
# tests/_file_exchange/test_routes_registrar.py
"""Matrix rows A7, A8, E2, E3: register_file_exchange_routes shape."""

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _routes
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store


class _Sink:
    async def store_artifact(self, artifact_id, metadata, stream):  # pragma: no cover
        raise AssertionError


class _Source:
    async def open_artifact(self, key):  # pragma: no cover
        raise AssertionError


def _cfg():
    return ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=1024,
    )


def test_registrar_both_none_raises_value_error():
    """E2: source=None, sink=None is misconfiguration."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    with pytest.raises(ValueError):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, source=None, sink=None, config=cfg
        )


def test_registrar_sink_without_config_raises_value_error():
    """E3: sink requires config (operator size cap)."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    with pytest.raises(ValueError):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, source=None, sink=_Sink(), config=None
        )


def test_registrar_source_only_mounts_download_route():
    """A8: source-only mounts only the download route."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, source=_Source(), sink=None, config=None
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/d") for p in paths)
    assert not any(p.startswith("/fx/u") for p in paths)


def test_registrar_sink_only_mounts_upload_route():
    """A8: sink-only mounts only the upload route."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, source=None, sink=_Sink(), config=cfg
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/u") for p in paths)
    assert not any(p.startswith("/fx/d") for p in paths)


def test_registrar_both_mounts_both():
    """A8: source+sink mounts both routes."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    _routes.register_file_exchange_routes(
        mcp, token_store=store, source=_Source(), sink=_Sink(), config=cfg
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/d") for p in paths)
    assert any(p.startswith("/fx/u") for p in paths)


def test_registrar_precondition_failure_mounts_nothing():
    """A7: ValueError from precondition validation -> no routes mounted."""
    cfg = _cfg()
    store = build_capability_token_store(cfg)
    mcp = FastMCP("t")
    # sink without config: should raise before either route is mounted.
    with pytest.raises(ValueError):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, source=_Source(), sink=_Sink(), config=None
        )
    paths = {r.path for r in mcp.http_app().routes}
    assert not any(p.startswith("/fx/") for p in paths)
```

- [ ] **Step 6.2: Run — confirm failure (module missing).**

```bash
uv run pytest tests/_file_exchange/test_routes_registrar.py -v
```

- [ ] **Step 6.3: Rename in `_download.py`.**

In `src/fastmcp_pvl_core/_file_exchange/_download.py`, rename the function `register_file_exchange_routes` to `register_download_route`. Update its docstring's first sentence to remove the umbrella framing — it is now an internal-to-`_routes` helper:

```python
def register_download_route(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    source: ArtifactSource,
) -> None:
    """Mount the ``download`` GET route on ``mcp`` (called by _routes).
    ...
    """
```

- [ ] **Step 6.4: Create `_routes.py`.**

```python
# src/fastmcp_pvl_core/_file_exchange/_routes.py
"""Cross-transport route registrar (#146).

``register_file_exchange_routes`` is the single public mount point for the
file-exchange data planes. It validates all preconditions before mounting
any route (matrix row A7), then delegates to per-transport registrars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastmcp_pvl_core._file_exchange._download import register_download_route
from fastmcp_pvl_core._file_exchange._upload import register_upload_route

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
    """Mount the file-exchange data-plane routes on ``mcp``.

    Mounts the ``download`` GET route iff ``source`` is given, and the
    ``upload`` PUT/POST route iff ``sink`` is given — supporting
    download-only, upload-only, or both. Mounting the upload route requires
    ``config`` (the operator body-size cap is load-bearing for §15 untrusted
    bytes). All preconditions are validated **before** any route is mounted
    (matrix row A7).
    """
    if source is None and sink is None:
        raise ValueError(
            "register_file_exchange_routes: at least one of source/sink "
            "must be given"
        )
    if sink is not None and config is None:
        raise ValueError(
            "register_file_exchange_routes: sink requires config for "
            "file_exchange_max_artifact_size"
        )
    # Preconditions validated; safe to mount.
    if source is not None:
        register_download_route(mcp, token_store=token_store, source=source)
    if sink is not None:
        register_upload_route(
            mcp,
            token_store=token_store,
            sink=sink,
            config=cast("ServerConfig", config),
        )
```

- [ ] **Step 6.5: Update `__init__.py` re-export.**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, change the `_download` import line:

```python
from fastmcp_pvl_core._file_exchange._download import (
    download_fetcher_consume,
    download_provider_mint,
)
```

(remove `register_file_exchange_routes` from this block.) Then add:

```python
from fastmcp_pvl_core._file_exchange._routes import register_file_exchange_routes
from fastmcp_pvl_core._file_exchange._upload import (
    upload_receiver_mint,
)
```

Update `__all__` alphabetically to include `upload_receiver_mint` (and `upload_sender_consume` once Task 7 lands).

- [ ] **Step 6.6: Update `file_exchange.py` re-export the same way.**

Add `upload_receiver_mint` to the imports and to `__all__` (alphabetically).

- [ ] **Step 6.7: Update `tests/_file_exchange/test_download.py` if it references `_download.register_file_exchange_routes`.**

Grep first:

```bash
grep -n "register_file_exchange_routes\|_download\." tests/_file_exchange/test_download.py tests/_file_exchange/test_download_e2e.py
```

Replace any local-import call sites with `from fastmcp_pvl_core._file_exchange._routes import register_file_exchange_routes` (or use the public `file_exchange.register_file_exchange_routes`). The `register_download_route` rename is internal — only update test references if they import the inner name. The download e2e test that previously called `register_file_exchange_routes(..., source=...)` continues to work because the new registrar accepts source-only.

- [ ] **Step 6.8: Run the full file-exchange suite — confirm pass.**

```bash
uv run pytest tests/_file_exchange -v
```

- [ ] **Step 6.9: Format, lint, type-check, commit.**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
git add -A
git commit -m "$(cat <<'EOF'
feat(file-exchange): cross-transport register_file_exchange_routes (#146)

Move the public registrar from _download.py to a new _routes.py and
add the upload-route arm. Preconditions (source-or-sink, sink-needs-
config) are validated BEFORE any route is mounted (matrix row A7).
_download.py's inner helper is renamed to register_download_route;
the public name register_file_exchange_routes is unchanged.

Matrix rows A7, A8, E2, E3. Refs #146.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7 — `upload_sender_consume` (matrix rows B2, B3, B5, C3, D1, D2, D3, D9, F6)

**Goal:** Stage the source bytes to a temp (hash on the fly), then `PUT` the temp through the SSRF guard with `Content-Type`, `Content-Length`, and `Content-Digest` headers. Non-2xx → `transfer-failed`; guard refusal propagates.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_upload.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py` + `src/fastmcp_pvl_core/file_exchange.py` (add `upload_sender_consume` to imports and `__all__`)
- Test: `tests/_file_exchange/test_upload_sender.py`

- [ ] **Step 7.1: Write the failing happy-path test.**

```python
# tests/_file_exchange/test_upload_sender.py
"""Matrix rows B2, B3, B5, C3, D1, D2, D3, D9, F6: upload_sender_consume."""

import base64
import contextlib
import hashlib
import os
from typing import BinaryIO

import pytest

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactMetadata,
    UploadSink,
)
from datetime import datetime, timezone


def _sink() -> UploadSink:
    return UploadSink(
        transport="upload",
        url="https://b.example/fx/u/tok",
        method="PUT",
        expiresAt=datetime.now(timezone.utc),
    )


def _cfg():
    return ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_http_timeout=30.0,
    )


class _StreamSource:
    """ArtifactSource that yields a fixed payload."""

    def __init__(self, payload: bytes, mime: str | None = "text/plain"):
        self.payload = payload
        self.mime = mime

    async def open_artifact(self, key):
        import io
        return io.BytesIO(self.payload), ArtifactMetadata(mimeType=self.mime)


@contextlib.asynccontextmanager
async def _fake_guarded_stream_factory(captured: dict, status: int = 204):
    """Patch guarded_stream so we capture headers/body and return a status."""

    class _Resp:
        def __init__(self, st):
            self.status = st

    @contextlib.asynccontextmanager
    async def fake(method, url, *, config, transport, headers=None, content=None):
        captured["method"] = method
        captured["url"] = url
        captured["transport"] = transport
        captured["headers"] = dict(headers or {})
        # Drain the content iterator (the sender passes an async iterator).
        body = b""
        if content is not None:
            async for chunk in content:
                body += chunk
        captured["body"] = body
        yield _Resp(status)

    yield fake


async def test_sender_happy_path_puts_with_headers(monkeypatch):
    """F6 + D1 success arm."""
    captured: dict = {}
    async with _fake_guarded_stream_factory(captured, status=204) as fake:
        monkeypatch.setattr(_upload, "guarded_stream", fake)
        await _upload.upload_sender_consume(
            _sink(),
            _StreamSource(b"hello world"),
            "art-1",
            config=_cfg(),
        )
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://b.example/fx/u/tok"
    assert captured["transport"] == "upload"
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["content-type"] == "text/plain"
    assert headers["content-length"] == str(len(b"hello world"))
    raw = hashlib.sha256(b"hello world").digest()
    assert headers["content-digest"] == "sha-256=:" + base64.b64encode(raw).decode() + ":"
    assert captured["body"] == b"hello world"
    # No stray fx-upload temp files
    assert not any(n.startswith("fx-upload-") for n in os.listdir("/tmp"))


async def test_sender_non_2xx_maps_to_transfer_failed(monkeypatch):
    """D1."""
    captured: dict = {}
    async with _fake_guarded_stream_factory(captured, status=500) as fake:
        monkeypatch.setattr(_upload, "guarded_stream", fake)
        with pytest.raises(FileExchangeTransferError) as ei:
            await _upload.upload_sender_consume(
                _sink(), _StreamSource(b"x"), "art-1", config=_cfg()
            )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
    assert ei.value.transport == "upload"


async def test_sender_guard_refusal_propagates(monkeypatch):
    """C3."""

    @contextlib.asynccontextmanager
    async def refusing(method, url, **kw):
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE, transport="upload", detail="blocked"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(_upload, "guarded_stream", refusing)
    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(
            _sink(), _StreamSource(b"x"), "art-1", config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_sender_source_raises_propagates_no_temp_leak():
    """D2."""
    class _Boom:
        async def open_artifact(self, key):
            raise RuntimeError("hook failure")

    with pytest.raises(RuntimeError):
        await _upload.upload_sender_consume(
            _sink(), _Boom(), "art-1", config=_cfg()
        )
    assert not any(n.startswith("fx-upload-") for n in os.listdir("/tmp"))


async def test_sender_mkstemp_failure_maps_to_transfer_failed(monkeypatch):
    """D3."""
    def boom(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(_upload.tempfile, "mkstemp", boom)
    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(
            _sink(), _StreamSource(b"x"), "art-1", config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED


async def test_sender_closes_source_stream():
    """B5: source stream closed on success."""
    class _TrackedBytes:
        def __init__(self, data: bytes):
            self._buf = bytearray(data)
            self.closed = False

        def read(self, n=-1):
            if n is None or n < 0:
                data, self._buf = bytes(self._buf), bytearray()
                return data
            data = bytes(self._buf[:n])
            del self._buf[:n]
            return data

        def close(self):
            self.closed = True

    tracked = _TrackedBytes(b"hello")

    class _S:
        async def open_artifact(self, key):
            return tracked, ArtifactMetadata(mimeType="text/plain")

    captured: dict = {}

    @contextlib.asynccontextmanager
    async def fake(method, url, **kw):
        async for _ in kw.get("content") or []:
            pass

        class R:
            status = 204
        yield R()

    import contextlib as _ctx
    async with _fake_guarded_stream_factory(captured, status=204) as fake_gs:
        import builtins
        monkey = pytest.MonkeyPatch()
        monkey.setattr(_upload, "guarded_stream", fake_gs)
        try:
            await _upload.upload_sender_consume(
                _sink(), _S(), "art-1", config=_cfg()
            )
        finally:
            monkey.undo()
    assert tracked.closed is True
```

- [ ] **Step 7.2: Run — confirm failure.**

```bash
uv run pytest tests/_file_exchange/test_upload_sender.py -v
```
Expected: `AttributeError: module 'fastmcp_pvl_core._file_exchange._upload' has no attribute 'upload_sender_consume'`.

- [ ] **Step 7.3: Implement `upload_sender_consume` in `_upload.py`.**

Add to `_upload.py` (re-using the temp-file primitives from `_staging` and `_outbound.guarded_stream`):

```python
# add near the other imports:
from collections.abc import AsyncIterator
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._outbound import guarded_stream

if TYPE_CHECKING:
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSource


async def upload_sender_consume(
    sink: UploadSink,
    source: ArtifactSource,
    key: str,
    *,
    config: ServerConfig,
) -> None:
    """Sender role (push): stage ``source[key]`` and PUT it to ``sink.url``.

    Streams the source bytes through a transient temp file (hashing on the
    fly), then sends the temp through the #147 SSRF guard with
    ``Content-Type``/``Content-Length``/``Content-Digest`` headers. Non-2xx
    -> ``transfer-failed``; guard refusals propagate verbatim. The temp is
    deleted on every path (matrix rows B2, B3, B5, C3, D1, D2, D3, D9, F6).
    """
    stream, metadata = await source.open_artifact(key)
    try:
        try:
            fd, tmp_path = tempfile.mkstemp(prefix="fx-upload-")
        except OSError as exc:
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
            try:
                hasher = hashlib.new("sha256")
                size = 0
                while True:
                    chunk = await asyncio.to_thread(stream.read, _CHUNK)
                    if not chunk:
                        break
                    try:
                        await asyncio.to_thread(_write_chunk, tmp, hasher, chunk)
                    except OSError as exc:
                        raise FileExchangeTransferError(
                            TransferErrorCode.TRANSFER_FAILED,
                            transport="upload",
                            detail="failed to stage the artifact",
                        ) from exc
                    size += len(chunk)
                try:
                    await asyncio.to_thread(tmp.flush)
                except OSError as exc:
                    raise FileExchangeTransferError(
                        TransferErrorCode.TRANSFER_FAILED,
                        transport="upload",
                        detail="failed to flush the staged artifact",
                    ) from exc
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(tmp.close)

            cd_header = _content_digest_format("sha-256", hasher.digest())
            headers: dict[str, str] = {
                "Content-Length": str(size),
                "Content-Digest": cd_header,
            }
            if metadata.mimeType:
                headers["Content-Type"] = metadata.mimeType

            async def _body() -> AsyncIterator[bytes]:
                f = await asyncio.to_thread(open, tmp_path, "rb")
                try:
                    while True:
                        chunk = await asyncio.to_thread(f.read, _CHUNK)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(f.close)

            async with guarded_stream(
                sink.method,
                sink.url,
                config=config,
                transport="upload",
                headers=headers,
                content=_body(),
            ) as resp:
                if not (200 <= resp.status < 300):
                    raise FileExchangeTransferError(
                        TransferErrorCode.TRANSFER_FAILED,
                        transport="upload",
                        detail="unexpected upload response status",
                    )
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, tmp_path)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.close)
```

- [ ] **Step 7.4: Run the sender tests — confirm pass.**

```bash
uv run pytest tests/_file_exchange/test_upload_sender.py -v
```

If any individual test fails, add the smallest possible fix and re-run; commit after each clean cycle. **Do not** add code that no test exercises.

- [ ] **Step 7.5: Add `upload_sender_consume` to public re-exports.**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, the `_upload` import block becomes:

```python
from fastmcp_pvl_core._file_exchange._upload import (
    upload_receiver_mint,
    upload_sender_consume,
)
```

Update `__all__` alphabetically. Do the same in `src/fastmcp_pvl_core/file_exchange.py`.

- [ ] **Step 7.6: Full suite + lint + type-check + commit.**

```bash
uv run pytest tests/_file_exchange -v
uv run ruff format .
uv run ruff check .
uv run mypy src
git add -A
git commit -m "$(cat <<'EOF'
feat(file-exchange): upload_sender_consume (#146)

Stage the source bytes to a transient temp file (hashing on the fly),
then PUT the temp through guarded_stream with Content-Type / Length /
Digest headers. Non-2xx maps to transfer-failed; guard refusals
propagate as not-accessible; mkstemp / write / flush OSErrors map to
transfer-failed; the temp is deleted on every path.

Matrix rows B2, B3, B5, C3, D1, D2, D3, D9, F6. Refs #146.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8 — End-to-end push test (matrix row implicit; ties together A1+B+C+D+F across two servers)

**Goal:** Two pvl-core-built mock servers — Server B (receiver) mints + serves; Server A (sender) selects + sends — push transport only.

**Files:**
- Test: `tests/_file_exchange/test_upload_e2e.py`

- [ ] **Step 8.1: Write the e2e test.**

```python
# tests/_file_exchange/test_upload_e2e.py
"""End-to-end push: sender + guard + receiver route + sink."""

import hashlib
from typing import BinaryIO

import httpx
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._routes import register_file_exchange_routes
from fastmcp_pvl_core._file_exchange._selection import select_sink
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _Sink:
    def __init__(self):
        self.received: tuple[str | None, ArtifactMetadata, bytes] | None = None

    async def store_artifact(self, artifact_id, metadata, stream: BinaryIO):
        self.received = (artifact_id, metadata, stream.read())


class _Src:
    def __init__(self, data: bytes):
        self.data = data

    async def open_artifact(self, key):
        import io
        return io.BytesIO(self.data), ArtifactMetadata(mimeType="application/json")


async def test_e2e_push_two_servers(monkeypatch):
    payload = b'{"hello":"world"}'

    # Receiver (server B)
    cfg_b = ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=1024,
    )
    store_b = build_capability_token_store(cfg_b)
    sink_b = _Sink()
    mcp_b = FastMCP("B")
    register_file_exchange_routes(
        mcp_b, token_store=store_b, sink=sink_b, config=cfg_b
    )

    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store_b, base_url="http://b.test", ttl=120.0
    )

    # Sender (server A): patch guarded_stream so the request lands on B's ASGI
    # rather than the network. The real guard's behaviour is exercised in
    # test_outbound.py; here we are testing the e2e wiring.
    import contextlib

    transport_b = httpx.ASGITransport(app=mcp_b.http_app())
    client_b = httpx.AsyncClient(transport=transport_b, base_url="http://b.test")

    @contextlib.asynccontextmanager
    async def fake_guarded_stream(method, url, *, config, transport, headers=None, content=None):
        assert transport == "upload"
        # Build body
        body = b""
        if content is not None:
            async for chunk in content:
                body += chunk
        resp = await client_b.request(
            method, url, headers=headers, content=body
        )

        class R:
            status = resp.status_code
        yield R()

    monkeypatch.setattr(_upload, "guarded_stream", fake_guarded_stream)

    cfg_a = ServerConfig(
        kv_store_url="memory://",
        file_exchange_http_timeout=30.0,
    )
    selected = select_sink(ticket)
    await _upload.upload_sender_consume(selected, _Src(payload), "art-1", config=cfg_a)
    await client_b.aclose()

    assert sink_b.received is not None
    aid, meta, body = sink_b.received
    assert aid == "art-1"
    assert body == payload
    assert meta.size == len(payload)
    assert meta.digest == "sha-256:" + hashlib.sha256(payload).hexdigest()
    # Token consumed on B
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    assert await store_b.lookup(token) is None
```

- [ ] **Step 8.2: Run — fix any wiring issues, commit.**

```bash
uv run pytest tests/_file_exchange/test_upload_e2e.py -v
```

If `select_sink` doesn't pick the upload sink because no `expected_role` is set or similar, follow its existing pattern (see `tests/_file_exchange/test_selection.py` and `_filesystem.py`'s caller) and adjust the test to the same calling convention.

- [ ] **Step 8.3: Full suite + lint + mypy + commit.**

```bash
uv run pytest -v
uv run ruff format .
uv run ruff check .
uv run mypy src
git add -A
git commit -m "$(cat <<'EOF'
test(file-exchange): end-to-end push two-server upload (#146)

Two FastMCP-built mock servers wired through ASGITransport: receiver
mints + serves the upload route; sender selects + sends through a
guarded_stream patched to land on the receiver's ASGI app. Bytes,
metadata, digest, and single-success consume verified.

Refs #146.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9 — Multi-Python sanity + push

**Goal:** Match CI's matrix (3.10, 3.11, 3.12, 3.13) locally before pushing, per the saved feedback `feedback_test_python_version_matrix`.

- [ ] **Step 9.1: Run the suite on min and max Python.**

```bash
uv run --python 3.10 pytest tests/_file_exchange -v
uv run --python 3.13 pytest tests/_file_exchange -v
```

If either fails, fix the version-dependent code (often: `typing` syntax, `Path.resolve(strict=)` symlink behaviour, stdlib `hashlib` availability). Commit each fix individually.

- [ ] **Step 9.2: Run the local preflight-circus.**

Invoke the `preflight-circus` skill against the diff (`main..HEAD`). Address findings scoring ≥80 per the saved calibration (`feedback_circus_scoring_calibration`); push at the first clean round (`feedback_circus_push_at_first_clean`).

- [ ] **Step 9.3: Push and open the PR.**

```bash
git push -u origin feat/146-upload-data-plane-v2
gh pr create --title "feat(file-exchange): upload transport data plane (#146)" \
  --body "$(cat <<'EOF'
## Summary

Implements the `upload` transport's push data plane as the mirror of
the merged #145 download data plane:

- `upload_receiver_mint` — token mint + IntakeTicket
- `register_upload_route` — PUT/POST `/fx/u/{token}` with body cap,
  RFC 7231 media-range, RFC 9530 Content-Digest verify-before-use,
  single-success-per-URL via atomic consume
- `upload_sender_consume` — stage + PUT through the SSRF guard
- `register_file_exchange_routes` — moved to `_routes.py`, now mounts
  download and/or upload based on source/sink presence with strict
  precondition validation

This is the fourth attempt after #163, #164, #165 each abandoned mid
bot-iteration. The new discipline: the failure-mode matrix at
`docs/superpowers/specs/2026-05-27-file-exchange-146-failure-modes.md`
enumerates every ordering / lifecycle / concurrency / failure-path /
reentrancy / wire-format mode the implementation must address, with
one test per row, written **before** the implementation. The matrix's
seeded rows include every finding the three prior PRs' bot rounds
surfaced.

Closes #146.

## Test plan

- [x] `uv run pytest tests/_file_exchange -v` on 3.10 and 3.13
- [x] `uv run ruff format --check . && uv run ruff check .`
- [x] `uv run mypy src`
- [x] Local preflight-circus clean at ≥80

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" --draft
```

---

## Self-review

**Spec coverage:**

| Spec section | Tasks |
|---|---|
| `upload_receiver_mint` signature + behaviour | Task 2 |
| Upload route (`PUT`/`POST /fx/u/{token}`): lookup → ACCEPT → cap → stage → verify → sink → consume | Tasks 4–5 |
| Token consumed only after successful store | Tasks 4 (skeleton), 5.1, 5.2 |
| `acceptMimeTypes` RFC 7231 | Tasks 3 (helper), 5.3 |
| `Content-Digest` RFC 9530 parse/format/verify | Tasks 3 (helpers), 4 (route uses it), 5.5, 5.6 |
| Body-size caps (per-mint + operator) | Task 5.4 |
| 404/415/413/400/500/204 status mapping | Tasks 4, 5.* |
| `upload_sender_consume` happy + edges | Task 7 |
| `register_file_exchange_routes` cross-transport shape | Task 6 |
| `_staging.py` extraction | Task 1 |
| Public re-exports + `__all__` | Tasks 6, 7 |
| End-to-end push test | Task 8 |
| Multi-Python + circus + push | Task 9 |

**Placeholder scan:** No "TBD"/"TODO"/"similar to" — every step has its code block, test, or command. The conditional language in Task 5 ("expect PASS — skeleton already does this") is intentional: that's how TDD verifies the skeleton is honest about what it claims to do.

**Type consistency:**

- `register_upload_route(mcp, *, token_store, sink, config)` — same signature used in Tasks 4, 5, 6, 8.
- `upload_receiver_mint(artifact_id, *, token_store, base_url, ttl, expected=None, method="PUT")` — same signature used in Tasks 2, 4–8.
- `upload_sender_consume(sink, source, key, *, config)` — same signature in Tasks 7, 8.
- `register_file_exchange_routes(mcp, *, token_store, source=None, sink=None, config=None)` — same signature in Tasks 6, 8.

All names match across tasks.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-27-file-exchange-146-upload-data-plane-v4.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
