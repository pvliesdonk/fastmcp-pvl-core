# File-Exchange #140 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the §9 descriptor-selection algorithm and §13 error-envelope helper as three focused private modules (`_codes.py`, `_selection.py`, `_errors.py`) plus their public namespace re-exports, completing the second EPIC #138 child.

**Architecture:** Three private modules under `src/fastmcp_pvl_core/_file_exchange/`, each one focused concern. `_codes.py` ships the `TransferErrorCode` enum + `KNOWN_CODES` frozenset. `_errors.py` builds the `_meta`-bearing `CallToolResult` (depends on `_codes.py` for default text). `_selection.py` provides typed `select_source`/`select_sink` returning `Optional[TransferSource]`/`Optional[TransferSink]` (independent of codes/errors). Public namespace at `src/fastmcp_pvl_core/file_exchange.py` adds explicit re-exports.

**Tech Stack:** Python 3.10+ (CI matrix is 3.10-3.13, `requires-python = ">=3.10"` in pyproject); Pydantic v2 (already a dep); `mcp.types.CallToolResult`/`TextContent` (already transitively from fastmcp); pytest, ruff, mypy (already in the dev group); no new third-party dependencies.

**Branch:** `feat/140-selection-error-envelope` (already created from `main` at commit `509af42`; design doc commit `2ffb633` already lands the spec).

**Spec:** `docs/superpowers/specs/2026-05-21-file-exchange-140-selection-error-design.md`.

---

### Pre-flight context (read once before starting Task 1)

Run these to set up your mental model — none of them modify state:

```bash
git status                       # confirm on feat/140-selection-error-envelope
git log --oneline -3              # confirm 2ffb633 (design doc) is the tip
uv sync --all-extras              # match CI's dependency state
uv run pytest -q                  # 603 passed should be the baseline; confirm
```

Files this plan depends on but does not modify (read enough to use the types correctly):

- `src/fastmcp_pvl_core/_file_exchange/_wire.py` lines 134-168 — `FilesystemSource`, `DownloadSource`, `FilesystemSink`, `UploadSink` field shapes. `expiresAt` is `AwareDatetime` on `DownloadSource` and `UploadSink`.
- `src/fastmcp_pvl_core/_file_exchange/_wire.py` lines 273-336 — `TransferHandle.sources` is `list[TransferSource]`, `IntakeTicket.sinks` is `list[TransferSink]`. `TransferSource`/`TransferSink` are discriminated unions whose members include `UnknownTransportDescriptor` (forward-compat fallthrough).
- `mcp.types` — `CallToolResult` has `meta: dict | None = None` with JSON alias `_meta`. **Constructor `CallToolResult(meta=...)` does NOT set `meta`** because the model lacks `populate_by_name=True`. Set `meta` after construction (`result.meta = {...}`) — verified in the spec brainstorm session.

---

### Task 1: `TransferErrorCode` enum + `KNOWN_CODES` frozenset

**Why:** Foundation. Task 2 (`_errors.py`) consumes the enum to render default text per code. No dependency on selection.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_codes.py`
- Create: `tests/_file_exchange/test_codes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/_file_exchange/test_codes.py`:

```python
"""Tests for the TransferErrorCode enum + KNOWN_CODES frozenset."""

from __future__ import annotations

from fastmcp_pvl_core._file_exchange._codes import (
    KNOWN_CODES,
    TransferErrorCode,
)


def test_member_values_match_spec_table():
    """§13 defines exactly these strings; the enum mirrors them verbatim."""
    assert TransferErrorCode.NO_SUPPORTED_TRANSPORT.value == "no-supported-transport"
    assert TransferErrorCode.DESCRIPTOR_EXPIRED.value == "descriptor-expired"
    assert TransferErrorCode.NOT_ACCESSIBLE.value == "not-accessible"
    assert TransferErrorCode.DIGEST_MISMATCH.value == "digest-mismatch"
    assert TransferErrorCode.SIZE_MISMATCH.value == "size-mismatch"
    assert TransferErrorCode.TOO_LARGE.value == "too-large"
    assert TransferErrorCode.MIME_TYPE_REJECTED.value == "mime-type-rejected"
    assert TransferErrorCode.UNSUPPORTED_REQUIREMENT.value == "unsupported-requirement"
    assert TransferErrorCode.TRANSFER_FAILED.value == "transfer-failed"


def test_known_codes_is_exactly_nine_spec_strings():
    """Drift guard: KNOWN_CODES must equal the spec's 9 strings, no more no less."""
    expected = frozenset({
        "no-supported-transport",
        "descriptor-expired",
        "not-accessible",
        "digest-mismatch",
        "size-mismatch",
        "too-large",
        "mime-type-rejected",
        "unsupported-requirement",
        "transfer-failed",
    })
    assert KNOWN_CODES == expected


def test_known_codes_covers_every_enum_member():
    """Every TransferErrorCode member must be in KNOWN_CODES (no enum/set drift)."""
    for member in TransferErrorCode:
        assert member.value in KNOWN_CODES, f"missing {member.value}"


def test_str_mixin_equality():
    """``(str, Enum)`` mixin: enum member compares equal to its str value."""
    assert TransferErrorCode.DIGEST_MISMATCH == "digest-mismatch"


def test_membership_test_works_for_known_and_rejects_typo():
    assert TransferErrorCode.DIGEST_MISMATCH in KNOWN_CODES
    assert "digestmismatch" not in KNOWN_CODES  # typo


def test_known_codes_is_frozenset():
    """Type matters: callers may use it as a dict key set."""
    assert isinstance(KNOWN_CODES, frozenset)
```

- [ ] **Step 2: Run tests to verify they fail with ImportError**

```bash
uv run pytest tests/_file_exchange/test_codes.py -q
```

Expected: `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._codes'`

- [ ] **Step 3: Implement `_codes.py`**

Create `src/fastmcp_pvl_core/_file_exchange/_codes.py`:

```python
"""§13 error codes for the file-exchange extension.

Ships the spec-defined codes as a single ``(str, Enum)`` mixin so each
member is both an enum (for autocompletion and grep-ability at call
sites) and a real ``str`` (for use anywhere a plain string is expected).

The code set is OPEN per §13: callers MAY pass arbitrary strings to
:func:`fastmcp_pvl_core.file_exchange.build_file_exchange_error`. This
module names the spec-defined values only.
"""

from __future__ import annotations

from enum import Enum


class TransferErrorCode(str, Enum):
    """The 9 error codes defined in §13 of the wire spec.

    Members are usable anywhere a ``str`` is expected (``str, Enum``
    mixin); ``TransferErrorCode.DIGEST_MISMATCH == "digest-mismatch"``.

    Stdlib :class:`enum.StrEnum` would be the natural choice on Python
    3.11+, but ``requires-python = ">=3.10"`` rules it out — the mixin
    form is the back-compat-safe equivalent.
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


KNOWN_CODES: frozenset[str] = frozenset(c.value for c in TransferErrorCode)
"""The 9 spec-defined codes as a frozenset for membership testing.

Derived from :class:`TransferErrorCode` at module load — single source
of truth. Callers SHOULD treat any code NOT in ``KNOWN_CODES`` as a
generic failure per §13's open-code-set rule.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/_file_exchange/test_codes.py -q
```

Expected: `6 passed`.

- [ ] **Step 5: Run format/lint/mypy across the repo**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
```

Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_codes.py \
        tests/_file_exchange/test_codes.py
git commit -m "$(cat <<'EOF'
feat(_file_exchange): TransferErrorCode enum + KNOWN_CODES frozenset

Adds the §13 spec-defined error codes as a ``(str, Enum)`` mixin (for
3.10 compat — stdlib StrEnum lands at 3.11) plus a frozenset over the
9 values for membership tests. The code set itself stays open per spec;
this enum just names the spec-defined values for autocompletion and
grep-ability.

Foundation for #140's error envelope; consumed by ``_errors.py`` for
default text rendering.

Refs: #140.
EOF
)"
```

---

### Task 2: `build_file_exchange_error` — error envelope CallToolResult builder

**Why:** Every role helper (#143/#145/#146) will produce a §13 failed `CallToolResult` via this helper. Depends on Task 1 for default text lookup.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_errors.py`
- Create: `tests/_file_exchange/test_errors.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/_file_exchange/test_errors.py`:

```python
"""Tests for build_file_exchange_error — the §13 envelope builder."""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from fastmcp_pvl_core._file_exchange._codes import (
    KNOWN_CODES,
    TransferErrorCode,
)
from fastmcp_pvl_core._file_exchange._errors import (
    _DEFAULT_TEXT,
    build_file_exchange_error,
)

_NAMESPACE_KEY = "nl.liesdonk.file-exchange/error"


def test_returns_call_tool_result_with_is_error_true():
    result = build_file_exchange_error(TransferErrorCode.NO_SUPPORTED_TRANSPORT)
    assert isinstance(result, CallToolResult)
    assert result.isError is True


def test_meta_carries_namespaced_envelope_with_code():
    result = build_file_exchange_error(TransferErrorCode.NO_SUPPORTED_TRANSPORT)
    assert result.meta == {
        _NAMESPACE_KEY: {"code": "no-supported-transport"},
    }


def test_meta_omits_transport_and_detail_when_none():
    """Absent fields are absent — no JSON nulls in the envelope."""
    result = build_file_exchange_error(TransferErrorCode.TRANSFER_FAILED)
    inner = result.meta[_NAMESPACE_KEY]
    assert "transport" not in inner
    assert "detail" not in inner


def test_content_is_single_text_block_with_default_text():
    result = build_file_exchange_error(TransferErrorCode.NO_SUPPORTED_TRANSPORT)
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.type == "text"
    assert block.text == _DEFAULT_TEXT[TransferErrorCode.NO_SUPPORTED_TRANSPORT]


def test_transport_kwarg_populates_meta_and_appends_to_text():
    """When transport is supplied, default text gets a ``(transport: X)`` suffix."""
    result = build_file_exchange_error(
        TransferErrorCode.NOT_ACCESSIBLE,
        transport="download",
    )
    assert result.meta[_NAMESPACE_KEY]["transport"] == "download"
    expected_text = (
        _DEFAULT_TEXT[TransferErrorCode.NOT_ACCESSIBLE] + " (transport: download)"
    )
    assert result.content[0].text == expected_text


def test_detail_goes_into_meta_but_not_into_text():
    """Log-leak guard: ``detail`` is structured data, never auto-rendered."""
    detail_str = "expected sha-256:9f..., got sha-256:1b..."
    result = build_file_exchange_error(
        TransferErrorCode.DIGEST_MISMATCH,
        detail=detail_str,
    )
    assert result.meta[_NAMESPACE_KEY]["detail"] == detail_str
    assert detail_str not in result.content[0].text


def test_explicit_text_overrides_default_and_skips_transport_suffix():
    """When ``text`` is given, ``_DEFAULT_TEXT`` is bypassed entirely."""
    result = build_file_exchange_error(
        TransferErrorCode.NOT_ACCESSIBLE,
        transport="filesystem",
        text="Custom operator-friendly message.",
    )
    assert result.content[0].text == "Custom operator-friendly message."
    # transport still goes into meta though
    assert result.meta[_NAMESPACE_KEY]["transport"] == "filesystem"


def test_unknown_code_renders_generic_text_and_passes_through():
    """§13: consumers SHOULD treat unrecognized codes as generic failures."""
    result = build_file_exchange_error("future-spec-code")
    assert result.meta[_NAMESPACE_KEY]["code"] == "future-spec-code"
    assert result.content[0].text == "File transfer failed: future-spec-code"


def test_every_known_code_has_a_default_text_mapping():
    """Drift guard: adding a TransferErrorCode without updating _DEFAULT_TEXT fails here."""
    for member in TransferErrorCode:
        assert member in _DEFAULT_TEXT, f"missing default text for {member}"


def test_code_accepts_enum_member_or_raw_string():
    """The signature is ``code: str | TransferErrorCode``."""
    from_enum = build_file_exchange_error(TransferErrorCode.DIGEST_MISMATCH)
    from_str = build_file_exchange_error("digest-mismatch")
    assert from_enum.meta[_NAMESPACE_KEY]["code"] == "digest-mismatch"
    assert from_str.meta[_NAMESPACE_KEY]["code"] == "digest-mismatch"


def test_known_codes_dont_render_generic_fallback_text():
    """Sanity: known codes never produce the ``File transfer failed: ...`` fallback."""
    for code in KNOWN_CODES:
        result = build_file_exchange_error(code)
        assert not result.content[0].text.startswith("File transfer failed: ")
```

- [ ] **Step 2: Run tests to verify they fail with ImportError**

```bash
uv run pytest tests/_file_exchange/test_errors.py -q
```

Expected: `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._errors'`

- [ ] **Step 3: Implement `_errors.py`**

Create `src/fastmcp_pvl_core/_file_exchange/_errors.py`:

```python
"""§13 error-envelope CallToolResult builder.

Single helper :func:`build_file_exchange_error` that returns a fully
formed ``CallToolResult`` with ``isError=True``, a human-readable
``TextContent`` block, and the spec-mandated ``_meta`` key
``"nl.liesdonk.file-exchange/error"`` carrying the structured
``{code, [transport], [detail]}`` payload.

The caller's tool function returns the resulting ``CallToolResult``
verbatim — fastmcp's ``tools/call`` handler passes it through with the
``isError`` flag and ``_meta`` intact.
"""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, TextContent

from fastmcp_pvl_core._file_exchange._codes import (
    KNOWN_CODES,
    TransferErrorCode,
)

_NAMESPACE_KEY = "nl.liesdonk.file-exchange/error"

# Default human-readable text per spec-defined code. Keyed by the enum
# member (which is also the str value via the mixin).
_DEFAULT_TEXT: dict[TransferErrorCode, str] = {
    TransferErrorCode.NO_SUPPORTED_TRANSPORT: (
        "No supported transport found in transfer reference."
    ),
    TransferErrorCode.DESCRIPTOR_EXPIRED: (
        "Selected transfer descriptor expired before transfer completed."
    ),
    TransferErrorCode.NOT_ACCESSIBLE: "Transfer location is not accessible.",
    TransferErrorCode.DIGEST_MISMATCH: (
        "Transferred bytes did not match the expected digest."
    ),
    TransferErrorCode.SIZE_MISMATCH: (
        "Transferred byte count did not match the expected size."
    ),
    TransferErrorCode.TOO_LARGE: "Artifact exceeded the declared size limit.",
    TransferErrorCode.MIME_TYPE_REJECTED: (
        "Artifact's media type was not in the receiver's accepted list."
    ),
    TransferErrorCode.UNSUPPORTED_REQUIREMENT: (
        "Transfer reference requires a feature this party does not implement."
    ),
    TransferErrorCode.TRANSFER_FAILED: "File transfer failed.",
}


def _render_text(
    code_str: str, transport: str | None, text: str | None
) -> str:
    """Pick the text block content.

    - ``text`` (caller-supplied): used verbatim, transport suffix NOT
      appended (caller already framed the message as they want).
    - Otherwise look up ``_DEFAULT_TEXT`` by code, append
      ``(transport: X)`` when ``transport`` is set.
    - Unknown code (not in ``KNOWN_CODES``): generic
      ``"File transfer failed: <code>"``.

    ``detail`` is never rendered into the text — log-leak guard. The
    structured ``detail`` field is for machine consumption via
    ``_meta``; operators who want it in the text pass ``text=``
    explicitly.
    """
    if text is not None:
        return text
    if code_str in KNOWN_CODES:
        default = _DEFAULT_TEXT[TransferErrorCode(code_str)]
    else:
        return f"File transfer failed: {code_str}"
    if transport is not None:
        return f"{default} (transport: {transport})"
    return default


def build_file_exchange_error(
    code: str | TransferErrorCode,
    *,
    transport: str | None = None,
    detail: str | None = None,
    text: str | None = None,
) -> CallToolResult:
    """Build the §13 tool-execution-error CallToolResult.

    Args:
        code: A spec-defined error code (any
            :class:`TransferErrorCode` member) or, for future-spec
            codes, a raw string. The literal string lands in
            ``_meta[..., "code"]``.
        transport: Optional transport name (``"filesystem"``,
            ``"download"``, ``"upload"``, etc.). When set, populates
            ``_meta[..., "transport"]`` and (if ``text`` is None)
            appends ``" (transport: X)"`` to the default text.
        detail: Optional structured detail string for machine
            consumers (e.g. ``"expected sha-256:9f..., got
            sha-256:1b..."``). Populates ``_meta[..., "detail"]``.
            **Never** appears in the human-readable text block — pass
            ``text=`` explicitly if you want it there.
        text: Optional caller-supplied text block content. Overrides
            the default text and suppresses the transport suffix.

    Returns:
        A ``CallToolResult`` with ``isError=True``, one
        ``TextContent`` block, and ``_meta`` carrying the namespaced
        envelope. Optional fields (``transport``, ``detail``) are
        OMITTED from ``_meta`` when None — no JSON nulls.
    """
    code_str = str(code) if isinstance(code, TransferErrorCode) else code

    envelope: dict[str, Any] = {"code": code_str}
    if transport is not None:
        envelope["transport"] = transport
    if detail is not None:
        envelope["detail"] = detail

    rendered = _render_text(code_str, transport, text)
    result = CallToolResult(
        content=[TextContent(type="text", text=rendered)],
        isError=True,
    )
    # ``CallToolResult.meta`` has JSON alias ``_meta``; the constructor
    # doesn't accept ``meta=`` by name because the model lacks
    # ``populate_by_name=True``. Set it after construction.
    result.meta = {_NAMESPACE_KEY: envelope}
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/_file_exchange/test_errors.py -q
```

Expected: `11 passed`.

- [ ] **Step 5: Run format/lint/mypy**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
```

Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_errors.py \
        tests/_file_exchange/test_errors.py
git commit -m "$(cat <<'EOF'
feat(_file_exchange): build_file_exchange_error envelope helper

Adds ``build_file_exchange_error(code, *, transport, detail, text)``
that returns a §13-conformant ``CallToolResult`` with ``isError=True``
and the structured envelope under
``_meta["nl.liesdonk.file-exchange/error"]``.

Default human-readable text is rendered per code from a private
``_DEFAULT_TEXT`` table; ``transport`` (when set) appends a
``(transport: X)`` suffix; ``detail`` is structured data only and is
deliberately never rendered into the text block (log-leak guard
matching PR #122's URL-redaction pattern). An unknown code passes
through the literal string in ``_meta`` and renders as ``"File
transfer failed: <code>"`` — §13's open-code-set rule applied to the
render side.

``code`` accepts either a :class:`TransferErrorCode` member or a raw
string so future-spec codes can be emitted without enum updates.

The constructor for ``mcp.types.CallToolResult`` does not accept
``meta=`` by name (no ``populate_by_name`` on the model), so the
helper sets ``result.meta`` after construction.

Refs: #140.
EOF
)"
```

---

### Task 3: `select_source` + `select_sink` — §9 descriptor selection

**Why:** The core deliverable. Every fetcher/sender role helper will call one of these. Independent of Tasks 1 and 2 (selection returns Optional, doesn't build error envelopes).

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_selection.py`
- Create: `tests/_file_exchange/test_selection.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/_file_exchange/test_selection.py`:

```python
"""Tests for select_source / select_sink — the §9 selection algorithm."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fastmcp_pvl_core._file_exchange._selection import (
    select_sink,
    select_source,
)
from fastmcp_pvl_core._file_exchange._wire import (
    DownloadSource,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    TransferHandle,
    UploadSink,
)

# Anchor time used across the tolerance tests so they don't drift with
# wall-clock-derived `now()` differences between assertions.
_NOW = datetime(2026, 5, 21, 19, 0, 0, tzinfo=timezone.utc)


def _handle(*sources: dict) -> TransferHandle:
    return TransferHandle.from_wire({
        "type": "nl.liesdonk.file-exchange/transfer-handle",
        "version": "0.1",
        "artifact": {"name": "x.bin"},
        "sources": list(sources),
    })


def _ticket(*sinks: dict) -> IntakeTicket:
    return IntakeTicket.from_wire({
        "type": "nl.liesdonk.file-exchange/intake-ticket",
        "version": "0.1",
        "artifactId": "art-1",
        "sinks": list(sinks),
    })


# --- select_source ---


def test_select_source_skips_unknown_transport_only():
    """A handle with only an unknown-transport source returns None."""
    handle = _handle({"transport": "future-thing", "url": "x://y"})
    assert select_source(handle) is None


def test_select_source_skips_expired_download_beyond_tolerance():
    """``expiresAt`` 60s in the past is well past the 30s tolerance."""
    expired = (_NOW - timedelta(seconds=60)).isoformat()
    handle = _handle({
        "transport": "download",
        "url": "https://x/y",
        "expiresAt": expired,
    })
    assert select_source(handle, now=_NOW) is None


def test_select_source_selects_download_within_tolerance():
    """``expiresAt`` 5s in the past is inside the 30s tolerance — selected."""
    almost_expired = (_NOW - timedelta(seconds=5)).isoformat()
    handle = _handle({
        "transport": "download",
        "url": "https://x/y",
        "expiresAt": almost_expired,
    })
    chosen = select_source(handle, now=_NOW)
    assert isinstance(chosen, DownloadSource)


def test_select_source_skips_download_just_past_tolerance():
    """``expiresAt`` 35s in the past is past the 30s tolerance."""
    past = (_NOW - timedelta(seconds=35)).isoformat()
    handle = _handle({
        "transport": "download",
        "url": "https://x/y",
        "expiresAt": past,
    })
    assert select_source(handle, now=_NOW) is None


def test_select_source_selects_future_download():
    """``expiresAt`` in the future — selected."""
    future = (_NOW + timedelta(minutes=5)).isoformat()
    handle = _handle({
        "transport": "download",
        "url": "https://x/y",
        "expiresAt": future,
    })
    assert isinstance(select_source(handle, now=_NOW), DownloadSource)


def test_select_source_skips_filesystem_when_callback_returns_false():
    handle = _handle({"transport": "filesystem", "uri": "exchange://v/a"})
    assert select_source(handle, is_accessible=lambda d: False) is None


def test_select_source_selects_filesystem_when_callback_returns_true():
    handle = _handle({"transport": "filesystem", "uri": "exchange://v/a"})
    chosen = select_source(handle, is_accessible=lambda d: True)
    assert isinstance(chosen, FilesystemSource)


def test_select_source_skips_all_filesystem_when_callback_is_none():
    """``is_accessible=None`` means party does not support filesystem at all."""
    handle = _handle({"transport": "filesystem", "uri": "exchange://v/a"})
    assert select_source(handle) is None


def test_select_source_returns_first_surviving_in_array_order():
    """§9: iterate in order, return the first descriptor that survives."""
    expired = (_NOW - timedelta(minutes=1)).isoformat()
    future = (_NOW + timedelta(minutes=5)).isoformat()
    handle = _handle(
        {"transport": "download", "url": "https://x/y", "expiresAt": expired},
        {"transport": "filesystem", "uri": "exchange://v/a"},
        {"transport": "download", "url": "https://x/z", "expiresAt": future},
    )
    chosen = select_source(
        handle, is_accessible=lambda d: True, now=_NOW
    )
    # The filesystem source is the first survivor, not the future download.
    assert isinstance(chosen, FilesystemSource)


def test_select_source_callback_receives_typed_filesystem_source():
    """The callback gets the typed descriptor, not just a URI."""
    seen: list[FilesystemSource] = []

    def cb(src: FilesystemSource) -> bool:
        seen.append(src)
        return True

    handle = _handle({"transport": "filesystem", "uri": "exchange://v/a"})
    select_source(handle, is_accessible=cb)
    assert len(seen) == 1
    assert isinstance(seen[0], FilesystemSource)
    assert seen[0].uri == "exchange://v/a"


def test_select_source_now_overrides_wall_clock():
    """The ``now`` parameter is the reference point for tolerance arithmetic."""
    # An "expired" descriptor relative to a fictitious far-future ``now``.
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    handle = _handle({
        "transport": "download",
        "url": "https://x/y",
        "expiresAt": _NOW.isoformat(),  # in 2026
    })
    # Relative to wall clock the descriptor may or may not be expired
    # right now; relative to ``far_future`` it's definitely past tolerance.
    assert select_source(handle, now=far_future) is None


def test_select_source_empty_when_no_descriptor_survives():
    """Mix of expired + filesystem-without-callback returns None."""
    expired = (_NOW - timedelta(minutes=1)).isoformat()
    handle = _handle(
        {"transport": "download", "url": "https://x/y", "expiresAt": expired},
        {"transport": "filesystem", "uri": "exchange://v/a"},
    )
    # is_accessible omitted → filesystem skipped; download expired → skipped.
    assert select_source(handle, now=_NOW) is None


# --- select_sink (symmetric) ---


def test_select_sink_skips_expired_upload_beyond_tolerance():
    expired = (_NOW - timedelta(seconds=60)).isoformat()
    ticket = _ticket({
        "transport": "upload",
        "url": "https://x/y",
        "expiresAt": expired,
    })
    assert select_sink(ticket, now=_NOW) is None


def test_select_sink_selects_upload_within_tolerance():
    almost_expired = (_NOW - timedelta(seconds=5)).isoformat()
    ticket = _ticket({
        "transport": "upload",
        "url": "https://x/y",
        "expiresAt": almost_expired,
    })
    chosen = select_sink(ticket, now=_NOW)
    assert isinstance(chosen, UploadSink)


def test_select_sink_skips_upload_past_tolerance():
    past = (_NOW - timedelta(seconds=35)).isoformat()
    ticket = _ticket({
        "transport": "upload",
        "url": "https://x/y",
        "expiresAt": past,
    })
    assert select_sink(ticket, now=_NOW) is None


def test_select_sink_selects_filesystem_when_callback_returns_true():
    ticket = _ticket({"transport": "filesystem", "uri": "exchange://v/in"})
    chosen = select_sink(ticket, is_accessible=lambda d: True)
    assert isinstance(chosen, FilesystemSink)


def test_select_sink_skips_filesystem_when_callback_is_none():
    ticket = _ticket({"transport": "filesystem", "uri": "exchange://v/in"})
    assert select_sink(ticket) is None


def test_select_sink_returns_first_surviving_in_array_order():
    expired = (_NOW - timedelta(minutes=1)).isoformat()
    future = (_NOW + timedelta(minutes=5)).isoformat()
    ticket = _ticket(
        {"transport": "upload", "url": "https://x/y", "expiresAt": expired},
        {"transport": "filesystem", "uri": "exchange://v/in"},
        {"transport": "upload", "url": "https://x/z", "expiresAt": future},
    )
    chosen = select_sink(
        ticket, is_accessible=lambda d: True, now=_NOW
    )
    assert isinstance(chosen, FilesystemSink)


def test_select_sink_callback_receives_typed_filesystem_sink():
    seen: list[FilesystemSink] = []

    def cb(s: FilesystemSink) -> bool:
        seen.append(s)
        return True

    ticket = _ticket({"transport": "filesystem", "uri": "exchange://v/in"})
    select_sink(ticket, is_accessible=cb)
    assert len(seen) == 1
    assert isinstance(seen[0], FilesystemSink)


# --- shared structural ---


@pytest.mark.parametrize(
    "selector_fn,reference_fn,unknown_descriptor",
    [
        (select_source, _handle, {"transport": "future-src", "url": "x://y"}),
        (select_sink, _ticket, {"transport": "future-sink", "url": "x://y"}),
    ],
)
def test_unknown_transport_always_skipped(
    selector_fn, reference_fn, unknown_descriptor
):
    """Forward-compat fallthrough: party never selects an unknown transport."""
    ref = reference_fn(unknown_descriptor)
    assert selector_fn(ref) is None
```

- [ ] **Step 2: Run tests to verify they fail with ImportError**

```bash
uv run pytest tests/_file_exchange/test_selection.py -q
```

Expected: `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._selection'`

- [ ] **Step 3: Implement `_selection.py`**

Create `src/fastmcp_pvl_core/_file_exchange/_selection.py`:

```python
"""§9 descriptor selection: pick the first survivable descriptor.

Two typed entry points, parallel in shape:

- :func:`select_source` returns the chosen :class:`TransferSource` from
  a :class:`TransferHandle`'s ``sources`` array, or ``None`` if none
  survive.
- :func:`select_sink` does the same for a :class:`TransferSink` on
  ``ticket.sinks``.

The §17.4 must-understand check is NOT re-run here — it has already
run inside :meth:`TransferHandle.from_wire` /
:meth:`IntakeTicket.from_wire`. Selection assumes the reference came
from one of those. Direct in-process construction enforces v0.1's
``requires``-must-be-empty rule at the Pydantic layer instead.

When selection returns ``None`` the caller is responsible for building
the §13 ``no-supported-transport`` error envelope via
:func:`fastmcp_pvl_core.file_exchange.build_file_exchange_error`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastmcp_pvl_core._file_exchange._wire import (
    DownloadSource,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    TransferHandle,
    TransferSink,
    TransferSource,
    UploadSink,
)

# §9 says ``a small tolerance (for example, 30 seconds)``. pvl-core
# picks 30s. Not a kwarg — shape decision per the framing principle.
# If a real operational need emerges, lift to an env var (operator
# config), never a per-call argument.
_EXPIRY_TOLERANCE = timedelta(seconds=30)


def select_source(
    handle: TransferHandle,
    *,
    is_accessible: Callable[[FilesystemSource], bool] | None = None,
    now: datetime | None = None,
) -> TransferSource | None:
    """Pick a source descriptor per §9.

    Args:
        handle: The :class:`TransferHandle` whose ``sources`` array
            will be searched in order.
        is_accessible: Callback invoked for each
            :class:`FilesystemSource` to confirm the resolved location
            is readable. ``None`` means the party does not support
            filesystem at all — every filesystem source is skipped.
            For HTTPS sources the callback is not consulted (URL
            reachability is checked at transfer time, not selection
            time).
        now: Reference time for expiry checks. Defaults to the wall
            clock when ``None``; pass an explicit value only from
            tests.

    Returns:
        The first descriptor that survives the §9 checks, or ``None``
        if none did. ``None`` is normal control flow — caller renders
        a ``no-supported-transport`` error envelope.
    """
    reference_time = now if now is not None else datetime.now(timezone.utc)
    for src in handle.sources:
        if isinstance(src, FilesystemSource):
            if is_accessible is None:
                continue
            if not is_accessible(src):
                continue
            return src
        if isinstance(src, DownloadSource):
            if src.expiresAt < reference_time - _EXPIRY_TOLERANCE:
                continue
            return src
        # UnknownTransportDescriptor or anything else not in the known
        # source union: forward-compat fallthrough — skip.
        continue
    return None


def select_sink(
    ticket: IntakeTicket,
    *,
    is_accessible: Callable[[FilesystemSink], bool] | None = None,
    now: datetime | None = None,
) -> TransferSink | None:
    """Pick a sink descriptor per §9.

    Mirrors :func:`select_source` for the write direction. The
    callback signature differs (``FilesystemSink`` instead of
    ``FilesystemSource``) because read vs write accessibility is a
    different check on the downstream's filesystem.

    Args:
        ticket: The :class:`IntakeTicket` whose ``sinks`` array will
            be searched in order.
        is_accessible: Callback invoked for each
            :class:`FilesystemSink` to confirm the resolved location
            is writable. ``None`` means the party does not support
            filesystem at all.
        now: Reference time for expiry checks. Defaults to the wall
            clock when ``None``.

    Returns:
        The first descriptor that survives the §9 checks, or ``None``.
    """
    reference_time = now if now is not None else datetime.now(timezone.utc)
    for sink in ticket.sinks:
        if isinstance(sink, FilesystemSink):
            if is_accessible is None:
                continue
            if not is_accessible(sink):
                continue
            return sink
        if isinstance(sink, UploadSink):
            if sink.expiresAt < reference_time - _EXPIRY_TOLERANCE:
                continue
            return sink
        continue
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/_file_exchange/test_selection.py -q
```

Expected: `21 passed` (12 source-side + 7 sink-side + 2 parametrised unknown-transport).

- [ ] **Step 5: Run format/lint/mypy**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
```

Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_selection.py \
        tests/_file_exchange/test_selection.py
git commit -m "$(cat <<'EOF'
feat(_file_exchange): §9 descriptor selection (select_source/select_sink)

Adds the iterate-and-skip selection algorithm as a typed pair:

- ``select_source(handle, *, is_accessible=None, now=None) ->
  TransferSource | None``
- ``select_sink(ticket, *, is_accessible=None, now=None) ->
  TransferSink | None``

Algorithm per §9:

1. Iterate descriptors in array order.
2. Filesystem branch: skip if ``is_accessible`` callback is ``None``
   (party doesn't speak filesystem) or returns ``False``.
3. HTTPS branch: skip if ``expiresAt < now - 30s`` (past the
   tolerance window; ``_EXPIRY_TOLERANCE`` constant per spec's
   "for example, 30 seconds" guidance).
4. ``UnknownTransportDescriptor`` always skipped (forward-compat).
5. Return ``None`` if iteration exhausts — caller renders the
   ``no-supported-transport`` error envelope.

§17.4 must-understand is not re-run; ``TransferHandle.from_wire`` /
``IntakeTicket.from_wire`` from #139 already enforce it. ``now`` is
injectable for tests; ``is_accessible`` takes the typed descriptor so
a callback can inspect any field (forward-defensive for a future spec
amendment that adds hints to filesystem descriptors).

Refs: #140.
EOF
)"
```

---

### Task 4: Public namespace re-exports + namespace test additions

**Why:** Downstream code imports from `fastmcp_pvl_core.file_exchange`, not from the private subpackage. The 5 new symbols (`TransferErrorCode`, `KNOWN_CODES`, `select_source`, `select_sink`, `build_file_exchange_error`) must be re-exported and covered by `test_file_exchange_namespace.py`.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Modify: `tests/test_file_exchange_namespace.py`

- [ ] **Step 1: Add the 5 new test cases**

Append to `tests/test_file_exchange_namespace.py`:

```python
def test_error_codes_exposed():
    from fastmcp_pvl_core import file_exchange

    assert hasattr(file_exchange, "TransferErrorCode")
    assert hasattr(file_exchange, "KNOWN_CODES")
    # Spot-check one member end-to-end (full coverage is in test_codes.py).
    assert file_exchange.TransferErrorCode.NO_SUPPORTED_TRANSPORT == (
        "no-supported-transport"
    )
    assert "transfer-failed" in file_exchange.KNOWN_CODES


def test_selection_helpers_exposed():
    from fastmcp_pvl_core import file_exchange

    assert callable(file_exchange.select_source)
    assert callable(file_exchange.select_sink)


def test_error_envelope_helper_exposed():
    from fastmcp_pvl_core import file_exchange

    assert callable(file_exchange.build_file_exchange_error)
    # End-to-end smoke through the namespace import path.
    result = file_exchange.build_file_exchange_error(
        file_exchange.TransferErrorCode.NO_SUPPORTED_TRANSPORT,
    )
    assert result.isError is True
    inner = result.meta["nl.liesdonk.file-exchange/error"]
    assert inner == {"code": "no-supported-transport"}
```

- [ ] **Step 2: Run new tests to verify they fail with AttributeError**

```bash
uv run pytest tests/test_file_exchange_namespace.py -q
```

Expected: AttributeError on `TransferErrorCode`, `KNOWN_CODES`, etc.

- [ ] **Step 3: Update subpackage `__init__.py`**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, add the new imports and extend `__all__`:

After the existing `from fastmcp_pvl_core._file_exchange._wire import (...)` block, add:

```python
from fastmcp_pvl_core._file_exchange._codes import (
    KNOWN_CODES,
    TransferErrorCode,
)
from fastmcp_pvl_core._file_exchange._errors import (
    build_file_exchange_error,
)
from fastmcp_pvl_core._file_exchange._selection import (
    select_sink,
    select_source,
)
```

Update `__all__` to add (kept alphabetical to match the existing convention):

```python
__all__ = [
    ...existing entries...
    "KNOWN_CODES",
    "TransferErrorCode",
    "build_file_exchange_error",
    "select_sink",
    "select_source",
]
```

Final alphabetised `__all__`:

```python
__all__ = [
    "ArtifactConstraints",
    "ArtifactMetadata",
    "DownloadSource",
    "FileExchangeCapability",
    "FilesystemSink",
    "FilesystemSource",
    "HANDLE_TYPE",
    "IntakeTicket",
    "KNOWN_CODES",
    "NAMESPACE",
    "Role",
    "SPEC_SOURCE_SHA",
    "SPEC_VERSION",
    "TICKET_TYPE",
    "TransferError",
    "TransferErrorCode",
    "TransferHandle",
    "TransferSink",
    "TransferSource",
    "UnknownTransportDescriptor",
    "UnsupportedRequirementError",
    "UnsupportedVersionError",
    "UploadSink",
    "VERSION_PATTERN",
    "WireFormatError",
    "build_file_exchange_error",
    "capability_declaration",
    "check_requires",
    "check_version_skew",
    "select_sink",
    "select_source",
    "validate_wire",
]
```

- [ ] **Step 4: Update public namespace `file_exchange.py`**

In `src/fastmcp_pvl_core/file_exchange.py`, extend the single `from fastmcp_pvl_core._file_exchange import (...)` block to include the 5 new names (alphabetised) and extend `__all__` symmetrically.

After update, the import block reads:

```python
from fastmcp_pvl_core._file_exchange import (
    HANDLE_TYPE,
    KNOWN_CODES,
    NAMESPACE,
    SPEC_SOURCE_SHA,
    SPEC_VERSION,
    TICKET_TYPE,
    VERSION_PATTERN,
    ArtifactConstraints,
    ArtifactMetadata,
    DownloadSource,
    FileExchangeCapability,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    Role,
    TransferError,
    TransferErrorCode,
    TransferHandle,
    TransferSink,
    TransferSource,
    UnknownTransportDescriptor,
    UnsupportedRequirementError,
    UnsupportedVersionError,
    UploadSink,
    WireFormatError,
    build_file_exchange_error,
    capability_declaration,
    check_requires,
    check_version_skew,
    select_sink,
    select_source,
    validate_wire,
)
```

And `__all__` becomes:

```python
__all__ = [
    "ArtifactConstraints",
    "ArtifactMetadata",
    "DownloadSource",
    "FileExchangeCapability",
    "FilesystemSink",
    "FilesystemSource",
    "HANDLE_TYPE",
    "IntakeTicket",
    "KNOWN_CODES",
    "NAMESPACE",
    "Role",
    "SPEC_SOURCE_SHA",
    "SPEC_VERSION",
    "TICKET_TYPE",
    "TransferError",
    "TransferErrorCode",
    "TransferHandle",
    "TransferSink",
    "TransferSource",
    "UnknownTransportDescriptor",
    "UnsupportedRequirementError",
    "UnsupportedVersionError",
    "UploadSink",
    "VERSION_PATTERN",
    "WireFormatError",
    "build_file_exchange_error",
    "capability_declaration",
    "check_requires",
    "check_version_skew",
    "select_sink",
    "select_source",
    "validate_wire",
]
```

- [ ] **Step 5: Run full namespace tests to verify they pass**

```bash
uv run pytest tests/test_file_exchange_namespace.py -q
```

Expected: all pre-existing + 3 new namespace tests pass.

- [ ] **Step 6: Run full repo test + lint sweep**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
uv run pytest -q
```

Expected: format clean, lint clean, mypy clean, all tests pass (603 baseline + 6 codes + 11 errors + 21 selection + 3 namespace = 644 expected; 1 skipped for the GITHUB_TOKEN-gated network test).

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/__init__.py \
        src/fastmcp_pvl_core/file_exchange.py \
        tests/test_file_exchange_namespace.py
git commit -m "$(cat <<'EOF'
feat(file_exchange): expose codes/selection/errors in public namespace

Adds the 5 new public names from #140 to ``fastmcp_pvl_core.file_exchange``:

- ``TransferErrorCode`` (the §13 enum)
- ``KNOWN_CODES`` (the frozenset)
- ``select_source`` / ``select_sink`` (§9 selection)
- ``build_file_exchange_error`` (§13 envelope helper)

Mirrors the existing explicit-re-export pattern from #139 — the
namespace module is the downstream-facing surface, the
``_file_exchange`` subpackage stays private. ``__all__`` updated on
both sides.

Refs: #140 (closes via the wrapping PR).
EOF
)"
```

---

### Final pre-push sweep (do this immediately before opening the PR)

The first three sub-skill steps are explicitly required by the user-global CLAUDE.md PR workflow.

- [ ] **Step 1: Verify branch is clean and ahead of main only with intended commits**

```bash
git fetch origin main
git log --oneline origin/main..HEAD
git status
```

Expected commits, in order:

1. `2ffb633` `docs: design record for #140 (selection + error envelope)`
2. Task 1's commit (`feat(_file_exchange): TransferErrorCode enum + KNOWN_CODES frozenset`)
3. Task 2's commit (`feat(_file_exchange): build_file_exchange_error envelope helper`)
4. Task 3's commit (`feat(_file_exchange): §9 descriptor selection (select_source/select_sink)`)
5. Task 4's commit (`feat(file_exchange): expose codes/selection/errors in public namespace`)

`git status` should be clean.

- [ ] **Step 2: Final local-checks pass**

```bash
uv sync --all-extras
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

All must be clean. CI runs the same suite on Python 3.10–3.13; the local pass is the floor.

- [ ] **Step 3: Invoke `preflight-circus`**

Mandatory per the user-global CLAUDE.md PR workflow ("every PR-affecting push, no exemption"). The skill runs the 5 core lenses + lens 6 + applicable supplementaries (silent-failure-hunter on the error-handling around `_meta` construction, type-design-analyzer on the new `TransferErrorCode` enum, pr-test-analyzer on the new test files, comment-analyzer on the new docstrings — `pr-review-toolkit:code-reviewer` always). Score each finding with Haiku at ≥80; do not push until status is clean.

If any finding survives ≥80, fix it and re-invoke `preflight-circus` full + verbatim + blind on the new cumulative diff. Do not narrow the re-run.

- [ ] **Step 4: Open the PR as draft, then flip to ready once bots LGTM**

```bash
git push -u origin feat/140-selection-error-envelope
gh pr create --draft \
  --base main \
  --title "feat: file-exchange selection + error envelope (closes #140)" \
  --body "$(cat <<'EOF'
## Summary

- §9 descriptor-selection algorithm as typed pair: ``select_source(handle, ...)`` / ``select_sink(ticket, ...)``, returning the first survivor or ``None``.
- §13 error envelope helper: ``build_file_exchange_error(code, *, transport, detail, text) -> CallToolResult`` with the spec-mandated ``_meta["nl.liesdonk.file-exchange/error"]`` payload.
- ``TransferErrorCode`` enum + ``KNOWN_CODES`` frozenset over the 9 spec-defined codes (set stays open per §13 — the helper accepts any string).
- Public namespace updated; subpackage ``__all__`` mirrored.

Design record at ``docs/superpowers/specs/2026-05-21-file-exchange-140-selection-error-design.md``.

## Test plan

- [x] ``uv run pytest`` — full repo green on 3.10 locally (CI verifies 3.10–3.13).
- [x] ``uv run ruff format --check . && uv run ruff check .`` — clean.
- [x] ``uv run mypy src`` — clean.
- [x] ``preflight-circus`` skill run on cumulative diff — clean at ≥80 confidence.

Closes #140.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Watch for `claude-review` settlement, read its actual review body (not just the green check), address findings within the one-round iteration cap, then `gh pr ready` once convergent.

---

## Self-review against the spec

**1. Spec coverage:**

| Spec section | Task |
|---|---|
| Module layout | All four tasks (file paths match the spec) |
| Error codes (`TransferErrorCode` + `KNOWN_CODES`) | Task 1 |
| Selection algorithm (signatures + 30s tolerance + accessibility callback) | Task 3 |
| Error envelope (default text, transport suffix, detail log-leak guard) | Task 2 |
| Tests (codes / selection / errors / namespace) | Tasks 1, 2, 3, 4 respectively |
| Public namespace re-exports | Task 4 |

Every spec requirement maps to a task. No gaps.

**2. Placeholder scan:** Searched for "TBD", "TODO", "implement later", "fill in details", "add error handling", "write tests for the above", "similar to Task N". None present — every step has concrete code, exact file paths, and explicit commands with expected output.

**3. Type consistency:** Function/method names used in later tasks are introduced in earlier tasks:

- `TransferErrorCode`, `KNOWN_CODES` defined in Task 1 → referenced in Task 2's tests + implementation, Task 4's namespace tests.
- `build_file_exchange_error`, `_DEFAULT_TEXT` defined in Task 2 → referenced in Task 4's namespace tests.
- `select_source`, `select_sink` defined in Task 3 → referenced in Task 4's namespace tests.
- All wire-model imports (`TransferHandle`, `IntakeTicket`, `FilesystemSource`, `FilesystemSink`, `DownloadSource`, `UploadSink`, `TransferSource`, `TransferSink`, `UnknownTransportDescriptor`) are #139 exports already shipped on `main` at `509af42` — no forward references.
