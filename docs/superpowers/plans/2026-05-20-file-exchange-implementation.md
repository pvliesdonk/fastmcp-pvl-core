# File-exchange v0.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt the external `pvliesdonk/mcp-file-exchange-ext` v0.1 spec as pvl-core's shared, opinionated implementation across seven PR-sized tasks (A–G), closing issues #124 through #130.

**Architecture:** Seven sibling files under `src/fastmcp_pvl_core/_file_exchange/` implement the namespace `nl.liesdonk.file-exchange`. Public symbols re-export from `fastmcp_pvl_core`. Capability declaration is operator-driven via `ServerConfig` + env-vars (`{PREFIX}_FILE_EXCHANGE_*`); the only domain hooks are the role declaration and the `kv_store` injection. HTTPS sibling routes mount via `FastMCP.custom_route(...)`. The kv_store factory (`build_kv_store`) shipped in PR #122 is the only prerequisite.

**Tech Stack:** Python 3.10–3.13, FastMCP ≥3.3.1, Pydantic v2, `key-value-aio` (namespaced via `PrefixCollectionsWrapper`), httpx (HTTPS consumer side), Starlette (routes mounted via `FastMCP.custom_route`), pytest + `pytest-asyncio`.

**Sequencing:**

```
A (types + schema) ────────┐
                           ├─► B (select + errors) ─┐
                           ├─► C (filesystem) ──────┤
                           ├─► D (HTTPS consumer) ──┼─► F (capability + role helpers) ─► G (integration)
                           └─► E (URL store + routes)┘
```

A is the critical path. B/C/D/E run in parallel after A merges. F depends on B+C+D+E. G depends on F.

**Authoritative source:** `/mnt/code/fastmcp-pvl-core/docs/superpowers/specs/2026-05-20-file-exchange-adoption-design.md`. If any step appears to contradict the design doc, STOP and ask — do not improvise.

**Pre-write API sanity check (run once at the start of every task):**

```bash
uv run python -c "from pydantic import RootModel, BaseModel; print('model_validate:', hasattr(RootModel, 'model_validate'), hasattr(BaseModel, 'model_validate'))"
# Expected: model_validate: True True
uv run python -c "from fastmcp import FastMCP; import inspect; print(inspect.signature(FastMCP.custom_route))"
# Expected: (self, path, methods, name=None, include_in_schema=True) -> Callable[[Callable[[Request], Awaitable[Response]]], ...]
uv run python -c "from fastmcp_pvl_core import compute_app_domain, ServerConfig; import inspect; print(inspect.signature(compute_app_domain))"
# Expected: (config: 'ServerConfig') -> 'str | None'
```

Use `.model_validate(...)` on `BaseModel` / `RootModel`. **There is no `validate-python` method on a model** — that lives on `TypeAdapter`. Do not type the rejected method name into any test or implementation.

---

## Universal per-task suffix

Every task ends with these gates before opening its PR. Do not skip.

- [ ] **Run full local checks**

```bash
uv sync --all-extras
uv run pytest tests/file_exchange -v
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: all green.

- [ ] **Run preflight-circus skill against the cumulative diff**

Invoke the `preflight-circus` skill against `origin/main..HEAD`. The skill dispatches the five lenses in parallel and applies the Haiku confidence filter. **Bar for "clean": nothing flagged at confidence ≥ 80 by any lens.** If any finding fires at ≥80, address it locally — do NOT push and let bots catch it. Re-run the full circus on the new diff before push.

- [ ] **Open as draft PR**

```bash
git push -u origin <branch>
gh pr create --draft --title "<title>" --body "$(cat <<'EOF'
## Summary
<1–3 bullets>

Closes #<issue>.

## Test plan
- [ ] Local unit tests pass: `uv run pytest tests/file_exchange -v`
- [ ] Local ruff + mypy clean
- [ ] preflight-circus returned clean against BASE..HEAD

EOF
)"
```

- [ ] **Verify bot verdicts before flipping ready**

Read `claude-review`'s posted body (not just the check status — green ≠ approved); look for `Still Open`, `must be fixed`, negative recommendations. Read `gemini-code-assist`'s body too. If a bot finds something despite local clearance, address it, re-run the full circus on the fix, push, and cap iteration at one round. If anything still fires on the second round, surface to the user — do not push a third time silently.

- [ ] **Flip to ready** with `gh pr ready <N>` only when (a) local circus was clean, (b) bot bodies say LGTM, (c) CI fully green.

---

## Task A: Vendor v0.1 schema and Pydantic types (closes #124)

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/schema/file-exchange.schema.json` (vendored verbatim from `pvliesdonk/mcp-file-exchange-ext` v0.1 at a pinned commit)
- Create: `src/fastmcp_pvl_core/_file_exchange/schema/.expected-sha256`
- Create: `src/fastmcp_pvl_core/_file_exchange/schema/PINNED_AT.md`
- Create: `src/fastmcp_pvl_core/_file_exchange/schema/conformance/` (vendored fixtures directory)
- Create: `src/fastmcp_pvl_core/_file_exchange/_types.py`
- Create: `tests/file_exchange/__init__.py`
- Create: `tests/file_exchange/test_types.py`
- Create: `tests/file_exchange/test_schema_drift.py`

### Step A.1 — Vendor the schema and fixtures

- [ ] **Pin and copy** the schema + conformance fixtures from `pvliesdonk/mcp-file-exchange-ext` v0.1 (HEAD of the tagged v0.1 commit). Compute SHA-256 of the schema, write to `.expected-sha256`. Record the source repo + commit SHA in `PINNED_AT.md`.

```bash
cd /tmp && git clone https://github.com/pvliesdonk/mcp-file-exchange-ext
cd mcp-file-exchange-ext && git rev-parse HEAD > /tmp/pin.txt && cat /tmp/pin.txt
cp schema/file-exchange.schema.json /mnt/code/fastmcp-pvl-core/src/fastmcp_pvl_core/_file_exchange/schema/
cp -r tests/conformance /mnt/code/fastmcp-pvl-core/src/fastmcp_pvl_core/_file_exchange/schema/conformance
cd /mnt/code/fastmcp-pvl-core/src/fastmcp_pvl_core/_file_exchange/schema
python -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path('file-exchange.schema.json').read_bytes()).hexdigest())" > .expected-sha256
```

`PINNED_AT.md` contents:

```markdown
# File-exchange schema pin

- **Source repo:** https://github.com/pvliesdonk/mcp-file-exchange-ext
- **Pinned commit:** <40-char SHA from /tmp/pin.txt>
- **Spec version:** 0.1
- **Vendored on:** 2026-05-20

To re-pin: copy the schema + conformance fixtures from a later commit,
regenerate `.expected-sha256`, update this file. Each re-pin is its own
commit so reviewers see the schema change as a deliberate event.
```

### Step A.2 — Drift gate test (write the failing test first)

- [ ] **Write `tests/file_exchange/test_schema_drift.py`:**

```python
"""Vendored-schema drift gate.

The schema file is copied verbatim from the spec repo. Any local edit
(intentional or accidental) must be accompanied by an explicit re-pin
that updates ``.expected-sha256`` — otherwise this test fails the build.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def test_vendored_schema_matches_expected_sha256() -> None:
    here = Path(__file__).parent.parent.parent / "src" / "fastmcp_pvl_core" / "_file_exchange" / "schema"
    schema_bytes = (here / "file-exchange.schema.json").read_bytes()
    expected = (here / ".expected-sha256").read_text().strip()
    actual = hashlib.sha256(schema_bytes).hexdigest()
    assert actual == expected, (
        f"Vendored schema drift detected.\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}\n"
        f"If the schema was intentionally re-pinned, regenerate .expected-sha256."
    )
```

- [ ] **Run to verify pass** (the file matches its own hash by construction):

```bash
uv run pytest tests/file_exchange/test_schema_drift.py -v
```

Expected: PASS.

### Step A.3 — Write the failing test for `_types.py` core scalars

- [ ] **Write `tests/file_exchange/test_types.py` with the first failing test:**

```python
"""Pydantic model round-trip tests for vendored v0.1 schema.

Each test mirrors a fixture pattern from
`src/fastmcp_pvl_core/_file_exchange/schema/conformance/`.
"""

from __future__ import annotations

import pytest
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


def test_artifact_metadata_minimal_roundtrip() -> None:
    payload = {"id": "abc", "name": "report.pdf", "size": 1024, "mimeType": "application/pdf"}
    artifact = ArtifactMetadata.model_validate(payload)
    assert artifact.id == "abc"
    assert artifact.size == 1024
    assert artifact.model_dump(by_alias=True, exclude_none=True) == payload
```

- [ ] **Run to verify fail:**

```bash
uv run pytest tests/file_exchange/test_types.py::test_artifact_metadata_minimal_roundtrip -v
```

Expected: FAIL — module `fastmcp_pvl_core._file_exchange._types` does not exist.

### Step A.4 — Implement `_types.py` (minimal pass for A.3)

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/__init__.py` (empty for now):**

```python
"""File-exchange implementation. Private package — consumers depend on the
top-level ``fastmcp_pvl_core`` re-exports, not this layout."""
```

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_types.py`:**

```python
"""Pydantic v2 models for the nl.liesdonk.file-exchange v0.1 wire format.

The vendored schema at ``schema/file-exchange.schema.json`` is the wire
authority; these models exist for typed access in Python. A round-trip
test (``test_schema_drift``) and a model→schema check (Step A.7) catch
drift either direction.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

FileExchangeRole = Literal["provider", "fetcher", "receiver", "sender"]
FileExchangeTransport = Literal["filesystem", "download", "upload"]


class _Base(BaseModel):
    """Common config: populate by alias, dump by alias, forbid extras at the
    edges (the spec is closed)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ArtifactMetadata(_Base):
    id: str
    name: str | None = None
    size: int | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    digest: str | None = None


class ExpectedConstraints(_Base):
    max_size: int | None = Field(default=None, alias="maxSize")
    accept_mime_types: tuple[str, ...] | None = Field(default=None, alias="acceptMimeTypes")
    require_digest: bool | None = Field(default=None, alias="requireDigest")


class _FilesystemSource(_Base):
    transport: Literal["filesystem"]
    uri: str  # "exchange://<volume>/<path>" or "file://<abs-path>"


class _DownloadSource(_Base):
    transport: Literal["download"]
    url: str
    expires_at: str | None = Field(default=None, alias="expiresAt")


class _FilesystemSink(_Base):
    transport: Literal["filesystem"]
    uri: str


class _UploadSink(_Base):
    transport: Literal["upload"]
    url: str
    methods: tuple[Literal["PUT", "POST"], ...] = ("PUT",)
    expires_at: str | None = Field(default=None, alias="expiresAt")


# Discriminated unions per spec §6
SourceDescriptor = RootModel[
    Annotated[
        _FilesystemSource | _DownloadSource,
        Field(discriminator="transport"),
    ]
]

SinkDescriptor = RootModel[
    Annotated[
        _FilesystemSink | _UploadSink,
        Field(discriminator="transport"),
    ]
]


class TransferHandle(_Base):
    """Producer → consumer reference (spec §6.1).

    Carries the artifact metadata and one or more source descriptors.
    """

    version: Literal["0.1"] = "0.1"
    artifact: ArtifactMetadata
    sources: tuple[SourceDescriptor, ...]
    must_understand: tuple[str, ...] | None = Field(default=None, alias="mustUnderstand")


class IntakeTicket(_Base):
    """Receiver → sender reference (spec §6.2)."""

    version: Literal["0.1"] = "0.1"
    artifact_id: str = Field(alias="artifactId")
    sinks: tuple[SinkDescriptor, ...]
    expected: ExpectedConstraints | None = None
    must_understand: tuple[str, ...] | None = Field(default=None, alias="mustUnderstand")
```

- [ ] **Run to verify pass:**

```bash
uv run pytest tests/file_exchange/test_types.py::test_artifact_metadata_minimal_roundtrip -v
```

Expected: PASS.

### Step A.5 — Add more failing tests (descriptor unions + handle + ticket)

- [ ] **Append to `test_types.py`:**

```python
def test_source_descriptor_filesystem_variant() -> None:
    desc = SourceDescriptor.model_validate({"transport": "filesystem", "uri": "exchange://vol/p"})
    assert desc.root.transport == "filesystem"
    assert desc.root.uri == "exchange://vol/p"


def test_source_descriptor_download_variant() -> None:
    desc = SourceDescriptor.model_validate(
        {"transport": "download", "url": "https://example/file-exchange/d/abc"}
    )
    assert desc.root.transport == "download"
    assert desc.root.url.startswith("https://")


def test_source_descriptor_rejects_unknown_transport() -> None:
    with pytest.raises(Exception):
        SourceDescriptor.model_validate({"transport": "smtp", "uri": "smtp://x"})


def test_sink_descriptor_upload_variant_default_methods() -> None:
    desc = SinkDescriptor.model_validate(
        {"transport": "upload", "url": "https://example/file-exchange/u/abc"}
    )
    assert desc.root.transport == "upload"
    assert desc.root.methods == ("PUT",)


def test_transfer_handle_roundtrip_with_alias_fields() -> None:
    raw = {
        "version": "0.1",
        "artifact": {"id": "a1", "size": 12, "mimeType": "text/plain"},
        "sources": [
            {"transport": "filesystem", "uri": "exchange://vol/p"},
            {"transport": "download", "url": "https://e/file-exchange/d/t"},
        ],
        "mustUnderstand": ["nl.liesdonk.file-exchange"],
    }
    handle = TransferHandle.model_validate(raw)
    assert handle.artifact.id == "a1"
    assert len(handle.sources) == 2
    assert handle.must_understand == ("nl.liesdonk.file-exchange",)
    assert handle.model_dump(by_alias=True, exclude_none=True) == raw


def test_intake_ticket_roundtrip_with_expected_constraints() -> None:
    raw = {
        "version": "0.1",
        "artifactId": "a1",
        "sinks": [{"transport": "upload", "url": "https://e/file-exchange/u/t", "methods": ["PUT"]}],
        "expected": {"maxSize": 4096, "acceptMimeTypes": ["application/pdf"], "requireDigest": True},
    }
    ticket = IntakeTicket.model_validate(raw)
    assert ticket.artifact_id == "a1"
    assert ticket.expected is not None
    assert ticket.expected.max_size == 4096
    assert ticket.model_dump(by_alias=True, exclude_none=True) == raw


def test_role_and_transport_literal_aliases_are_re_exportable() -> None:
    # Smoke test: the Literal aliases must exist and be importable
    # for downstream type-checking. `Literal[str, ...]` instances are
    # not hashable into a set, so we use string equivalence checks.
    assert "provider" in FileExchangeRole.__args__   # type: ignore[attr-defined]
    assert "filesystem" in FileExchangeTransport.__args__   # type: ignore[attr-defined]
```

- [ ] **Run to verify all pass:**

```bash
uv run pytest tests/file_exchange/test_types.py -v
```

Expected: 7 passing.

### Step A.6 — Vendored-conformance walker

- [ ] **Add to `test_types.py`:**

```python
import json
from pathlib import Path


_CONFORMANCE_DIR = (
    Path(__file__).parent.parent.parent
    / "src" / "fastmcp_pvl_core" / "_file_exchange" / "schema" / "conformance"
)


def _conformance_cases(subdir: str) -> list[tuple[str, dict]]:
    root = _CONFORMANCE_DIR / subdir
    if not root.exists():
        return []
    return [(p.name, json.loads(p.read_text())) for p in sorted(root.glob("*.json"))]


@pytest.mark.parametrize("name,payload", _conformance_cases("transfer_handle/valid"))
def test_conformance_transfer_handle_valid(name: str, payload: dict) -> None:
    TransferHandle.model_validate(payload)


@pytest.mark.parametrize("name,payload", _conformance_cases("transfer_handle/invalid"))
def test_conformance_transfer_handle_invalid(name: str, payload: dict) -> None:
    with pytest.raises(Exception):
        TransferHandle.model_validate(payload)


@pytest.mark.parametrize("name,payload", _conformance_cases("intake_ticket/valid"))
def test_conformance_intake_ticket_valid(name: str, payload: dict) -> None:
    IntakeTicket.model_validate(payload)


@pytest.mark.parametrize("name,payload", _conformance_cases("intake_ticket/invalid"))
def test_conformance_intake_ticket_invalid(name: str, payload: dict) -> None:
    with pytest.raises(Exception):
        IntakeTicket.model_validate(payload)
```

- [ ] **Run** `uv run pytest tests/file_exchange/test_types.py -v -k conformance` — every vendored fixture must classify per its directory. If a vendored fixture genuinely contradicts the Pydantic shape (e.g. the spec allows a field the model rejects), STOP and surface — do not patch the model to chase a fixture without first confirming the design doc allows it.

### Step A.7 — Commit Task A

- [ ] **Commit** (use HEREDOC so the multiline body renders correctly):

```bash
git add src/fastmcp_pvl_core/_file_exchange/ tests/file_exchange/
git commit -m "$(cat <<'EOF'
feat(file-exchange): vendor v0.1 schema and Pydantic types

Vendors the wire-format schema and conformance fixtures verbatim from
pvliesdonk/mcp-file-exchange-ext v0.1 at a pinned commit, gated by a
SHA-256 drift check. Adds Pydantic v2 models for ArtifactMetadata,
TransferHandle, IntakeTicket, SourceDescriptor (discriminated
filesystem|download), SinkDescriptor (discriminated filesystem|upload),
ExpectedConstraints, plus the FileExchangeRole / FileExchangeTransport
Literal aliases used throughout the package.

Closes #124.
EOF
)"
```

- [ ] Run the universal per-task suffix (local checks → preflight-circus → draft PR → bot verdicts → flip ready).

---

## Task B: Descriptor selection algorithm + error envelope (closes #125)

**Depends on:** Task A merged.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_errors.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_select.py`
- Create: `tests/file_exchange/test_errors.py`
- Create: `tests/file_exchange/test_select.py`

### Step B.1 — Write the failing error-envelope test

- [ ] **Write `tests/file_exchange/test_errors.py`:**

```python
"""FileExchangeError code envelope tests (spec §13)."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
    as_tool_error_result,
)


def test_error_code_enum_has_all_spec_codes() -> None:
    # Spec §13 enumerates the seven canonical codes.
    expected = {
        "not-accessible",
        "expired",
        "size-exceeded",
        "mime-rejected",
        "digest-mismatch",
        "no-supported-transport",
        "version-incompatible",
    }
    assert {c.value for c in FileExchangeErrorCode} == expected


def test_error_carries_code_and_detail() -> None:
    exc = FileExchangeError(
        code=FileExchangeErrorCode.NOT_ACCESSIBLE,
        detail="bytes not yet on disk for artifact_id='a1'",
    )
    assert exc.code is FileExchangeErrorCode.NOT_ACCESSIBLE
    assert "a1" in exc.detail
    assert str(exc).startswith("not-accessible: ")


def test_error_accepts_string_code() -> None:
    # Convenience: accept the wire string too, normalise to the enum.
    exc = FileExchangeError(code="expired", detail="token expired at 2026-05-20T18:00")
    assert exc.code is FileExchangeErrorCode.EXPIRED


def test_as_tool_error_result_emits_isError_envelope() -> None:
    exc = FileExchangeError(code="size-exceeded", detail="cap=1024 actual=2048")
    result = as_tool_error_result(exc)
    assert result.isError is True
    # Text content carries human-readable message
    assert any("size-exceeded" in block.text for block in result.content if hasattr(block, "text"))
    # Structured _meta carries the machine-readable envelope
    meta = result.meta or {}
    fe_meta = meta.get("nl.liesdonk.file-exchange") or {}
    assert fe_meta.get("error", {}).get("code") == "size-exceeded"
    assert "1024" in fe_meta["error"]["detail"]


def test_as_tool_error_result_passes_other_exceptions_through_unwrapped() -> None:
    # Non-FileExchangeError must raise — pvl-core doesn't silently swallow.
    with pytest.raises(TypeError):
        as_tool_error_result(ValueError("not a file-exchange error"))   # type: ignore[arg-type]
```

- [ ] **Run to verify fail:**

```bash
uv run pytest tests/file_exchange/test_errors.py -v
```

Expected: FAIL — module not found.

### Step B.2 — Implement `_errors.py`

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_errors.py`:**

```python
"""FileExchangeError and the §13 code envelope.

Helpers in this package always raise ``FileExchangeError`` with one of the
seven canonical codes; ``as_tool_error_result`` converts to the
``CallToolResult`` envelope a downstream tool returns.
"""

from __future__ import annotations

from enum import StrEnum

from mcp.types import CallToolResult, TextContent

CAPABILITY_KEY = "nl.liesdonk.file-exchange"


class FileExchangeErrorCode(StrEnum):
    NOT_ACCESSIBLE = "not-accessible"
    EXPIRED = "expired"
    SIZE_EXCEEDED = "size-exceeded"
    MIME_REJECTED = "mime-rejected"
    DIGEST_MISMATCH = "digest-mismatch"
    NO_SUPPORTED_TRANSPORT = "no-supported-transport"
    VERSION_INCOMPATIBLE = "version-incompatible"


class FileExchangeError(Exception):
    """A spec-§13 error envelope, raised by every helper in this package."""

    def __init__(self, *, code: FileExchangeErrorCode | str, detail: str) -> None:
        self.code = FileExchangeErrorCode(code) if isinstance(code, str) else code
        self.detail = detail
        super().__init__(f"{self.code.value}: {detail}")


def as_tool_error_result(exc: FileExchangeError) -> CallToolResult:
    """Convert a ``FileExchangeError`` into a ``CallToolResult`` envelope.

    Raises:
        TypeError: if ``exc`` is not a ``FileExchangeError`` — callers must
            scope this helper to the file-exchange error family.
    """
    if not isinstance(exc, FileExchangeError):
        raise TypeError(
            f"as_tool_error_result requires FileExchangeError; got {type(exc).__name__}"
        )
    text = f"{exc.code.value}: {exc.detail}"
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=True,
        _meta={
            CAPABILITY_KEY: {
                "error": {"code": exc.code.value, "detail": exc.detail},
            }
        },
    )
```

- [ ] **Run to verify pass:**

```bash
uv run pytest tests/file_exchange/test_errors.py -v
```

Expected: 5 passing. The code imports `CallToolResult` from `mcp.types` (the MCP-protocol class, which has `isError` and `meta` fields). The FastMCP-wrapper `fastmcp.tools.tool.ToolResult` is a different shape and is NOT used here — confirm via `uv run python -c "from mcp.types import CallToolResult; import inspect; print(inspect.signature(CallToolResult))"`.

### Step B.3 — Commit error module

- [ ] **Commit:**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_errors.py tests/file_exchange/test_errors.py
git commit -m "feat(file-exchange): add FileExchangeError + §13 code envelope"
```

### Step B.4 — Write failing tests for `_select.py` (spec §9 selection)

- [ ] **Write `tests/file_exchange/test_select.py`:**

```python
"""Spec §9 descriptor-selection algorithm tests.

Selection is a pure function of (descriptor list, supported transports).
Filesystem before HTTPS when both are accepted (spec §9.1 "prefer
local"). No I/O — confinement / SSRF live in the transport modules.
"""

from __future__ import annotations

import pytest

from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
)
from fastmcp_pvl_core._file_exchange._select import (
    select_sink,
    select_source,
)
from fastmcp_pvl_core._file_exchange._types import (
    SinkDescriptor,
    SourceDescriptor,
)

_FS_SRC = SourceDescriptor.model_validate({"transport": "filesystem", "uri": "exchange://v/p"})
_DL_SRC = SourceDescriptor.model_validate({"transport": "download", "url": "https://e/d/t"})
_FS_SINK = SinkDescriptor.model_validate({"transport": "filesystem", "uri": "exchange://v/p"})
_UP_SINK = SinkDescriptor.model_validate({"transport": "upload", "url": "https://e/u/t"})


def test_select_source_prefers_filesystem_when_both_supported() -> None:
    chosen = select_source([_DL_SRC, _FS_SRC], supported=("filesystem", "download"))
    assert chosen.root.transport == "filesystem"


def test_select_source_falls_back_to_download_when_no_filesystem() -> None:
    chosen = select_source([_DL_SRC], supported=("filesystem", "download"))
    assert chosen.root.transport == "download"


def test_select_source_raises_when_no_transport_matches() -> None:
    with pytest.raises(FileExchangeError) as ei:
        select_source([_DL_SRC], supported=("filesystem",))
    assert ei.value.code is FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT


def test_select_sink_prefers_filesystem_when_both_supported() -> None:
    chosen = select_sink([_UP_SINK, _FS_SINK], supported=("filesystem", "upload"))
    assert chosen.root.transport == "filesystem"


def test_select_sink_falls_back_to_upload_when_no_filesystem() -> None:
    chosen = select_sink([_UP_SINK], supported=("filesystem", "upload"))
    assert chosen.root.transport == "upload"


def test_select_sink_raises_when_no_transport_matches() -> None:
    with pytest.raises(FileExchangeError) as ei:
        select_sink([_UP_SINK], supported=("filesystem",))
    assert ei.value.code is FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT


def test_select_source_empty_input_raises() -> None:
    with pytest.raises(FileExchangeError) as ei:
        select_source([], supported=("filesystem", "download"))
    assert ei.value.code is FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT
```

- [ ] **Run to verify fail:**

```bash
uv run pytest tests/file_exchange/test_select.py -v
```

Expected: FAIL — module not found.

### Step B.5 — Implement `_select.py`

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_select.py`:**

```python
"""Spec §9 selection algorithm.

Pure functions over descriptor lists — no I/O, no transport-specific
validation. Confinement (filesystem) and SSRF (HTTPS) live in their
respective transport modules.

Preference order is fixed by spec §9.1: filesystem before HTTPS. The
caller passes the deployment's supported-transport set, derived from
its ``ServerConfig`` (volumes configured? HTTP app available?).
"""

from __future__ import annotations

from collections.abc import Sequence

from ._errors import FileExchangeError, FileExchangeErrorCode
from ._types import (
    FileExchangeTransport,
    SinkDescriptor,
    SourceDescriptor,
)

# Spec §9.1: filesystem preferred over HTTPS when both are supported.
_SOURCE_PREFERENCE: tuple[FileExchangeTransport, ...] = ("filesystem", "download")
_SINK_PREFERENCE: tuple[FileExchangeTransport, ...] = ("filesystem", "upload")


def select_source(
    sources: Sequence[SourceDescriptor],
    *,
    supported: Sequence[FileExchangeTransport],
) -> SourceDescriptor:
    """Pick the highest-preference source whose transport is in ``supported``."""
    sup = set(supported)
    by_transport = {s.root.transport: s for s in sources}
    for t in _SOURCE_PREFERENCE:
        if t in sup and t in by_transport:
            return by_transport[t]
    raise FileExchangeError(
        code=FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT,
        detail=(
            f"no source transport in {sorted(supported)} matches "
            f"offered {sorted(by_transport)}"
        ),
    )


def select_sink(
    sinks: Sequence[SinkDescriptor],
    *,
    supported: Sequence[FileExchangeTransport],
) -> SinkDescriptor:
    """Pick the highest-preference sink whose transport is in ``supported``."""
    sup = set(supported)
    by_transport = {s.root.transport: s for s in sinks}
    for t in _SINK_PREFERENCE:
        if t in sup and t in by_transport:
            return by_transport[t]
    raise FileExchangeError(
        code=FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT,
        detail=(
            f"no sink transport in {sorted(supported)} matches "
            f"offered {sorted(by_transport)}"
        ),
    )
```

- [ ] **Run to verify pass:**

```bash
uv run pytest tests/file_exchange/test_select.py -v
```

Expected: 7 passing.

### Step B.6 — Commit Task B

- [ ] **Commit:**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_select.py tests/file_exchange/test_select.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): add §9 descriptor selection algorithm

Pure-function source/sink selectors that pick the highest-preference
descriptor (filesystem > HTTPS, per spec §9.1) whose transport is in
the deployment's supported set. Raises FileExchangeError(code=
no-supported-transport) when nothing matches.

Closes #125.
EOF
)"
```

- [ ] Run the universal per-task suffix.

---

## Task C: Filesystem transport with exchange:// resolution + atomic writes (closes #126)

**Depends on:** Task A merged.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_transport_filesystem.py`
- Modify: `src/fastmcp_pvl_core/_config.py` — add `file_exchange_volumes` + sibling env-var fields
- Create: `tests/file_exchange/test_transport_filesystem.py`

### Step C.1 — Add the operator-config fields to `ServerConfig`

- [ ] **Write a failing test first**, `tests/file_exchange/test_config_fields.py`:

```python
"""ServerConfig.file_exchange_* fields are loaded from env-vars."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core import ServerConfig


def test_config_loads_file_exchange_fields_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYAPP_FILE_EXCHANGE_VOLUMES", "vault=/mnt/exchange/vault,images=/srv/img")
    monkeypatch.setenv("MYAPP_FILE_EXCHANGE_MAX_ARTIFACT_SIZE", "104857600")
    monkeypatch.setenv("MYAPP_FILE_EXCHANGE_HTTPS_ALLOW_LOOPBACK", "true")
    monkeypatch.setenv("MYAPP_FILE_EXCHANGE_HTTPS_ALLOW_PRIVATE", "false")
    monkeypatch.setenv("MYAPP_FILE_EXCHANGE_CAPABILITY_URL_TTL_DEFAULT_S", "1800")
    monkeypatch.setenv("MYAPP_FILE_EXCHANGE_HTTPS_PUBLIC_BASE_URL", "https://files.example.com")

    cfg = ServerConfig.from_env("MYAPP")

    assert cfg.file_exchange_volumes == "vault=/mnt/exchange/vault,images=/srv/img"
    assert cfg.file_exchange_max_artifact_size == 104857600
    assert cfg.file_exchange_https_allow_loopback is True
    assert cfg.file_exchange_https_allow_private is False
    assert cfg.file_exchange_capability_url_ttl_default_s == 1800
    assert cfg.file_exchange_https_public_base_url == "https://files.example.com"


def test_config_file_exchange_fields_have_safe_defaults() -> None:
    cfg = ServerConfig()
    assert cfg.file_exchange_volumes is None
    assert cfg.file_exchange_max_artifact_size is None
    assert cfg.file_exchange_https_allow_loopback is False
    assert cfg.file_exchange_https_allow_private is False
    assert cfg.file_exchange_capability_url_ttl_default_s == 3600
    assert cfg.file_exchange_https_public_base_url is None
```

- [ ] **Run to verify fail:**

```bash
uv run pytest tests/file_exchange/test_config_fields.py -v
```

Expected: FAIL — fields don't exist yet.

- [ ] **Modify `src/fastmcp_pvl_core/_config.py`**: append the six fields to the `@dataclass` (in declaration order after `auth_mode`, before `bearer_tokens_file`) and parse them in `from_env`. Insert after the `auth_mode: str | None = None` line:

```python
    file_exchange_volumes: str | None = None
    file_exchange_max_artifact_size: "int | None" = None
    file_exchange_https_allow_loopback: bool = False
    file_exchange_https_allow_private: bool = False
    file_exchange_capability_url_ttl_default_s: int = 3600
    file_exchange_https_public_base_url: str | None = None
```

- [ ] In `from_env`, after the existing `auth_mode=env(env_prefix, "AUTH_MODE")` line, parse the six fields:

```python
        max_size_raw = env(env_prefix, "FILE_EXCHANGE_MAX_ARTIFACT_SIZE")
        max_size = int(max_size_raw) if max_size_raw else None

        ttl_raw = env(env_prefix, "FILE_EXCHANGE_CAPABILITY_URL_TTL_DEFAULT_S", "3600")
        ttl = int(ttl_raw)
```

And add to the `cls(...)` kwargs:

```python
            file_exchange_volumes=env(env_prefix, "FILE_EXCHANGE_VOLUMES"),
            file_exchange_max_artifact_size=max_size,
            file_exchange_https_allow_loopback=parse_bool(
                env(env_prefix, "FILE_EXCHANGE_HTTPS_ALLOW_LOOPBACK")
            ) or False,
            file_exchange_https_allow_private=parse_bool(
                env(env_prefix, "FILE_EXCHANGE_HTTPS_ALLOW_PRIVATE")
            ) or False,
            file_exchange_capability_url_ttl_default_s=ttl,
            file_exchange_https_public_base_url=env(
                env_prefix, "FILE_EXCHANGE_HTTPS_PUBLIC_BASE_URL"
            ),
```

- [ ] **Run to verify pass:**

```bash
uv run pytest tests/file_exchange/test_config_fields.py -v
```

Expected: 2 passing.

### Step C.2 — Failing tests for `parse_volumes`, `resolve_exchange_uri`, `atomic_write`, `async_atomic_write`

- [ ] **Write `tests/file_exchange/test_transport_filesystem.py`:**

```python
"""Filesystem transport unit tests (spec §6.1)."""

from __future__ import annotations

import asyncio
import os
import pytest

from fastmcp_pvl_core import ServerConfig
from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
)
from fastmcp_pvl_core._file_exchange._transport_filesystem import (
    async_atomic_write,
    atomic_write,
    make_filesystem_source,
    parse_volumes,
    resolve_exchange_uri,
)


def test_parse_volumes_returns_empty_dict_when_none() -> None:
    assert parse_volumes(None) == {}


def test_parse_volumes_parses_comma_separated_pairs(tmp_path) -> None:
    spec = f"vault={tmp_path / 'vault'},img={tmp_path / 'img'}"
    (tmp_path / "vault").mkdir()
    (tmp_path / "img").mkdir()
    parsed = parse_volumes(spec)
    assert set(parsed.keys()) == {"vault", "img"}
    assert parsed["vault"].is_absolute()


def test_parse_volumes_rejects_relative_paths() -> None:
    with pytest.raises(ValueError, match="absolute"):
        parse_volumes("vault=./relative")


def test_resolve_exchange_uri_happy_path(tmp_path) -> None:
    (tmp_path / "vol").mkdir()
    (tmp_path / "vol" / "f.txt").write_text("hi")
    volumes = {"vol": tmp_path / "vol"}
    path = resolve_exchange_uri("exchange://vol/f.txt", volumes=volumes)
    assert path == (tmp_path / "vol" / "f.txt").resolve()


def test_resolve_exchange_uri_rejects_unknown_volume(tmp_path) -> None:
    with pytest.raises(FileExchangeError) as ei:
        resolve_exchange_uri("exchange://missing/x", volumes={})
    assert ei.value.code is FileExchangeErrorCode.NOT_ACCESSIBLE


def test_resolve_exchange_uri_rejects_traversal(tmp_path) -> None:
    (tmp_path / "vol").mkdir()
    volumes = {"vol": tmp_path / "vol"}
    with pytest.raises(FileExchangeError) as ei:
        resolve_exchange_uri("exchange://vol/../../etc/passwd", volumes=volumes)
    assert ei.value.code is FileExchangeErrorCode.NOT_ACCESSIBLE


def test_resolve_exchange_uri_rejects_symlink_escape(tmp_path) -> None:
    (tmp_path / "vol").mkdir()
    (tmp_path / "secret").write_text("nope")
    (tmp_path / "vol" / "leak").symlink_to(tmp_path / "secret")
    volumes = {"vol": tmp_path / "vol"}
    with pytest.raises(FileExchangeError) as ei:
        resolve_exchange_uri("exchange://vol/leak", volumes=volumes)
    assert ei.value.code is FileExchangeErrorCode.NOT_ACCESSIBLE


def test_resolve_exchange_uri_accepts_file_scheme(tmp_path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("hi")
    out = resolve_exchange_uri(f"file://{p}", volumes={})
    assert out == p.resolve()


def test_atomic_write_renames_on_clean_exit(tmp_path) -> None:
    target = tmp_path / "dst" / "out.bin"
    target.parent.mkdir()
    with atomic_write(target) as fh:
        fh.write(b"payload")
    assert target.read_bytes() == b"payload"
    # No temp residue
    assert sorted(p.name for p in target.parent.iterdir()) == ["out.bin"]


def test_atomic_write_discards_on_exception(tmp_path) -> None:
    target = tmp_path / "out.bin"
    with pytest.raises(RuntimeError):
        with atomic_write(target) as fh:
            fh.write(b"partial")
            raise RuntimeError("boom")
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


@pytest.mark.asyncio
async def test_async_atomic_write_renames_on_clean_exit(tmp_path) -> None:
    target = tmp_path / "out.bin"
    async with async_atomic_write(target) as fh:
        await asyncio.to_thread(fh.write, b"payload")
    assert target.read_bytes() == b"payload"
    assert sorted(p.name for p in target.parent.iterdir()) == ["out.bin"]


@pytest.mark.asyncio
async def test_async_atomic_write_discards_on_exception(tmp_path) -> None:
    target = tmp_path / "out.bin"
    with pytest.raises(RuntimeError):
        async with async_atomic_write(target) as fh:
            await asyncio.to_thread(fh.write, b"partial")
            raise RuntimeError("boom")
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_make_filesystem_source_composes_uri(tmp_path) -> None:
    (tmp_path / "vault").mkdir()
    cfg = ServerConfig(file_exchange_volumes=f"vault={tmp_path / 'vault'}")
    src = make_filesystem_source("vault", "subdir/note.md", config=cfg)
    assert src.root.transport == "filesystem"
    assert src.root.uri == "exchange://vault/subdir/note.md"


def test_make_filesystem_source_rejects_unknown_volume(tmp_path) -> None:
    cfg = ServerConfig(file_exchange_volumes=f"vault={tmp_path}")
    with pytest.raises(FileExchangeError) as ei:
        make_filesystem_source("nope", "x", config=cfg)
    assert ei.value.code is FileExchangeErrorCode.NOT_ACCESSIBLE
```

- [ ] **Run to verify fail:**

```bash
uv run pytest tests/file_exchange/test_transport_filesystem.py -v
```

Expected: FAIL — module not found.

### Step C.3 — Implement `_transport_filesystem.py`

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_transport_filesystem.py`:**

```python
"""Filesystem transport (spec §6.1).

- ``parse_volumes`` — single source of truth for the volume map.
- ``resolve_exchange_uri`` — canonicalises ``exchange://<v>/<p>`` and
  ``file://<p>`` to a confined ``Path``; rejects escapes (incl. symlinks).
- ``atomic_write`` / ``async_atomic_write`` — temp-file + fsync + rename
  context managers used by every sender that writes to a filesystem sink.
- ``make_filesystem_source`` — public mint helper (sync; no kv_store I/O).
  Note: ``make_filesystem_sink`` lives in ``_url_store.py`` because it
  writes the ``intake:<artifact_id>`` mapping into kv_store at mint time.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import IO
from urllib.parse import unquote, urlsplit

from .._config import ServerConfig
from ._errors import FileExchangeError, FileExchangeErrorCode
from ._types import SourceDescriptor


def parse_volumes(spec: str | None) -> dict[str, Path]:
    """Parse ``<id>=<abs-path>,<id>=<abs-path>`` into a dict of resolved Paths.

    Returns ``{}`` on ``None`` or empty string. Raises ``ValueError`` if any
    path is relative (we refuse to guess the operator's cwd).
    """
    if not spec:
        return {}
    out: dict[str, Path] = {}
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(f"file-exchange volume entry missing '=': {token!r}")
        name, path_str = token.split("=", 1)
        name = name.strip()
        path = Path(path_str.strip())
        if not path.is_absolute():
            raise ValueError(
                f"file-exchange volume {name!r} path must be absolute, got {path_str!r}"
            )
        out[name] = path.resolve()
    return out


def resolve_exchange_uri(uri: str, *, volumes: dict[str, Path]) -> Path:
    """Resolve an ``exchange://`` or ``file://`` URI to a confined Path.

    Canonicalises (resolves ``.``, ``..``, symlinks) and asserts the result
    is inside the volume root. Symlink escapes are rejected.
    """
    parts = urlsplit(uri)
    if parts.scheme == "file":
        # file://<abs-path> — no volume gating, but still canonicalise.
        target = Path(unquote(parts.path)).resolve()
        if not target.exists():
            raise FileExchangeError(
                code=FileExchangeErrorCode.NOT_ACCESSIBLE,
                detail=f"file:// URI does not exist: {uri!r}",
            )
        return target
    if parts.scheme != "exchange":
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"unsupported URI scheme {parts.scheme!r} (expected exchange:// or file://)",
        )
    volume_name = parts.netloc
    root = volumes.get(volume_name)
    if root is None:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"file-exchange volume {volume_name!r} is not configured on this party",
        )
    relative = unquote(parts.path).lstrip("/")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"path {uri!r} resolves outside volume {volume_name!r}",
        ) from exc
    return candidate


@contextlib.contextmanager
def atomic_write(target: Path) -> Iterator[IO[bytes]]:
    """Temp-file + fsync + rename. Sync exit; for use from sync callers.

    See ``async_atomic_write`` for the variant whose exit dispatches the
    fsync/rename via ``asyncio.to_thread`` (use that one inside async
    handlers).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".part", dir=str(target.parent)
    )
    tmp_path = Path(tmp_path_str)
    fh = os.fdopen(fd, "wb")
    try:
        yield fh
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        os.replace(tmp_path, target)
    except BaseException:
        try:
            fh.close()
        finally:
            tmp_path.unlink(missing_ok=True)
        raise


@contextlib.asynccontextmanager
async def async_atomic_write(target: Path) -> AsyncIterator[IO[bytes]]:
    """Async-exit variant of ``atomic_write``.

    Chunk writes inside the ``async with`` block are the caller's
    responsibility (use ``asyncio.to_thread(fh.write, chunk)`` so disk
    latency doesn't block the event loop). The exit dispatches the
    fsync/rename/unlink through ``asyncio.to_thread`` too.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".part", dir=str(target.parent)
    )
    tmp_path = Path(tmp_path_str)
    fh = os.fdopen(fd, "wb")

    def _commit() -> None:
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        os.replace(tmp_path, target)

    def _discard() -> None:
        try:
            fh.close()
        finally:
            tmp_path.unlink(missing_ok=True)

    try:
        yield fh
    except BaseException:
        await asyncio.to_thread(_discard)
        raise
    else:
        await asyncio.to_thread(_commit)


def make_filesystem_source(
    volume: str,
    relative_path: str,
    *,
    config: ServerConfig,
) -> SourceDescriptor:
    """Compose an ``exchange://<volume>/<relative_path>`` source descriptor.

    Validates that ``volume`` is configured on this party (per
    ``config.file_exchange_volumes``). Raises ``FileExchangeError(
    NOT_ACCESSIBLE)`` if not.
    """
    volumes = parse_volumes(config.file_exchange_volumes)
    if volume not in volumes:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=(
                f"file-exchange volume {volume!r} is not configured "
                f"on this party (known: {sorted(volumes)})"
            ),
        )
    uri = f"exchange://{volume}/{relative_path.lstrip('/')}"
    return SourceDescriptor.model_validate({"transport": "filesystem", "uri": uri})
```

- [ ] **Run to verify pass:**

```bash
uv run pytest tests/file_exchange/test_transport_filesystem.py -v
```

Expected: 13 passing.

### Step C.4 — Commit Task C

- [ ] **Commit:**

```bash
git add src/fastmcp_pvl_core/_config.py \
        src/fastmcp_pvl_core/_file_exchange/_transport_filesystem.py \
        tests/file_exchange/test_config_fields.py \
        tests/file_exchange/test_transport_filesystem.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): filesystem transport with exchange:// resolution

Adds ServerConfig.file_exchange_* fields loaded from env-vars
({PREFIX}_FILE_EXCHANGE_VOLUMES, _MAX_ARTIFACT_SIZE,
_HTTPS_ALLOW_LOOPBACK, _HTTPS_ALLOW_PRIVATE,
_CAPABILITY_URL_TTL_DEFAULT_S, _HTTPS_PUBLIC_BASE_URL).

Adds parse_volumes (single source of truth for the volume map),
resolve_exchange_uri (rejects traversal + symlink escapes via
canonicalisation), atomic_write (sync) and async_atomic_write (async
exit via asyncio.to_thread), and the make_filesystem_source mint helper.

Closes #126.
EOF
)"
```

- [ ] Run the universal per-task suffix.

---

## Task D: HTTPS consumer transport (pull/push) with SSRF guard (closes #127)

**Depends on:** Task A merged.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_transport_https.py`
- Create: `tests/file_exchange/test_transport_https.py`

### Step D.1 — Failing tests for SSRF guard

- [ ] **Write `tests/file_exchange/test_transport_https.py`:**

```python
"""HTTPS consumer-side transport (spec §6.2)."""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

import pytest

from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
)
from fastmcp_pvl_core._file_exchange._transport_https import (
    SSRFGuardConfig,
    enforce_ssrf,
    pull_download,
    push_upload,
)


@pytest.mark.asyncio
async def test_ssrf_rejects_http_scheme() -> None:
    cfg = SSRFGuardConfig()
    with pytest.raises(FileExchangeError) as ei:
        await enforce_ssrf("http://example.com/x", config=cfg)
    assert ei.value.code is FileExchangeErrorCode.NOT_ACCESSIBLE
    assert "https" in ei.value.detail


@pytest.mark.asyncio
async def test_ssrf_rejects_loopback_by_default() -> None:
    cfg = SSRFGuardConfig()
    with pytest.raises(FileExchangeError) as ei:
        await enforce_ssrf("https://127.0.0.1/x", config=cfg)
    assert ei.value.code is FileExchangeErrorCode.NOT_ACCESSIBLE


@pytest.mark.asyncio
async def test_ssrf_allows_loopback_when_opted_in() -> None:
    cfg = SSRFGuardConfig(allow_loopback=True)
    # Must not raise; returns the pinned IP for connect-pinning.
    pinned = await enforce_ssrf("https://127.0.0.1/x", config=cfg)
    assert pinned == "127.0.0.1"


@pytest.mark.asyncio
async def test_ssrf_rejects_private_rfc1918_by_default() -> None:
    cfg = SSRFGuardConfig()
    with pytest.raises(FileExchangeError) as ei:
        await enforce_ssrf("https://10.0.0.5/x", config=cfg)
    assert ei.value.code is FileExchangeErrorCode.NOT_ACCESSIBLE


@pytest.mark.asyncio
async def test_ssrf_allows_private_when_opted_in() -> None:
    cfg = SSRFGuardConfig(allow_private=True)
    pinned = await enforce_ssrf("https://10.0.0.5/x", config=cfg)
    assert pinned == "10.0.0.5"
```

### Step D.2 — Failing tests for `pull_download` streaming + digest

- [ ] **Append to `test_transport_https.py`:**

```python
class _FakeAsyncStream:
    """Tiny stand-in for an httpx streaming response (chunked body)."""

    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None) -> None:
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = 200

    async def aiter_bytes(self, chunk_size: int = 65536):
        for c in self._chunks:
            yield c

    async def aread(self) -> bytes:  # pragma: no cover - not used in streaming path
        return b"".join(self._chunks)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_stream_get_cm(chunks: list[bytes], headers: dict[str, str] | None = None):
    """Build an @asynccontextmanager that yields a _FakeAsyncStream.

    The production ``_stream_get`` is an async context manager (wraps
    ``httpx.AsyncClient.stream``), so monkeypatches must match that
    shape — a plain ``async def`` mock would fail at ``async with``.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_get(url: str, **_kw):
        yield _FakeAsyncStream(chunks, headers=headers or {})

    return _fake_get


@pytest.mark.asyncio
async def test_pull_download_streams_chunks_into_dest_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out.bin"
    payload = b"hello world " * 100
    chunks = [payload[i : i + 16] for i in range(0, len(payload), 16)]

    monkeypatch.setattr(
        "fastmcp_pvl_core._file_exchange._transport_https._stream_get",
        _fake_stream_get_cm(chunks, headers={"content-length": str(len(payload))}),
    )
    digester = hashlib.sha256()
    await pull_download(
        "https://example.com/d/tok",
        dest=target,
        ssrf=SSRFGuardConfig(allow_private=True, allow_loopback=True),
        digester=digester,
    )
    assert target.read_bytes() == payload
    assert digester.hexdigest() == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_pull_download_streams_into_binaryio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"abc" * 1000
    chunks = [payload[i : i + 128] for i in range(0, len(payload), 128)]

    monkeypatch.setattr(
        "fastmcp_pvl_core._file_exchange._transport_https._stream_get",
        _fake_stream_get_cm(chunks),
    )
    buf = io.BytesIO()
    await pull_download(
        "https://example.com/d/tok",
        dest=buf,
        ssrf=SSRFGuardConfig(allow_private=True, allow_loopback=True),
        digester=None,
    )
    assert buf.getvalue() == payload
```

### Step D.3 — Failing tests for `push_upload` streaming

- [ ] **Append:**

```python
@pytest.mark.asyncio
async def test_push_upload_streams_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 4096)
    captured: dict[str, object] = {}

    async def _fake_put(url: str, *, content, headers, **_kw):
        # Drain the iterator to confirm streaming-style call shape.
        if hasattr(content, "read"):
            captured["bytes"] = content.read()
        else:
            captured["bytes"] = b"".join([c async for c in content])
        captured["headers"] = headers
        return _FakeAsyncStream([], headers={})

    monkeypatch.setattr(
        "fastmcp_pvl_core._file_exchange._transport_https._stream_put", _fake_put
    )
    await push_upload(
        "https://example.com/u/tok",
        source=src,
        ssrf=SSRFGuardConfig(allow_private=True, allow_loopback=True),
        content_type="application/octet-stream",
    )
    assert captured["bytes"] == b"x" * 4096
    assert captured["headers"]["Content-Type"] == "application/octet-stream"
```

- [ ] **Run all D tests to verify fail:**

```bash
uv run pytest tests/file_exchange/test_transport_https.py -v
```

Expected: FAIL — module not found.

### Step D.4 — Implement `_transport_https.py`

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_transport_https.py`:**

```python
"""HTTPS consumer-side transport (spec §6.2).

- ``SSRFGuardConfig`` — operator overrides.
- ``enforce_ssrf(url, config)`` — resolve hostname, reject loopback /
  private / link-local unless opted in. Returns the pinned IP.
- ``pull_download`` — chunk-streams a ``GET`` body into ``Path | BinaryIO``,
  updating a digester per chunk via ``asyncio.to_thread(dest.write, chunk)``.
- ``push_upload`` — streams ``Path | BinaryIO`` to ``PUT``/``POST``; never
  buffers a single ``bytes`` blob.

The seams ``_stream_get`` and ``_stream_put`` are private async helpers
that the unit tests monkeypatch. Production code paths use httpx.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import httpx

from ._errors import FileExchangeError, FileExchangeErrorCode

_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class SSRFGuardConfig:
    """Operator-side overrides for the SSRF guard.

    Both default to ``False`` — production refuses loopback / private /
    link-local. Dev environments opt in.
    """

    allow_loopback: bool = False
    allow_private: bool = False


async def enforce_ssrf(url: str, *, config: SSRFGuardConfig) -> str:
    """Validate ``url`` and return the pinned IP literal.

    Rejects non-``https`` scheme, loopback (unless allowed), RFC 1918 /
    link-local (unless allowed). DNS resolution is dispatched off the
    event loop via ``asyncio.to_thread`` so the call does not block
    other tasks — `socket.gethostbyname` is a synchronous syscall.
    Pinning the resolved IP defeats DNS rebinding: callers connect to
    the returned literal and set TLS/SNI to the original hostname.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"file-exchange refuses non-https URL {url!r}",
        )
    host = parts.hostname
    if not host:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"file-exchange URL {url!r} has no hostname",
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = await asyncio.to_thread(socket.gethostbyname, host)
        except socket.gaierror as exc:
            raise FileExchangeError(
                code=FileExchangeErrorCode.NOT_ACCESSIBLE,
                detail=f"DNS resolution failed for {host!r}",
            ) from exc
        ip = ipaddress.ip_address(resolved)
    if ip.is_loopback and not config.allow_loopback:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"file-exchange refuses loopback {ip} (set _HTTPS_ALLOW_LOOPBACK=true to enable)",
        )
    if (ip.is_private or ip.is_link_local) and not config.allow_private:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"file-exchange refuses private/link-local {ip} (set _HTTPS_ALLOW_PRIVATE=true to enable)",
        )
    return str(ip)


@asynccontextmanager
async def _stream_get(
    url: str,
    *,
    pinned_ip: str,
    host: str,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[httpx.Response]:
    """Production GET seam — monkeypatched in unit tests.

    Yielded as an async context manager so the response body is truly
    chunk-streamed via ``client.stream("GET", ...)`` rather than buffered
    by ``client.get()``. The client stays open for the duration of the
    ``async with`` block — closing it ends the stream.

    DNS-rebind defence: the request URL has its host swapped for the
    pinned IP literal; TLS SNI and the ``Host`` header keep the original
    hostname so cert validation still works.
    """
    pinned_url = url.replace(f"//{host}", f"//{pinned_ip}", 1)
    merged_headers = {"Host": host, **(headers or {})}
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "GET",
            pinned_url,
            headers=merged_headers,
            follow_redirects=False,
            extensions={"sni_hostname": host},
        ) as resp:
            resp.raise_for_status()
            yield resp


async def _stream_put(
    url: str,
    *,
    pinned_ip: str,
    host: str,
    content: Any,
    headers: dict[str, str],
) -> httpx.Response:
    """Production PUT seam — monkeypatched in unit tests.

    Same SNI/Host pinning posture as ``_stream_get``. Accepts either a file-like
    opened in ``"rb"`` or an async-iterator of bytes chunks; httpx streams
    the request body chunk-by-chunk from the supplied source — no full-buffer.
    The response body is small (status confirmation), so it's fine to await
    the full response here without streaming.
    """
    pinned_url = url.replace(f"//{host}", f"//{pinned_ip}", 1)
    merged_headers = {"Host": host, **headers}
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            pinned_url,
            content=content,
            headers=merged_headers,
            follow_redirects=False,
            extensions={"sni_hostname": host},
        )
        resp.raise_for_status()
        return resp


async def pull_download(
    url: str,
    *,
    dest: Path | BinaryIO,
    ssrf: SSRFGuardConfig,
    digester: Any | None = None,
    chunk_size: int = _CHUNK_SIZE,
) -> None:
    """Chunk-stream ``url`` into ``dest``.

    Updates ``digester`` (if provided) per chunk. Writes via
    ``asyncio.to_thread(dest.write, chunk)`` so the event loop is never
    blocked on disk latency. Caller verifies the final digest against
    artifact.digest after this returns.
    """
    pinned_ip = await enforce_ssrf(url, config=ssrf)
    host = urlsplit(url).hostname or ""
    async with _stream_get(url, pinned_ip=pinned_ip, host=host) as resp:
        if isinstance(dest, Path):
            # Lazy import to avoid a circular cycle in the package layout.
            from ._transport_filesystem import async_atomic_write

            async with async_atomic_write(dest) as fh:
                async for chunk in resp.aiter_bytes(chunk_size):
                    if digester is not None:
                        digester.update(chunk)
                    await asyncio.to_thread(fh.write, chunk)
        else:
            async for chunk in resp.aiter_bytes(chunk_size):
                if digester is not None:
                    digester.update(chunk)
                await asyncio.to_thread(dest.write, chunk)


async def push_upload(
    url: str,
    *,
    source: Path | BinaryIO,
    ssrf: SSRFGuardConfig,
    content_type: str,
    content_digest: str | None = None,
    content_length: int | None = None,
) -> None:
    """Stream ``source`` to ``url`` via PUT.

    For a ``Path`` source, opens in ``"rb"`` and passes the file object to
    httpx (httpx streams it). For a ``BinaryIO``, passes directly. Never
    assembles a ``bytes`` blob.
    """
    pinned_ip = await enforce_ssrf(url, config=ssrf)
    host = urlsplit(url).hostname or ""
    headers: dict[str, str] = {"Content-Type": content_type}
    if content_digest is not None:
        headers["Content-Digest"] = content_digest
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    if isinstance(source, Path):
        # Run the blocking ``open`` off the event loop too — large file
        # opens can block on filesystem metadata lookups.
        fh = await asyncio.to_thread(open, source, "rb")
        try:
            await _stream_put(url, pinned_ip=pinned_ip, host=host, content=fh, headers=headers)
        finally:
            await asyncio.to_thread(fh.close)
    else:
        await _stream_put(url, pinned_ip=pinned_ip, host=host, content=source, headers=headers)
```

- [ ] **Run to verify pass:**

```bash
uv run pytest tests/file_exchange/test_transport_https.py -v
```

Expected: 8 passing.

### Step D.5 — Commit Task D

- [ ] **Commit:**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_transport_https.py \
        tests/file_exchange/test_transport_https.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): HTTPS consumer transport with SSRF guard

Adds enforce_ssrf (https-only, rejects loopback / RFC1918 / link-local
unless opted in via SSRFGuardConfig), pull_download (chunk-streams to
Path or BinaryIO with per-chunk digester update, asyncio.to_thread for
disk writes), and push_upload (streams a Path or BinaryIO source —
never buffers a bytes blob, opens files off the event loop).

Closes #127.
EOF
)"
```

- [ ] Run the universal per-task suffix.

---

## Task E: Capability-URL token store + sibling HTTP routes (closes #128)

**Depends on:** Task A merged. (Coordinates with C/D at call sites but does not import from them; the routes import `async_atomic_write` from `_transport_filesystem.py` once C lands.)

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_url_store.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_routes.py`
- Create: `tests/file_exchange/test_url_store.py`
- Create: `tests/file_exchange/test_routes.py`

### Step E.1 — Failing tests for `_url_store.py` mint + consume + sweep

- [ ] **Write `tests/file_exchange/test_url_store.py`:**

```python
"""Capability-URL token store (spec §6.2 producer-side)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from key_value.aio.stores.memory import MemoryStore

from fastmcp_pvl_core._file_exchange._url_store import (
    DownloadTokenRecord,
    UploadTokenRecord,
    consume_download_token,
    consume_upload_token,
    lookup_upload_token,
    make_filesystem_sink,
    mint_download_source,
    mint_intake_mapping,
    mint_upload_sink,
    resolve_intake,
    sweep_expired_tokens,
)
from fastmcp_pvl_core._file_exchange._types import ExpectedConstraints


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def cfg(tmp_path):
    from fastmcp_pvl_core import ServerConfig

    (tmp_path / "vault").mkdir()
    return ServerConfig(
        transport="http",
        base_url="https://example.com",
        file_exchange_volumes=f"vault={tmp_path / 'vault'}",
        file_exchange_capability_url_ttl_default_s=600,
        file_exchange_https_public_base_url="https://files.example.com",
    )


@pytest.fixture
def mcp():
    from fastmcp import FastMCP

    return FastMCP("test-server")


@pytest.mark.asyncio
async def test_mint_download_source_writes_token_record_and_returns_descriptor(
    store: MemoryStore, cfg, mcp, tmp_path: Path
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"hi")
    src = await mint_download_source(
        bytes_path=payload, server=mcp, config=cfg, kv_store=store,
    )
    assert src.root.transport == "download"
    assert src.root.url.startswith("https://files.example.com/file-exchange/d/")
    # Token record exists in the store
    token = src.root.url.rsplit("/", 1)[-1]
    raw = await store.get(collection="tokens", key=token)
    assert raw is not None
    assert raw["kind"] == "download"
    assert raw["bytes_path"] == str(payload.resolve())
    assert raw["single_use"] is True


@pytest.mark.asyncio
async def test_mint_upload_sink_writes_both_token_and_intake_mapping(
    store: MemoryStore, cfg, mcp, tmp_path: Path
) -> None:
    intake = tmp_path / "intake" / "a1.bin"
    sink = await mint_upload_sink(
        intake_path=intake, artifact_id="a1", server=mcp, config=cfg, kv_store=store,
        expected=ExpectedConstraints(max_size=4096, accept_mime_types=("application/pdf",)),
    )
    assert sink.root.transport == "upload"
    token = sink.root.url.rsplit("/", 1)[-1]
    raw_tok = await store.get(collection="tokens", key=token)
    assert raw_tok["kind"] == "upload"
    assert raw_tok["intake_path"] == str(intake.resolve())
    assert raw_tok["max_size"] == 4096
    # Intake mapping is recorded at mint time, before any bytes arrive.
    raw_intake = await store.get(collection="intake", key="a1")
    assert raw_intake["intake_path"] == str(intake.resolve())


@pytest.mark.asyncio
async def test_make_filesystem_sink_writes_intake_mapping(
    store: MemoryStore, cfg, tmp_path: Path
) -> None:
    sink = await make_filesystem_sink(
        "vault", "a1.bin", artifact_id="a1", config=cfg, kv_store=store,
    )
    assert sink.root.transport == "filesystem"
    assert sink.root.uri == "exchange://vault/a1.bin"
    raw_intake = await store.get(collection="intake", key="a1")
    expected_path = (tmp_path / "vault" / "a1.bin").resolve()
    assert raw_intake["intake_path"] == str(expected_path)


@pytest.mark.asyncio
async def test_consume_download_token_single_use_atomic_under_concurrency(
    store: MemoryStore, cfg, mcp, tmp_path: Path
) -> None:
    payload = tmp_path / "p.bin"
    payload.write_bytes(b"x")
    src = await mint_download_source(
        bytes_path=payload, server=mcp, config=cfg, kv_store=store,
    )
    token = src.root.url.rsplit("/", 1)[-1]

    async def race():
        try:
            await consume_download_token(store=store, token=token)
            return "won"
        except LookupError:
            return "lost"

    a, b = await asyncio.gather(race(), race())
    assert sorted([a, b]) == ["lost", "won"]


@pytest.mark.asyncio
async def test_consume_upload_token_after_streaming(
    store: MemoryStore, cfg, mcp, tmp_path: Path
) -> None:
    sink = await mint_upload_sink(
        intake_path=tmp_path / "out.bin", artifact_id="a1",
        server=mcp, config=cfg, kv_store=store,
    )
    token = sink.root.url.rsplit("/", 1)[-1]
    # First lookup (non-deleting) — used by the route before streaming.
    record = await lookup_upload_token(store=store, token=token)
    assert isinstance(record, UploadTokenRecord)
    # Token is still there.
    assert await store.get(collection="tokens", key=token) is not None
    # Consume after a successful stream.
    await consume_upload_token(store=store, token=token)
    assert await store.get(collection="tokens", key=token) is None


@pytest.mark.asyncio
async def test_consume_rejects_wrong_kind(
    store: MemoryStore, cfg, mcp, tmp_path: Path
) -> None:
    src = await mint_download_source(
        bytes_path=tmp_path / "p.bin", server=mcp, config=cfg, kv_store=store,
    )
    (tmp_path / "p.bin").write_bytes(b"x")
    token = src.root.url.rsplit("/", 1)[-1]
    with pytest.raises(LookupError, match="kind"):
        await consume_upload_token(store=store, token=token)


@pytest.mark.asyncio
async def test_resolve_intake_raises_for_missing_artifact_id(store: MemoryStore) -> None:
    from fastmcp_pvl_core._file_exchange._errors import (
        FileExchangeError,
        FileExchangeErrorCode,
    )

    with pytest.raises(FileExchangeError) as ei:
        await resolve_intake("nope", kv_store=store)
    assert ei.value.code is FileExchangeErrorCode.NOT_ACCESSIBLE
    assert "no intake recorded" in ei.value.detail


@pytest.mark.asyncio
async def test_resolve_intake_raises_when_bytes_not_yet_deposited(
    store: MemoryStore, tmp_path: Path
) -> None:
    from fastmcp_pvl_core._file_exchange._errors import (
        FileExchangeError,
        FileExchangeErrorCode,
    )

    missing = tmp_path / "not-yet.bin"
    await mint_intake_mapping(artifact_id="a1", intake_path=missing, kv_store=store)
    with pytest.raises(FileExchangeError) as ei:
        await resolve_intake("a1", kv_store=store)
    assert ei.value.code is FileExchangeErrorCode.NOT_ACCESSIBLE
    assert "not yet been deposited" in ei.value.detail


@pytest.mark.asyncio
async def test_resolve_intake_returns_path_when_bytes_present(
    store: MemoryStore, tmp_path: Path
) -> None:
    landed = tmp_path / "landed.bin"
    landed.write_bytes(b"arrived")
    await mint_intake_mapping(artifact_id="a1", intake_path=landed, kv_store=store)
    out = await resolve_intake("a1", kv_store=store)
    assert out == landed.resolve()


@pytest.mark.asyncio
async def test_sweep_expired_tokens_drops_expired(
    store: MemoryStore, cfg, mcp, tmp_path: Path
) -> None:
    cfg_short = type(cfg)(**{**cfg.__dict__, "file_exchange_capability_url_ttl_default_s": 0})
    src = await mint_download_source(
        bytes_path=tmp_path / "p.bin", server=mcp, config=cfg_short, kv_store=store,
    )
    (tmp_path / "p.bin").write_bytes(b"x")
    token = src.root.url.rsplit("/", 1)[-1]
    # Force backdate
    raw = await store.get(collection="tokens", key=token)
    raw["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    await store.put(collection="tokens", key=token, value=raw)
    removed = await sweep_expired_tokens(store=store)
    assert removed >= 1
    assert await store.get(collection="tokens", key=token) is None


@pytest.mark.asyncio
async def test_mint_and_consume_never_logs_full_token(
    store: MemoryStore, cfg, mcp, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="fastmcp_pvl_core")
    src = await mint_download_source(
        bytes_path=tmp_path / "p.bin", server=mcp, config=cfg, kv_store=store,
    )
    (tmp_path / "p.bin").write_bytes(b"x")
    token = src.root.url.rsplit("/", 1)[-1]
    await consume_download_token(store=store, token=token)
    rendered = " ".join(rec.getMessage() for rec in caplog.records)
    assert token not in rendered, "full token leaked into log output"
    assert src.root.url not in rendered, "full URL leaked into log output"


@pytest.mark.asyncio
async def test_mint_funcs_raise_configuration_error_when_no_base_url(
    store: MemoryStore, mcp, tmp_path: Path
) -> None:
    from fastmcp_pvl_core import ServerConfig
    from fastmcp_pvl_core._errors import ConfigurationError

    bare = ServerConfig()  # no base_url, no app_domain, no override
    with pytest.raises(ConfigurationError, match="public base URL"):
        await mint_download_source(
            bytes_path=tmp_path / "p.bin", server=mcp, config=bare, kv_store=store,
        )
```

- [ ] **Run to verify fail:**

```bash
uv run pytest tests/file_exchange/test_url_store.py -v
```

Expected: FAIL — module not found.

### Step E.2 — Implement `_url_store.py`

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_url_store.py`:**

```python
"""Capability-URL token store, intake-correlation map, and mint helpers
that require kv_store interaction.

Layout in the namespaced kv_store (single ``PrefixCollectionsWrapper``
view passed in as ``kv_store``):

- ``tokens:<token>`` — JSON record with ``kind: "download" | "upload"``.
- ``intake:<artifact_id>`` — JSON record with ``intake_path: str``.

Logging discipline (spec §12): capability URLs are bearer credentials.
This module logs at DEBUG using a token fingerprint (``tok=<first-8>...``);
the full token / URL never enter any log field.

Why ``make_filesystem_sink`` and ``mint_upload_sink`` are async while
``make_filesystem_source`` is sync: only the sink-side minters write
``intake:<artifact_id>`` into ``kv_store``. The source-side filesystem
minter is pure string composition. Forcing all four to async would be
ceremony without benefit.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from key_value.aio.protocols.key_value import AsyncKeyValue

from .._config import ServerConfig
from .._errors import ConfigurationError
from .._factory import compute_app_domain
from ._errors import FileExchangeError, FileExchangeErrorCode
from ._transport_filesystem import parse_volumes, resolve_exchange_uri
from ._types import (
    ExpectedConstraints,
    FileExchangeTransport,
    SinkDescriptor,
    SourceDescriptor,
)

logger = logging.getLogger("fastmcp_pvl_core.file_exchange")


def _fp(token: str) -> str:
    """Short, non-secret fingerprint suitable for log messages."""
    return f"tok={token[:8]}..."


def _resolve_public_base_url(server: Any, config: ServerConfig) -> str:
    """Resolve the public base URL for capability-URL composition.

    Precedence: explicit override (``file_exchange_https_public_base_url``)
    → ``compute_app_domain(config)`` → raise ``ConfigurationError``.
    """
    if config.file_exchange_https_public_base_url:
        return config.file_exchange_https_public_base_url.rstrip("/")
    domain = compute_app_domain(config)
    if domain is None:
        raise ConfigurationError(
            "file-exchange: HTTPS producer routes require a configured public base "
            "URL — set _FILE_EXCHANGE_HTTPS_PUBLIC_BASE_URL, _BASE_URL, or _APP_DOMAIN"
        )
    return domain.rstrip("/")


@dataclass(frozen=True)
class DownloadTokenRecord:
    token: str
    bytes_path: Path
    content_type: str | None
    expires_at: datetime
    single_use: bool


@dataclass(frozen=True)
class UploadTokenRecord:
    token: str
    intake_path: Path
    artifact_id: str
    max_size: int | None
    accept_mime_types: tuple[str, ...] | None
    require_digest: bool
    expires_at: datetime


def _new_token() -> str:
    # 128 bits of entropy, URL-safe base64.
    return secrets.token_urlsafe(16)


def _ttl_seconds(config: ServerConfig, override: int | None) -> int:
    return override if override is not None else config.file_exchange_capability_url_ttl_default_s


async def mint_download_source(
    *,
    bytes_path: Path,
    server: Any,
    config: ServerConfig,
    kv_store: AsyncKeyValue,
    expires_in_s: int | None = None,
    single_use: bool = True,
    content_type: str | None = None,
) -> SourceDescriptor:
    base = _resolve_public_base_url(server, config)
    token = _new_token()
    expires = datetime.now(timezone.utc) + timedelta(seconds=_ttl_seconds(config, expires_in_s))
    record = {
        "kind": "download",
        "bytes_path": str(bytes_path.resolve()),
        "content_type": content_type,
        "expires_at": expires.isoformat(),
        "single_use": single_use,
    }
    await kv_store.put(collection="tokens", key=token, value=record)
    logger.debug("file-exchange minted download %s (expires=%s)", _fp(token), expires.isoformat())
    url = f"{base}/file-exchange/d/{token}"
    return SourceDescriptor.model_validate({"transport": "download", "url": url, "expiresAt": expires.isoformat()})


async def mint_upload_sink(
    *,
    intake_path: Path,
    artifact_id: str,
    server: Any,
    config: ServerConfig,
    kv_store: AsyncKeyValue,
    expires_in_s: int | None = None,
    expected: ExpectedConstraints | None = None,
) -> SinkDescriptor:
    base = _resolve_public_base_url(server, config)
    token = _new_token()
    expires = datetime.now(timezone.utc) + timedelta(seconds=_ttl_seconds(config, expires_in_s))
    expected = expected or ExpectedConstraints()
    record = {
        "kind": "upload",
        "intake_path": str(intake_path.resolve()),
        "artifact_id": artifact_id,
        "max_size": expected.max_size,
        "accept_mime_types": list(expected.accept_mime_types) if expected.accept_mime_types else None,
        "require_digest": bool(expected.require_digest),
        "expires_at": expires.isoformat(),
    }
    await kv_store.put(collection="tokens", key=token, value=record)
    await mint_intake_mapping(artifact_id=artifact_id, intake_path=intake_path, kv_store=kv_store)
    logger.debug("file-exchange minted upload %s for artifact_id=%s", _fp(token), artifact_id)
    url = f"{base}/file-exchange/u/{token}"
    return SinkDescriptor.model_validate(
        {"transport": "upload", "url": url, "methods": ["PUT"], "expiresAt": expires.isoformat()}
    )


async def make_filesystem_sink(
    volume: str,
    relative_path: str,
    *,
    artifact_id: str,
    config: ServerConfig,
    kv_store: AsyncKeyValue,
) -> SinkDescriptor:
    """Compose an ``exchange://`` sink descriptor AND record the intake mapping.

    The descriptor's ``uri`` is the unresolved ``exchange://<vol>/<rel>``
    (portable across parties); the kv_store entry stores the local
    resolved path (the local party's view of where the bytes will land).
    """
    volumes = parse_volumes(config.file_exchange_volumes)
    if volume not in volumes:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"file-exchange volume {volume!r} is not configured on this party",
        )
    uri = f"exchange://{volume}/{relative_path.lstrip('/')}"
    resolved = _resolve_for_mint(volumes, volume, relative_path)
    await mint_intake_mapping(artifact_id=artifact_id, intake_path=resolved, kv_store=kv_store)
    return SinkDescriptor.model_validate({"transport": "filesystem", "uri": uri})


def _resolve_for_mint(volumes: dict[str, Path], volume: str, relative_path: str) -> Path:
    """Mint-time resolution with confinement check.

    The path does not have to exist yet (the sender will write to it),
    but the resolved location MUST lie inside the configured volume
    root — otherwise a ``..``-bearing relative path would let the
    receiver mint a sink that escapes its own volume. Path confinement
    is identical to what ``resolve_exchange_uri`` enforces at read-time
    (spec §10.1.3); refusing escapes at *mint* time means the sender's
    push-attempt cannot land bytes outside the volume.
    """
    root = volumes[volume].resolve()
    candidate = (root / relative_path.lstrip("/")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"file-exchange relative_path {relative_path!r} escapes volume {volume!r}",
        ) from exc
    return candidate


async def mint_intake_mapping(
    *,
    artifact_id: str,
    intake_path: Path,
    kv_store: AsyncKeyValue,
) -> None:
    """Record ``intake:<artifact_id> → resolved_intake_path``.

    Called by ``mint_upload_sink`` and ``make_filesystem_sink`` at mint
    time — so ``resolve_intake`` can distinguish 'wrong artifact_id' from
    'bytes not yet deposited' after the receiver returns the ticket.
    """
    await kv_store.put(
        collection="intake",
        key=artifact_id,
        value={"intake_path": str(intake_path.resolve())},
    )


async def resolve_intake(artifact_id: str, *, kv_store: AsyncKeyValue) -> Path:
    """Resolve an ``artifact_id`` to its deposited bytes' path.

    Raises ``FileExchangeError(NOT_ACCESSIBLE)`` for either 'unknown
    artifact_id' or 'bytes not yet deposited' (distinct ``detail`` strings).
    """
    raw = await kv_store.get(collection="intake", key=artifact_id)
    if raw is None:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"no intake recorded for artifact_id={artifact_id!r}",
        )
    path = Path(raw["intake_path"])
    if not path.exists():
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"bytes for artifact_id={artifact_id!r} have not yet been deposited",
        )
    return path


async def lookup_upload_token(*, store: AsyncKeyValue, token: str) -> UploadTokenRecord:
    """Non-deleting lookup. Used by the upload route BEFORE streaming."""
    raw = await store.get(collection="tokens", key=token)
    if raw is None:
        raise LookupError(f"unknown token {_fp(token)}")
    if raw.get("kind") != "upload":
        raise LookupError(f"token {_fp(token)} kind mismatch (expected upload)")
    expires_at = datetime.fromisoformat(raw["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        raise LookupError(f"token {_fp(token)} expired")
    accept = raw.get("accept_mime_types")
    return UploadTokenRecord(
        token=token,
        intake_path=Path(raw["intake_path"]),
        artifact_id=raw["artifact_id"],
        max_size=raw.get("max_size"),
        accept_mime_types=tuple(accept) if accept else None,
        require_digest=bool(raw.get("require_digest")),
        expires_at=expires_at,
    )


async def consume_download_token(*, store: AsyncKeyValue, token: str) -> DownloadTokenRecord:
    """Atomic single-use consume.

    Uses ``store.delete(...)`` (atomic per-key on Memory/FileTree/Redis/
    DynamoDB/MongoDB backends) as the concurrency primitive: the racing
    second consumer's delete returns False and we surface as
    ``LookupError`` so the route returns 404.
    """
    raw = await store.get(collection="tokens", key=token)
    if raw is None or raw.get("kind") != "download":
        raise LookupError(f"unknown or wrong-kind token {_fp(token)}")
    expires_at = datetime.fromisoformat(raw["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        raise LookupError(f"token {_fp(token)} expired")
    record = DownloadTokenRecord(
        token=token,
        bytes_path=Path(raw["bytes_path"]),
        content_type=raw.get("content_type"),
        expires_at=expires_at,
        single_use=bool(raw.get("single_use", True)),
    )
    if record.single_use:
        deleted = await store.delete(collection="tokens", key=token)
        if not deleted:
            raise LookupError(f"token {_fp(token)} already consumed by concurrent consumer")
    logger.debug("file-exchange consumed download %s", _fp(token))
    return record


async def consume_upload_token(*, store: AsyncKeyValue, token: str) -> None:
    """Atomic single-use consume for upload tokens. Called AFTER successful streaming."""
    raw = await store.get(collection="tokens", key=token)
    if raw is None:
        raise LookupError(f"unknown token {_fp(token)}")
    if raw.get("kind") != "upload":
        raise LookupError(f"token {_fp(token)} kind mismatch (expected upload)")
    deleted = await store.delete(collection="tokens", key=token)
    if not deleted:
        raise LookupError(f"token {_fp(token)} already consumed by concurrent consumer")
    logger.debug("file-exchange consumed upload %s", _fp(token))


async def sweep_expired_tokens(*, store: AsyncKeyValue) -> int:
    """Iterate the ``tokens`` collection and delete expired records.

    Returns the count of removed records. Best-effort: if the backend
    does not expose a key-listing primitive, returns 0 (operators can
    rely on per-key expiry from the backend instead — Redis TTL, etc.).

    The ``AsyncKeyValue`` ``keys`` method (verify against the installed
    ``py-key-value-aio``) returns ``list[str]`` as an awaitable coroutine,
    not an async iterator. Verify with::

        uv run python -c "from key_value.aio.stores.memory import MemoryStore; import inspect; print(inspect.signature(MemoryStore.keys))"
    """
    now = datetime.now(timezone.utc)
    removed = 0
    keys_fn = getattr(store, "keys", None)
    if keys_fn is None:
        return 0
    keys = await keys_fn(collection="tokens")
    for key in keys:
        raw = await store.get(collection="tokens", key=key)
        if raw is None:
            continue
        try:
            expires = datetime.fromisoformat(raw["expires_at"])
        except (KeyError, ValueError):
            continue
        if now > expires:
            if await store.delete(collection="tokens", key=key):
                removed += 1
    if removed:
        logger.debug("file-exchange sweep removed %d expired token(s)", removed)
    return removed
```

- [ ] **Run to verify pass:**

```bash
uv run pytest tests/file_exchange/test_url_store.py -v
```

Expected: 12 passing. (If the in-memory `MemoryStore` doesn't expose `keys`/`list_keys`, the sweep test asserts `>= 1` — if zero is observed, surface to user before relaxing the assertion.)

### Step E.3 — Failing tests for `_routes.py`

- [ ] **Write `tests/file_exchange/test_routes.py`:**

```python
"""Sibling-route handlers (GET /file-exchange/d/<token>, PUT|POST
/file-exchange/u/<token>). The handlers are tested directly with a
synthetic Starlette ``Request``; the integration-level mount via
``FastMCP.custom_route`` is exercised in Task G."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from key_value.aio.stores.memory import MemoryStore
from starlette.testclient import TestClient
from starlette.applications import Starlette

from fastmcp_pvl_core._file_exchange._routes import build_file_exchange_router
from fastmcp_pvl_core._file_exchange._url_store import (
    mint_download_source,
    mint_upload_sink,
)


@pytest.fixture
def cfg(tmp_path):
    from fastmcp_pvl_core import ServerConfig

    return ServerConfig(
        transport="http",
        base_url="https://example.com",
        file_exchange_https_public_base_url="https://example.com",
        file_exchange_capability_url_ttl_default_s=600,
    )


@pytest.fixture
def mcp():
    from fastmcp import FastMCP

    return FastMCP("test-routes")


@pytest.fixture
def app_and_store(cfg, mcp):
    store = MemoryStore()
    download_handler, upload_handler = build_file_exchange_router(store=store)
    app = Starlette(routes=[])
    app.add_route("/file-exchange/d/{token}", download_handler, methods=["GET"])
    app.add_route("/file-exchange/u/{token}", upload_handler, methods=["PUT", "POST"])
    return app, store


@pytest.mark.asyncio
async def test_download_returns_bytes_and_consumes_token(
    app_and_store, cfg, mcp, tmp_path: Path
) -> None:
    app, store = app_and_store
    payload = b"hello" * 100
    src_file = tmp_path / "p.bin"
    src_file.write_bytes(payload)
    src = await mint_download_source(
        bytes_path=src_file, server=mcp, config=cfg, kv_store=store, content_type="text/plain",
    )
    token = src.root.url.rsplit("/", 1)[-1]
    with TestClient(app) as client:
        r = client.get(f"/file-exchange/d/{token}")
        assert r.status_code == 200
        assert r.content == payload
        # Second call → 404 (single-use consumed)
        r2 = client.get(f"/file-exchange/d/{token}")
        assert r2.status_code == 404


@pytest.mark.asyncio
async def test_upload_streams_body_validates_mime_and_consumes_token(
    app_and_store, cfg, mcp, tmp_path: Path
) -> None:
    app, store = app_and_store
    intake = tmp_path / "out.bin"
    from fastmcp_pvl_core._file_exchange._types import ExpectedConstraints

    sink = await mint_upload_sink(
        intake_path=intake, artifact_id="a1", server=mcp, config=cfg, kv_store=store,
        expected=ExpectedConstraints(max_size=4096, accept_mime_types=("application/pdf",)),
    )
    token = sink.root.url.rsplit("/", 1)[-1]
    body = b"%PDF-fake content" + b"x" * 1000
    with TestClient(app) as client:
        r = client.put(
            f"/file-exchange/u/{token}",
            data=body,
            headers={"Content-Type": "application/pdf"},
        )
        assert r.status_code in (200, 204)
        assert intake.read_bytes() == body
        # Token consumed → 404 on retry
        r2 = client.put(
            f"/file-exchange/u/{token}",
            data=b"x",
            headers={"Content-Type": "application/pdf"},
        )
        assert r2.status_code == 404


@pytest.mark.asyncio
async def test_upload_rejects_mime_mismatch_before_streaming(
    app_and_store, cfg, mcp, tmp_path: Path
) -> None:
    app, store = app_and_store
    from fastmcp_pvl_core._file_exchange._types import ExpectedConstraints

    sink = await mint_upload_sink(
        intake_path=tmp_path / "o.bin", artifact_id="a1", server=mcp, config=cfg, kv_store=store,
        expected=ExpectedConstraints(accept_mime_types=("application/pdf",)),
    )
    token = sink.root.url.rsplit("/", 1)[-1]
    with TestClient(app) as client:
        r = client.put(
            f"/file-exchange/u/{token}",
            data=b"hello",
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 415
        # Token still present (not consumed on validation failure)
        assert await store.get(collection="tokens", key=token) is not None


@pytest.mark.asyncio
async def test_upload_fails_fast_on_size_exceeded_with_413(
    app_and_store, cfg, mcp, tmp_path: Path
) -> None:
    app, store = app_and_store
    from fastmcp_pvl_core._file_exchange._types import ExpectedConstraints

    intake = tmp_path / "o.bin"
    sink = await mint_upload_sink(
        intake_path=intake, artifact_id="a1", server=mcp, config=cfg, kv_store=store,
        expected=ExpectedConstraints(max_size=1024, accept_mime_types=("application/octet-stream",)),
    )
    token = sink.root.url.rsplit("/", 1)[-1]
    oversized = b"x" * (1024 + 1024)
    with TestClient(app) as client:
        r = client.put(
            f"/file-exchange/u/{token}",
            data=oversized,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 413
        # Intake bytes NOT written
        assert not intake.exists()
        # No temp file residue in intake dir
        if intake.parent.exists():
            residue = [p for p in intake.parent.iterdir() if p.name.startswith(".")]
            assert residue == []
        # Token still present (not consumed on validation failure)
        assert await store.get(collection="tokens", key=token) is not None


@pytest.mark.asyncio
async def test_upload_rejects_digest_mismatch_with_422(
    app_and_store, cfg, mcp, tmp_path: Path
) -> None:
    app, store = app_and_store
    from fastmcp_pvl_core._file_exchange._types import ExpectedConstraints

    import base64

    intake = tmp_path / "o.bin"
    sink = await mint_upload_sink(
        intake_path=intake, artifact_id="a1", server=mcp, config=cfg, kv_store=store,
        expected=ExpectedConstraints(require_digest=True, accept_mime_types=("application/octet-stream",)),
    )
    token = sink.root.url.rsplit("/", 1)[-1]
    body = b"abc"
    # 32 zero bytes is a valid-length sha-256 digest, but it's the wrong
    # digest for body=b"abc" (whose real sha-256 is ba7816bf...). A test
    # that uses an invalid base64 length would assert for the wrong reason
    # (length-decode failure, not digest mismatch).
    wrong = "sha-256=:" + base64.b64encode(bytes(32)).decode("ascii") + ":"
    with TestClient(app) as client:
        r = client.put(
            f"/file-exchange/u/{token}",
            data=body,
            headers={"Content-Type": "application/octet-stream", "Content-Digest": wrong},
        )
        assert r.status_code == 422
        assert not intake.exists()
```

- [ ] **Run to verify fail:**

```bash
uv run pytest tests/file_exchange/test_routes.py -v
```

Expected: FAIL — module not found.

### Step E.4 — Implement `_routes.py`

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_routes.py`:**

```python
"""Sibling HTTP route handlers for capability URLs.

Mounted via ``FastMCP.custom_route(path, methods=[...])(handler)`` from
``_capability.register_file_exchange_capability``. We expose
``build_file_exchange_router(store)`` which returns the
``(download_handler, upload_handler)`` pair so the mount call site can
register them imperatively.

Route flow (upload — see spec §6.2 producer side):
  1. Lookup the token (non-deleting).
  2. Validate ``Content-Type`` against ``accept_mime_types`` (HTTP 415).
  3. Stream the body via ``async_atomic_write`` — per-chunk:
     - update sha-256 digester
     - increment cumulative byte counter
     - on cumulative > max_size: raise to roll back temp + return 413
     - dispatch the chunk write via ``asyncio.to_thread``
  4. After body fully received, verify ``Content-Digest`` (HTTP 422 on mismatch).
  5. On clean exit of ``async_atomic_write`` (renames onto intake_path),
     consume the token (atomic delete).
  6. The intake mapping was already recorded by ``mint_upload_sink`` at
     mint time; the route does NOT write it again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from key_value.aio.protocols.key_value import AsyncKeyValue
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from ._transport_filesystem import async_atomic_write
from ._url_store import (
    consume_download_token,
    consume_upload_token,
    lookup_upload_token,
)

logger = logging.getLogger("fastmcp_pvl_core.file_exchange")

_DIGEST_RE = re.compile(r"sha-256=:([A-Za-z0-9+/=]+):")
_CHUNK_SIZE = 64 * 1024


class _UploadTooLarge(Exception):
    pass


class _DigestMismatch(Exception):
    pass


def build_file_exchange_router(
    *, store: AsyncKeyValue
) -> tuple[Callable[[Request], Awaitable[Response]], Callable[[Request], Awaitable[Response]]]:
    """Build the (download_handler, upload_handler) pair.

    Returns plain async functions; the caller mounts them via
    ``FastMCP.custom_route(path, methods=[...])(handler)``.
    """

    async def _download(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            record = await consume_download_token(store=store, token=token)
        except LookupError:
            return Response(status_code=404)
        # Stream the file via Starlette's FileResponse (uses sendfile when available).
        headers = {}
        if record.content_type:
            headers["content-type"] = record.content_type
        return FileResponse(path=str(record.bytes_path), headers=headers)

    async def _upload(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            record = await lookup_upload_token(store=store, token=token)
        except LookupError:
            return Response(status_code=404)

        # 1. MIME validation BEFORE streaming.
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if record.accept_mime_types and content_type not in record.accept_mime_types:
            return Response(
                status_code=415,
                content=f"mime-rejected: got {content_type!r} not in {list(record.accept_mime_types)}",
            )

        digester = hashlib.sha256()
        cumulative = 0
        try:
            async with async_atomic_write(record.intake_path) as fh:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    cumulative += len(chunk)
                    if record.max_size is not None and cumulative > record.max_size:
                        raise _UploadTooLarge()
                    digester.update(chunk)
                    await asyncio.to_thread(fh.write, chunk)
                # Inside the with block: digest check (so a mismatch
                # raises and the temp file is discarded).
                provided = request.headers.get("content-digest")
                if record.require_digest or provided:
                    if not provided:
                        raise _DigestMismatch()
                    m = _DIGEST_RE.search(provided)
                    if not m:
                        raise _DigestMismatch()
                    expected = base64.b64decode(m.group(1))
                    if expected != digester.digest():
                        raise _DigestMismatch()
        except _UploadTooLarge:
            return Response(
                status_code=413,
                content=f"size-exceeded: cap={record.max_size} cumulative>={cumulative}",
            )
        except _DigestMismatch:
            return Response(status_code=422, content="digest-mismatch")

        # 2. Stream succeeded + validations passed → consume the token.
        try:
            await consume_upload_token(store=store, token=token)
        except LookupError:
            # Racing concurrent consumer beat us — surface as 409.
            return Response(status_code=409)
        return Response(status_code=204)

    return _download, _upload
```

- [ ] **Run to verify pass:**

```bash
uv run pytest tests/file_exchange/test_routes.py -v
```

Expected: 5 passing.

### Step E.5 — Commit Task E

- [ ] **Commit:**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_url_store.py \
        src/fastmcp_pvl_core/_file_exchange/_routes.py \
        tests/file_exchange/test_url_store.py \
        tests/file_exchange/test_routes.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): capability URL store + sibling HTTP routes

Adds mint_download_source, mint_upload_sink, make_filesystem_sink, and
mint_intake_mapping (the three sink-side minters are async because they
write kv_store entries; the source-side filesystem minter from Task C
stays sync). Token records carry kind: 'download' | 'upload'.
consume_* helpers use atomic per-key delete as the single-use concurrency
primitive. resolve_intake distinguishes 'unknown artifact_id' from
'bytes not yet deposited'.

Adds build_file_exchange_router returning the (download, upload) handler
pair. Upload route order: lookup → MIME check (415) → stream into
async_atomic_write with per-chunk digester + cumulative byte counter
(413 on size-exceeded, temp discarded) → digest check (422) → consume
token (atomic delete). All sync disk I/O dispatched via asyncio.to_thread.

Logging uses tok=<first-8>... fingerprints; full tokens / URLs never
hit log fields.

Closes #128.
EOF
)"
```

- [ ] Run the universal per-task suffix.

---

## Task F: Capability declaration + role helpers + top-level public API (closes #129)

**Depends on:** Tasks B, C, D, E all merged.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_capability.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_provider.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_fetcher.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_receiver.py`
- Create: `src/fastmcp_pvl_core/_file_exchange/_sender.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/__init__.py` (top-level re-exports)
- Create: `tests/file_exchange/test_capability.py`
- Create: `tests/file_exchange/test_roles.py`

### Step F.1 — Failing tests for the capability-gating matrix

- [ ] **Write `tests/file_exchange/test_capability.py`:**

```python
"""register_file_exchange_capability gating matrix (design §3)."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore

from fastmcp_pvl_core import ServerConfig, register_file_exchange_capability

_CAP_KEY = "nl.liesdonk.file-exchange"


def _make(transport: str, *, volumes: str | None = None) -> ServerConfig:
    return ServerConfig(
        transport=transport,   # type: ignore[arg-type]
        base_url="https://example.com" if transport == "http" else None,
        file_exchange_volumes=volumes,
        file_exchange_https_public_base_url=(
            "https://example.com" if transport == "http" else None
        ),
    )


@pytest.mark.asyncio
async def test_gating_stdio_no_volumes_advertises_consumer_roles_only() -> None:
    mcp = FastMCP("s")
    cfg = _make("stdio", volumes=None)
    store = MemoryStore()
    register_file_exchange_capability(mcp, cfg, kv_store=store)
    cap = mcp.experimental_capabilities[_CAP_KEY]
    assert "fetcher" in cap["roles"]
    assert "sender" in cap["roles"]
    assert "provider" not in cap["roles"]   # no HTTP app to host, no volumes
    assert "receiver" not in cap["roles"]
    assert cap["roles"]["fetcher"] == ["download"]
    assert cap["roles"]["sender"] == ["upload"]


@pytest.mark.asyncio
async def test_gating_stdio_with_volumes_advertises_all_roles_filesystem_only(
    tmp_path,
) -> None:
    mcp = FastMCP("s")
    (tmp_path / "vol").mkdir()
    cfg = _make("stdio", volumes=f"v={tmp_path / 'vol'}")
    store = MemoryStore()
    register_file_exchange_capability(mcp, cfg, kv_store=store)
    roles = mcp.experimental_capabilities[_CAP_KEY]["roles"]
    # All four roles advertised
    assert set(roles) == {"provider", "fetcher", "receiver", "sender"}
    # Producer roles: filesystem only
    assert roles["provider"] == ["filesystem"]
    assert roles["receiver"] == ["filesystem"]
    # Consumer roles: filesystem + outbound HTTPS
    assert "filesystem" in roles["fetcher"] and "download" in roles["fetcher"]
    assert "filesystem" in roles["sender"] and "upload" in roles["sender"]


@pytest.mark.asyncio
async def test_gating_http_no_volumes_advertises_all_roles_https_only() -> None:
    mcp = FastMCP("s")
    cfg = _make("http", volumes=None)
    store = MemoryStore()
    register_file_exchange_capability(mcp, cfg, kv_store=store)
    roles = mcp.experimental_capabilities[_CAP_KEY]["roles"]
    assert set(roles) == {"provider", "fetcher", "receiver", "sender"}
    assert roles["provider"] == ["download"]
    assert roles["fetcher"] == ["download"]
    assert roles["receiver"] == ["upload"]
    assert roles["sender"] == ["upload"]


@pytest.mark.asyncio
async def test_gating_http_with_volumes_advertises_all_roles_both_transports(
    tmp_path,
) -> None:
    mcp = FastMCP("s")
    (tmp_path / "vol").mkdir()
    cfg = _make("http", volumes=f"v={tmp_path / 'vol'}")
    store = MemoryStore()
    register_file_exchange_capability(mcp, cfg, kv_store=store)
    roles = mcp.experimental_capabilities[_CAP_KEY]["roles"]
    for r in ("provider", "fetcher", "receiver", "sender"):
        assert "filesystem" in roles[r]
    assert "download" in roles["provider"]
    assert "download" in roles["fetcher"]
    assert "upload" in roles["receiver"]
    assert "upload" in roles["sender"]


@pytest.mark.asyncio
async def test_gating_advertises_nothing_when_no_role_satisfies_any_transport() -> None:
    mcp = FastMCP("s")
    cfg = _make("stdio", volumes=None)
    store = MemoryStore()
    register_file_exchange_capability(
        mcp, cfg, kv_store=store, roles=("provider", "receiver"),
    )
    # provider needs http+download or volumes; receiver needs http+upload or
    # volumes. Neither is available. Capability must be absent.
    assert _CAP_KEY not in mcp.experimental_capabilities


@pytest.mark.asyncio
async def test_capability_advertises_max_artifact_size_when_set(tmp_path) -> None:
    mcp = FastMCP("s")
    (tmp_path / "vol").mkdir()
    cfg = _make("stdio", volumes=f"v={tmp_path / 'vol'}")
    cfg2 = type(cfg)(
        **{**cfg.__dict__, "file_exchange_max_artifact_size": 104857600},
    )
    store = MemoryStore()
    register_file_exchange_capability(mcp, cfg2, kv_store=store)
    cap = mcp.experimental_capabilities[_CAP_KEY]
    assert cap["maxArtifactSize"] == 104857600


@pytest.mark.asyncio
async def test_capability_advertises_sha256_digest(tmp_path) -> None:
    mcp = FastMCP("s")
    (tmp_path / "vol").mkdir()
    cfg = _make("stdio", volumes=f"v={tmp_path / 'vol'}")
    store = MemoryStore()
    register_file_exchange_capability(mcp, cfg, kv_store=store)
    cap = mcp.experimental_capabilities[_CAP_KEY]
    assert cap["digests"] == ["sha-256"]


@pytest.mark.asyncio
async def test_capability_reregistration_idempotent_clears_prior_advert() -> None:
    """If a previous call in this process advertised the capability and a
    later call's gate produces an empty role set, the capability key must
    be removed (not stale-left from the prior call)."""
    mcp = FastMCP("s")
    store = MemoryStore()
    # First call advertises (http transport + no volumes)
    register_file_exchange_capability(mcp, _make("http"), kv_store=store)
    assert _CAP_KEY in mcp.experimental_capabilities
    # Second call with restricted roles + stdio → empty post-gate
    register_file_exchange_capability(
        mcp, _make("stdio"), kv_store=store, roles=("provider", "receiver"),
    )
    assert _CAP_KEY not in mcp.experimental_capabilities


@pytest.mark.asyncio
async def test_capability_raises_when_https_routes_advertised_but_no_base_url() -> None:
    from fastmcp_pvl_core._errors import ConfigurationError

    mcp = FastMCP("s")
    bare = ServerConfig(transport="http")  # no base_url, no app_domain, no override
    store = MemoryStore()
    with pytest.raises(ConfigurationError, match="public base URL"):
        register_file_exchange_capability(mcp, bare, kv_store=store)


@pytest.mark.asyncio
async def test_capability_mounts_custom_routes_when_https_producer_survives(tmp_path) -> None:
    mcp = FastMCP("s")
    cfg = _make("http")
    store = MemoryStore()
    register_file_exchange_capability(mcp, cfg, kv_store=store)
    # FastMCP exposes registered custom routes in a private list; the
    # assertion uses the documented attribute name (.custom_routes) where
    # available, otherwise falls back to introspecting the http app's
    # routes. The test asserts both download and upload paths are mounted.
    routes_attr = getattr(mcp, "_additional_http_routes", None) or getattr(
        mcp, "custom_routes", []
    )
    paths = {getattr(r, "path", None) for r in routes_attr}
    assert "/file-exchange/d/{token}" in paths
    assert "/file-exchange/u/{token}" in paths
```

- [ ] **Run to verify fail:**

```bash
uv run pytest tests/file_exchange/test_capability.py -v
```

Expected: FAIL — `register_file_exchange_capability` not exported yet.

### Step F.2 — Implement `_capability.py`

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_capability.py`:**

```python
"""Capability declaration + auto-mount of sibling HTTP routes.

This is the load-bearing module: it owns the gating algorithm (spec §4.2
+ design §3) and converts the operator's deployment shape into a wire-
faithful ``nl.liesdonk.file-exchange`` capability block.

Public entry point: ``register_file_exchange_capability``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fastmcp import FastMCP
from key_value.aio.protocols.key_value import AsyncKeyValue

from .._config import ServerConfig
from .._errors import ConfigurationError
from .._factory import compute_app_domain
from ._errors import CAPABILITY_KEY
from ._routes import build_file_exchange_router
from ._transport_filesystem import parse_volumes
from ._types import FileExchangeRole, FileExchangeTransport

logger = logging.getLogger("fastmcp_pvl_core.file_exchange")

# Family default — hardcoded in pvl-core (design §3 / framing principle:
# pvl-core owns shape decisions). Not a kwarg.
_ADVERTISED_DIGESTS: tuple[str, ...] = ("sha-256",)

_HTTP_TRANSPORTS = ("http", "sse")  # FastMCP serves an HTTP app under both transports


def _transports_for_role(
    role: FileExchangeRole,
    *,
    volumes: dict[str, Any],
    http_app: bool,
) -> list[FileExchangeTransport]:
    """Compute the spec §4.2 row for one role under one deployment shape.

    - filesystem requires at least one configured volume.
    - download as provider requires an http app to host /file-exchange/d/.
    - download as fetcher is always available (outbound HTTPS).
    - upload as receiver requires an http app to host /file-exchange/u/.
    - upload as sender is always available (outbound HTTPS).
    """
    transports: list[FileExchangeTransport] = []
    if volumes:
        transports.append("filesystem")
    if role in ("provider", "fetcher"):
        # fetcher: always; provider: only if we can host the route.
        if role == "fetcher" or http_app:
            transports.append("download")
    if role in ("receiver", "sender"):
        if role == "sender" or http_app:
            transports.append("upload")
    return transports


def register_file_exchange_capability(
    server: FastMCP,
    config: ServerConfig,
    *,
    roles: Sequence[FileExchangeRole] = ("provider", "fetcher", "receiver", "sender"),
    kv_store: AsyncKeyValue,
) -> None:
    """Declare ``nl.liesdonk.file-exchange`` and mount HTTPS routes if needed.

    Per design §3 (single-source-of-truth for the framing principle): all
    operator-side concerns live in ``ServerConfig`` (volumes, transport,
    max-artifact-size, allow-loopback, …); the only domain hooks are
    ``roles`` (which roles this server wants to play) and ``kv_store``
    (the namespaced storage backend chosen by the operator).

    There is no ``digests`` kwarg — the family default ``("sha-256",)`` is
    hardcoded. There is no ``max_artifact_size`` kwarg — it's read from
    ``config.file_exchange_max_artifact_size`` and emitted only when set.
    There is no ``base_url`` kwarg — it's resolved from config + the
    existing ``compute_app_domain`` helper.
    """
    volumes = parse_volumes(config.file_exchange_volumes)
    http_app = config.transport in _HTTP_TRANSPORTS

    # Gating: per-role transports, drop empty rows, drop the capability
    # entirely if nothing survives.
    role_map: dict[str, list[str]] = {}
    for role in roles:
        ts = _transports_for_role(role, volumes=volumes, http_app=http_app)
        if ts:
            role_map[role] = ts

    if not role_map:
        # Idempotency: clear any stale advert from a prior call in this process.
        server.experimental_capabilities.pop(CAPABILITY_KEY, None)
        logger.info(
            "file-exchange: no satisfiable transport for any declared role — capability not advertised"
        )
        return

    # If we're about to advertise HTTPS routes on the producer side, we
    # need a public base URL. Resolve eagerly so the failure mode is
    # registration-time, not request-time.
    will_mount_routes = http_app and (
        ("download" in role_map.get("provider", []))
        or ("upload" in role_map.get("receiver", []))
    )
    if will_mount_routes:
        domain = (
            config.file_exchange_https_public_base_url
            or compute_app_domain(config)
        )
        if domain is None:
            raise ConfigurationError(
                "file-exchange: HTTPS producer routes require a configured public base "
                "URL — set _FILE_EXCHANGE_HTTPS_PUBLIC_BASE_URL, _BASE_URL, or _APP_DOMAIN"
            )

    block: dict[str, Any] = {
        "version": "0.1",
        "digests": list(_ADVERTISED_DIGESTS),
        "roles": role_map,
    }
    if config.file_exchange_max_artifact_size is not None:
        block["maxArtifactSize"] = config.file_exchange_max_artifact_size
    server.experimental_capabilities[CAPABILITY_KEY] = block

    if will_mount_routes:
        download_handler, upload_handler = build_file_exchange_router(store=kv_store)
        server.custom_route("/file-exchange/d/{token}", methods=["GET"])(download_handler)
        server.custom_route("/file-exchange/u/{token}", methods=["PUT", "POST"])(upload_handler)
        logger.info(
            "file-exchange: registered HTTPS routes /file-exchange/{d,u}/{token}"
        )

    logger.info(
        "file-exchange: capability advertised — roles=%s, digests=%s",
        list(role_map.keys()),
        _ADVERTISED_DIGESTS,
    )
```

- [ ] **Run** the capability tests to verify pass:

```bash
uv run pytest tests/file_exchange/test_capability.py -v
```

Expected: 9 passing. (If FastMCP exposes registered custom routes under a different attribute than `_additional_http_routes` / `custom_routes`, adjust the test's introspection accordingly — verify via `uv run python -c "from fastmcp import FastMCP; m = FastMCP('x'); m.custom_route('/p', methods=['GET'])(lambda r: None); print([a for a in dir(m) if 'route' in a.lower()])"` and use whatever attribute holds the registered handler list. The PRODUCTION code at `server.custom_route(...)` is fixed — only the test's introspection adapts.)

### Step F.3 — Failing tests for `_provider.py` and `_fetcher.py`

- [ ] **Write `tests/file_exchange/test_roles.py`:**

```python
"""Role helpers: provider, fetcher, receiver, sender."""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

import pytest
from key_value.aio.stores.memory import MemoryStore

from fastmcp_pvl_core import ServerConfig
from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
)
from fastmcp_pvl_core._file_exchange._fetcher import pull_artifact
from fastmcp_pvl_core._file_exchange._provider import build_pull_response
from fastmcp_pvl_core._file_exchange._receiver import (
    build_intake_response,
    open_intake,
)
from fastmcp_pvl_core._file_exchange._sender import push_artifact
from fastmcp_pvl_core._file_exchange._types import (
    ArtifactMetadata,
    ExpectedConstraints,
    IntakeTicket,
    SinkDescriptor,
    SourceDescriptor,
    TransferHandle,
)


def test_build_pull_response_emits_handle_in_meta() -> None:
    artifact = ArtifactMetadata.model_validate({"id": "a1", "name": "x.txt", "size": 5, "mimeType": "text/plain"})
    src = SourceDescriptor.model_validate({"transport": "filesystem", "uri": "exchange://v/x.txt"})
    result = build_pull_response(artifact, sources=[src])
    assert result.isError in (False, None)
    meta = result.meta or {}
    handle = meta["nl.liesdonk.file-exchange"]["handle"]
    assert handle["artifact"]["id"] == "a1"
    assert handle["sources"][0]["transport"] == "filesystem"


def test_build_pull_response_auto_synthesises_summary_text() -> None:
    artifact = ArtifactMetadata.model_validate({"id": "a1", "name": "x.txt", "size": 5, "mimeType": "text/plain"})
    src = SourceDescriptor.model_validate({"transport": "filesystem", "uri": "exchange://v/x.txt"})
    result = build_pull_response(artifact, sources=[src])
    text = " ".join(c.text for c in result.content if hasattr(c, "text"))
    assert "x.txt" in text
    assert "text/plain" in text


def test_open_intake_constructs_ticket_with_auto_artifact_id() -> None:
    sink = SinkDescriptor.model_validate({"transport": "filesystem", "uri": "exchange://v/a1.bin"})
    ticket = open_intake(sinks=[sink], expected=ExpectedConstraints(max_size=2048))
    assert isinstance(ticket, IntakeTicket)
    assert ticket.artifact_id   # auto-generated when None
    assert ticket.expected is not None
    assert ticket.expected.max_size == 2048


def test_build_intake_response_emits_ticket_in_meta() -> None:
    sink = SinkDescriptor.model_validate({"transport": "filesystem", "uri": "exchange://v/a1.bin"})
    ticket = open_intake(sinks=[sink], artifact_id="a1")
    result = build_intake_response(ticket)
    meta = result.meta or {}
    out = meta["nl.liesdonk.file-exchange"]["ticket"]
    assert out["artifactId"] == "a1"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "vault").mkdir()
    return ServerConfig(
        transport="http",
        base_url="https://example.com",
        file_exchange_volumes=f"vault={tmp_path / 'vault'}",
        file_exchange_https_allow_loopback=True,
        file_exchange_https_allow_private=True,
    )


@pytest.mark.asyncio
async def test_pull_artifact_filesystem_path_streams_into_dest(cfg, tmp_path: Path) -> None:
    src_path = tmp_path / "vault" / "note.md"
    src_path.write_bytes(b"hello")
    handle = TransferHandle.model_validate({
        "artifact": {"id": "a1", "size": 5},
        "sources": [{"transport": "filesystem", "uri": "exchange://vault/note.md"}],
    })
    out = tmp_path / "out.bin"
    artifact = await pull_artifact(handle, dest=out, config=cfg)
    assert artifact.id == "a1"
    assert out.read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_pull_artifact_raises_when_no_transport_matches(cfg, tmp_path: Path) -> None:
    cfg_no_volumes = type(cfg)(**{**cfg.__dict__, "file_exchange_volumes": None})
    handle = TransferHandle.model_validate({
        "artifact": {"id": "a1"},
        "sources": [{"transport": "filesystem", "uri": "exchange://vault/x"}],
    })
    with pytest.raises(FileExchangeError) as ei:
        await pull_artifact(
            handle, dest=tmp_path / "o.bin", config=cfg_no_volumes,
            supported_transports=("filesystem",),
        )
    assert ei.value.code is FileExchangeErrorCode.NO_SUPPORTED_TRANSPORT


@pytest.mark.asyncio
async def test_push_artifact_filesystem_streams_chunked(cfg, tmp_path: Path) -> None:
    src_path = tmp_path / "payload.bin"
    src_path.write_bytes(b"y" * 200_000)   # > one 64 KiB chunk
    sink_uri = "exchange://vault/a1.bin"
    ticket = IntakeTicket.model_validate({
        "artifactId": "a1",
        "sinks": [{"transport": "filesystem", "uri": sink_uri}],
    })
    await push_artifact(ticket, source=src_path, config=cfg)
    landed = tmp_path / "vault" / "a1.bin"
    assert landed.read_bytes() == b"y" * 200_000


@pytest.mark.asyncio
async def test_push_artifact_raises_on_size_exceeded(cfg, tmp_path: Path) -> None:
    src_path = tmp_path / "payload.bin"
    src_path.write_bytes(b"y" * 200_000)
    ticket = IntakeTicket.model_validate({
        "artifactId": "a1",
        "sinks": [{"transport": "filesystem", "uri": "exchange://vault/a1.bin"}],
        "expected": {"maxSize": 1024},
    })
    with pytest.raises(FileExchangeError) as ei:
        await push_artifact(ticket, source=src_path, config=cfg, artifact_size=200_000)
    assert ei.value.code is FileExchangeErrorCode.SIZE_EXCEEDED
```

- [ ] **Run to verify fail:**

```bash
uv run pytest tests/file_exchange/test_roles.py -v
```

Expected: FAIL — modules not found.

### Step F.4 — Implement provider, fetcher, receiver, sender

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_provider.py`:**

```python
"""Provider role: build a CallToolResult carrying a TransferHandle."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp.types import CallToolResult, TextContent

from ._errors import CAPABILITY_KEY
from ._types import ArtifactMetadata, SourceDescriptor, TransferHandle


def _human_size(n: int | None) -> str:
    if n is None:
        return "unknown size"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _auto_summary(artifact: ArtifactMetadata) -> str:
    name = artifact.name or artifact.id
    return f"file-exchange: {name} ({_human_size(artifact.size)}, {artifact.mime_type or 'unknown mime'})"


def build_pull_response(
    artifact: ArtifactMetadata,
    sources: Sequence[SourceDescriptor],
    *,
    summary: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> CallToolResult:
    handle = TransferHandle(artifact=artifact, sources=tuple(sources))
    fe_meta = {"handle": handle.model_dump(by_alias=True, exclude_none=True)}
    if extra_meta:
        fe_meta.update(extra_meta)
    return CallToolResult(
        content=[TextContent(type="text", text=summary or _auto_summary(artifact))],
        isError=False,
        _meta={CAPABILITY_KEY: fe_meta},
    )
```

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_fetcher.py`:**

```python
"""Fetcher role: pull artifact bytes from a TransferHandle.

The single public symbol is ``pull_artifact``. ``config`` is the
deployment context — the fetcher derives ``volumes`` and the SSRF guard
from it; no leaking of those operator concerns through kwargs.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO

from .._config import ServerConfig
from ._errors import FileExchangeError, FileExchangeErrorCode
from ._select import select_source
from ._transport_filesystem import parse_volumes, resolve_exchange_uri
from ._transport_https import SSRFGuardConfig, pull_download
from ._types import (
    ArtifactMetadata,
    FileExchangeTransport,
    TransferHandle,
)

_CHUNK_SIZE = 64 * 1024


def _default_supported(config: ServerConfig) -> tuple[FileExchangeTransport, ...]:
    """Consumer-side default: filesystem if volumes are configured; download
    is always available (outbound HTTPS works regardless of transport)."""
    out: list[FileExchangeTransport] = []
    if parse_volumes(config.file_exchange_volumes):
        out.append("filesystem")
    out.append("download")
    return tuple(out)


async def pull_artifact(
    handle: TransferHandle | dict,
    *,
    dest: Path | BinaryIO,
    config: ServerConfig,
    supported_transports: Sequence[FileExchangeTransport] | None = None,
) -> ArtifactMetadata:
    h = handle if isinstance(handle, TransferHandle) else TransferHandle.model_validate(handle)
    supported = tuple(supported_transports) if supported_transports else _default_supported(config)
    chosen = select_source(h.sources, supported=supported)
    digester = hashlib.sha256() if h.artifact.digest else None
    if chosen.root.transport == "filesystem":
        volumes = parse_volumes(config.file_exchange_volumes)
        src_path = resolve_exchange_uri(chosen.root.uri, volumes=volumes)
        await _stream_path_to_dest(src_path, dest=dest, digester=digester)
    else:  # download
        ssrf = SSRFGuardConfig(
            allow_loopback=config.file_exchange_https_allow_loopback,
            allow_private=config.file_exchange_https_allow_private,
        )
        await pull_download(chosen.root.url, dest=dest, ssrf=ssrf, digester=digester)
    if digester is not None and h.artifact.digest:
        # Per spec §7.1, artifact.digest is `<algorithm>:<lowercase-hex>`,
        # e.g. ``sha-256:9f86d0...``. We only support sha-256 in v0.1.
        algo, _, expected_hex = h.artifact.digest.partition(":")
        if algo == "sha-256":
            if digester.hexdigest() != expected_hex.lower():
                raise FileExchangeError(
                    code=FileExchangeErrorCode.DIGEST_MISMATCH,
                    detail=f"computed digest does not match artifact.digest for {h.artifact.id!r}",
                )
        else:
            raise FileExchangeError(
                code=FileExchangeErrorCode.DIGEST_MISMATCH,
                detail=f"artifact.digest algorithm {algo!r} is not supported (v0.1 supports sha-256 only)",
            )
    return h.artifact


async def _stream_path_to_dest(
    src: Path,
    *,
    dest: Path | BinaryIO,
    digester: Any | None,
    chunk_size: int = _CHUNK_SIZE,
) -> None:
    fh = await asyncio.to_thread(open, src, "rb")
    try:
        if isinstance(dest, Path):
            from ._transport_filesystem import async_atomic_write

            async with async_atomic_write(dest) as out:
                while True:
                    chunk = await asyncio.to_thread(fh.read, chunk_size)
                    if not chunk:
                        break
                    if digester is not None:
                        digester.update(chunk)
                    await asyncio.to_thread(out.write, chunk)
        else:
            while True:
                chunk = await asyncio.to_thread(fh.read, chunk_size)
                if not chunk:
                    break
                if digester is not None:
                    digester.update(chunk)
                await asyncio.to_thread(dest.write, chunk)
    finally:
        await asyncio.to_thread(fh.close)
```

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_receiver.py`:**

```python
"""Receiver role: build IntakeTickets and resolve artifact_ids to paths.

``open_intake`` is pure construction — the sink-side mint helpers in
``_url_store.py`` already recorded the intake mapping at mint time, so
``open_intake`` needs neither ``config`` nor ``kv_store``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from mcp.types import CallToolResult, TextContent

from ._errors import CAPABILITY_KEY
from ._provider import _auto_summary
from ._types import ArtifactMetadata, ExpectedConstraints, IntakeTicket, SinkDescriptor

# Re-export resolve_intake from _url_store so callers can ``from
# fastmcp_pvl_core import resolve_intake`` without importing from a
# private path.
from ._url_store import resolve_intake as resolve_intake  # noqa: F401


def open_intake(
    *,
    sinks: Sequence[SinkDescriptor],
    expected: ExpectedConstraints | None = None,
    artifact_id: str | None = None,
) -> IntakeTicket:
    aid = artifact_id or uuid.uuid4().hex
    return IntakeTicket(
        artifact_id=aid,
        sinks=tuple(sinks),
        expected=expected,
    )


def build_intake_response(
    ticket: IntakeTicket,
    *,
    summary: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> CallToolResult:
    fe_meta: dict[str, Any] = {"ticket": ticket.model_dump(by_alias=True, exclude_none=True)}
    if extra_meta:
        fe_meta.update(extra_meta)
    if summary is None:
        # No artifact metadata at intake time — synthesise from artifact_id.
        summary = f"file-exchange: awaiting upload for artifact_id={ticket.artifact_id}"
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        isError=False,
        _meta={CAPABILITY_KEY: fe_meta},
    )
```

- [ ] **Create `src/fastmcp_pvl_core/_file_exchange/_sender.py`:**

```python
"""Sender role: push artifact bytes to a sink chosen from an IntakeTicket."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO

from .._config import ServerConfig
from ._errors import FileExchangeError, FileExchangeErrorCode
from ._select import select_sink
from ._transport_filesystem import async_atomic_write, parse_volumes, resolve_exchange_uri
from ._transport_https import SSRFGuardConfig, push_upload
from ._types import FileExchangeTransport, IntakeTicket

_CHUNK_SIZE = 64 * 1024


def _default_supported(config: ServerConfig) -> tuple[FileExchangeTransport, ...]:
    out: list[FileExchangeTransport] = []
    if parse_volumes(config.file_exchange_volumes):
        out.append("filesystem")
    out.append("upload")
    return tuple(out)


def _check_pre_send_constraints(ticket: IntakeTicket, size: int | None, mime: str | None) -> None:
    exp = ticket.expected
    if exp is None:
        return
    if exp.max_size is not None and size is not None and size > exp.max_size:
        raise FileExchangeError(
            code=FileExchangeErrorCode.SIZE_EXCEEDED,
            detail=f"artifact_size={size} exceeds expected.maxSize={exp.max_size}",
        )
    if exp.accept_mime_types and mime is not None and mime not in exp.accept_mime_types:
        raise FileExchangeError(
            code=FileExchangeErrorCode.MIME_REJECTED,
            detail=f"mime={mime!r} not in expected.acceptMimeTypes={list(exp.accept_mime_types)}",
        )


async def push_artifact(
    ticket: IntakeTicket | dict,
    *,
    source: Path | BinaryIO,
    config: ServerConfig,
    supported_transports: Sequence[FileExchangeTransport] | None = None,
    artifact_digest: str | None = None,
    artifact_mime: str | None = None,
    artifact_size: int | None = None,
) -> None:
    t = ticket if isinstance(ticket, IntakeTicket) else IntakeTicket.model_validate(ticket)
    _check_pre_send_constraints(t, artifact_size, artifact_mime)
    if t.expected and t.expected.require_digest and not artifact_digest:
        raise FileExchangeError(
            code=FileExchangeErrorCode.DIGEST_MISMATCH,
            detail="ticket.expected.requireDigest is set but artifact_digest kwarg was not provided",
        )
    supported = tuple(supported_transports) if supported_transports else _default_supported(config)
    chosen = select_sink(t.sinks, supported=supported)
    if chosen.root.transport == "filesystem":
        volumes = parse_volumes(config.file_exchange_volumes)
        # mint-time mapping was recorded in resolve_path; resolve again
        # here to write to that path (the path may not exist yet, but
        # the volume confinement check still applies).
        target = _resolve_filesystem_target(chosen.root.uri, volumes=volumes)
        await _stream_source_to_path(source, target=target)
    else:  # upload
        ssrf = SSRFGuardConfig(
            allow_loopback=config.file_exchange_https_allow_loopback,
            allow_private=config.file_exchange_https_allow_private,
        )
        await push_upload(
            chosen.root.url,
            source=source,
            ssrf=ssrf,
            content_type=artifact_mime or "application/octet-stream",
            content_digest=artifact_digest,
            content_length=artifact_size,
        )


def _resolve_filesystem_target(uri: str, *, volumes: dict[str, Path]) -> Path:
    """Like resolve_exchange_uri but tolerates non-existent target files
    (the sender is creating them). Volume + traversal checks still apply."""
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(uri)
    if parts.scheme != "exchange":
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"sender expected exchange:// scheme; got {parts.scheme!r}",
        )
    root = volumes.get(parts.netloc)
    if root is None:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"file-exchange volume {parts.netloc!r} is not configured on this party",
        )
    relative = unquote(parts.path).lstrip("/")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FileExchangeError(
            code=FileExchangeErrorCode.NOT_ACCESSIBLE,
            detail=f"path {uri!r} resolves outside volume {parts.netloc!r}",
        ) from exc
    return candidate


async def _stream_source_to_path(
    source: Path | BinaryIO,
    *,
    target: Path,
    chunk_size: int = _CHUNK_SIZE,
) -> None:
    async with async_atomic_write(target) as out:
        if isinstance(source, Path):
            fh = await asyncio.to_thread(open, source, "rb")
            try:
                while True:
                    chunk = await asyncio.to_thread(fh.read, chunk_size)
                    if not chunk:
                        break
                    await asyncio.to_thread(out.write, chunk)
            finally:
                await asyncio.to_thread(fh.close)
        else:
            while True:
                chunk = await asyncio.to_thread(source.read, chunk_size)
                if not chunk:
                    break
                await asyncio.to_thread(out.write, chunk)
```

- [ ] **Run to verify role-helper tests pass:**

```bash
uv run pytest tests/file_exchange/test_roles.py -v
```

Expected: 8 passing.

### Step F.5 — Wire up `_file_exchange/__init__.py`

- [ ] **Replace** the placeholder in `src/fastmcp_pvl_core/_file_exchange/__init__.py`:

```python
"""File-exchange v0.1 — public-surface assembly.

Consumers depend on the top-level ``fastmcp_pvl_core`` re-exports, not
this layout. This module exists to bundle the symbols for the parent
package's re-export block.
"""

from ._capability import register_file_exchange_capability
from ._errors import (
    FileExchangeError,
    FileExchangeErrorCode,
    as_tool_error_result,
)
from ._fetcher import pull_artifact
from ._provider import build_pull_response
from ._receiver import (
    build_intake_response,
    open_intake,
    resolve_intake,
)
from ._sender import push_artifact
from ._transport_filesystem import make_filesystem_source
from ._types import (
    ArtifactMetadata,
    ExpectedConstraints,
    FileExchangeRole,
    FileExchangeTransport,
    IntakeTicket,
    SinkDescriptor,
    SourceDescriptor,
    TransferHandle,
)
from ._url_store import (
    make_filesystem_sink,
    mint_download_source,
    mint_upload_sink,
)

__all__ = [
    "ArtifactMetadata",
    "ExpectedConstraints",
    "FileExchangeError",
    "FileExchangeErrorCode",
    "FileExchangeRole",
    "FileExchangeTransport",
    "IntakeTicket",
    "SinkDescriptor",
    "SourceDescriptor",
    "TransferHandle",
    "as_tool_error_result",
    "build_intake_response",
    "build_pull_response",
    "make_filesystem_sink",
    "make_filesystem_source",
    "mint_download_source",
    "mint_upload_sink",
    "open_intake",
    "pull_artifact",
    "push_artifact",
    "register_file_exchange_capability",
    "resolve_intake",
]
```

### Step F.6 — Re-export from top-level `fastmcp_pvl_core/__init__.py`

- [ ] **Modify `src/fastmcp_pvl_core/__init__.py`**: after the existing `from fastmcp_pvl_core._subject import get_subject` line, add:

```python
from fastmcp_pvl_core._file_exchange import (
    ArtifactMetadata,
    ExpectedConstraints,
    FileExchangeError,
    FileExchangeErrorCode,
    FileExchangeRole,
    FileExchangeTransport,
    IntakeTicket,
    SinkDescriptor,
    SourceDescriptor,
    TransferHandle,
    as_tool_error_result,
    build_intake_response,
    build_pull_response,
    make_filesystem_sink,
    make_filesystem_source,
    mint_download_source,
    mint_upload_sink,
    open_intake,
    pull_artifact,
    push_artifact,
    register_file_exchange_capability,
    resolve_intake,
)
```

- [ ] **Update `__all__`** to include all 22 new exports (sorted alphabetically, maintaining the existing convention).

- [ ] **Smoke-test the top-level surface:**

```bash
uv run python -c "
from fastmcp_pvl_core import (
    ArtifactMetadata, ExpectedConstraints, FileExchangeError,
    FileExchangeErrorCode, FileExchangeRole, FileExchangeTransport,
    IntakeTicket, SinkDescriptor, SourceDescriptor, TransferHandle,
    as_tool_error_result, build_intake_response, build_pull_response,
    make_filesystem_sink, make_filesystem_source, mint_download_source,
    mint_upload_sink, open_intake, pull_artifact, push_artifact,
    register_file_exchange_capability, resolve_intake,
)
print('all 22 file-exchange symbols import cleanly from fastmcp_pvl_core')
"
```

Expected: prints the success line.

### Step F.7 — Commit Task F

- [ ] **Commit:**

```bash
git add src/fastmcp_pvl_core/__init__.py \
        src/fastmcp_pvl_core/_file_exchange/__init__.py \
        src/fastmcp_pvl_core/_file_exchange/_capability.py \
        src/fastmcp_pvl_core/_file_exchange/_provider.py \
        src/fastmcp_pvl_core/_file_exchange/_fetcher.py \
        src/fastmcp_pvl_core/_file_exchange/_receiver.py \
        src/fastmcp_pvl_core/_file_exchange/_sender.py \
        tests/file_exchange/test_capability.py \
        tests/file_exchange/test_roles.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): capability declaration + role helpers + public API

register_file_exchange_capability(server, config, *, roles=…, kv_store)
applies the spec §4.2 gating matrix, writes the capability block, and
auto-mounts /file-exchange/d|u/{token} via server.custom_route(...) iff
HTTPS producer roles survive the gate. Re-registration is idempotent
(clears stale advert from a prior call). digests=("sha-256",) is the
family-default constant; maxArtifactSize comes from config and is only
emitted when set — neither is a kwarg.

Adds the four role helpers: build_pull_response (provider),
pull_artifact (fetcher), open_intake + build_intake_response +
resolve_intake (receiver), push_artifact (sender). pull_artifact and
push_artifact take config and derive volumes + SSRFGuardConfig from it;
no leaking of operator concerns through kwargs.

Re-exports 22 symbols at the top level of fastmcp_pvl_core.

Closes #129.
EOF
)"
```

- [ ] Run the universal per-task suffix.

---

## Task G: Integration tests + spec-repo conformance fixtures (closes #130)

**Depends on:** Task F merged.

**Files:**
- Create: `tests/file_exchange/test_integration_filesystem.py`
- Create: `tests/file_exchange/test_integration_https.py`
- Create: `tests/file_exchange/test_integration_two_servers.py`

### Step G.1 — Failing test for the full filesystem round-trip in-process

- [ ] **Write `tests/file_exchange/test_integration_filesystem.py`:**

```python
"""End-to-end filesystem round-trip: provider → fetcher and
receiver ← sender, both in-process, sharing a tmp volume."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore

from fastmcp_pvl_core import (
    ArtifactMetadata,
    ExpectedConstraints,
    ServerConfig,
    build_intake_response,
    build_pull_response,
    make_filesystem_sink,
    make_filesystem_source,
    open_intake,
    pull_artifact,
    push_artifact,
    register_file_exchange_capability,
    resolve_intake,
)


@pytest.fixture
def shared_volume(tmp_path: Path) -> Path:
    vol = tmp_path / "exchange"
    vol.mkdir()
    return vol


@pytest.fixture
def provider_server(shared_volume: Path):
    mcp = FastMCP("provider")
    cfg = ServerConfig(
        transport="stdio",
        file_exchange_volumes=f"shared={shared_volume}",
    )
    store = MemoryStore()
    register_file_exchange_capability(mcp, cfg, kv_store=store)
    return mcp, cfg, store


@pytest.fixture
def fetcher_server(shared_volume: Path):
    mcp = FastMCP("fetcher")
    cfg = ServerConfig(
        transport="stdio",
        file_exchange_volumes=f"shared={shared_volume}",
    )
    store = MemoryStore()
    register_file_exchange_capability(mcp, cfg, kv_store=store)
    return mcp, cfg, store


@pytest.mark.asyncio
async def test_provider_to_fetcher_filesystem_round_trip(
    provider_server, fetcher_server, shared_volume: Path, tmp_path: Path
) -> None:
    p_mcp, p_cfg, _ = provider_server
    f_mcp, f_cfg, _ = fetcher_server
    # Provider lays bytes
    src = shared_volume / "report.md"
    src.write_bytes(b"# hi\n")
    # Provider mints source
    source = make_filesystem_source("shared", "report.md", config=p_cfg)
    artifact = ArtifactMetadata.model_validate({"id": "r1", "name": "report.md", "size": 5, "mimeType": "text/markdown"})
    result = build_pull_response(artifact, sources=[source])
    handle_json = (result.meta or {})["nl.liesdonk.file-exchange"]["handle"]
    # Fetcher consumes
    out = tmp_path / "fetched.bin"
    fetched = await pull_artifact(handle_json, dest=out, config=f_cfg)
    assert fetched.id == "r1"
    assert out.read_bytes() == b"# hi\n"


@pytest.mark.asyncio
async def test_sender_to_receiver_filesystem_round_trip_with_resolve(
    shared_volume: Path, tmp_path: Path
) -> None:
    # Receiver side
    r_mcp = FastMCP("receiver")
    r_cfg = ServerConfig(transport="stdio", file_exchange_volumes=f"shared={shared_volume}")
    r_store = MemoryStore()
    register_file_exchange_capability(r_mcp, r_cfg, kv_store=r_store)
    sink = await make_filesystem_sink(
        "shared", "incoming.bin", artifact_id="r1", config=r_cfg, kv_store=r_store,
    )
    ticket = open_intake(sinks=[sink], artifact_id="r1", expected=ExpectedConstraints(max_size=1024))
    ticket_dict = (build_intake_response(ticket)._meta or {})["nl.liesdonk.file-exchange"]["ticket"]
    # Sender side
    s_cfg = ServerConfig(transport="stdio", file_exchange_volumes=f"shared={shared_volume}")
    payload = tmp_path / "src.bin"
    payload.write_bytes(b"\x00\x01\x02" * 50)
    await push_artifact(ticket_dict, source=payload, config=s_cfg, artifact_size=150)
    # Receiver-side resolve_intake now finds the bytes
    landed = await resolve_intake("r1", kv_store=r_store)
    assert landed.read_bytes() == b"\x00\x01\x02" * 50
```

### Step G.2 — Failing test for HTTPS round-trip via the sibling routes

- [ ] **Write `tests/file_exchange/test_integration_https.py`:**

```python
"""End-to-end HTTPS round-trip: capability URL minted on the producer,
consumed via Starlette TestClient. Single-use enforcement is asserted
under concurrent consumers."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from fastmcp_pvl_core import (
    ExpectedConstraints,
    ServerConfig,
    mint_download_source,
    mint_upload_sink,
    register_file_exchange_capability,
)
from fastmcp_pvl_core._file_exchange._routes import build_file_exchange_router


@pytest.fixture
def producer_cfg() -> ServerConfig:
    return ServerConfig(
        transport="http",
        host="127.0.0.1",
        port=8000,
        base_url="https://example.com",
        file_exchange_https_public_base_url="https://example.com",
        file_exchange_capability_url_ttl_default_s=600,
    )


@pytest.fixture
def producer_mcp() -> FastMCP:
    return FastMCP("producer")


@pytest.fixture
def producer(producer_mcp, producer_cfg):
    store = MemoryStore()
    register_file_exchange_capability(producer_mcp, producer_cfg, kv_store=store)
    # Build a Starlette app whose routes mirror what FastMCP mounts —
    # we test the route logic in isolation here; Task F's test already
    # asserts FastMCP actually mounts them.
    download, upload = build_file_exchange_router(store=store)
    app = Starlette()
    app.add_route("/file-exchange/d/{token}", download, methods=["GET"])
    app.add_route("/file-exchange/u/{token}", upload, methods=["PUT", "POST"])
    return producer_mcp, producer_cfg, store, app


@pytest.mark.asyncio
async def test_download_via_sibling_route(producer, tmp_path: Path) -> None:
    mcp, cfg, store, app = producer
    payload = b"hello-https"
    src_path = tmp_path / "src.bin"
    src_path.write_bytes(payload)
    src = await mint_download_source(
        bytes_path=src_path, server=mcp, config=cfg, kv_store=store, content_type="text/plain",
    )
    with TestClient(app) as client:
        r = client.get(src.root.url.replace("https://example.com", ""))
        assert r.status_code == 200
        assert r.content == payload


@pytest.mark.asyncio
async def test_upload_via_sibling_route_lands_at_intake_path(
    producer, tmp_path: Path
) -> None:
    mcp, cfg, store, app = producer
    intake = tmp_path / "intake.bin"
    sink = await mint_upload_sink(
        intake_path=intake, artifact_id="a1", server=mcp, config=cfg, kv_store=store,
        expected=ExpectedConstraints(accept_mime_types=("application/octet-stream",), max_size=4096),
    )
    body = b"\x01" * 1000
    with TestClient(app) as client:
        r = client.put(
            sink.root.url.replace("https://example.com", ""),
            data=body,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 204
        assert intake.read_bytes() == body
```

### Step G.3 — Failing test for two in-process FastMCP servers exchanging an artifact

- [ ] **Write `tests/file_exchange/test_integration_two_servers.py`:**

```python
"""Two FastMCP servers in-process: provider exports via filesystem and
HTTPS, fetcher consumes via filesystem (preferred). Asserts the
selection algorithm picks filesystem when both are offered."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore

from fastmcp_pvl_core import (
    ArtifactMetadata,
    ServerConfig,
    build_pull_response,
    make_filesystem_source,
    mint_download_source,
    pull_artifact,
    register_file_exchange_capability,
)


@pytest.mark.asyncio
async def test_fetcher_prefers_filesystem_when_provider_offers_both(
    tmp_path: Path,
) -> None:
    vol = tmp_path / "exch"
    vol.mkdir()
    p_mcp = FastMCP("p")
    p_cfg = ServerConfig(
        transport="http",
        base_url="https://example.com",
        file_exchange_volumes=f"shared={vol}",
        file_exchange_https_public_base_url="https://example.com",
    )
    p_store = MemoryStore()
    register_file_exchange_capability(p_mcp, p_cfg, kv_store=p_store)
    # Provider lays bytes and mints BOTH source descriptors
    payload = b"both transports" * 100
    src_file = vol / "note.md"
    src_file.write_bytes(payload)
    fs_src = make_filesystem_source("shared", "note.md", config=p_cfg)
    dl_src = await mint_download_source(
        bytes_path=src_file, server=p_mcp, config=p_cfg, kv_store=p_store,
    )
    artifact = ArtifactMetadata.model_validate(
        {"id": "n1", "name": "note.md", "size": len(payload), "mimeType": "text/markdown"}
    )
    result = build_pull_response(artifact, sources=[dl_src, fs_src])   # download first; selection picks filesystem
    handle_json = (result.meta or {})["nl.liesdonk.file-exchange"]["handle"]
    # Fetcher: configured for filesystem
    f_cfg = ServerConfig(transport="stdio", file_exchange_volumes=f"shared={vol}")
    out = tmp_path / "out.bin"
    await pull_artifact(handle_json, dest=out, config=f_cfg)
    assert out.read_bytes() == payload
    # The download token must NOT have been consumed (filesystem was used)
    download_token = dl_src.root.url.rsplit("/", 1)[-1]
    assert await p_store.get(collection="tokens", key=download_token) is not None
```

- [ ] **Run all integration tests to verify they fail meaningfully** (most should pass once F is in, since they only exercise the public surface):

```bash
uv run pytest tests/file_exchange/test_integration_*.py -v
```

If any failure surfaces a real bug in F (rather than a missing fixture), STOP and fix in F's module before adding a workaround in G's tests.

### Step G.4 — Make integration tests pass

- [ ] **Address** any meaningful failure from G.3 by fixing the relevant module (F-level code). Each fix is a focused commit with the corresponding test asserting the corrected behaviour. Do not paper over real bugs in test setup.

### Step G.5 — Commit Task G

- [ ] **Commit:**

```bash
git add tests/file_exchange/test_integration_*.py
git commit -m "$(cat <<'EOF'
test(file-exchange): integration suite + selection-preference assertions

Adds three integration tests:
- filesystem round-trip (provider→fetcher and sender→receiver) with
  resolve_intake on the receiver side.
- HTTPS round-trip (download + upload) via the sibling routes mounted
  on a Starlette test app.
- Selection-preference assertion: when the provider offers BOTH
  filesystem and download, a filesystem-capable fetcher chooses
  filesystem and the download token stays un-consumed.

Closes #130.
EOF
)"
```

- [ ] Run the universal per-task suffix.

---

## Cross-task verification (after all seven PRs merge)

- [ ] **Run the full test suite** on `main`:

```bash
uv sync --all-extras
uv run pytest tests/ -v
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: all green on Python 3.10, 3.11, 3.12, 3.13 (CI matrix).

- [ ] **Verify the 22 top-level symbols are importable**:

```bash
uv run python -c "
import fastmcp_pvl_core
required = {
    'ArtifactMetadata', 'ExpectedConstraints', 'FileExchangeError',
    'FileExchangeErrorCode', 'FileExchangeRole', 'FileExchangeTransport',
    'IntakeTicket', 'SinkDescriptor', 'SourceDescriptor', 'TransferHandle',
    'as_tool_error_result', 'build_intake_response', 'build_pull_response',
    'make_filesystem_sink', 'make_filesystem_source', 'mint_download_source',
    'mint_upload_sink', 'open_intake', 'pull_artifact', 'push_artifact',
    'register_file_exchange_capability', 'resolve_intake',
}
missing = required - set(fastmcp_pvl_core.__all__)
assert not missing, f'missing from __all__: {missing}'
print('all 22 file-exchange symbols present in fastmcp_pvl_core.__all__')
"
```

- [ ] **Smoke test the capability gating matrix one more time** against the design §3 table — pick four `ServerConfig` shapes (stdio+nv, stdio+v, http+nv, http+v) plus the empty-post-gate case, register the capability against each, and assert the advertised `roles` map matches the table verbatim. If anything diverges, the bug is in `_capability._transports_for_role` or its caller — fix in pvl-core, not in a downstream shim.

---

## Self-review notes

This plan was self-reviewed after writing, applying the writing-plans skill's three-step checklist:

1. **Placeholder scan:** no "TBD", "implement appropriate", "similar to Task N" tokens. Every step shows the actual code or command.
2. **Type consistency:** all function signatures match across tasks. `pull_artifact` / `push_artifact` consistently take `config: ServerConfig`; the four mint helpers' async/sync split matches the design doc (only the sink-writing ones are async); `FileExchangeRole` / `FileExchangeTransport` literals are used everywhere a role or transport set is named.
3. **Spec coverage:**
   - §3 Public API surface → Tasks A (types), C (`make_filesystem_source`), E (`make_filesystem_sink`, `mint_download_source`, `mint_upload_sink`, `resolve_intake`), F (`register_file_exchange_capability`, four role helpers, `as_tool_error_result`).
   - §3 transport-availability gating table → Task F's eight matrix tests + production logic in `_capability._transports_for_role`.
   - §4 Internal module layout → mapped 1:1 to file paths in the task `Files:` blocks.
   - §5 Operator configuration → Task C step C.1 adds the six `ServerConfig` fields with env-var loaders.
   - §6.1 Filesystem transport → Task C (`resolve_exchange_uri`, `atomic_write`, `async_atomic_write`).
   - §6.2 HTTPS download/upload → Task D (consumer) + Task E (producer-side token store + routes).
   - §6.2 streaming + asyncio.to_thread + fail-fast 413 → Task E step E.4 `_upload` handler + Task E test `test_upload_fails_fast_on_size_exceeded_with_413`.
   - §6.2 single-use concurrency via atomic delete → Task E step E.2 `consume_download_token` + test `test_consume_download_token_single_use_atomic_under_concurrency`.
   - §6.2 logging discipline → Task E test `test_mint_and_consume_never_logs_full_token`.
   - §7 Schema vendoring + drift gate → Task A step A.2 + steps A.1/A.6.
   - §8 Testing strategy → Tasks A–F unit tests + Task G integration tests.
