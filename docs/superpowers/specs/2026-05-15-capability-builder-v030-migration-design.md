# Design: capability builder → v0.3.0 `source`/`sink` shape (issue #86)

**Status**: approved (brainstorm 2026-05-15)
**Issue**: [pvliesdonk/fastmcp-pvl-core#86](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/86)
**Umbrella**: #75
**Blocks**: #74 (the upload tool/route rebuild lands on the migrated builder)

## Problem

#83 (PR #84, merged) changed the file-exchange spec's capability declaration:
tool-based transfer methods declare their tool(s) under `source` / `sink`
role sub-objects, and `http` / `http_upload` are separate top-level method
keys. The implementation has not caught up. Three concrete gaps:

1. **Wrong capability shape.** `_FileExchangeCapabilityBuilder._build_http_block`
   (`src/fastmcp_pvl_core/_file_exchange_protocol.py`) emits the pre-#83
   v0.4-amendments shape — a single `transfer_methods["http"]` block with
   nested `download` / `upload` sub-keys:

   ```json
   "http": {"download": {"tool": "..."}, "upload": {"tool": "...", "accepts": [...], "max_bytes": ..., "max_ttl_seconds": ...}}
   ```

   The v0.3.0 shape is two top-level keys, each role-keyed:

   ```json
   "http": {"source": {"tool": "..."}, "sink": {"tool": "..."}},
   "http_upload": {"sink": {"tool": "...", "accepts": [...], "max_bytes": ..., "max_ttl_seconds": ...}}
   ```

2. **Stale version.** `SPEC_VERSION = "0.4"` (`_file_exchange_protocol.py:36`)
   advertises a spec version that the spec doc itself now declares
   permanently skipped. The published spec is `0.3`.

3. **Dual-role collapse bug.** `register_file_exchange` registers the
   producer tool (`create_download_link`) when `produce`, **else** the
   consumer tool (`fetch_file`) when `consume` — an `if`/`elif`
   (`file_exchange.py:777-787`). The builder stores whichever one in a
   single `_download_tool` field. A server that both produces and consumes
   only ever advertises the producer side; the consumer `fetch_file` tool
   is never declared. This is the same dual-role gap #83 fixed at the spec
   level — still live in the implementation.

This is the precursor to #74: #74 rebuilds the `create_upload_link` tool
contract and the POST route, and should land on a builder that already
emits the correct capability shape.

## The design

### A. Capability builder rework (`_file_exchange_protocol.py`)

Replace the single `_download_tool` field with separate role tracking, and
emit `http_upload` as its own top-level method key.

New builder API (internal — pvl-core owns it; no downstream surface):

- `set_http_source(tool_name: str)` — records the `http` producer tool.
- `set_http_sink(tool_name: str)` — records the `http` consumer tool.
- `set_http_upload_sink(tool_name: str, max_bytes: int, max_ttl_seconds: int, accepts: tuple[str, ...] | None)`
  — records the `http_upload` receiver tool plus its admission metadata.

These replace the current `set_download` / `set_upload`. Backing fields
become `_http_source_tool`, `_http_sink_tool`, `_http_upload_sink_tool`
(plus the upload metadata fields, unchanged).

`build()` materialises `transfer_methods` as:

- `transfer_methods["exchange"] = {}` when the exchange method is present
  (unchanged).
- `transfer_methods["http"]` — a dict with `source` and/or `sink` sub-keys,
  whichever role(s) were registered. Omitted entirely if neither.
- `transfer_methods["http_upload"]` — `{"sink": {...}}` when the upload
  receiver tool was registered. Omitted otherwise.

`_build_http_block` is replaced by two private helpers
(`_build_http_block` returning the `{source?, sink?}` dict, and
`_build_http_upload_block` returning `{sink: {...}}`), or one helper that
returns both — an implementation detail for the plan.

### B. Call-site fix (`register_file_exchange`, `file_exchange.py:777-787`)

Change the `if produce / elif consume` to two independent `if`s:

```python
if produce and store is not None and store.has_base_url:
    builder.set_http_source(tool_name=_DEFAULT_DOWNLOAD_TOOL)
if consume:
    builder.set_http_sink(tool_name=_DEFAULT_FETCH_TOOL)
```

A produce-and-consume server now advertises both `http.source` and
`http.sink`. The `register_file_exchange_upload` call site
(`file_exchange.py:1783`) switches `set_upload(...)` → `set_http_upload_sink(...)`.

### C. Version + dead kwarg

- `SPEC_VERSION` `"0.4"` → `"0.3"` (`_file_exchange_protocol.py:36`). The
  capability `version` field is `MAJOR.MINOR` per the spec, so `"0.3"` is
  the correct wire value. `FileExchangeCapability.version` already defaults
  to `SPEC_VERSION`.
- Remove `legacy_capability_shape` entirely:
  - the builder field (`_file_exchange_protocol.py:468`),
  - the legacy branch in `_build_http_block` (`:526-536`),
  - the `"0.2"`-vs-`SPEC_VERSION` version ternary in `build()` (`:515`),
  - the kwarg on `register_file_exchange` (`file_exchange.py:143`) and
    `register_file_exchange_upload` (`:1554`),
  - the first-caller-wins mismatch logic in `_get_or_create_builder`
    (`file_exchange.py:157-206`).

  It is a shape-override kwarg — it fails the #72/#73 classification test
  (pvl-core owns capability shape; downstream has no domain basis to pick a
  different one). The v0.2 flat shape it served is retired: a v0.2 reader
  meeting a v0.3 declaration is handled by the spec's own cross-version
  compatibility rules, not by pvl-core emitting an old shape on request.

### D. Tests

`test_file_exchange_capability_merge.py` and any other test asserting the
old `http: {download, upload}` shape or `version: "0.4"` are rewritten to
assert the v0.3.0 shape. New assertions:

- A produce-only server advertises `transfer_methods.http.source` and no
  `http.sink`.
- A consume-only server advertises `transfer_methods.http.sink` and no
  `http.source`.
- A **produce-and-consume** server advertises **both** `http.source` and
  `http.sink` — the dual-role regression guard.
- An upload receiver advertises `transfer_methods.http_upload.sink` with
  `tool` / `accepts` / `max_bytes` / `max_ttl_seconds`, and no
  `http.upload` key survives anywhere.
- The capability `version` field is `"0.3"`.

## Transfer-mechanism coverage

The implementation surface for transfer mechanisms is a matrix: `exchange`
has no tool roles; `http` and `http_upload` each have a `source` and a
`sink` role — four role-implementations in total. This design records the
matrix so the umbrella's remaining work is explicit and #74/#85 are not
mistaken for the whole job.

| Method / role | Tool(s) | pvl-core helper | Tool + route conformance | Capability shape |
|---|---|---|---|---|
| `exchange` | none (shared volume) | `register_file_exchange` | n/a — no tools | #86 (`{}`) |
| `http.source` | `create_download_link` | `register_file_exchange` (produce) | **not audited** — see below | #86 |
| `http.sink` | `fetch_file` | `register_file_exchange` (consume) | **not audited** — see below | #86 |
| `http_upload.sink` | `create_upload_link` + POST route | `register_file_exchange_upload` | #74 (clean-slate rebuild) | #86 |
| `http_upload.source` | `upload` | (new helper) | #85 | #86 (when the helper lands) |

**#86 migrates the capability *shape* for every method/role at once** —
that is its whole job, and it is complete for the shape axis.

**The `http` row's tool-and-route conformance is not audited.** `http` is a
v0.2.x method; v0.3.0 added `http_upload` and #83 changed only the
capability shape — neither touched `http`'s tool contracts or route
mechanics. So `create_download_link` / `fetch_file` and the download-serving
route are *presumed* conformant. But "presumed" is not "verified": the
download path was last substantially touched in the A11-amendments era, and
this design's own discovery turned up loose ends (the upload POST route is
the only `custom_route` in `_file_exchange_runtime.py` — where and how
download bytes are served was not traced). #74 gives `http_upload` a
clean-slate conformance rebuild; `http` has no equivalent.

**Recommendation:** file an `http`-direction conformance-audit issue under
#75 — verify `create_download_link` / `fetch_file` tool contracts and the
download-serving route against the v0.3.0 spec, the same way #74 audits
`http_upload`. It may close as "already conformant"; the point is to make
that a verified outcome rather than an assumption. This design does not
depend on the audit — #86 ships the shape migration regardless — but the
umbrella is not complete until the `http` row is verified.

## Backward compatibility

- **No v0.3-flat or v0.4 peer to protect.** No deployed server advertises a
  published `0.3`/`0.4` capability that this changes incompatibly — the
  `"0.4"` constant was an internal stale value, never a released spec
  version. The capability output simply becomes spec-conformant.
- **v0.2.x peers** — a v0.2.x server emitting the flat `http: {tool}` shape,
  or a v0.2.x reader meeting this server's v0.3.0 declaration, is covered by
  the spec's §"Versioning and compatibility" cross-version rules (ignore
  unrecognised fields, tolerate missing optional fields). pvl-core does not
  emit an old shape on request; that is what removing `legacy_capability_shape`
  finalises.

## Out of scope

- **The `create_upload_link` tool contract** — `target_id` → `origin_id` /
  `destination`, the response field renames (`upload_url` → `url`,
  `expires_in_seconds` → `ttl_seconds`, the mandatory `max_bytes`), the
  `extra` parameter. → #74.
- **The POST upload route** — `410` → `404` collapse, status-code classes.
  → #74.
- **The sender-side `upload` tool** (`http_upload.source`). → #85.
- **The `http`-direction tool/route conformance audit.** → recommended new
  issue (see "Transfer-mechanism coverage").

#86 is the capability-declaration **shape and version** migration only.

## Acceptance

- [ ] `transfer_methods` is emitted in the v0.3.0 `source`/`sink` shape for
  `http` and `http_upload`; `exchange` stays `{}`.
- [ ] A produce-and-consume server advertises both `http.source` and
  `http.sink`.
- [ ] `SPEC_VERSION` is `"0.3"`; the capability `version` field is `"0.3"`.
- [ ] `legacy_capability_shape` is gone from the codebase (builder, helpers,
  merge logic).
- [ ] Capability tests assert the v0.3.0 shape, including the dual-role
  guard.
- [ ] `register_file_exchange` / `register_file_exchange_upload` public
  behaviour is otherwise unchanged — #86 touches capability emission only.
