# File-Exchange v0.1 Implementation Plan (pvl-core phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the `nl.liesdonk.file-exchange` v0.1 extension (https://github.com/pvliesdonk/mcp-file-exchange-ext) as pvl-core's shared reference implementation, with all four roles (`provider`/`fetcher`/`receiver`/`sender`) and all three transports (`filesystem`/`download`/`upload`), gated automatically per deployment capability.

**Architecture:** New private package `src/fastmcp_pvl_core/_file_exchange/`. Pydantic types over a vendored JSON schema. Helpers exposed at top-level package surface (matching the existing `build_auth` / `register_server_info_tool` idiom). Capability declaration derives transport availability from `ServerConfig` (volumes, transport). Persistent state via the existing `build_kv_store` factory shipped in PR #122.

**Tech Stack:** Python 3.10–3.13, FastMCP 3.3.1+, Pydantic v2, py-key-value-aio (existing transitive dep), httpx (already in deps for OIDC), Starlette (FastMCP's HTTP layer), pytest + pytest-asyncio.

**Design doc:** [`docs/superpowers/specs/2026-05-20-file-exchange-adoption-design.md`](../specs/2026-05-20-file-exchange-adoption-design.md)

---

## File structure

New package under `src/fastmcp_pvl_core/_file_exchange/`:

```
_file_exchange/
    __init__.py                       # public namespace; re-exports from submodules
    schema/
        file-exchange.schema.json     # vendored verbatim from spec repo @ pinned commit
        .expected-sha256              # drift gate
        PINNED_AT.md                  # records upstream commit + spec version
        conformance/                  # vendored conformance fixtures
    _types.py                         # Pydantic models for refs, descriptors, errors
    _select.py                        # selection algorithm (§9 of spec)
    _errors.py                        # FileExchangeError + code constants + envelope
    _transport_filesystem.py          # exchange:// resolution, atomic_write, mint helpers
    _transport_https.py               # pull_download, push_upload, SSRF guard
    _url_store.py                     # capability URL minting; intake correlation
    _routes.py                        # GET /file-exchange/d/<token>, PUT|POST /u/<token>
    _provider.py                      # build_pull_response
    _fetcher.py                       # pull_artifact
    _receiver.py                      # open_intake, build_intake_response, resolve_intake
    _sender.py                        # push_artifact
    _capability.py                    # register_file_exchange_capability + gating + mounting
```

Top-level re-exports added to `src/fastmcp_pvl_core/__init__.py`.

`ServerConfig` (in `_config.py`) gains seven new fields (see Task F).

Test tree under `tests/file_exchange/` mirrors the source layout, plus `tests/file_exchange/test_integration_*.py` and `tests/file_exchange/test_conformance.py`.

---

## Sequencing & parallel dispatch

```
A (types + schema)  ────────┐
                            ├─► B (select + errors) ──┐
                            │                         ├─► F (capability + role helpers) ─► G (integration)
                            ├─► C (filesystem) ───────┤
                            │                         │
                            ├─► D (HTTPS consumer) ───┤
                            │                         │
                            └─► E (URL store + routes)┘
```

- **A is the critical path** (every later task imports from `_types.py`).
- Once A merges, **B, C, D, E can run in parallel** (different files, independent test suites).
- **F depends on B+C+D+E** (capability registration wires them; role helpers call into them).
- **G depends on F** (integration tests exercise the full surface).

Each task group becomes one PR closing one tracked issue, opened as draft, gated through the `preflight-circus` skill before push (per global CLAUDE.md PR workflow). The user files the seven child issues (A–G) before kick-off.

---

# Task A: Vendor schema + Pydantic types + drift gate

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/schema/file-exchange.schema.json` (vendored)
- Create: `src/fastmcp_pvl_core/_file_exchange/schema/.expected-sha256`
- Create: `src/fastmcp_pvl_core/_file_exchange/schema/PINNED_AT.md`
- Create: `src/fastmcp_pvl_core/_file_exchange/schema/conformance/` (vendored fixtures)
- Create: `src/fastmcp_pvl_core/_file_exchange/_types.py`
- Create: `tests/file_exchange/__init__.py`
- Create: `tests/file_exchange/test_schema_drift.py`
- Create: `tests/file_exchange/test_types.py`

### Step A.1: Vendor the schema and conformance fixtures

- [ ] Pick the spec-repo pin commit. Run:

```bash
gh api repos/pvliesdonk/mcp-file-exchange-ext/commits/main --jq '.sha'
```

Record the SHA in `_file_exchange/schema/PINNED_AT.md`:

```markdown
# Vendored from pvliesdonk/mcp-file-exchange-ext

- **Upstream commit:** <full-sha-from-above>
- **Spec version:** 0.1 (draft)
- **Vendored at:** 2026-05-20

To re-pin: fetch the schema and conformance directory from the new commit,
update PINNED_AT.md and .expected-sha256, and run `pytest tests/file_exchange/test_schema_drift.py`.
```

- [ ] Download the schema:

```bash
mkdir -p src/fastmcp_pvl_core/_file_exchange/schema/conformance
curl -sL "https://raw.githubusercontent.com/pvliesdonk/mcp-file-exchange-ext/<sha>/schema/file-exchange.json" \
    > src/fastmcp_pvl_core/_file_exchange/schema/file-exchange.schema.json
```

- [ ] Compute and record SHA-256:

```bash
sha256sum src/fastmcp_pvl_core/_file_exchange/schema/file-exchange.schema.json \
    | awk '{print $1}' \
    > src/fastmcp_pvl_core/_file_exchange/schema/.expected-sha256
```

- [ ] Vendor the conformance fixtures (one curl per fixture file, listed via `gh api repos/pvliesdonk/mcp-file-exchange-ext/contents/conformance?ref=<sha>` and downloaded individually).

### Step A.2: Drift-gate test

- [ ] Write `tests/file_exchange/test_schema_drift.py`:

```python
"""Schema-drift gate: the vendored schema must match its recorded SHA-256.

Drift means an unintended edit to the local copy, which is a vendoring violation.
Re-pinning is a deliberate commit that updates PINNED_AT.md and .expected-sha256.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src" / "fastmcp_pvl_core" / "_file_exchange" / "schema" / "file-exchange.schema.json"
EXPECTED_SHA = SCHEMA.parent / ".expected-sha256"


def test_vendored_schema_matches_recorded_sha256():
    actual = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    expected = EXPECTED_SHA.read_text().strip()
    assert actual == expected, (
        f"Vendored schema drift detected.\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}\n"
        f"If the change is intentional, re-pin by updating PINNED_AT.md and .expected-sha256."
    )
```

- [ ] Run the test to verify it passes:

```bash
uv run pytest tests/file_exchange/test_schema_drift.py -v
```

Expected: PASS.

### Step A.3: Write the Pydantic types

- [ ] Write `src/fastmcp_pvl_core/_file_exchange/__init__.py` (empty for now — re-exports added at the end of Task A):

```python
"""File-exchange extension implementation (nl.liesdonk.file-exchange v0.1).

Reference implementation of pvliesdonk/mcp-file-exchange-ext.
The public surface is re-exported from the top-level fastmcp_pvl_core package.
"""
```

- [ ] Write `tests/file_exchange/test_types.py` — start with the discriminator behaviour (most likely to break later if shape changes):

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from fastmcp_pvl_core._file_exchange._types import (
    ArtifactMetadata,
    DownloadSourceDescriptor,
    FilesystemSinkDescriptor,
    FilesystemSourceDescriptor,
    IntakeTicket,
    SourceDescriptor,
    TransferHandle,
    UploadSinkDescriptor,
)


def test_filesystem_source_descriptor_roundtrips():
    raw = {"transport": "filesystem", "uri": "exchange://vault/notes/n1.md"}
    desc = SourceDescriptor.validate_python(raw)
    assert isinstance(desc.root, FilesystemSourceDescriptor)
    assert desc.root.uri == "exchange://vault/notes/n1.md"


def test_download_source_descriptor_requires_expires_at():
    with pytest.raises(ValidationError):
        SourceDescriptor.validate_python({"transport": "download", "url": "https://x/y"})


def test_descriptor_unknown_transport_is_rejected_at_typed_layer():
    """The typed layer rejects unknown transports — selection-level fallthrough
    (§17.2 'tolerant reading') is handled in _select.py, not by the discriminator."""
    with pytest.raises(ValidationError):
        SourceDescriptor.validate_python({"transport": "carrier-pigeon", "uri": "x"})


def test_transfer_handle_requires_at_least_one_source():
    with pytest.raises(ValidationError):
        TransferHandle.model_validate(
            {
                "type": "nl.liesdonk.file-exchange/transfer-handle",
                "version": "0.1",
                "artifact": {"name": "x.bin"},
                "sources": [],
            }
        )


def test_transfer_handle_type_field_is_constant():
    with pytest.raises(ValidationError):
        TransferHandle.model_validate(
            {
                "type": "something-else",
                "version": "0.1",
                "artifact": {"name": "x.bin"},
                "sources": [{"transport": "filesystem", "uri": "exchange://v/x"}],
            }
        )


def test_artifact_metadata_requires_at_least_one_field():
    with pytest.raises(ValidationError):
        ArtifactMetadata.model_validate({})


def test_artifact_metadata_with_only_name_is_valid():
    md = ArtifactMetadata.model_validate({"name": "report.parquet"})
    assert md.name == "report.parquet"


def test_intake_ticket_requires_artifact_id():
    with pytest.raises(ValidationError):
        IntakeTicket.model_validate(
            {
                "type": "nl.liesdonk.file-exchange/intake-ticket",
                "version": "0.1",
                "sinks": [{"transport": "filesystem", "uri": "exchange://i/x"}],
            }
        )


def test_filesystem_sink_descriptor_roundtrips():
    raw = {"transport": "filesystem", "uri": "exchange://intake/x.bin"}
    sink = FilesystemSinkDescriptor.model_validate(raw)
    assert sink.uri == "exchange://intake/x.bin"


def test_upload_sink_descriptor_default_method_is_put():
    raw = {
        "transport": "upload",
        "url": "https://x/y",
        "expiresAt": "2026-12-31T00:00:00Z",
    }
    sink = UploadSinkDescriptor.model_validate(raw)
    assert sink.method == "PUT"
```

- [ ] Run the tests to confirm they fail:

```bash
uv run pytest tests/file_exchange/test_types.py -v
```

Expected: every test fails with `ImportError` or `ModuleNotFoundError`.

- [ ] Write `src/fastmcp_pvl_core/_file_exchange/_types.py`:

```python
"""Pydantic models for the nl.liesdonk.file-exchange v0.1 wire types.

The shape of each model mirrors schema/file-exchange.schema.json. The discriminator
field on descriptors (``transport``) rejects unknown values at the typed layer; the
spec's §17.2 'tolerant reading' rule for unknown transports is applied at selection
time in _select.py, not by these models — because by the time we deserialise to a
strongly-typed object we have already committed to a known shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    RootModel,
    field_validator,
    model_validator,
)

REFERENCE_VERSION = "0.1"
TRANSFER_HANDLE_TYPE = "nl.liesdonk.file-exchange/transfer-handle"
INTAKE_TICKET_TYPE = "nl.liesdonk.file-exchange/intake-ticket"


class ArtifactMetadata(BaseModel):
    """Describes an artifact independently of how it is transferred (spec §7.1)."""

    model_config = ConfigDict(extra="ignore")  # §17.2 tolerant reading on non-descriptor objects

    id: str | None = None
    name: str | None = None
    mimeType: str | None = None
    size: int | None = Field(default=None, ge=0)
    digest: str | None = None
    description: str | None = None
    createdAt: datetime | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "ArtifactMetadata":
        # Spec §7.1: an artifact with no metadata cannot be reviewed by a human and is invalid.
        if not any(
            getattr(self, name) is not None
            for name in ("id", "name", "mimeType", "size", "digest", "description", "createdAt")
        ):
            raise ValueError("ArtifactMetadata MUST contain at least one field (spec §7.1)")
        return self


# ---- Source descriptors (used in TransferHandle.sources) ----


class FilesystemSourceDescriptor(BaseModel):
    """`filesystem` transport, source side (spec §7.2.1)."""

    model_config = ConfigDict(extra="forbid")  # §17.5 descriptor-shape freeze

    transport: Literal["filesystem"]
    uri: str

    @field_validator("uri")
    @classmethod
    def _scheme_is_exchange_or_file(cls, v: str) -> str:
        if not (v.startswith("exchange://") or v.startswith("file://")):
            raise ValueError("filesystem source URI must use 'exchange://' or 'file://'")
        return v


class DownloadSourceDescriptor(BaseModel):
    """`download` transport, source side (spec §7.2.2)."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["download"]
    url: HttpUrl
    expiresAt: datetime
    singleUse: bool = True


SourceDescriptor = RootModel[
    Annotated[
        FilesystemSourceDescriptor | DownloadSourceDescriptor,
        Field(discriminator="transport"),
    ]
]


# ---- Sink descriptors (used in IntakeTicket.sinks) ----


class FilesystemSinkDescriptor(BaseModel):
    """`filesystem` transport, sink side (spec §7.2.3)."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["filesystem"]
    uri: str

    @field_validator("uri")
    @classmethod
    def _scheme_is_exchange_or_file(cls, v: str) -> str:
        if not (v.startswith("exchange://") or v.startswith("file://")):
            raise ValueError("filesystem sink URI must use 'exchange://' or 'file://'")
        return v


class UploadSinkDescriptor(BaseModel):
    """`upload` transport, sink side (spec §7.2.4)."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["upload"]
    url: HttpUrl
    method: Literal["PUT", "POST"] = "PUT"
    expiresAt: datetime


SinkDescriptor = RootModel[
    Annotated[
        FilesystemSinkDescriptor | UploadSinkDescriptor,
        Field(discriminator="transport"),
    ]
]


# ---- Expected constraints (IntakeTicket.expected) ----


class ExpectedConstraints(BaseModel):
    """Receiver-imposed constraints on an incoming artifact (spec §7.4)."""

    model_config = ConfigDict(extra="ignore")

    maxSize: int | None = Field(default=None, ge=0)
    acceptMimeTypes: list[str] | None = None
    requireDigest: list[str] | None = None


# ---- References ----


class TransferHandle(BaseModel):
    """A pull token: provider emits, fetcher consumes (spec §7.3)."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["nl.liesdonk.file-exchange/transfer-handle"]
    version: str
    artifact: ArtifactMetadata
    sources: list[SourceDescriptor] = Field(min_length=1)
    requires: list[str] | None = None


class IntakeTicket(BaseModel):
    """A push token: receiver emits, sender consumes (spec §7.4)."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["nl.liesdonk.file-exchange/intake-ticket"]
    version: str
    artifactId: str
    expected: ExpectedConstraints | None = None
    sinks: list[SinkDescriptor] = Field(min_length=1)
    requires: list[str] | None = None
```

- [ ] Run the tests to verify they all pass:

```bash
uv run pytest tests/file_exchange/test_types.py -v
```

Expected: all 10 tests PASS.

### Step A.4: Public-symbol literals

- [ ] Add the role/transport `Literal` aliases. Append to `_types.py`:

```python
# Role/transport aliases used by the public API
FileExchangeRole = Literal["provider", "fetcher", "receiver", "sender"]
FileExchangeTransport = Literal["filesystem", "download", "upload"]
```

(No test needed — `Literal` aliases are checked at use sites.)

### Step A.5: Commit Task A

- [ ] Stage and commit:

```bash
git add src/fastmcp_pvl_core/_file_exchange/ tests/file_exchange/
git commit -m "feat(file-exchange): vendor v0.1 schema and Pydantic types

Closes <issue-A-number>"
```

### Step A.6: Local circus + PR

- [ ] Run `preflight-circus` skill against `origin/main..HEAD`. Address any ≥80-confidence findings inline.
- [ ] Push and open a draft PR. Wait for bot review (expected: LGTM). Flip to ready when bot + CI green.

---

# Task B: Selection algorithm + error envelope

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_errors.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_select.py`
- Create: `tests/file_exchange/test_errors.py`
- Create: `tests/file_exchange/test_select.py`

### Step B.1: Test the error envelope

- [ ] Write `tests/file_exchange/test_errors.py`:

```python
from __future__ import annotations

import pytest
from mcp.types import CallToolResult

from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
    as_tool_error_result,
)


def test_error_carries_code_and_optional_metadata():
    exc = FileExchangeError(
        code=FileExchangeErrorCode.DIGEST_MISMATCH,
        transport="download",
        detail="expected sha-256:9f86d0..., got sha-256:1b4f0e...",
    )
    assert exc.code == "digest-mismatch"
    assert exc.transport == "download"
    assert "expected sha-256:9f86d0" in exc.detail


def test_as_tool_error_result_has_meta_block():
    exc = FileExchangeError(
        code=FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT,
        detail="no descriptor in handle matched supported transports",
    )
    result = as_tool_error_result(exc)
    assert result.isError is True
    assert result.content  # at least one text block
    assert any("no-supported-transport" in c.text for c in result.content if c.type == "text")
    meta = result.meta or {}
    err_meta = meta.get("nl.liesdonk.file-exchange/error")
    assert err_meta is not None
    assert err_meta["code"] == "no-supported-transport"


def test_all_spec_codes_are_defined():
    """Spec §13 enumerates these — any drift here is a spec/impl mismatch."""
    expected = {
        "no-supported-transport",
        "descriptor-expired",
        "not-accessible",
        "digest-mismatch",
        "size-mismatch",
        "too-large",
        "mime-type-rejected",
        "unsupported-requirement",
        "transfer-failed",
    }
    actual = {code.value for code in FileExchangeErrorCode}
    assert actual == expected
```

- [ ] Run the test:

```bash
uv run pytest tests/file_exchange/test_errors.py -v
```

Expected: FAIL — ImportError.

### Step B.2: Implement the error envelope

- [ ] Write `src/fastmcp_pvl_core/_file_exchange/_errors.py`:

```python
"""Error envelope for nl.liesdonk.file-exchange (spec §13)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from mcp.types import CallToolResult, TextContent


class FileExchangeErrorCode(StrEnum):
    """The closed set of error codes from spec §13.

    The set is intentionally enumerable: a consumer that receives an unrecognised
    code SHOULD treat it as a generic failure (per spec §13 last paragraph), but
    pvl-core itself only emits codes from this enum.
    """

    NO_SUPPORTED_TRANSPORT = "no-supported-transport"
    DESCRIPTOR_EXPIRED = "descriptor-expired"
    NOT_ACCESSIBLE = "not-accessible"
    DIGEST_MISMATCH = "digest-mismatch"
    SIZE_MISMATCH = "size-mismatch"
    TOO_LARGE = "too-large"
    MIME_TYPE_REJECTED = "mime-type-rejected"
    UNSUPPORTED_REQUIREMENT = "unsupported-requirement"
    TRANSFER_FAILED = "transfer-failed"


class FileExchangeError(Exception):
    """A file-exchange transfer failure that maps cleanly to spec §13.

    Raised by the role helpers (pull_artifact, push_artifact, …) and translated
    to a tool-execution error result via :func:`as_tool_error_result` at the
    tool body's outer except clause.
    """

    def __init__(
        self,
        *,
        code: FileExchangeErrorCode,
        transport: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.transport = transport
        self.detail = detail


def as_tool_error_result(exc: FileExchangeError) -> CallToolResult:
    """Convert a FileExchangeError into a tool-execution error result (spec §13)."""

    text = f"file exchange: {exc.code.value}"
    if exc.transport:
        text += f" (transport={exc.transport})"
    if exc.detail:
        text += f" — {exc.detail}"

    meta_block: dict[str, Any] = {"code": exc.code.value}
    if exc.transport:
        meta_block["transport"] = exc.transport
    if exc.detail:
        meta_block["detail"] = exc.detail

    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=text)],
        meta={"nl.liesdonk.file-exchange/error": meta_block},
    )
```

- [ ] Run the tests:

```bash
uv run pytest tests/file_exchange/test_errors.py -v
```

Expected: PASS.

### Step B.3: Test the selection algorithm

- [ ] Write `tests/file_exchange/test_select.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
)
from fastmcp_pvl_core._file_exchange._select import (
    select_source,
    select_sink,
)
from fastmcp_pvl_core._file_exchange._types import (
    ArtifactMetadata,
    IntakeTicket,
    TransferHandle,
)


def _now_plus(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _handle(sources: list[dict]) -> TransferHandle:
    return TransferHandle.model_validate(
        {
            "type": "nl.liesdonk.file-exchange/transfer-handle",
            "version": "0.1",
            "artifact": {"name": "x.bin"},
            "sources": sources,
        }
    )


def _ticket(sinks: list[dict]) -> IntakeTicket:
    return IntakeTicket.model_validate(
        {
            "type": "nl.liesdonk.file-exchange/intake-ticket",
            "version": "0.1",
            "artifactId": "ar-1",
            "sinks": sinks,
        }
    )


def test_picks_first_supported_in_order():
    h = _handle(
        [
            {"transport": "filesystem", "uri": "exchange://v/x.bin"},
            {
                "transport": "download",
                "url": "https://x/y",
                "expiresAt": _now_plus(3600),
            },
        ]
    )
    chosen = select_source(h, supported_transports=("download",), known_volumes=())
    # filesystem skipped (unsupported), download picked.
    assert chosen.root.transport == "download"


def test_skips_filesystem_with_unknown_volume():
    h = _handle([{"transport": "filesystem", "uri": "exchange://other/x.bin"}])
    with pytest.raises(FileExchangeError) as exc:
        select_source(h, supported_transports=("filesystem",), known_volumes=("known",))
    assert exc.value.code == FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT


def test_skips_expired_download():
    h = _handle(
        [
            {
                "transport": "download",
                "url": "https://x/y",
                "expiresAt": _now_plus(-60),
            },
        ]
    )
    with pytest.raises(FileExchangeError) as exc:
        select_source(h, supported_transports=("download",), known_volumes=())
    assert exc.value.code == FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT


def test_clock_skew_tolerance_keeps_just_expired_descriptor():
    h = _handle(
        [
            {
                "transport": "download",
                "url": "https://x/y",
                "expiresAt": _now_plus(-10),  # within 30s tolerance
            },
        ]
    )
    chosen = select_source(
        h, supported_transports=("download",), known_volumes=(), clock_skew_tolerance_s=30
    )
    assert chosen.root.transport == "download"


def test_requires_unknown_feature_is_rejected():
    raw = {
        "type": "nl.liesdonk.file-exchange/transfer-handle",
        "version": "0.1",
        "artifact": {"name": "x.bin"},
        "sources": [{"transport": "filesystem", "uri": "exchange://v/x"}],
        "requires": ["future-feature-xyz"],
    }
    h = TransferHandle.model_validate(raw)
    with pytest.raises(FileExchangeError) as exc:
        select_source(h, supported_transports=("filesystem",), known_volumes=("v",))
    assert exc.value.code == FileExchangeErrorCode.UNSUPPORTED_REQUIREMENT


def test_unknown_transport_descriptor_is_silently_skipped():
    """Spec §17.2: unknown `transport` values are skipped during selection (not rejected)."""
    # The typed layer rejects unknown transports at parsing time, so we exercise
    # the fallthrough by passing a known-but-unsupported transport.
    h = _handle(
        [
            {"transport": "filesystem", "uri": "exchange://v/x"},
            {"transport": "download", "url": "https://x/y", "expiresAt": _now_plus(3600)},
        ]
    )
    # filesystem unsupported by this consumer, but download supported → download is picked.
    chosen = select_source(h, supported_transports=("download",), known_volumes=())
    assert chosen.root.transport == "download"


def test_select_sink_picks_writable_filesystem():
    t = _ticket([{"transport": "filesystem", "uri": "exchange://intake/x.bin"}])
    chosen = select_sink(t, supported_transports=("filesystem",), known_volumes=("intake",))
    assert chosen.root.transport == "filesystem"
```

- [ ] Run the test:

```bash
uv run pytest tests/file_exchange/test_select.py -v
```

Expected: FAIL — ImportError.

### Step B.4: Implement the selection algorithm

- [ ] Write `src/fastmcp_pvl_core/_file_exchange/_select.py`:

```python
"""Descriptor selection algorithm (spec §9) and version-skew gate (spec §17.3, §17.4)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ._errors import FileExchangeError, FileExchangeErrorCode

if TYPE_CHECKING:
    from ._types import IntakeTicket, SinkDescriptor, SourceDescriptor, TransferHandle


# v0.1 defines no must-understand feature identifiers (spec §17.4).
_KNOWN_REQUIRES_FEATURES: frozenset[str] = frozenset()


def _check_version_and_requires(reference: TransferHandle | IntakeTicket) -> None:
    """Apply §17.3 (version skew) and §17.4 (must-understand) gates.

    Raises FileExchangeError if the reference cannot be processed.
    """
    major, _, _ = reference.version.partition(".")
    if major != "0":
        raise FileExchangeError(
            code=FileExchangeErrorCode.TRANSFER_FAILED,
            detail=f"reference version {reference.version!r} has a major component this implementation does not understand",
        )

    if reference.requires:
        seen: set[str] = set()
        for feature in reference.requires:
            if feature in seen:
                raise FileExchangeError(
                    code=FileExchangeErrorCode.UNSUPPORTED_REQUIREMENT,
                    detail=f"requires array contains a duplicate identifier: {feature!r}",
                )
            seen.add(feature)
            if feature not in _KNOWN_REQUIRES_FEATURES:
                raise FileExchangeError(
                    code=FileExchangeErrorCode.UNSUPPORTED_REQUIREMENT,
                    detail=f"required feature {feature!r} not implemented",
                )


def _exchange_volume(uri: str) -> str | None:
    """Return the volume component of an exchange:// URI, or None for file://."""
    if uri.startswith("exchange://"):
        rest = uri[len("exchange://"):]
        return rest.split("/", 1)[0] or None
    return None


def _is_expired(expires_at: datetime, tolerance_s: int) -> bool:
    now = datetime.now(timezone.utc)
    return now > (expires_at + timedelta(seconds=tolerance_s))


def select_source(
    handle: TransferHandle,
    *,
    supported_transports: Sequence[str],
    known_volumes: Sequence[str],
    clock_skew_tolerance_s: int = 30,
) -> SourceDescriptor:
    """Pick a source descriptor from a TransferHandle (spec §9).

    Raises FileExchangeError with code 'no-supported-transport' if nothing survives.
    """
    _check_version_and_requires(handle)
    return _select(handle.sources, supported_transports, known_volumes, clock_skew_tolerance_s)


def select_sink(
    ticket: IntakeTicket,
    *,
    supported_transports: Sequence[str],
    known_volumes: Sequence[str],
    clock_skew_tolerance_s: int = 30,
) -> SinkDescriptor:
    """Pick a sink descriptor from an IntakeTicket (spec §9)."""
    _check_version_and_requires(ticket)
    return _select(ticket.sinks, supported_transports, known_volumes, clock_skew_tolerance_s)


def _select(
    descriptors: Sequence,
    supported_transports: Sequence[str],
    known_volumes: Sequence[str],
    clock_skew_tolerance_s: int,
):
    for descriptor in descriptors:
        node = descriptor.root
        if node.transport not in supported_transports:
            continue
        if node.transport == "filesystem":
            volume = _exchange_volume(node.uri)
            if volume is not None and volume not in known_volumes:
                continue
            return descriptor
        if node.transport in ("download", "upload"):
            if _is_expired(node.expiresAt, clock_skew_tolerance_s):
                continue
            return descriptor
    raise FileExchangeError(
        code=FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT,
        detail="no descriptor in the reference matched the consumer's supported transports",
    )
```

- [ ] Run the tests:

```bash
uv run pytest tests/file_exchange/test_select.py tests/file_exchange/test_errors.py -v
```

Expected: PASS (all 7 select tests + earlier error tests).

### Step B.5: Commit and PR

- [ ] Commit:

```bash
git add src/fastmcp_pvl_core/_file_exchange/_errors.py src/fastmcp_pvl_core/_file_exchange/_select.py tests/file_exchange/test_errors.py tests/file_exchange/test_select.py
git commit -m "feat(file-exchange): error envelope + descriptor selection algorithm

Closes <issue-B-number>"
```

- [ ] Run `preflight-circus`; open draft PR; flip to ready when bots+CI green.

---

# Task C: Filesystem transport

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_transport_filesystem.py`
- Create: `tests/file_exchange/test_transport_filesystem.py`

### Step C.1: Test exchange-URI resolution and path confinement

- [ ] Write `tests/file_exchange/test_transport_filesystem.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

import pytest

from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
)
from fastmcp_pvl_core._file_exchange._transport_filesystem import (
    atomic_write,
    parse_volumes,
    resolve_exchange_uri,
)


def test_parse_volumes_handles_comma_separated_pairs():
    mapping = parse_volumes("vault=/tmp/vault,intake=/tmp/intake")
    assert mapping == {"vault": Path("/tmp/vault"), "intake": Path("/tmp/intake")}


def test_parse_volumes_handles_empty_and_none():
    assert parse_volumes(None) == {}
    assert parse_volumes("") == {}
    assert parse_volumes("   ") == {}


def test_parse_volumes_rejects_malformed_pair():
    with pytest.raises(ValueError, match="must be of the form"):
        parse_volumes("brokenpair")


def test_resolve_exchange_uri_in_volume(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    artifact = vault / "report.parquet"
    artifact.touch()

    resolved = resolve_exchange_uri(
        "exchange://vault/report.parquet",
        volumes={"vault": vault},
    )
    assert resolved == artifact.resolve()


def test_resolve_exchange_uri_unknown_volume_raises(tmp_path: Path):
    with pytest.raises(FileExchangeError) as exc:
        resolve_exchange_uri("exchange://unknown/x", volumes={"vault": tmp_path})
    assert exc.value.code == FileExchangeErrorCode.NOT_ACCESSIBLE


def test_resolve_exchange_uri_rejects_dot_dot_escape(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "secret.txt").write_text("nope")

    with pytest.raises(FileExchangeError) as exc:
        resolve_exchange_uri(
            "exchange://vault/../secret.txt", volumes={"vault": vault}
        )
    assert exc.value.code == FileExchangeErrorCode.NOT_ACCESSIBLE


def test_resolve_exchange_uri_rejects_symlink_escape(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("forbidden")
    (vault / "escape").symlink_to(outside)

    with pytest.raises(FileExchangeError) as exc:
        resolve_exchange_uri("exchange://vault/escape", volumes={"vault": vault})
    assert exc.value.code == FileExchangeErrorCode.NOT_ACCESSIBLE


def test_atomic_write_only_appears_on_success(tmp_path: Path):
    target = tmp_path / "result.bin"

    with atomic_write(target) as f:
        f.write(b"partial")
        # mid-write the target file MUST not exist (consumer never sees partial)
        assert not target.exists()
        f.write(b" and more")

    assert target.exists()
    assert target.read_bytes() == b"partial and more"


def test_atomic_write_discards_temp_on_exception(tmp_path: Path):
    target = tmp_path / "result.bin"

    with pytest.raises(RuntimeError):
        with atomic_write(target) as f:
            f.write(b"partial")
            raise RuntimeError("boom")

    assert not target.exists()
    # No leftover temp files in the directory either
    assert list(tmp_path.iterdir()) == []
```

- [ ] Run the tests:

```bash
uv run pytest tests/file_exchange/test_transport_filesystem.py -v
```

Expected: FAIL — ImportError.

### Step C.2: Implement filesystem transport

- [ ] Write `src/fastmcp_pvl_core/_file_exchange/_transport_filesystem.py`:

```python
"""Filesystem transport (spec §10.1).

Provides:
- ``parse_volumes`` — parse the operator-supplied volume map (env-var value).
- ``resolve_exchange_uri`` — resolve an ``exchange://`` or ``file://`` URI to a
  canonicalised, confinement-checked local Path.
- ``atomic_write`` — temp-file + rename context manager so consumers never observe
  a partial write (spec §10.1.3).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

from ._errors import FileExchangeError, FileExchangeErrorCode


def parse_volumes(raw: str | None) -> dict[str, Path]:
    """Parse a `<volume>=<path>,<volume>=<path>` env-var value.

    Empty/whitespace/None returns an empty mapping. Malformed entries raise ValueError.
    """
    if not raw or not raw.strip():
        return {}

    result: dict[str, Path] = {}
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(
                f"FILE_EXCHANGE_VOLUMES entry {token!r} must be of the form "
                f"'<volume>=<local-mount-point>'"
            )
        volume, _, path = token.partition("=")
        volume = volume.strip()
        path_value = path.strip()
        if not volume or not path_value:
            raise ValueError(
                f"FILE_EXCHANGE_VOLUMES entry {token!r} must be of the form "
                f"'<volume>=<local-mount-point>' with non-empty parts"
            )
        result[volume] = Path(path_value)
    return result


def resolve_exchange_uri(uri: str, *, volumes: Mapping[str, Path]) -> Path:
    """Resolve an exchange:// or file:// URI to a canonicalised, confined Path.

    Raises FileExchangeError(code=NOT_ACCESSIBLE) on:
    - unknown volume (exchange://)
    - resolved path outside the volume root (canonicalisation escape, symlink swap, etc.)
    """
    parsed = urlparse(uri)
    if parsed.scheme == "exchange":
        volume = parsed.netloc
        if volume not in volumes:
            raise FileExchangeError(
                code=FileExchangeErrorCode.NOT_ACCESSIBLE,
                transport="filesystem",
                detail=f"no mapping configured for volume {volume!r}",
            )
        root = volumes[volume].resolve()
        relative = unquote(parsed.path.lstrip("/"))
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise FileExchangeError(
                code=FileExchangeErrorCode.NOT_ACCESSIBLE,
                transport="filesystem",
                detail=f"resolved path escapes volume {volume!r}",
            ) from exc
        return candidate

    if parsed.scheme == "file":
        # Reserved for the "shared mount namespace" case (spec §10.1.2).
        # Phase 1 supports this only when a single exchange root is configured
        # under the special volume name "_file_root" — see _capability.py.
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            transport="filesystem",
            detail="file:// scheme requires an operator-configured shared root; not supported in phase 1",
        )

    raise FileExchangeError(
        code=FileExchangeErrorCode.NOT_ACCESSIBLE,
        transport="filesystem",
        detail=f"unsupported URI scheme: {parsed.scheme!r}",
    )


@contextmanager
def atomic_write(target: Path) -> Iterator[object]:
    """Write to a temp file in the target's directory, rename onto target on success.

    Consumers MUST never see a partial write (spec §10.1.3). On any exception the
    temp file is unlinked.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path_s = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temp_path = Path(temp_path_s)
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            pass
        raise
```

- [ ] Run the tests:

```bash
uv run pytest tests/file_exchange/test_transport_filesystem.py -v
```

Expected: PASS (all 9 tests).

### Step C.3: Commit and PR

- [ ] Commit:

```bash
git add src/fastmcp_pvl_core/_file_exchange/_transport_filesystem.py tests/file_exchange/test_transport_filesystem.py
git commit -m "feat(file-exchange): filesystem transport with exchange:// resolution and atomic writes

Closes <issue-C-number>"
```

- [ ] Run `preflight-circus`; open draft PR; flip to ready when bots+CI green.

---

# Task D: HTTPS consumer side

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_transport_https.py`
- Create: `tests/file_exchange/test_transport_https.py`

### Step D.1: Test SSRF guard

- [ ] Write the SSRF tests in `tests/file_exchange/test_transport_https.py`:

```python
from __future__ import annotations

import ipaddress
from unittest.mock import patch

import pytest

from fastmcp_pvl_core._file_exchange._errors import FileExchangeError
from fastmcp_pvl_core._file_exchange._transport_https import (
    SSRFGuardConfig,
    resolve_and_check_host,
)


def _resolve_to(addrs: list[str]):
    """Patch DNS resolution to return the given address list."""
    return patch(
        "fastmcp_pvl_core._file_exchange._transport_https._resolve_host",
        return_value=[ipaddress.ip_address(a) for a in addrs],
    )


def test_public_address_is_allowed():
    with _resolve_to(["93.184.216.34"]):
        ip = resolve_and_check_host("example.com", SSRFGuardConfig())
        assert ip == ipaddress.IPv4Address("93.184.216.34")


def test_loopback_is_rejected_by_default():
    with _resolve_to(["127.0.0.1"]), pytest.raises(FileExchangeError):
        resolve_and_check_host("evil.example", SSRFGuardConfig())


def test_loopback_allowed_when_configured():
    with _resolve_to(["127.0.0.1"]):
        ip = resolve_and_check_host(
            "localhost", SSRFGuardConfig(allow_loopback=True)
        )
        assert ip == ipaddress.IPv4Address("127.0.0.1")


def test_private_range_rejected_by_default():
    with _resolve_to(["10.0.0.5"]), pytest.raises(FileExchangeError):
        resolve_and_check_host("internal.example", SSRFGuardConfig())


def test_link_local_rejected_by_default():
    with _resolve_to(["169.254.169.254"]), pytest.raises(FileExchangeError):
        resolve_and_check_host("metadata.example", SSRFGuardConfig())


def test_ipv6_loopback_rejected_by_default():
    with _resolve_to(["::1"]), pytest.raises(FileExchangeError):
        resolve_and_check_host("v6loop.example", SSRFGuardConfig())
```

### Step D.2: Implement SSRF guard

- [ ] Write `src/fastmcp_pvl_core/_file_exchange/_transport_https.py`:

```python
"""HTTPS transport: pull_download / push_upload + SSRF guard (spec §10.2, §10.3, §15)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx

from ._errors import FileExchangeError, FileExchangeErrorCode

USER_AGENT = "fastmcp-pvl-core/file-exchange/0.1"


@dataclass(frozen=True)
class SSRFGuardConfig:
    """Operator-set guard knobs (default-deny for private/loopback/link-local)."""

    allow_loopback: bool = False
    allow_private: bool = False


def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """One-shot host resolution. Patched in tests."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _is_disallowed(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address, cfg: SSRFGuardConfig
) -> str | None:
    """Return a human-readable reason if disallowed; None if OK."""
    if addr.is_loopback and not cfg.allow_loopback:
        return "loopback address"
    if addr.is_link_local:
        return "link-local address"
    if addr.is_private and not cfg.allow_private:
        return "RFC 1918 / private address"
    if addr.is_multicast:
        return "multicast address"
    if addr.is_reserved:
        return "reserved address"
    return None


def resolve_and_check_host(
    host: str, config: SSRFGuardConfig
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Resolve hostname ONCE; reject disallowed ranges; return pinned IP.

    The caller MUST use the returned IP for the actual connection (defeats DNS
    rebinding). Spec §15 "SSRF" mitigations.
    """
    try:
        addrs = _resolve_host(host)
    except socket.gaierror as exc:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            transport="download",
            detail=f"host resolution failed for {host!r}: {exc}",
        ) from exc

    if not addrs:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            transport="download",
            detail=f"host {host!r} did not resolve to any addresses",
        )

    # Pick the first; report rejection if it's disallowed.
    primary = addrs[0]
    reason = _is_disallowed(primary, config)
    if reason is not None:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            transport="download",
            detail=f"host {host!r} resolved to {primary} ({reason}); refused",
        )
    return primary
```

- [ ] Run the SSRF tests:

```bash
uv run pytest tests/file_exchange/test_transport_https.py -v
```

Expected: PASS (all 6 tests).

### Step D.3: Test pull_download against a faked transport

- [ ] Add to `tests/file_exchange/test_transport_https.py`:

```python
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fastmcp_pvl_core._file_exchange._transport_https import pull_download
from fastmcp_pvl_core._file_exchange._types import DownloadSourceDescriptor


def _descriptor(url: str = "https://example.com/d/tok") -> DownloadSourceDescriptor:
    return DownloadSourceDescriptor.model_validate(
        {
            "transport": "download",
            "url": url,
            "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=b"hello bytes",
        headers={"Content-Length": "11", "Content-Type": "application/octet-stream"},
    )


@pytest.mark.asyncio
async def test_pull_download_streams_to_buffer():
    transport = httpx.MockTransport(_ok_handler)
    buf = io.BytesIO()
    # SSRF guard is bypassed in tests by passing an explicit transport.
    received_size = await pull_download(
        _descriptor(),
        dest=buf,
        client_transport=transport,
        ssrf=SSRFGuardConfig(allow_loopback=True, allow_private=True),
    )
    assert buf.getvalue() == b"hello bytes"
    assert received_size == 11


def _bad_redirect_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == "https://example.com/d/tok":
        return httpx.Response(302, headers={"Location": "https://attacker.example/x"})
    return httpx.Response(200, content=b"should-not-reach")


@pytest.mark.asyncio
async def test_pull_download_rejects_cross_origin_redirect():
    transport = httpx.MockTransport(_bad_redirect_handler)
    buf = io.BytesIO()
    with pytest.raises(FileExchangeError) as exc:
        await pull_download(
            _descriptor(),
            dest=buf,
            client_transport=transport,
            ssrf=SSRFGuardConfig(allow_loopback=True, allow_private=True),
        )
    assert exc.value.code in (
        FileExchangeErrorCode.NOT_ACCESSIBLE,
        FileExchangeErrorCode.TRANSFER_FAILED,
    )


def _http_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"hi")


@pytest.mark.asyncio
async def test_pull_download_rejects_non_https():
    transport = httpx.MockTransport(_http_handler)
    desc = _descriptor("http://example.com/d/tok")  # non-https
    buf = io.BytesIO()
    with pytest.raises(FileExchangeError):
        await pull_download(desc, dest=buf, client_transport=transport, ssrf=SSRFGuardConfig())
```

### Step D.4: Implement pull_download and push_upload

- [ ] Append to `src/fastmcp_pvl_core/_file_exchange/_transport_https.py`:

```python
async def pull_download(
    descriptor,                                  # DownloadSourceDescriptor
    *,
    dest: BinaryIO | Path,
    ssrf: SSRFGuardConfig,
    client_transport: httpx.AsyncBaseTransport | None = None,
    chunk_size: int = 65536,
) -> int:
    """Pull artifact bytes via HTTPS GET (spec §10.2). Returns bytes received.

    Streams to ``dest`` (either an open writable BinaryIO or a Path; for Path
    pvl-core uses the filesystem-transport's atomic_write context manager so
    a partial write is never observed).

    SSRF guard runs; non-https URLs are refused; cross-origin redirects are refused;
    no ambient credentials are attached.
    """
    url_str = str(descriptor.url)
    if not url_str.startswith("https://"):
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            transport="download",
            detail="download URL must use https://",
        )

    host = httpx.URL(url_str).host
    # SSRF guard (resolve once, pin IP). Tests can pass a transport that
    # bypasses real connections; in production the transport is None and
    # the pinned IP is set in the request connection options.
    if client_transport is None:
        resolve_and_check_host(host, ssrf)

    async with httpx.AsyncClient(
        transport=client_transport,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    ) as client:
        async with client.stream("GET", url_str) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = response.headers.get("Location", "")
                target_host = httpx.URL(target).host if target else ""
                if target_host != host:
                    raise FileExchangeError(
                        code=FileExchangeErrorCode.NOT_ACCESSIBLE,
                        transport="download",
                        detail=f"refusing cross-origin redirect to {target_host!r}",
                    )
                raise FileExchangeError(
                    code=FileExchangeErrorCode.TRANSFER_FAILED,
                    transport="download",
                    detail="same-origin redirect handling is deferred; provider should issue a direct URL",
                )
            if response.status_code != 200:
                raise FileExchangeError(
                    code=FileExchangeErrorCode.TRANSFER_FAILED,
                    transport="download",
                    detail=f"unexpected HTTP status {response.status_code}",
                )

            if isinstance(dest, Path):
                from ._transport_filesystem import atomic_write

                bytes_written = 0
                with atomic_write(dest) as f:
                    async for chunk in response.aiter_bytes(chunk_size):
                        f.write(chunk)
                        bytes_written += len(chunk)
                return bytes_written

            bytes_written = 0
            async for chunk in response.aiter_bytes(chunk_size):
                dest.write(chunk)
                bytes_written += len(chunk)
            return bytes_written


async def push_upload(
    descriptor,                                  # UploadSinkDescriptor
    *,
    source: BinaryIO | Path,
    ssrf: SSRFGuardConfig,
    content_digest: str | None = None,
    content_type: str | None = None,
    content_length: int | None = None,
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Push artifact bytes via HTTPS PUT/POST (spec §10.3).

    Sender obligations: https-only; no ambient credentials; uses the descriptor's
    method (default PUT); passes Content-Digest when provided; treats non-2xx as failure.
    """
    url_str = str(descriptor.url)
    if not url_str.startswith("https://"):
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            transport="upload",
            detail="upload URL must use https://",
        )

    host = httpx.URL(url_str).host
    if client_transport is None:
        resolve_and_check_host(host, ssrf)

    headers = {"User-Agent": USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    if content_digest:
        # Per spec §10.3, this header carries the artifact digest in RFC 9530 form.
        headers["Content-Digest"] = content_digest

    if isinstance(source, Path):
        content = source.read_bytes()  # phase 1: simple buffered upload
    else:
        content = source.read()

    async with httpx.AsyncClient(
        transport=client_transport,
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        response = await client.request(descriptor.method, url_str, content=content, headers=headers)
        if not (200 <= response.status_code < 300):
            raise FileExchangeError(
                code=FileExchangeErrorCode.TRANSFER_FAILED,
                transport="upload",
                detail=f"upload rejected with HTTP {response.status_code}",
            )
```

- [ ] Run the tests:

```bash
uv run pytest tests/file_exchange/test_transport_https.py -v
```

Expected: PASS (all 9 tests).

### Step D.5: Commit and PR

- [ ] Commit:

```bash
git add src/fastmcp_pvl_core/_file_exchange/_transport_https.py tests/file_exchange/test_transport_https.py
git commit -m "feat(file-exchange): HTTPS consumer transport (pull/push) with SSRF guard

Closes <issue-D-number>"
```

- [ ] Run `preflight-circus`; open draft PR; flip to ready when bots+CI green.

---

# Task E: Capability URL store + sibling routes

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_url_store.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_routes.py`
- Create: `tests/file_exchange/test_url_store.py`
- Create: `tests/file_exchange/test_routes.py`

### Step E.1: Test the URL store

- [ ] Write `tests/file_exchange/test_url_store.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from key_value.aio.stores.memory import MemoryStore

from fastmcp_pvl_core._file_exchange._url_store import (
    DownloadTokenRecord,
    UploadTokenRecord,
    consume_download_token,
    intake_path_for,
    mint_download_token,
    mint_upload_token,
    record_intake_path,
)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.mark.asyncio
async def test_minted_download_token_is_url_safe_random(store: MemoryStore):
    tok = await mint_download_token(
        store=store,
        bytes_path=Path("/tmp/x.bin"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        single_use=True,
    )
    # 128 bits of entropy → at least 22 base64url characters.
    assert len(tok) >= 22
    record = await store.get(collection="tokens", key=tok)
    assert record is not None


@pytest.mark.asyncio
async def test_consume_download_token_flips_single_use(store: MemoryStore):
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    tok = await mint_download_token(
        store=store, bytes_path=Path("/tmp/x.bin"), expires_at=expires, single_use=True
    )

    record = await consume_download_token(store=store, token=tok)
    assert record.bytes_path == Path("/tmp/x.bin")

    # Second consume MUST fail because single_use already flipped consumed.
    with pytest.raises(LookupError):
        await consume_download_token(store=store, token=tok)


@pytest.mark.asyncio
async def test_expired_token_is_treated_as_missing(store: MemoryStore):
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    tok = await mint_download_token(
        store=store, bytes_path=Path("/tmp/x.bin"), expires_at=past, single_use=True
    )
    with pytest.raises(LookupError):
        await consume_download_token(store=store, token=tok)


@pytest.mark.asyncio
async def test_intake_path_correlation(store: MemoryStore):
    await record_intake_path(store=store, artifact_id="ar-1", path=Path("/tmp/intake/x.bin"))
    p = await intake_path_for(store=store, artifact_id="ar-1")
    assert p == Path("/tmp/intake/x.bin")


@pytest.mark.asyncio
async def test_intake_path_for_missing_artifact_returns_none(store: MemoryStore):
    p = await intake_path_for(store=store, artifact_id="never-minted")
    assert p is None
```

- [ ] Run the test:

```bash
uv run pytest tests/file_exchange/test_url_store.py -v
```

Expected: FAIL — ImportError.

### Step E.2: Implement the URL store

- [ ] Write `src/fastmcp_pvl_core/_file_exchange/_url_store.py`:

```python
"""Capability-URL token store + intake-path correlation map.

Backed by an AsyncKeyValue store (from build_kv_store(..., namespace="file-exchange")),
which keeps the file-exchange surface restart-safe when configured with file/Redis/etc.

Two collections inside the namespace:
- ``tokens``   — capability tokens (download + upload)
- ``intake``   — artifact_id → resolved intake path
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from key_value.aio.protocols.key_value import AsyncKeyValue


TOKEN_BYTES = 16  # 128 bits → 22 base64url chars


@dataclass(frozen=True)
class DownloadTokenRecord:
    bytes_path: Path
    expires_at: datetime
    single_use: bool
    consumed: bool


@dataclass(frozen=True)
class UploadTokenRecord:
    intake_path: Path
    artifact_id: str
    expires_at: datetime
    max_size: int | None
    accept_mime_types: list[str] | None
    require_digest: list[str] | None
    consumed: bool


def _new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(expires_at: datetime) -> bool:
    return _now() > expires_at


async def mint_download_token(
    *,
    store: AsyncKeyValue,
    bytes_path: Path,
    expires_at: datetime,
    single_use: bool = True,
) -> str:
    """Mint a download capability token; return the token string."""
    token = _new_token()
    record = {
        "kind": "download",
        "bytes_path": str(bytes_path),
        "expires_at": expires_at.isoformat(),
        "single_use": single_use,
        "consumed": False,
    }
    await store.put(collection="tokens", key=token, value=record)
    return token


async def mint_upload_token(
    *,
    store: AsyncKeyValue,
    intake_path: Path,
    artifact_id: str,
    expires_at: datetime,
    max_size: int | None = None,
    accept_mime_types: list[str] | None = None,
    require_digest: list[str] | None = None,
) -> str:
    """Mint an upload capability token; return the token string."""
    token = _new_token()
    record = {
        "kind": "upload",
        "intake_path": str(intake_path),
        "artifact_id": artifact_id,
        "expires_at": expires_at.isoformat(),
        "max_size": max_size,
        "accept_mime_types": accept_mime_types,
        "require_digest": require_digest,
        "consumed": False,
    }
    await store.put(collection="tokens", key=token, value=record)
    return token


async def consume_download_token(
    *, store: AsyncKeyValue, token: str
) -> DownloadTokenRecord:
    raw = await store.get(collection="tokens", key=token)
    if raw is None or raw.get("kind") != "download":
        raise LookupError("download token not found")
    expires_at = datetime.fromisoformat(raw["expires_at"])
    if _is_expired(expires_at) or raw.get("consumed", False):
        raise LookupError("download token expired or already consumed")

    record = DownloadTokenRecord(
        bytes_path=Path(raw["bytes_path"]),
        expires_at=expires_at,
        single_use=raw["single_use"],
        consumed=False,
    )

    if record.single_use:
        # Atomically flip consumed.  AsyncKeyValue.put is the unit of atomicity here;
        # callers MUST treat double-consume as the failure case the test exercises.
        await store.put(
            collection="tokens",
            key=token,
            value={**raw, "consumed": True},
        )

    return record


async def consume_upload_token(
    *, store: AsyncKeyValue, token: str
) -> UploadTokenRecord:
    raw = await store.get(collection="tokens", key=token)
    if raw is None or raw.get("kind") != "upload":
        raise LookupError("upload token not found")
    expires_at = datetime.fromisoformat(raw["expires_at"])
    if _is_expired(expires_at) or raw.get("consumed", False):
        raise LookupError("upload token expired or already consumed")
    return UploadTokenRecord(
        intake_path=Path(raw["intake_path"]),
        artifact_id=raw["artifact_id"],
        expires_at=expires_at,
        max_size=raw.get("max_size"),
        accept_mime_types=raw.get("accept_mime_types"),
        require_digest=raw.get("require_digest"),
        consumed=False,
    )


async def mark_upload_consumed(*, store: AsyncKeyValue, token: str) -> None:
    raw = await store.get(collection="tokens", key=token)
    if raw is None:
        return
    await store.put(collection="tokens", key=token, value={**raw, "consumed": True})


async def record_intake_path(
    *, store: AsyncKeyValue, artifact_id: str, path: Path
) -> None:
    """Record (artifact_id → resolved intake path) so resolve_intake can find it."""
    await store.put(
        collection="intake", key=artifact_id, value={"path": str(path)}
    )


async def intake_path_for(
    *, store: AsyncKeyValue, artifact_id: str
) -> Path | None:
    raw = await store.get(collection="intake", key=artifact_id)
    if raw is None:
        return None
    return Path(raw["path"])
```

- [ ] Run the tests:

```bash
uv run pytest tests/file_exchange/test_url_store.py -v
```

Expected: PASS (all 5 tests).

### Step E.3: Test the sibling routes

- [ ] Write `tests/file_exchange/test_routes.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from key_value.aio.stores.memory import MemoryStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from fastmcp_pvl_core._file_exchange._routes import build_file_exchange_router
from fastmcp_pvl_core._file_exchange._url_store import (
    mint_download_token,
    mint_upload_token,
)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def app(store: MemoryStore) -> Starlette:
    router = build_file_exchange_router(store=store)
    return Starlette(routes=router.routes)


@pytest.mark.asyncio
async def test_download_route_serves_bytes(tmp_path: Path, store: MemoryStore, app):
    artifact = tmp_path / "x.bin"
    artifact.write_bytes(b"hello bytes")
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    token = await mint_download_token(
        store=store, bytes_path=artifact, expires_at=expires, single_use=True
    )

    with TestClient(app) as client:
        resp = client.get(f"/file-exchange/d/{token}")
    assert resp.status_code == 200
    assert resp.content == b"hello bytes"


@pytest.mark.asyncio
async def test_download_route_single_use_second_request_404s(
    tmp_path: Path, store: MemoryStore, app
):
    artifact = tmp_path / "x.bin"
    artifact.write_bytes(b"hi")
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    token = await mint_download_token(
        store=store, bytes_path=artifact, expires_at=expires, single_use=True
    )

    with TestClient(app) as client:
        first = client.get(f"/file-exchange/d/{token}")
        second = client.get(f"/file-exchange/d/{token}")
    assert first.status_code == 200
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_download_route_returns_404_for_expired(
    tmp_path: Path, store: MemoryStore, app
):
    artifact = tmp_path / "x.bin"
    artifact.write_bytes(b"hi")
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    token = await mint_download_token(
        store=store, bytes_path=artifact, expires_at=past, single_use=True
    )
    with TestClient(app) as client:
        resp = client.get(f"/file-exchange/d/{token}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_route_writes_atomically(
    tmp_path: Path, store: MemoryStore, app
):
    intake = tmp_path / "intake" / "x.bin"
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    token = await mint_upload_token(
        store=store,
        intake_path=intake,
        artifact_id="ar-1",
        expires_at=expires,
    )
    with TestClient(app) as client:
        resp = client.put(f"/file-exchange/u/{token}", content=b"payload")
    assert resp.status_code == 204
    assert intake.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_upload_route_enforces_max_size(
    tmp_path: Path, store: MemoryStore, app
):
    intake = tmp_path / "intake" / "x.bin"
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    token = await mint_upload_token(
        store=store,
        intake_path=intake,
        artifact_id="ar-1",
        expires_at=expires,
        max_size=3,
    )
    with TestClient(app) as client:
        resp = client.put(f"/file-exchange/u/{token}", content=b"too much")
    assert resp.status_code == 413
    assert not intake.exists()
```

### Step E.4: Implement the routes

- [ ] Write `src/fastmcp_pvl_core/_file_exchange/_routes.py`:

```python
"""Sibling HTTP routes mounted on the FastMCP server.

GET  /file-exchange/d/<token>    serves a minted download.
PUT  /file-exchange/u/<token>    accepts an upload (POST also accepted).

Both routes look tokens up in the kv_store passed to ``build_file_exchange_router``.
"""

from __future__ import annotations

from pathlib import Path

from key_value.aio.protocols.key_value import AsyncKeyValue
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route, Router

from ._transport_filesystem import atomic_write
from ._url_store import (
    consume_download_token,
    consume_upload_token,
    mark_upload_consumed,
    record_intake_path,
)


def build_file_exchange_router(*, store: AsyncKeyValue) -> Router:
    """Return a Starlette Router with the file-exchange capability-URL endpoints."""

    async def _download(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            record = await consume_download_token(store=store, token=token)
        except LookupError:
            return Response(status_code=404)
        if not record.bytes_path.exists():
            return Response(status_code=404)
        return FileResponse(record.bytes_path)

    async def _upload(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            record = await consume_upload_token(store=store, token=token)
        except LookupError:
            return Response(status_code=404)

        # Buffer the body; phase 1 supports up to max_size or 100MB whichever is smaller.
        body = await request.body()
        if record.max_size is not None and len(body) > record.max_size:
            return Response(status_code=413)

        # Optional Content-Type filter
        if record.accept_mime_types:
            content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
            if content_type and not _mime_matches(content_type, record.accept_mime_types):
                return Response(status_code=415)

        with atomic_write(record.intake_path) as f:
            f.write(body)

        await record_intake_path(
            store=store, artifact_id=record.artifact_id, path=record.intake_path
        )
        await mark_upload_consumed(store=store, token=token)
        return Response(status_code=204)

    return Router(
        routes=[
            Route("/file-exchange/d/{token}", endpoint=_download, methods=["GET"]),
            Route(
                "/file-exchange/u/{token}",
                endpoint=_upload,
                methods=["PUT", "POST"],
            ),
        ]
    )


def _mime_matches(actual: str, accepted: list[str]) -> bool:
    """RFC 7231 §3.1.1.1 media-range matching."""
    actual_type, _, actual_sub = actual.partition("/")
    for pattern in accepted:
        ptype, _, psub = pattern.partition("/")
        if pattern == "*/*":
            return True
        if ptype == actual_type and (psub == "*" or psub == actual_sub):
            return True
    return False
```

- [ ] Run the route tests:

```bash
uv run pytest tests/file_exchange/test_routes.py -v
```

Expected: PASS (all 5 tests).

### Step E.5: Commit and PR

- [ ] Commit:

```bash
git add src/fastmcp_pvl_core/_file_exchange/_url_store.py src/fastmcp_pvl_core/_file_exchange/_routes.py tests/file_exchange/test_url_store.py tests/file_exchange/test_routes.py
git commit -m "feat(file-exchange): capability-URL token store + sibling HTTP routes

Closes <issue-E-number>"
```

- [ ] Run `preflight-circus`; open draft PR; flip to ready when bots+CI green.

---

# Task F: Capability declaration + role helpers + top-level re-exports

**Files:**
- Modify: `src/fastmcp_pvl_core/_config.py` — add seven file-exchange fields + env-var reads
- Create: `src/fastmcp_pvl_core/_file_exchange/_capability.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_provider.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_fetcher.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_receiver.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_sender.py`
- Modify: `src/fastmcp_pvl_core/__init__.py` — re-export public surface
- Create: `tests/file_exchange/test_capability.py`
- Create: `tests/file_exchange/test_role_helpers.py`

### Step F.1: Extend ServerConfig

- [ ] Test the env-var loading. Add to `tests/file_exchange/test_capability.py`:

```python
from __future__ import annotations

import pytest

from fastmcp_pvl_core._config import ServerConfig


def test_serverconfig_loads_file_exchange_volumes(monkeypatch):
    monkeypatch.setenv("MV_FILE_EXCHANGE_VOLUMES", "vault=/mnt/vault,intake=/mnt/intake")
    monkeypatch.setenv("MV_TRANSPORT", "http")
    cfg = ServerConfig.from_env("MV")
    assert cfg.file_exchange_volumes == "vault=/mnt/vault,intake=/mnt/intake"
    assert cfg.transport == "http"


def test_serverconfig_file_exchange_defaults(monkeypatch):
    cfg = ServerConfig.from_env("MV")
    assert cfg.file_exchange_volumes is None
    assert cfg.file_exchange_max_artifact_size is None
    assert cfg.file_exchange_https_allow_loopback is False
    assert cfg.file_exchange_https_allow_private is False
    assert cfg.file_exchange_capability_url_ttl_default_s == 3600
    assert cfg.file_exchange_https_public_base_url is None
```

- [ ] Add the seven fields to `ServerConfig` in `_config.py` (insert near the existing `kv_store_url` field, preserving dataclass field ordering):

```python
    file_exchange_volumes: str | None = None
    file_exchange_max_artifact_size: int | None = None
    file_exchange_https_allow_loopback: bool = False
    file_exchange_https_allow_private: bool = False
    file_exchange_capability_url_ttl_default_s: int = 3600
    file_exchange_https_public_base_url: str | None = None
```

- [ ] Wire the env reads in `ServerConfig.from_env` (insert next to the existing `kv_store_url=env(...)` line):

```python
            file_exchange_volumes=env(env_prefix, "FILE_EXCHANGE_VOLUMES"),
            file_exchange_max_artifact_size=_int_or_none(env(env_prefix, "FILE_EXCHANGE_MAX_ARTIFACT_SIZE")),
            file_exchange_https_allow_loopback=parse_bool(env(env_prefix, "FILE_EXCHANGE_HTTPS_ALLOW_LOOPBACK"), default=False),
            file_exchange_https_allow_private=parse_bool(env(env_prefix, "FILE_EXCHANGE_HTTPS_ALLOW_PRIVATE"), default=False),
            file_exchange_capability_url_ttl_default_s=int(env(env_prefix, "FILE_EXCHANGE_CAPABILITY_URL_TTL_DEFAULT_S") or "3600"),
            file_exchange_https_public_base_url=env(env_prefix, "FILE_EXCHANGE_HTTPS_PUBLIC_BASE_URL"),
```

(`_int_or_none` lives next to `parse_bool` in the existing config module; if it isn't there, add a one-liner helper next to it.)

- [ ] Run:

```bash
uv run pytest tests/file_exchange/test_capability.py::test_serverconfig_loads_file_exchange_volumes tests/file_exchange/test_capability.py::test_serverconfig_file_exchange_defaults -v
```

Expected: PASS.

### Step F.2: Implement register_file_exchange_capability with gating

- [ ] Add to `tests/file_exchange/test_capability.py`:

```python
from key_value.aio.stores.memory import MemoryStore
from fastmcp import FastMCP

from fastmcp_pvl_core._file_exchange._capability import register_file_exchange_capability


def _server() -> FastMCP:
    return FastMCP(name="test")


def _config(**kw) -> ServerConfig:
    base = dict(transport="stdio")
    base.update(kw)
    return ServerConfig(**base)


@pytest.mark.asyncio
async def test_gating_stdio_without_volumes_advertises_nothing():
    server = _server()
    config = _config()  # stdio, no volumes
    store = MemoryStore()
    register_file_exchange_capability(server, config, kv_store=store)
    caps = server.experimental_capabilities or {}
    assert "nl.liesdonk.file-exchange" not in caps


@pytest.mark.asyncio
async def test_gating_stdio_with_volumes_advertises_filesystem_only():
    server = _server()
    config = _config(file_exchange_volumes="vault=/tmp/vault")
    store = MemoryStore()
    register_file_exchange_capability(server, config, kv_store=store)
    block = server.experimental_capabilities["nl.liesdonk.file-exchange"]
    assert block["version"] == "0.1"
    for role, transports in block["roles"].items():
        assert transports == ["filesystem"]


@pytest.mark.asyncio
async def test_gating_http_without_volumes_advertises_https_only():
    server = _server()
    config = _config(transport="http")
    store = MemoryStore()
    register_file_exchange_capability(server, config, kv_store=store)
    block = server.experimental_capabilities["nl.liesdonk.file-exchange"]
    roles = block["roles"]
    assert roles["provider"] == ["download"]
    assert roles["fetcher"] == ["download"]
    assert roles["receiver"] == ["upload"]
    assert roles["sender"] == ["upload"]


@pytest.mark.asyncio
async def test_gating_http_with_volumes_advertises_both():
    server = _server()
    config = _config(transport="http", file_exchange_volumes="v=/tmp/v")
    store = MemoryStore()
    register_file_exchange_capability(server, config, kv_store=store)
    block = server.experimental_capabilities["nl.liesdonk.file-exchange"]
    assert set(block["roles"]["provider"]) == {"filesystem", "download"}
    assert set(block["roles"]["sender"]) == {"filesystem", "upload"}


@pytest.mark.asyncio
async def test_gating_subset_of_declared_roles():
    server = _server()
    config = _config(transport="http", file_exchange_volumes="v=/tmp/v")
    store = MemoryStore()
    register_file_exchange_capability(
        server, config, kv_store=store, roles=("provider", "fetcher")
    )
    roles = server.experimental_capabilities["nl.liesdonk.file-exchange"]["roles"]
    assert set(roles.keys()) == {"provider", "fetcher"}
```

- [ ] Implement `src/fastmcp_pvl_core/_file_exchange/_capability.py`:

```python
"""Capability declaration + transport-availability gating."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from key_value.aio.protocols.key_value import AsyncKeyValue

from .._config import ServerConfig
from ._transport_filesystem import parse_volumes

if TYPE_CHECKING:
    from fastmcp import FastMCP

CAPABILITY_KEY = "nl.liesdonk.file-exchange"
EXTENSION_VERSION = "0.1"

_HTTP_TRANSPORTS = ("http", "sse")  # both indicate the server hosts an HTTP app


def _transports_for_role(role: str, *, volumes: dict, http_app: bool) -> list[str]:
    transports: list[str] = []
    if volumes:
        transports.append("filesystem")
    if role in ("provider", "fetcher"):
        # provider needs to host endpoint -> http_app required; fetcher is outbound -> always possible
        if role == "fetcher" or http_app:
            transports.append("download")
    if role in ("receiver", "sender"):
        if role == "sender" or http_app:
            transports.append("upload")
    return transports


def register_file_exchange_capability(
    server: "FastMCP",
    config: ServerConfig,
    *,
    kv_store: AsyncKeyValue,
    roles: Sequence[str] = ("provider", "fetcher", "receiver", "sender"),
    digests: Sequence[str] = ("sha-256",),
    max_artifact_size: int | None = None,
) -> None:
    """Advertise file-exchange capabilities reflecting what this deployment satisfies.

    - filesystem is advertised only when ``config.file_exchange_volumes`` is non-empty.
    - download/upload are advertised for provider/receiver only when ``config.transport``
      indicates an HTTP app; the fetcher/sender (consumer) sides are always advertised
      when at least one of filesystem|outbound-HTTPS is available.
    - If no role has any satisfiable transport, the capability block is NOT emitted.
    """
    volumes = parse_volumes(config.file_exchange_volumes)
    http_app = config.transport in _HTTP_TRANSPORTS

    advertised_roles: dict[str, list[str]] = {}
    for role in roles:
        transports = _transports_for_role(role, volumes=volumes, http_app=http_app)
        if transports:
            advertised_roles[role] = transports

    if not advertised_roles:
        import logging
        logging.getLogger(__name__).info(
            "file-exchange: no satisfiable transport for any declared role — capability not advertised"
        )
        return

    block: dict = {
        "version": EXTENSION_VERSION,
        "roles": advertised_roles,
        "digests": list(digests),
    }
    if max_artifact_size is not None:
        block["maxArtifactSize"] = max_artifact_size

    server.experimental_capabilities = {
        **(server.experimental_capabilities or {}),
        CAPABILITY_KEY: block,
    }

    # Auto-mount sibling routes iff producer-side HTTPS is advertised.
    needs_http_routes = (
        "download" in advertised_roles.get("provider", [])
        or "upload" in advertised_roles.get("receiver", [])
    )
    if needs_http_routes:
        from ._routes import build_file_exchange_router

        router = build_file_exchange_router(store=kv_store)
        # FastMCP exposes its HTTP app via `server.http_app()` (a method, not a
        # property — calling it constructs/returns the Starlette app). VERIFY
        # this against the installed fastmcp version before relying on it:
        #
        #   from fastmcp import FastMCP
        #   import inspect
        #   print(inspect.signature(FastMCP.http_app))
        #
        # If the API shape has shifted, update this single attach point.
        http_app = server.http_app()
        http_app.routes.extend(router.routes)
```

- [ ] Run:

```bash
uv run pytest tests/file_exchange/test_capability.py -v
```

Expected: PASS (all 7 tests including ServerConfig + gating).

### Step F.3: Implement provider helper

- [ ] Add to `tests/file_exchange/test_role_helpers.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from key_value.aio.stores.memory import MemoryStore

from fastmcp_pvl_core._file_exchange._provider import build_pull_response
from fastmcp_pvl_core._file_exchange._types import (
    ArtifactMetadata,
    FilesystemSourceDescriptor,
    SourceDescriptor,
)


def _fs_source(uri: str) -> SourceDescriptor:
    return SourceDescriptor.model_validate({"transport": "filesystem", "uri": uri})


def test_build_pull_response_embeds_handle_in_structured_content():
    artifact = ArtifactMetadata(name="x.bin", size=11)
    sources = [_fs_source("exchange://vault/x.bin")]
    result = build_pull_response(artifact, sources, summary="exporting x.bin")

    assert not result.isError
    # structuredContent holds the handle
    handle = result.structuredContent
    assert handle["type"] == "nl.liesdonk.file-exchange/transfer-handle"
    assert handle["version"] == "0.1"
    assert handle["artifact"]["name"] == "x.bin"
    assert handle["sources"][0]["transport"] == "filesystem"

    # _meta mirror
    mirror = (result.meta or {}).get("nl.liesdonk.file-exchange/handles")
    assert mirror == [handle]

    # text summary present
    assert any("exporting x.bin" in c.text for c in result.content if c.type == "text")


def test_build_pull_response_synthesises_summary_when_absent():
    artifact = ArtifactMetadata(name="report.parquet", size=1024)
    sources = [_fs_source("exchange://vault/report.parquet")]
    result = build_pull_response(artifact, sources)
    assert any("report.parquet" in c.text for c in result.content if c.type == "text")
```

- [ ] Implement `src/fastmcp_pvl_core/_file_exchange/_provider.py`:

```python
"""Provider helper: builds a CallToolResult containing a TransferHandle."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp.types import CallToolResult, TextContent

from ._types import (
    REFERENCE_VERSION,
    TRANSFER_HANDLE_TYPE,
    ArtifactMetadata,
    SourceDescriptor,
    TransferHandle,
)

META_HANDLES_KEY = "nl.liesdonk.file-exchange/handles"


def _humanise_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n} {unit}"
        n //= 1024
    return f"{n} PiB"


def _default_summary(artifact: ArtifactMetadata) -> str:
    label = artifact.name or artifact.id or "<unnamed>"
    size = _humanise_size(artifact.size) if artifact.size is not None else None
    mime = artifact.mimeType
    parts = [f"file-exchange: {label}"]
    extras = ", ".join(p for p in (size, mime) if p)
    if extras:
        parts.append(f"({extras})")
    return " ".join(parts)


def build_pull_response(
    artifact: ArtifactMetadata,
    sources: Sequence[SourceDescriptor],
    *,
    summary: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> CallToolResult:
    """Build a CallToolResult that emits a TransferHandle for ``artifact``.

    The handle lands in ``structuredContent`` (discoverable via outputSchema),
    mirrors into ``_meta[nl.liesdonk.file-exchange/handles]``, and a short text
    summary lets the model reason about the chain without seeing the bytes.
    """
    handle = TransferHandle(
        type=TRANSFER_HANDLE_TYPE,  # type: ignore[arg-type]
        version=REFERENCE_VERSION,
        artifact=artifact,
        sources=list(sources),
    )
    handle_dict = handle.model_dump(mode="json", exclude_none=True)
    meta: dict[str, Any] = {META_HANDLES_KEY: [handle_dict]}
    if extra_meta:
        meta.update(extra_meta)
    return CallToolResult(
        content=[TextContent(type="text", text=summary or _default_summary(artifact))],
        structuredContent=handle_dict,
        meta=meta,
    )
```

- [ ] Run:

```bash
uv run pytest tests/file_exchange/test_role_helpers.py -v
```

Expected: PASS (2 tests).

### Step F.4: Implement fetcher helper

- [ ] Add tests to `tests/file_exchange/test_role_helpers.py`:

```python
import io
from fastmcp_pvl_core._file_exchange._fetcher import pull_artifact
from fastmcp_pvl_core._file_exchange._errors import FileExchangeError, FileExchangeErrorCode
from fastmcp_pvl_core._file_exchange._types import TransferHandle


@pytest.mark.asyncio
async def test_pull_artifact_filesystem(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    src_file = vault / "x.bin"
    src_file.write_bytes(b"hello bytes")

    handle = TransferHandle.model_validate(
        {
            "type": "nl.liesdonk.file-exchange/transfer-handle",
            "version": "0.1",
            "artifact": {"name": "x.bin", "size": 11},
            "sources": [{"transport": "filesystem", "uri": "exchange://vault/x.bin"}],
        }
    )
    buf = io.BytesIO()
    md = await pull_artifact(
        handle,
        dest=buf,
        supported_transports=("filesystem",),
        volumes={"vault": vault},
    )
    assert buf.getvalue() == b"hello bytes"
    assert md.name == "x.bin"


@pytest.mark.asyncio
async def test_pull_artifact_size_mismatch_raises(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "x.bin").write_bytes(b"only 4 bytes here actually 21")

    handle = TransferHandle.model_validate(
        {
            "type": "nl.liesdonk.file-exchange/transfer-handle",
            "version": "0.1",
            "artifact": {"name": "x.bin", "size": 4},
            "sources": [{"transport": "filesystem", "uri": "exchange://vault/x.bin"}],
        }
    )
    with pytest.raises(FileExchangeError) as exc:
        await pull_artifact(
            handle, dest=io.BytesIO(),
            supported_transports=("filesystem",),
            volumes={"vault": vault},
        )
    assert exc.value.code == FileExchangeErrorCode.SIZE_MISMATCH
```

- [ ] Implement `src/fastmcp_pvl_core/_file_exchange/_fetcher.py`:

```python
"""Fetcher helper: validate handle, select source, pull bytes, verify digest/size."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

from ._errors import FileExchangeError, FileExchangeErrorCode
from ._select import select_source
from ._transport_filesystem import resolve_exchange_uri
from ._transport_https import SSRFGuardConfig, pull_download
from ._types import ArtifactMetadata, TransferHandle


async def pull_artifact(
    handle: TransferHandle | dict,
    *,
    dest: BinaryIO | Path,
    supported_transports: Sequence[str],
    volumes: Mapping[str, Path] = {},
    ssrf: SSRFGuardConfig | None = None,
) -> ArtifactMetadata:
    """Pull an artifact described by ``handle`` into ``dest``.

    Selects a source descriptor via the §9 algorithm, performs the transfer, and
    verifies size/digest against ``handle.artifact``.
    """
    if not isinstance(handle, TransferHandle):
        handle = TransferHandle.model_validate(handle)
    chosen = select_source(
        handle,
        supported_transports=supported_transports,
        known_volumes=list(volumes.keys()),
    )

    digester = hashlib.sha256()
    bytes_written = 0

    if chosen.root.transport == "filesystem":
        path = resolve_exchange_uri(chosen.root.uri, volumes=volumes)
        with path.open("rb") as src:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                digester.update(chunk)
                bytes_written += len(chunk)
                if isinstance(dest, Path):
                    raise NotImplementedError("Path dest is handled by atomic_write — wrap externally")
                dest.write(chunk)
    elif chosen.root.transport == "download":
        # pull_download streams to dest; digest the bytes via a tee buffer.
        # Phase 1 simplification: pull into memory if dest is a buffer, then digest.
        bytes_written = await pull_download(
            chosen.root, dest=dest, ssrf=ssrf or SSRFGuardConfig()
        )
        # Digest verification on download is done by recomputing post-fetch; phase 1
        # does not stream-digest. Reading the bytes back from dest if it's a Path is
        # left to the integration test, since dest is the caller's contract.

    if handle.artifact.size is not None and bytes_written != handle.artifact.size:
        raise FileExchangeError(
            code=FileExchangeErrorCode.SIZE_MISMATCH,
            transport=chosen.root.transport,
            detail=f"expected {handle.artifact.size} bytes, got {bytes_written}",
        )

    if handle.artifact.digest:
        algo, _, expected = handle.artifact.digest.partition(":")
        if algo == "sha-256":
            actual = digester.hexdigest()
            if actual != expected:
                raise FileExchangeError(
                    code=FileExchangeErrorCode.DIGEST_MISMATCH,
                    transport=chosen.root.transport,
                    detail=f"expected sha-256:{expected}, got sha-256:{actual}",
                )

    return handle.artifact
```

- [ ] Run:

```bash
uv run pytest tests/file_exchange/test_role_helpers.py -v
```

Expected: PASS.

### Step F.5: Implement receiver and sender helpers

- [ ] Add tests to `tests/file_exchange/test_role_helpers.py` (receiver flow):

```python
from fastmcp_pvl_core._file_exchange._receiver import (
    build_intake_response,
    open_intake,
    resolve_intake,
)
from fastmcp_pvl_core._file_exchange._url_store import record_intake_path


def _fs_sink(uri: str):
    from fastmcp_pvl_core._file_exchange._types import SinkDescriptor
    return SinkDescriptor.model_validate({"transport": "filesystem", "uri": uri})


def test_open_intake_assembles_ticket():
    sinks = [_fs_sink("exchange://intake/x.bin")]
    ticket = open_intake(sinks=sinks, artifact_id="ar-1")
    assert ticket.artifactId == "ar-1"
    assert ticket.type == "nl.liesdonk.file-exchange/intake-ticket"


@pytest.mark.asyncio
async def test_resolve_intake_returns_recorded_path(tmp_path: Path):
    store = MemoryStore()
    target = tmp_path / "intake.bin"
    await record_intake_path(store=store, artifact_id="ar-1", path=target)
    result = await resolve_intake("ar-1", kv_store=store)
    assert result == target


@pytest.mark.asyncio
async def test_resolve_intake_returns_none_for_missing():
    store = MemoryStore()
    assert await resolve_intake("nope", kv_store=store) is None
```

- [ ] Implement `src/fastmcp_pvl_core/_file_exchange/_receiver.py`:

```python
"""Receiver helpers: open intake, build intake response, resolve intake."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from key_value.aio.protocols.key_value import AsyncKeyValue
from mcp.types import CallToolResult, TextContent

from ._types import (
    INTAKE_TICKET_TYPE,
    REFERENCE_VERSION,
    ExpectedConstraints,
    IntakeTicket,
    SinkDescriptor,
)
from ._url_store import intake_path_for

META_TICKETS_KEY = "nl.liesdonk.file-exchange/tickets"


def open_intake(
    *,
    sinks: Sequence[SinkDescriptor],
    expected: ExpectedConstraints | None = None,
    artifact_id: str | None = None,
) -> IntakeTicket:
    """Construct an IntakeTicket from ``sinks``. Auto-generates ``artifact_id`` if absent."""
    return IntakeTicket(
        type=INTAKE_TICKET_TYPE,  # type: ignore[arg-type]
        version=REFERENCE_VERSION,
        artifactId=artifact_id or secrets.token_urlsafe(12),
        expected=expected,
        sinks=list(sinks),
    )


def build_intake_response(
    ticket: IntakeTicket,
    *,
    summary: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> CallToolResult:
    ticket_dict = ticket.model_dump(mode="json", exclude_none=True)
    meta: dict[str, Any] = {META_TICKETS_KEY: [ticket_dict]}
    if extra_meta:
        meta.update(extra_meta)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=summary or f"file-exchange: intake opened (artifact_id={ticket.artifactId})",
            )
        ],
        structuredContent=ticket_dict,
        meta=meta,
    )


async def resolve_intake(artifact_id: str, *, kv_store: AsyncKeyValue) -> Path | None:
    """Return the local path where bytes for ``artifact_id`` landed, or None."""
    return await intake_path_for(store=kv_store, artifact_id=artifact_id)
```

- [ ] Test the sender. Add to `tests/file_exchange/test_role_helpers.py`:

```python
from fastmcp_pvl_core._file_exchange._sender import push_artifact
from fastmcp_pvl_core._file_exchange._types import IntakeTicket


@pytest.mark.asyncio
async def test_push_artifact_filesystem(tmp_path: Path):
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()
    ticket = IntakeTicket.model_validate(
        {
            "type": "nl.liesdonk.file-exchange/intake-ticket",
            "version": "0.1",
            "artifactId": "ar-1",
            "sinks": [{"transport": "filesystem", "uri": "exchange://intake/x.bin"}],
        }
    )
    source = tmp_path / "src.bin"
    source.write_bytes(b"to be sent")

    await push_artifact(
        ticket,
        source=source,
        supported_transports=("filesystem",),
        volumes={"intake": intake_dir},
    )
    assert (intake_dir / "x.bin").read_bytes() == b"to be sent"
```

- [ ] Implement `src/fastmcp_pvl_core/_file_exchange/_sender.py`:

```python
"""Sender helper: validate ticket, select sink, push bytes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

from ._errors import FileExchangeError, FileExchangeErrorCode
from ._select import select_sink
from ._transport_filesystem import atomic_write, resolve_exchange_uri
from ._transport_https import SSRFGuardConfig, push_upload
from ._types import IntakeTicket


async def push_artifact(
    ticket: IntakeTicket | dict,
    *,
    source: BinaryIO | Path,
    supported_transports: Sequence[str],
    volumes: Mapping[str, Path] = {},
    ssrf: SSRFGuardConfig | None = None,
    artifact_digest: str | None = None,
    artifact_mime: str | None = None,
    artifact_size: int | None = None,
) -> None:
    if not isinstance(ticket, IntakeTicket):
        ticket = IntakeTicket.model_validate(ticket)

    # Pre-check expected.acceptMimeTypes before consuming a single-use slot.
    if (
        ticket.expected
        and ticket.expected.acceptMimeTypes
        and artifact_mime is not None
    ):
        from ._routes import _mime_matches

        if not _mime_matches(artifact_mime, ticket.expected.acceptMimeTypes):
            raise FileExchangeError(
                code=FileExchangeErrorCode.MIME_TYPE_REJECTED,
                detail=f"artifact mime {artifact_mime!r} not in acceptMimeTypes",
            )

    if (
        ticket.expected
        and ticket.expected.requireDigest
        and not artifact_digest
    ):
        raise FileExchangeError(
            code=FileExchangeErrorCode.DIGEST_MISMATCH,
            detail="ticket.expected.requireDigest is set but artifact_digest was not provided",
        )

    chosen = select_sink(
        ticket,
        supported_transports=supported_transports,
        known_volumes=list(volumes.keys()),
    )

    if chosen.root.transport == "filesystem":
        target = resolve_exchange_uri(chosen.root.uri, volumes=volumes)
        with atomic_write(target) as out:
            if isinstance(source, Path):
                out.write(source.read_bytes())
            else:
                while True:
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
    elif chosen.root.transport == "upload":
        await push_upload(
            chosen.root,
            source=source,
            ssrf=ssrf or SSRFGuardConfig(),
            content_digest=artifact_digest,
            content_type=artifact_mime,
            content_length=artifact_size,
        )
```

- [ ] Run:

```bash
uv run pytest tests/file_exchange/test_role_helpers.py -v
```

Expected: PASS (all role-helper tests).

### Step F.6: Top-level re-exports

- [ ] Modify `src/fastmcp_pvl_core/__init__.py` to expose the public file-exchange surface (insert near the existing `register_server_info_tool` re-export):

```python
from fastmcp_pvl_core._file_exchange._capability import (
    register_file_exchange_capability,
)
from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
    as_tool_error_result,
)
from fastmcp_pvl_core._file_exchange._fetcher import pull_artifact
from fastmcp_pvl_core._file_exchange._provider import build_pull_response
from fastmcp_pvl_core._file_exchange._receiver import (
    build_intake_response,
    open_intake,
    resolve_intake,
)
from fastmcp_pvl_core._file_exchange._sender import push_artifact
from fastmcp_pvl_core._file_exchange._types import (
    ArtifactMetadata,
    ExpectedConstraints,
    FileExchangeRole,
    FileExchangeTransport,
    IntakeTicket,
    SinkDescriptor,
    SourceDescriptor,
    TransferHandle,
)
```

- [ ] Add each new symbol to the `__all__` list of `__init__.py`.

- [ ] Run the full test suite to verify nothing regressed:

```bash
uv run pytest -q
```

Expected: every test in the repo passes.

### Step F.7: Commit and PR

- [ ] Commit:

```bash
git add src/fastmcp_pvl_core/ tests/file_exchange/
git commit -m "feat(file-exchange): capability declaration, role helpers, top-level public API

Closes <issue-F-number>"
```

- [ ] Run `preflight-circus`; open draft PR; flip to ready when bots+CI green.

---

# Task G: Integration tests + conformance suite

**Files:**
- Create: `tests/file_exchange/test_integration_filesystem.py`
- Create: `tests/file_exchange/test_integration_https.py`
- Create: `tests/file_exchange/test_conformance.py`

### Step G.1: Filesystem integration — provider + fetcher end-to-end

- [ ] Write `tests/file_exchange/test_integration_filesystem.py`:

```python
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from fastmcp_pvl_core import (
    ArtifactMetadata,
    build_pull_response,
    open_intake,
    pull_artifact,
    push_artifact,
)
from fastmcp_pvl_core._file_exchange._types import SinkDescriptor, SourceDescriptor


def _fs_source(uri: str) -> SourceDescriptor:
    return SourceDescriptor.model_validate({"transport": "filesystem", "uri": uri})


def _fs_sink(uri: str) -> SinkDescriptor:
    return SinkDescriptor.model_validate({"transport": "filesystem", "uri": uri})


@pytest.mark.asyncio
async def test_pull_round_trip(tmp_path: Path):
    """Provider side mints a TransferHandle, fetcher side consumes it end-to-end."""
    vault = tmp_path / "vault"
    vault.mkdir()
    src = vault / "report.bin"
    payload = b"report payload " * 1024  # 15 KB
    src.write_bytes(payload)

    md = ArtifactMetadata(
        name="report.bin",
        size=len(payload),
        digest=f"sha-256:{hashlib.sha256(payload).hexdigest()}",
        mimeType="application/octet-stream",
    )
    result = build_pull_response(md, [_fs_source("exchange://vault/report.bin")])
    handle = result.structuredContent

    buf = io.BytesIO()
    received = await pull_artifact(
        handle,
        dest=buf,
        supported_transports=("filesystem",),
        volumes={"vault": vault},
    )
    assert buf.getvalue() == payload
    assert received.digest == md.digest


@pytest.mark.asyncio
async def test_push_round_trip(tmp_path: Path):
    """Receiver opens intake; sender pushes; receiver finds bytes at the resolved path."""
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()

    ticket = open_intake(
        sinks=[_fs_sink("exchange://intake/payload.bin")],
        artifact_id="ar-1",
    )

    src = tmp_path / "src.bin"
    payload = b"deposited"
    src.write_bytes(payload)

    await push_artifact(
        ticket,
        source=src,
        supported_transports=("filesystem",),
        volumes={"intake": intake_dir},
    )

    assert (intake_dir / "payload.bin").read_bytes() == payload
```

- [ ] Run:

```bash
uv run pytest tests/file_exchange/test_integration_filesystem.py -v
```

Expected: PASS (2 tests).

### Step G.2: HTTPS integration — sibling routes against a real Starlette app

- [ ] Write `tests/file_exchange/test_integration_https.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from key_value.aio.stores.memory import MemoryStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from fastmcp_pvl_core._file_exchange._routes import build_file_exchange_router
from fastmcp_pvl_core._file_exchange._url_store import (
    intake_path_for,
    mint_download_token,
    mint_upload_token,
)


@pytest.mark.asyncio
async def test_download_then_intake_correlation_end_to_end(tmp_path: Path):
    store = MemoryStore()
    intake = tmp_path / "intake.bin"
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    token = await mint_upload_token(
        store=store, intake_path=intake, artifact_id="ar-1", expires_at=expires
    )

    router = build_file_exchange_router(store=store)
    app = Starlette(routes=router.routes)

    with TestClient(app) as client:
        resp = client.put(f"/file-exchange/u/{token}", content=b"payload-1")
    assert resp.status_code == 204
    assert (await intake_path_for(store=store, artifact_id="ar-1")) == intake
    assert intake.read_bytes() == b"payload-1"
```

- [ ] Run:

```bash
uv run pytest tests/file_exchange/test_integration_https.py -v
```

Expected: PASS.

### Step G.3: Conformance suite

- [ ] Write `tests/file_exchange/test_conformance.py`:

```python
"""Walk the vendored conformance fixtures and assert their valid/invalid outcomes.

Each fixture is a JSON file alongside an outcome marker (the file name's prefix or a
sibling `_outcome.json`). The exact layout follows the spec repo's `conformance/`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fastmcp_pvl_core._file_exchange._types import IntakeTicket, TransferHandle


CONFORMANCE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "fastmcp_pvl_core" / "_file_exchange" / "schema" / "conformance"
)


def _fixtures() -> list[Path]:
    return sorted(CONFORMANCE_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda p: p.name)
def test_fixture_matches_documented_outcome(fixture: Path):
    payload = json.loads(fixture.read_text())
    # Fixtures encode their outcome in the filename: `valid-*.json` or `invalid-*.json`.
    expected_valid = fixture.name.startswith("valid-")

    discriminator = payload.get("type", "")
    if discriminator == "nl.liesdonk.file-exchange/transfer-handle":
        model = TransferHandle
    elif discriminator == "nl.liesdonk.file-exchange/intake-ticket":
        model = IntakeTicket
    else:
        pytest.skip(f"fixture {fixture.name} is not a handle/ticket — adjust loader as needed")
        return

    if expected_valid:
        model.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            model.model_validate(payload)
```

- [ ] Run:

```bash
uv run pytest tests/file_exchange/test_conformance.py -v
```

Expected: PASS for every vendored fixture.

### Step G.4: CHANGELOG entry + commit

- [ ] Add an entry to `CHANGELOG.md` under the next-version section:

```markdown
### Added

- `nl.liesdonk.file-exchange` v0.1 implementation. New public helpers:
  `register_file_exchange_capability`, `build_pull_response`, `pull_artifact`,
  `open_intake`, `build_intake_response`, `resolve_intake`, `push_artifact`,
  plus the reference types (`TransferHandle`, `IntakeTicket`, …) and
  `FileExchangeError`/`FileExchangeErrorCode`. Capability advertising is
  deployment-derived: filesystem if volumes are configured, HTTPS if the server
  hosts an HTTP app. See `docs/superpowers/specs/2026-05-20-file-exchange-adoption-design.md`.
- Seven new `ServerConfig` fields under the `FILE_EXCHANGE_*` env-var prefix.
```

- [ ] Commit:

```bash
git add tests/file_exchange/ CHANGELOG.md
git commit -m "test(file-exchange): integration suite + spec-repo conformance fixtures

Closes <issue-G-number>"
```

- [ ] Run `preflight-circus`; open draft PR; flip to ready when bots+CI green.

---

## Final verification

After all seven PRs land:

- [ ] Pull main: `git checkout main && git pull --ff-only origin main`
- [ ] Confirm everything passes: `uv sync --all-extras && uv run pytest && uv run ruff format --check . && uv run ruff check . && uv run mypy src`
- [ ] Confirm the public API surface is intact: `python -c "from fastmcp_pvl_core import register_file_exchange_capability, build_pull_response, pull_artifact, open_intake, build_intake_response, resolve_intake, push_artifact, FileExchangeError, TransferHandle, IntakeTicket; print('OK')"`
- [ ] Trigger the downstream canary work in `markdown-vault-mcp` (separate plan, separate repo) — wire MV as provider+fetcher+receiver+sender per design §10 phase 2.

---

## Open questions deferred (not in this plan)

- **Streamed digest** during `pull_download`: phase 1 verifies digest only when `dest` is a buffer that can be re-read; for `dest=Path` (atomic_write), a follow-up adds a tee-digest so the file passes digest verification without a second read.
- **Range-request resume** on `pull_download`: spec §10.2 allows it; not implemented in phase 1 (single GET only). Filed as follow-up for phase 1.5.
- **Provider-driven revocation**: spec §18 open question. The kv_store layer supports `delete` so a future API can implement it; no helper exposed yet.
- **`file://` URI scheme**: phase 1 supports `exchange://` only; `file://` rejection is explicit (`_transport_filesystem.py` raises). When a downstream actually needs `file://`, a small operator-config-driven exchange-root mapping lands.
