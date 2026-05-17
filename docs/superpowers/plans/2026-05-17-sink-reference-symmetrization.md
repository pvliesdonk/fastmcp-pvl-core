# Sink-Reference Symmetrization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> or `superpowers:executing-plans`. Design-style plan (matching the repo's prior
> file-exchange plans) — concrete changes per file, not bite-sized TDD micro-steps.

**Goal:** Complete file-exchange's source/sink symmetry — `fetch_file`'s `path`
parameter becomes the opaque `destination` reference, matching `create_upload_link`
and mirroring `origin_id` on the source side. Ships as file-exchange spec **v0.6**.

**Architecture:** One coherent change — the spec evolution
(`docs/specs/file-exchange.md` → v0.6) plus the matching pvl-core implementation
and tests — shipped as **one issue ([#114](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/114)), one PR**.

**Tech stack:** Python 3.10–3.13, `uv`, `pytest`, `ruff`, `mypy`. FastMCP.

Design doc: `docs/superpowers/specs/2026-05-17-sink-reference-symmetrization-design.md`.

---

## Context

The file-exchange *source* side has one reference name — `origin_id` — across
every source tool. The *sink* side has two: `fetch_file` takes `path` (still
filesystem-framed), `create_upload_link` takes `destination` (made an opaque
domain reference in v0.5). Same role, two names. This change unifies them:
`fetch_file`'s `path` → `destination`, one opaque sink-side domain reference,
with `SinkContext` exposing it as a first-class field. Scope boundary:
file-exchange's sink job ends at placement — domain postprocessing (e.g.
markdown-vault front matter) is downstream follow-up tooling, out of scope.

No downstream server has adopted file-exchange → the `path`→`destination` rename
is a clean breaking change with no compatibility alias.

## Part A — Spec evolution (`docs/specs/file-exchange.md` → v0.6)

`0.5 → 0.6` is a **minor** bump (changes a tool-contract parameter and its
validation regime). `0.4` was already permanently skipped; `0.6` follows `0.5`
normally.

### A1. Version field
`**Version:** 0.5.0` → `**Version:** 0.6.0`.

### A2. `http` consumer tool — the `path` parameter
In the `http` method's consumer-tool bullet (current text: *"It SHOULD accept
an optional parameter named `path` to allow client-directed placement. If
`path` is omitted or invalid, the consumer MUST auto-generate a safe local
path (e.g. derived from `origin_id` or a UUID)…"*):

- Rename the parameter `path` → `destination`.
- Replace the "safe local path" filesystem framing with the opaque-domain-reference
  definition — reuse the wording of the `create_upload_link` `destination` row:
  an opaque domain reference, validated against the minimal-safety floor
  (§"Security and Path Resolution"), which the consumer interprets per its own
  domain (directly as a path/slot, a handle minted by a downstream
  prepare-receive tool, or a — possibly parametrized — resource URI).
- Keep the omitted-→-auto-place fallback, generalised: *"If `destination` is
  omitted, the consumer MUST auto-place the bytes per its own domain (a
  domain-default location, a UUID-derived name, etc.). This prevents failures
  caused by LLMs hallucinating invalid directory structures."*

### A3. Consuming-server requirements
The `SHOULD … accept a parameter named `url` and an optional parameter named
`path`` bullet → `destination`. Keep the "if omitted, auto-place" clause.

### A4. Worked example / step list
Every reference to the consumer placement parameter `path` in the worked
example / negotiation step list → `destination`.

### A5. Capability `version` examples
`"version": "0.5"` → `"version": "0.6"` in the capability-declaration JSON
examples; the `version` field-table row `(e.g. "0.5")` / `0.5.0` → `0.6`.

**Do not touch** the `exchange://` URI section's `{id}.{ext}` text or
§"Security and Path Resolution" — those use "path" for genuine path components
and are unrelated. Only the consumer *tool parameter* is renamed.

## Part B — pvl-core implementation

### B1. `SPEC_VERSION` — `src/fastmcp_pvl_core/_file_exchange_protocol.py`
`SPEC_VERSION = "0.5"` → `SPEC_VERSION = "0.6"`.

### B2. `SinkContext` — `src/fastmcp_pvl_core/file_exchange.py` (~line 292)
Add a first-class `destination: str | None` field, directly after `origin_id`,
mirroring it:

```python
class SinkContext(NamedTuple):
    origin_id: str | None
    destination: str | None
    mime_type: str | None
    size_bytes: int | None
    file_ref: FileRef | None
    params: Mapping[str, Any]
    handle: FileExchangeHandle | None
```

Update the docstring: document `destination` as "the sink-side opaque domain
reference — the caller's placement handle for this consumer, when supplied;
`None` when the caller left placement to the consumer." Drop the `path` /
`destination` examples from the `params` field's docstring (it is now caller
*extras* only). `params` itself stays — removing it is out of scope ([#106](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/106)).

### B3. `fetch_file` — `file_exchange.py` (~line 968)
- Rename the parameter `path: str | None = None` → `destination: str | None = None`.
- Validate it: if `destination is not None`, call `validate_reference(destination,
  field="destination")` inside `try/except ExchangeURIError`; on failure return
  `{"error": "invalid_input", "message": str(exc)}` — `fetch_file`'s existing
  error envelope for malformed input (this is the concrete form of the design
  doc's "supplied-but-invalid is an error, not a silent fallback"; `fetch_file`
  reports bad input as `invalid_input`, distinct from `create_upload_link`'s
  `_upload_transfer_failed` — each tool keeps its own envelope).
- Stop putting the value into the `params` dict. Thread `destination` to the
  downstream consume helpers as a dedicated argument (see B4).
- Update the `fetch_file` docstring.

### B4. Thread `destination` to the `SinkContext` construction sites
`destination` must reach the three `SinkContext(...)` constructions. Add a
`destination: str | None` parameter to `_fetch_via_url`, `_fetch_via_file_ref`,
`_consume_exchange`, and `_consume_http`, threaded from `fetch_file` through to
the constructors. The `params` dict continues to be threaded unchanged (it is
now empty on the `fetch_file` path — that residue is [#106](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/106)'s concern, not this PR's).

The two `fetch_file`-side `SinkContext(...)` sites (`_consume_exchange` ~1193,
`_consume_http` ~1275) gain `destination=destination`.

### B5. `http_upload` receive — `_route_sink` (`file_exchange.py` ~1700)
`_route_sink` currently puts `record.destination` into `params["destination"]`.
Change: pass it as the first-class field — `SinkContext(origin_id=record.origin_id,
destination=record.destination, …)` — and drop the `params["destination"]`
assignment (`params` becomes `{}`).

## Part C — Tests

Run `uv run pytest -q` for a baseline first. Then under `tests/`:

- **Rename in existing tests:** every `fetch_file(..., path=...)` call →
  `destination=...`. Grep `tests/` for `path=` in file-exchange fetch tests and
  for `SinkContext` / `.params` assertions keyed on `path`.
- **`SinkContext.destination` populated:** assert the sink hook receives
  `ctx.destination` equal to the caller-supplied value — on the `fetch_file`
  `exchange` path, the `fetch_file` `http` path, and the `http_upload` receive
  path. Assert `ctx.origin_id` and `ctx.destination` are both first-class.
- **Omitted `destination`:** `fetch_file` with no `destination` → `ctx.destination
  is None`, transfer still succeeds (auto-place fallback intact).
- **Floor violation:** `fetch_file(url=..., destination="bad\x00id")` →
  `{"error": "invalid_input", ...}`; symmetric values (empty, edge whitespace,
  control char) rejected.
- **Capability version:** any test asserting the capability `version` is `"0.5"`
  → `"0.6"` (grep `tests/` for `"0.5"` and `SPEC_VERSION`).
- Any test that asserted the old `params["path"]` / `params["destination"]`
  routing is rewritten to assert the first-class field.

## Out of scope

- Domain postprocessing surface (front matter, tagging) — downstream follow-up
  tooling by design.
- Removing the now-vestigial `SinkContext.params` field / its threading —
  [#106](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/106) (SinkContext type-design).
- `ArtifactStore` → `DownloadStore` rename — [#113](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/113).

## Verification

```bash
uv sync --all-extras
uv run pytest
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```

End-to-end checks:
- `fetch_file` accepts `destination` and no longer accepts `path`
  (`grep -n "def fetch_file" -A6 src/fastmcp_pvl_core/file_exchange.py`).
- `SinkContext` has both `origin_id` and `destination` as first-class fields;
  `destination` is populated on the `fetch_file` (`http`/`exchange`) and
  `http_upload` sink paths.
- `grep -rn '"0.5"' src/` shows no surviving file-exchange spec-version literal;
  `SPEC_VERSION == "0.6"`; `docs/specs/file-exchange.md` is at v0.6.0.
- A floor-violating `destination` on `fetch_file` yields `invalid_input`; an
  omitted `destination` triggers the auto-place fallback.

## Issue + PR

Issue [#114](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/114) is filed.
One PR carries Parts A+B+C (`Closes #114`). Branch `feat/sink-reference-symmetrization`
already exists with the design doc + this plan committed. Run `preflight-circus`
on the full diff before opening as draft.
