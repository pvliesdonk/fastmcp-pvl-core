# #139 — File-exchange wire format (design)

- Date: 2026-05-21
- Issue: [#139](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/139)
- EPIC: [#138](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/138)
- Status: implemented (this document is the contemporaneous design record;
  the implementation in the same PR is the source of truth)

## Goal

Vendor the `mcp-file-exchange-ext` v0.1 wire schema into pvl-core and expose
the foundation every later child of EPIC #138 builds on:

- Pydantic wire models for handle / ticket / capability / error and each
  transport descriptor.
- A layered jsonschema-then-Pydantic validation pipeline that surfaces
  spec-conformant errors before Pydantic decoding runs.
- The §17.3 version-skew rule and §17.4 must-understand check, each with a
  typed exception carrying the context #140's error envelope needs.
- A capability declaration helper that builds the
  `capabilities.experimental["nl.liesdonk.file-exchange"]` dict.
- A conformance test harness running every vendored upstream fixture
  through the validation layer and through the Pydantic layer, asserting
  agreement.
- A vendoring sync script + CI job, pinned to a specific upstream commit,
  so the vendored copy cannot drift silently from the spec.

No transport mechanics, selection algorithm, error envelope wrapper, or
FastMCP server mutation lives here — those are later issues in EPIC #138.

## External spec pin

- Repo: `pvliesdonk/mcp-file-exchange-ext`
- Commit: `5f50a4e16a33a6bbc0888c142baec7fdfe858cb6`
- Extension version: `0.1` (draft), targets MCP revision `2025-11-25`
- Namespace: `nl.liesdonk.file-exchange`

The pin lives at `_file_exchange/_spec.py:SPEC_SOURCE_SHA`. The sync script
script (`scripts/sync_file_exchange_spec.py`) is the only sanctioned way to
update it — CI runs `--check` on every push to enforce.

## Module layout

```
src/fastmcp_pvl_core/
├── _file_exchange/
│   ├── __init__.py         # explicit re-export of the public surface
│   ├── _spec.py            # constants + check_version_skew + check_requires
│   ├── _validation.py      # jsonschema layer + WireFormatError (RFC 6901)
│   ├── _wire.py            # all Pydantic wire models incl. discriminated unions
│   ├── _capability.py      # FileExchangeCapability + capability_declaration()
│   └── _schema/
│       └── file-exchange.json   # vendored upstream
└── file_exchange.py        # public namespace re-export (explicit imports)

tests/_file_exchange/
├── __init__.py
├── conftest.py             # discover_fixtures, fixture_ids helpers
├── conformance/{valid,invalid}/{capability,error,handle,ticket}/*.json   # vendored
├── test_spec.py            # constants + skew + must-understand + malformed-version
├── test_validation.py      # jsonschema layer + RFC 6901 escape ordering
├── test_conformance.py     # parametrised over every vendored fixture
├── test_wire_metadata.py   # ArtifactMetadata + ArtifactConstraints
├── test_wire_sources.py    # FilesystemSource + DownloadSource
├── test_wire_sinks.py      # FilesystemSink + UploadSink
├── test_wire_unions.py     # discriminated unions + fallthrough + drift detection
├── test_wire_handle.py     # TransferHandle + TransferError + list-level routing
├── test_wire_ticket.py     # IntakeTicket + sink-side list-level routing
├── test_from_wire.py       # four-layer pipeline + json_pointer survival
├── test_capability.py      # FileExchangeCapability + helper + WARNING logs
└── test_roundtrip.py       # outbound + jsonschema/Pydantic agreement (all 4 kinds)

scripts/sync_file_exchange_spec.py   # --check (CI default), --bump <sha>
tests/test_sync_file_exchange_spec.py  # vendoring sanity + regex format drift
tests/test_file_exchange_namespace.py  # public namespace surface
.github/workflows/ci.yml             # +file-exchange-spec-sync job with GITHUB_TOKEN
```

## Public surface

`fastmcp_pvl_core.file_exchange` is the stable import path. The
implementation lives under the underscore-prefixed private subpackage; the
namespace module re-exports explicitly (not via star-import) so static
analyzers — Pyright, mypy in strict mode, IDE autocomplete — resolve
`file_exchange.SomeName` without running the import.

Exported names: constants (`SPEC_VERSION`, `NAMESPACE`, `HANDLE_TYPE`,
`TICKET_TYPE`, `SPEC_SOURCE_SHA`, `VERSION_PATTERN`); wire models
(`ArtifactMetadata`, `ArtifactConstraints`, `FilesystemSource`,
`DownloadSource`, `FilesystemSink`, `UploadSink`,
`UnknownTransportDescriptor`, `TransferSource`, `TransferSink`,
`TransferHandle`, `IntakeTicket`, `FileExchangeCapability`,
`TransferError`); exceptions (`WireFormatError`, `UnsupportedVersionError`,
`UnsupportedRequirementError`); helpers (`capability_declaration`,
`validate_wire`, `check_requires`, `check_version_skew`); type alias
(`Role`).

## Wire models

Pydantic v2, two base configurations encoding §17 structurally:

- `_WireBase` — `extra="allow"` (per §17.2 tolerant reading), frozen,
  `populate_by_name=True`. Base of every non-descriptor wire object.
- `_DescriptorBase` — `extra="forbid"` (per §17.5 closed shape), frozen,
  `populate_by_name=True`. Base of the four named transport descriptors.

`UnknownTransportDescriptor` deliberately does NOT inherit from
`_DescriptorBase` — that base would close it to extras, but §17.5 keeps
the fallthrough open. The class carries a `model_validator` that refuses
known transport names (`filesystem`, `download`, `upload`) at construction,
mirroring the schema's `not.enum` exclusion so direct Python construction
can't bypass the invariant.

`TransferSource` / `TransferSink` are Pydantic v2 callable-discriminator
unions. The discriminator returns the transport name when known, else
`"unknown"` (routing to the fallthrough branch). A malformed known
descriptor (e.g. `filesystem` with an extra field) routes to its closed
branch and fails there — it does not silently match the fallthrough.

`TransferHandle` and `IntakeTicket` carry the §17.4 `requires` invariants
(non-empty entries, unique, v0.1-must-be-empty) as a Pydantic
`model_validator`. They also expose a `from_wire(raw)` classmethod that
runs the four-layer pipeline:

1. `validate_wire(raw, kind=...)` — jsonschema-conformant errors with
   RFC 6901 JSON Pointer to the offending field.
2. `cls.model_validate(raw)` — typed Pydantic view.
3. `check_version_skew(version, kind="reference")` — §17.3 MUST-fail.
4. `check_requires(requires)` — §17.4 must-understand.

The skew + must-understand checks live in `from_wire`, not in a
`model_validator`, because Pydantic v2 wraps any exception raised in a
validator into a `ValidationError` — which erases the typed
`UnsupportedVersionError` / `UnsupportedRequirementError` that #140's
error envelope dispatches on. The class docstrings note this explicitly
so a future refactor doesn't "tidy" the checks into the validator.

`TransferError` keeps `code: str` open per §13 (the defined codes are
advisory; consumers SHOULD treat unknown codes as generic failures).
`from_wire` skips the skew + requires layers — the error envelope has no
`version` and no `requires`.

## Validation pipeline

`_validation.validate_wire(raw, *, kind)` loads the vendored schema once
(`@lru_cache`), builds a Draft 2020-12 validator per kind scoped to
`#/$defs/<KIND>` (`@lru_cache(maxsize=4)`), and raises
`WireFormatError` on `ValidationError`. The error carries a `json_pointer`
attribute — an RFC 6901 pointer to the offending field, with `~` escaped
*first* (then `/`) so a literal `/` doesn't round-trip into a literal `~`.
Dedicated tests exercise both escape characters individually and in the
order-sensitive combined case.

## Version skew + must-understand

`_spec.check_version_skew(version, *, kind)`:

- Validates the version string matches `^[0-9]+\.[0-9]+$` *first*;
  malformed input raises `UnsupportedVersionError` (the typed error
  callers expect, rather than the opaque `int()` `ValueError` that would
  otherwise leak through). Direct callers bypassing the schema layer get
  the same typed error as a major mismatch.
- `kind="reference"` raises `UnsupportedVersionError` on major mismatch
  (§17.3 MUST-fail).
- `kind="capability"` returns `False` on major mismatch (§17.3 SHOULD-fail
  — caller treats peer as non-participant).

`_spec.check_requires(requires)` raises `UnsupportedRequirementError`
carrying `unknown_features: frozenset[str]` for any entry not in
`_KNOWN_REQUIRES` (empty in v0.1).

## Capability declaration

`FileExchangeCapability` is the wire model for a peer capability. Its
`roles` field is typed `dict[str, list[str]]` — deliberately wider than
the `Role` Literal — because the schema's `additionalProperties: true` on
`roles` permits a peer to advertise a future role name. The outbound
builder `capability_declaration(...)` constrains producers to the four
`Role` values; Postel's principle applied at the type level.

`FileExchangeCapability.from_wire` returns `None` on major-version
mismatch and emits a `logging.WARNING` naming the peer version, the
implemented major, and the namespace — operators reading the log can grep
for the skip rather than debugging absence.

`capability_declaration(...)` emits a `logging.WARNING` when a role name
is outside v0.1's known set (without misattributing the unknown role to
its transports), and when a transport string is outside v0.1's known set
for a known role. Both are informational only — the spec's role and
transport sets are both open per §17 — but typos surface in logs.

## Conformance test harness

Every vendored `valid/<kind>/*.json` fixture goes through both
`validate_wire(..., kind=<kind>)` (must not raise) and `Model.model_validate(...)`
(must not raise) — the agreement parametrisation is the load-bearing
safety net. Every vendored `invalid/<kind>/*.json` fixture goes through
`validate_wire(..., kind=<kind>)` (must raise `WireFormatError`).

A sanity test guards against the silent-no-op failure mode where a future
spec amendment ships zero fixtures for a kind: each (kind × bucket) must
have at least one fixture.

Outbound roundtrip tests cover all four wire kinds (handle, ticket,
capability, error), including a handle containing a `DownloadSource`
(datetime field) and a ticket containing an `UploadSink` (method enum +
datetime) — `model_dump(mode="json")` must produce schema-valid output.

A drift-detection test asserts that the schema's
`UnknownTransportDescriptor.not.enum` and Pydantic's
`_ALL_KNOWN_DESCRIPTOR_TRANSPORTS` stay in lockstep. A future spec
amendment adding a transport must update both sites or this test fails.

## Vendoring sync

`scripts/sync_file_exchange_spec.py` is the sole gate on the vendored
artifacts. `--check` (CI default) fetches upstream at the pinned SHA and
diffs against vendored copies; exit code 1 on any drift. `--bump <sha>`
rewrites the constant + all vendored files; the `re.fullmatch(40-hex)`
guard refuses anything that isn't a full SHA.

Network handling:

- Honors `GITHUB_TOKEN` (env var, auto-provided by GitHub Actions) so the
  GitHub API calls escape the 60/hr anonymous rate limit. CI sets it via
  `secrets.GITHUB_TOKEN`; local invocations without the token skip the
  one network-marked test (`@pytest.mark.network`).
- Explicit `resp.status != 200` check after `urlopen` — some
  200-with-error-body cases (rate-limit interstitials) slip past
  urlopen's built-in non-2xx raising.
- Empty-body and non-list-response guards for the same reason.
- `_NETWORK_ERRORS` is a specific tuple (`URLError`, `HTTPError`,
  `TimeoutError`, `OSError`, `json.JSONDecodeError`, `RuntimeError`) —
  not a bare `Exception` — so a parsing bug or API-shape drift
  surfaces with its real traceback rather than being misreported as a
  fetch failure. `RuntimeError` is included because `_fetch` and
  `_list_remote_dir` raise it deliberately on HTTP non-200, empty
  bodies, and non-list directory listings (the rate-limit-with-200
  case that slips past urlopen's built-in raising).
- Failure messages name the path + the exception class, so an operator
  can distinguish a transient network blip from a real upstream change.

A test pins the `_write_pin` regex against `_spec.py`'s current shape so
a future format change to that file can't make `--bump` silently no-op.

## Dependencies

`pydantic` (>= 2.7 — v2 callable Discriminator) and `jsonschema` (>= 4.18
— Draft 2020-12 validator) move from "transitive via fastmcp" to explicit
`dependencies =` entries in `pyproject.toml`. pvl-core code directly
imports both.

A new `network` pytest marker (in `pyproject.toml`'s
`[tool.pytest.ini_options]`) lets the GitHub-hitting sync-script
round-trip test skip when `GITHUB_TOKEN` isn't set.

## Out of scope of #139

- Selection algorithm (`#140` — uses `validate_wire` + `_check_requires`
  to surface the §13 error envelope).
- `_meta["nl.liesdonk.file-exchange/error"]` envelope wrapper (`#140`).
- `exchange://` URI canonicalize-and-confine (`#141`).
- Mechanism-agnostic byte hooks (`#142`).
- Filesystem / HTTPS transport implementations (`#143` / `#145` / `#146`).
- FastMCP server-side capability registration (`#148`).
