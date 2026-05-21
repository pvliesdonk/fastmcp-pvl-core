# #140 — Selection algorithm + error envelope (design)

> **Status:** contemporaneous design record. The implementation in the
> same PR is the source of truth; this document captures the shape that
> was agreed before implementation started and the rationale for the
> non-obvious choices.

EPIC: [#138](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/138).
Issue: [#140](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/140).
Depends on: [#139](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/139)
(merged as commit `509af42`).
Wire-spec authority: `pvliesdonk/mcp-file-exchange-ext` at pinned commit
[`5f50a4e16a33a6bbc0888c142baec7fdfe858cb6`](https://github.com/pvliesdonk/mcp-file-exchange-ext/commit/5f50a4e16a33a6bbc0888c142baec7fdfe858cb6),
sections §9 (descriptor selection), §13 (error handling), §17.4
(must-understand check that runs before §9).

## Goal

Land §9's iterate-and-skip descriptor selection algorithm and §13's
error-envelope shape as a pair of small, focused modules that every
role helper (#143/#145/#146) and the top-level `register_file_exchange_*`
helpers (#148) will compose. Out of scope here: the role helpers
themselves, transport-specific data-plane logic, filesystem URI
resolution (#141), the byte-source/sink hook contracts (#142).

## Module layout

Three new private modules under `src/fastmcp_pvl_core/_file_exchange/`,
each with one focused concern:

```
src/fastmcp_pvl_core/_file_exchange/
├── _codes.py        # TransferErrorCode StrEnum + KNOWN_CODES frozenset
├── _selection.py    # select_source(handle, ...) / select_sink(ticket, ...)
└── _errors.py       # build_file_exchange_error(code, ...) -> CallToolResult
```

Public namespace at `src/fastmcp_pvl_core/file_exchange.py` adds
explicit re-exports for `TransferErrorCode`, `KNOWN_CODES`,
`select_source`, `select_sink`, `build_file_exchange_error`. The
subpackage `__init__.py` mirrors with the same explicit pattern as
#139.

Test files mirror the split: `tests/_file_exchange/test_codes.py`,
`test_selection.py`, `test_errors.py`.

This PR consumes #139's wire-format types directly (`TransferHandle`,
`IntakeTicket`, `TransferSource`, `TransferSink`, `FilesystemSource`,
`FilesystemSink`, `DownloadSource`, `UploadSink`,
`UnknownTransportDescriptor`) and imports `CallToolResult` /
`TextContent` from `mcp.types`. No new third-party dependencies.

## Error codes

`_codes.py` ships a single enum mirroring the §13 defined code set,
plus a frozenset for introspection and drift-detection tests.

```python
from enum import Enum


class TransferErrorCode(str, Enum):
    """§13 defined error codes. The code set is OPEN per spec —
    callers MAY pass arbitrary strings to ``build_file_exchange_error``;
    this enum just names the spec-defined values for autocompletion
    and grep-ability.
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
```

**Why `str, Enum` mixin and not stdlib `StrEnum`:** `pyproject.toml`
declares `requires-python = ">=3.10"` and CI tests Python 3.10. The
stdlib `StrEnum` lands in 3.11; the `(str, Enum)` mixin form is the
back-compat-safe equivalent — `TransferErrorCode.DIGEST_MISMATCH ==
"digest-mismatch"` evaluates True and the member is usable anywhere
a `str` is expected.

**Why expose the frozenset separately:** lets tests assert
"every enum member is in `KNOWN_CODES`" (catches a drift where a member
is added or removed without updating the set) and lets callers do
`if code in KNOWN_CODES: ...` membership tests without iterating the
enum. Single source of truth: the frozenset is derived from the enum
at module load.

**Why not `Literal[...]` on the helper signature:** would close the
open code set the spec mandates. `build_file_exchange_error`'s `code`
parameter is `str | TransferErrorCode` — accepts the enum member for
known codes (mypy-checked) AND raw strings for future-spec codes that
a caller has been told about out-of-band.

## Selection algorithm

Two typed selection functions, parallel in shape, one per direction:

```python
from collections.abc import Callable
from datetime import datetime


def select_source(
    handle: TransferHandle,
    *,
    is_accessible: Callable[[FilesystemSource], bool] | None = None,
    now: datetime | None = None,
) -> TransferSource | None: ...


def select_sink(
    ticket: IntakeTicket,
    *,
    is_accessible: Callable[[FilesystemSink], bool] | None = None,
    now: datetime | None = None,
) -> TransferSink | None: ...
```

Algorithm body per §9, applied to `handle.sources` or `ticket.sinks`
in array order:

1. **Filesystem branch** (`FilesystemSource` / `FilesystemSink`): if
   `is_accessible is None`, the party does not support filesystem at
   all — skip. Otherwise call `is_accessible(descriptor)`; skip on
   False, select on True.
2. **HTTPS branch** (`DownloadSource` / `UploadSink`): skip if
   `expiresAt < now - 30s` (past the tolerance window).
3. **Unknown branch** (`UnknownTransportDescriptor`): always skip —
   forward-compat fallthrough, the party by definition does not speak
   this transport.
4. If iteration exhausts: return `None`.

**30s tolerance is hardcoded** in `_selection.py` as a module constant
`_EXPIRY_TOLERANCE = timedelta(seconds=30)`. The spec says "for
example, 30 seconds"; pvl-core picks 30s and downstreams conform per
the framing principle. No kwarg. If a real operational need emerges
later it lifts to an env var (operator-side config), not a kwarg.

**`now` defaults to `None`** and is materialised to
`datetime.now(timezone.utc)` inside the body. Injectable so tests can
fix it; production code never passes it explicitly.

**`is_accessible` defaults to `None`**, meaning "this party does not
support filesystem" — selection silently skips every filesystem
descriptor in that case. A party that supports filesystem supplies the
callback; the callback embeds whatever URI-resolution and
read/write-check logic the downstream needs. The mechanics of
`exchange://` resolution and path confinement live in #141; the role
helpers in #143/#148 wire the callback up.

**§17.4 is NOT re-run by selection.** `TransferHandle.from_wire` /
`IntakeTicket.from_wire` already call `check_requires` per #139.
Selection assumes the reference has been through `from_wire` and
proceeds straight to §9. If a caller constructed a reference directly
(bypassing `from_wire`), `requires` is enforced at the Pydantic layer
for v0.1 anyway via `_check_handle_or_ticket_requires`.

**`UnknownTransportDescriptor` always skipped** even when iteration
encounters one in the middle of an otherwise-supported array. The wire
layer routes unknown transports into the fallthrough type per #139,
and v0.1 has no mechanism for downstream-pluggable transports. A
future minor that adds one changes pvl-core, not the downstream call
sites.

## Error envelope

```python
from mcp.types import CallToolResult, TextContent


def build_file_exchange_error(
    code: str | TransferErrorCode,
    *,
    transport: str | None = None,
    detail: str | None = None,
    text: str | None = None,
) -> CallToolResult: ...
```

Returns an `mcp.types.CallToolResult` with:

- `isError=True`
- `content=[TextContent(type="text", text=<rendered text>)]`
- `_meta={"nl.liesdonk.file-exchange/error": {"code": <code-str>,
  ["transport": <transport>,] ["detail": <detail>,]}}`

The namespaced `_meta` key matches §13's example verbatim. Optional
`transport` and `detail` keys are omitted when `None` rather than
emitted as JSON `null` — keeps the envelope minimal.

**Default text rendering:** if the caller passes `text=...`, that
string is used verbatim. Otherwise the helper looks the code up in a
private `_DEFAULT_TEXT` dict:

```python
_DEFAULT_TEXT: dict[str, str] = {
    TransferErrorCode.NO_SUPPORTED_TRANSPORT:
        "No supported transport found in transfer reference.",
    TransferErrorCode.DESCRIPTOR_EXPIRED:
        "Selected transfer descriptor expired before transfer completed.",
    TransferErrorCode.NOT_ACCESSIBLE:
        "Transfer location is not accessible.",
    TransferErrorCode.DIGEST_MISMATCH:
        "Transferred bytes did not match the expected digest.",
    TransferErrorCode.SIZE_MISMATCH:
        "Transferred byte count did not match the expected size.",
    TransferErrorCode.TOO_LARGE:
        "Artifact exceeded the declared size limit.",
    TransferErrorCode.MIME_TYPE_REJECTED:
        "Artifact's media type was not in the receiver's accepted list.",
    TransferErrorCode.UNSUPPORTED_REQUIREMENT:
        "Transfer reference requires a feature this party does not implement.",
    TransferErrorCode.TRANSFER_FAILED:
        "File transfer failed.",
}
```

For an **unknown code** (not in `KNOWN_CODES`), default text is
`f"File transfer failed: {code}"`. This honours the spec's
"unrecognized code SHOULD be treated as a generic failure" rule on
the *render* side.

If `transport` is set and `text` isn't, the helper appends
`f" (transport: {transport})"` to the default text — useful when an
error is transport-specific (e.g. `not-accessible` on a `filesystem`
descriptor vs an HTTPS one).

**`detail` is never auto-rendered into the text.** It's a structured
machine-readable extension (e.g. `"expected sha-256:9f..., got
sha-256:1b..."`), and inlining it into operator-facing text would
invite log-leak issues (per the URL-redaction pattern from PR #122).
Callers who want detail in the text pass `text=` explicitly.

**Return type is `mcp.types.CallToolResult` directly.** The caller's
tool function returns this verbatim and fastmcp's `tools/call` handler
passes it through. A `dict` return would force every caller to know
the CallToolResult shape; the typed object is what the framework
expects.

**No exception variant ships in this PR.** No `FileExchangeError`
class, no `build_file_exchange_error_from_exc` dispatcher. The wire
layer's typed exceptions (`UnsupportedRequirementError`,
`WireFormatError`, `UnsupportedVersionError`) already exist — callers
in #143/#145/#146 explicitly map them to codes:

```python
try:
    handle = TransferHandle.from_wire(raw)
except UnsupportedRequirementError as exc:
    return build_file_exchange_error(
        TransferErrorCode.UNSUPPORTED_REQUIREMENT,
        detail=f"unknown features: {sorted(exc.unknown_features)}",
    )
```

The explicit mapping keeps the code/exception correspondence visible
in role-helper code. If a from-exc dispatcher pattern emerges later
(more than ~3 role helpers writing the same mapping), it's extracted
then. YAGNI now.

## Tests

Three new test files under `tests/_file_exchange/`.

### `test_codes.py`

- Each `TransferErrorCode` member's value matches the §13 spec table
  verbatim (`NO_SUPPORTED_TRANSPORT.value == "no-supported-transport"`,
  …, `TRANSFER_FAILED.value == "transfer-failed"`).
- `KNOWN_CODES` equals exactly the 9 spec-defined codes (frozenset
  equality against a hand-written set — catches drift if a member is
  added without updating the test).
- `str` mixin behaviour holds: `TransferErrorCode.DIGEST_MISMATCH ==
  "digest-mismatch"` is True on all supported Python versions.
- `TransferErrorCode.DIGEST_MISMATCH in KNOWN_CODES` is True; a typo
  (`"digestmismatch"`) is False.

### `test_selection.py`

Covers every §9 rule the issue requires plus the symmetry between
source and sink directions:

- `test_select_source_skips_unknown_transport` — handle with only an
  `UnknownTransportDescriptor` source returns `None`.
- `test_select_source_skips_expired_download` — `expiresAt` 60s in the
  past, no other sources → `None`.
- `test_select_source_selects_download_within_tolerance` — `expiresAt`
  5s in the past (inside 30s tolerance) → selected.
- `test_select_source_skips_download_past_tolerance` — `expiresAt` 35s
  in the past (past 30s tolerance) → skipped, falls to next descriptor.
- `test_select_source_skips_filesystem_when_callback_returns_false` —
  `is_accessible` returns False → skipped.
- `test_select_source_selects_filesystem_when_callback_returns_true` —
  `is_accessible` returns True → selected.
- `test_select_source_skips_all_filesystem_when_callback_is_none` —
  `is_accessible=None`, handle has filesystem source → `None`.
- `test_select_source_returns_first_surviving_in_array_order` — handle
  with `[expired-download, valid-filesystem, valid-download]` and
  callback returning True → filesystem is selected (first survivor in
  array order, not the last).
- `test_select_source_empty_when_no_descriptor_survives` — handle with
  only expired downloads → `None`.
- `test_select_source_now_parameter_overrides_wall_clock` — pass an
  explicit `now`, assert tolerance arithmetic is computed relative to
  it (defends against a refactor that reads the wall clock inside the
  loop instead of once at entry).

Symmetric mirror tests on `select_sink` (`IntakeTicket.sinks`) cover
one happy path plus the 30s tolerance ladder via a parametrise — the
algorithm is structurally identical to the source side, so a full
ten-test mirror would just duplicate coverage. The two-direction
parametrise catches accidental direction-specific divergence (e.g.
the sink branch silently drops the tolerance window).

### `test_errors.py`

- `build_file_exchange_error(NO_SUPPORTED_TRANSPORT)` returns
  `CallToolResult` with `isError=True`, `_meta` key
  `"nl.liesdonk.file-exchange/error"` containing `{"code":
  "no-supported-transport"}`, content has one `TextContent` with the
  default text from `_DEFAULT_TEXT`.
- Passing `transport="download"` adds `"transport": "download"` to
  `_meta` AND appends `" (transport: download)"` to the default text.
- Passing `detail="..."` adds it to `_meta` but does NOT appear in
  the text (the log-leak guard).
- Passing explicit `text="custom"` uses that string verbatim;
  `_DEFAULT_TEXT` is bypassed; the transport-suffix is not appended.
- An unknown code (`"future-spec-code"`) renders text `"File transfer
  failed: future-spec-code"` and emits the literal code string in
  `_meta`.
- Every `TransferErrorCode` member has a default-text mapping — loop
  through the enum and assert `code in _DEFAULT_TEXT`.
- `_meta` does NOT include `transport` or `detail` keys when those
  args are `None` (no JSON nulls in the envelope).

### What is not tested here

- The role-helper integration (catching `UnsupportedRequirementError`
  → `build_file_exchange_error`) — that lands when the role helpers
  do, in #143/#145/#146.
- The `descriptor-expired` *during transfer* emit site — that's
  data-plane code in #145/#146. This PR ships only the constant.
- Filesystem URI resolution / volume mapping / path confinement —
  that's #141. Selection's accessibility callback is downstream-supplied
  here; pvl-core just calls it.

## Risks and non-risks

**Risk:** the 30s tolerance is hardcoded. Mitigation: if downstream
needs different in practice, lift to an env var without changing the
kwarg surface (operator config, not domain hook).

**Risk:** `now` injection lets tests stub the clock, but production
code that passes `now` explicitly would break the tolerance against
wall-clock skew. Mitigation: docstring on each function explicitly
says `now` is for test injection, not normal use.

**Non-risk:** the open code set in `_meta` could in principle let a
downstream emit a typo (`"digest-misamatch"`). Mitigation: the
`KNOWN_CODES` frozenset + the `TransferErrorCode` enum make typo-free
emission cheap; consumers SHOULD treat unknown codes as generic
failures per §13, so a typo degrades gracefully rather than crashing.

**Non-risk:** no `from_exc` dispatcher. Mitigation: explicit mapping
in the ~3 future call sites (role helpers) keeps the code/exception
correspondence visible; pattern is extractable later once it's seen in
practice.
