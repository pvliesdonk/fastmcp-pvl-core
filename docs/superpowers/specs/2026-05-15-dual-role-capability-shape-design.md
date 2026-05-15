# Design: dual-role capability shape (issue #83)

**Status**: approved (brainstorm 2026-05-15)
**Issue**: [pvliesdonk/fastmcp-pvl-core#83](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/83)
**Umbrella**: #75
**Blocks**: #74 (the upload re-implementation — its capability-builder rework targets this corrected shape)

## Problem

The file-exchange spec's capability declaration uses `transfer_methods.<method>` blocks shaped `{tool: <name>, ...}` — one `tool` value per method key. A bidirectional transfer method has two roles, each with its own tool. A server implementing **both roles** cannot advertise both tools under the single key.

- **`http` (download):** producer role (`create_download_link` — mints a download URL) + consumer role (`fetch` — pulls bytes from a URL). A server that both stores files and serves them back is both — and that is the *normal* case, not an exotic one. The v0.2.5 producer example shows `http: {tool: "create_download_link"}`, the consumer example shows `http: {tool: "fetch"}`, and the both-roles case is never addressed. `http`'s two roles also have no field-presence discriminator — both are bare `{tool: <name>}`.
- **`http_upload` (v0.3):** receiver role (`create_upload_link`) + sender role (`upload`). The v0.3 spec section already documents this limitation and notes "a future spec version MAY introduce explicit sub-keying." This design *is* that future.

Surfaced during the #74 brainstorm — the first attempt to implement the v0.3 spec.

## Version framing — fix in place, v0.4 skipped

**v0.3.0 was never published or implemented.** It is a spec-*document* state (merged via PR #82) with zero implementations; no code advertises `version: "0.3"`. The current deployed pvl-core still carries a stale `SPEC_VERSION = "0.4"` constant and the reverted A11 nested shape — an impl/doc mismatch #74 was always going to correct.

Because v0.3.0 has no implementations, this dual-role bug is a **pre-first-implementation correction**, not a post-release amendment:

- **The capability shape is corrected in place; the spec version stays `0.3.0`.** There is no interop to protect — no v0.3 peers exist. The spec's "no inline amendments to a published version" rule targets *implemented* versions; v0.3.0 has none. The corrected v0.3.0 doc supersedes the transient flat-`http` v0.3.0 doc from PR #82.
- **v0.4 is permanently skipped.** The `0.4` label was used by the reverted A11 amendment set (#76 / #77) and by the stale `SPEC_VERSION` implementation constant. Reusing it would resurrect a "which 0.4 — A11-nested or source/sink?" ambiguity. The spec will state explicitly that the next minor release after 0.3 is `0.5`.

## The design

### Capability shape — `source` / `sink` role sub-objects

Every tool-based transfer method declares its tool(s) under role sub-objects:

```json
"transfer_methods": {
  "exchange": {},
  "http": {
    "source": {"tool": "create_download_link"},
    "sink":   {"tool": "fetch"}
  },
  "http_upload": {
    "source": {"tool": "upload", "source_variants": ["path", "exchange_uri", "http_url", "inline_b64"]},
    "sink":   {"tool": "create_upload_link", "accepts": ["*/*"], "max_bytes": 10485760, "max_ttl_seconds": 3600}
  }
}
```

### `source` / `sink` role model

`source` / `sink` are **data-direction roles**, unified across every tool-based transfer method:

- **`source`** — the endpoint the bytes originate from / are served from.
  - `http`: the producer (mints the download URL via `create_download_link`).
  - `http_upload`: the sender (POSTs bytes via `upload`).
- **`sink`** — the endpoint the bytes land at.
  - `http`: the consumer (pulls via `fetch`).
  - `http_upload`: the receiver (mints the upload URL via `create_upload_link`).

The model deliberately makes one asymmetry explicit: in `http` the **source** mints the URL; in `http_upload` the **sink** mints it. The role names track *data direction*, not who-mints — which is what a client orchestrating a transfer reasons about. The mechanics (who mints) are defined per method in the spec body.

A server populates whichever sub-keys it implements: a both-roles server has both `source` and `sink`; a single-role server has one. **Role detection = sub-key presence** — never the tool-name string (consistent with the v0.3 principle that tool names are implementation-defined).

### Structural rule (new spec prose)

The three method blocks have different shapes — `exchange: {}`, `http` with tools only, `http_upload` with tools plus admission-policy metadata. This variation is **principled**, and the spec states the rule so it reads as design, not drift:

> Every *tool-based* transfer method declares its tool(s) under `source` / `sink` role sub-objects. Within each role sub-object, `tool` is the one mandatory field; any further fields are method-specific metadata a caller needs up front. `exchange` is the sole *tool-less* method — it carries `{}`.

Why the shapes legitimately differ:

- **`exchange: {}`** — no tools at all. `exchange` is shared-filesystem convention; the producer writes a file into the volume, the consumer resolves an `exchange://` URI to a local path and reads directly. No MCP tool is invoked. The one thing it needs — which exchange group — is the top-level `exchange_id` field. Every participant is implicitly both source and sink.
- **`http`: tools only** — the download direction has no receiver-side admission policy. The file already exists; its negotiable properties (MIME type, size) travel in the `file_ref`. `create_download_link` serves bytes that already exist; it does not admit or reject. So `http` has nothing to advertise beyond the two tool names.
- **`http_upload`: tools + metadata** — the upload direction *does* have admission policy. The receiver has real up-front constraints (`max_bytes`, `max_ttl_seconds`, `accepts`) a sender must know before POSTing, or it wastes a large request on a `413`/`415`. `source_variants` is the symmetric sender-side declaration. None of it is arbitrary; it is exactly the metadata the push direction negotiates and the pull direction does not.

### `file_ref` `transfer` object — unchanged, stays flat

The `file_ref` `transfer` object (e.g. `transfer.http: {tool: "create_download_link"}`) is **not** changed. It is producer-emitted, per-file, and inherently source-side — it advertises how to get *one specific file*. It has no dual-role problem and needs no `source`/`sink` nesting.

`transfer_methods` (capability declaration — server-wide, multi-role) and `transfer` (file_ref — per-file, single-role) are different objects with different shapes. The spec states this distinction explicitly so the asymmetry reads as design.

`http_upload` never appears in a `file_ref` `transfer` object at all (per #71 — it is push-direction, not file-reference-based).

## Backward compatibility

v0.3.0 is fixed in place; the version stays 0.3.0. Consequences:

- **No "v0.3 flat" peer exists.** The flat-`http` v0.3.0 doc from PR #82 was a transient document state with zero implementations. The corrected v0.3.0 doc supersedes it. There is no v0.3-flat ↔ v0.3-nested compatibility surface.
- **The only older backward-compat surface is v0.2.x** — download-only, flat `http: {tool}`, no `http_upload`. The spec's existing §"Versioning and compatibility" cross-version rules already cover a v0.3.0 reader meeting a v0.2.x declaration ("ignore unrecognised fields, tolerate missing optional fields, attempt transfer with whatever methods are mutually understood").
- **One small piece of new prose:** a reader encountering a flat `http` block (`{tool: ...}`, no `source`/`sink` — as a v0.2.x server emits) treats it as a single-role declaration, inferring the role from the peer's `produces`/`consumes` (non-empty `produces` → the flat tool is source-side; non-empty `consumes` → sink-side). This makes the v0.2.x-compat path explicit. That is the entire compatibility addition — one sentence.
- **The A11-era `"0.4"` nested shape** that deployed servers currently advertise is an *implementation* artifact, never a published spec. The spec document owes it nothing. The transition from A11-nested deployments to corrected-v0.3.0 deployments is an implementation/rollout matter, handled by #74 and the downstream migration issues.

## Spec-document edit map

All edits to `docs/specs/file-exchange.md`; **version stays `0.3.0`**:

1. **§"Transfer Methods"** — new structural-rule prose (the `source`/`sink` rule + the tool-less-`exchange` exception).
2. **§"Transfer Methods / `http`"** — producer/consumer capability examples → nested `{source: {tool}, sink: {tool}}`.
3. **§"Transfer Methods / `http_upload`"** — receiver/sender capability examples → nested `{source: {...}, sink: {...}}`. Rewrite the "a single server cannot express both roles / a future version MAY introduce sub-keying" paragraph — this design *is* that future; the paragraph now presents `source`/`sink` as the answer. "Role identified by field presence" → "role identified by sub-key presence."
4. **§"Discovery / Capability declaration"** — the producer/consumer worked examples → nested; the `transfer_methods` field-description in the capability table updated.
5. **§"Transfer Negotiation / Step 1: Method selection"** — the intersection algorithm becomes role-aware: matching a file_ref's `transfer.http` (producer source-side) against a consumer's `transfer_methods.http.sink`. The post-#82 "restricted to pull-direction methods" wording is updated for the role-keyed lookup.
6. **§"Versioning and compatibility"** — two additions: (a) the v0.2.x flat-form reading note (one sentence, above); (b) the v0.4-skipped note: "the next minor release after 0.3 is `0.5`; `0.4` is permanently skipped — the label was used by an earlier reverted set of inline amendments and by a stale implementation constant, and reusing it would be ambiguous."
7. **`file_ref` `transfer` object** — an explicit sentence (in §"File Reference" or §"Transfer Methods") distinguishing `transfer` (file_ref, flat, single-role) from `transfer_methods` (capability, role-keyed, multi-role).

No Python in this PR — spec-only, like #71. The capability-builder rework lands in #74 against this corrected spec.

## Out of scope

- **Implementation** — the capability-builder rework, `SPEC_VERSION` realignment, the upload helper — all land in #74 against this corrected spec.
- **Method mechanics** — URL minting, the POST contract, status codes, tool contracts are unchanged. #83 is purely the capability-declaration *shape*.
- **The `file_ref` `transfer` object** — stays flat; explicitly documented as such, but not restructured.

## Acceptance (from #83)

- [ ] Spec PR: capability declaration expresses dual-role servers for `http` and `http_upload` via `source`/`sink` sub-objects. Merged into `docs/specs/file-exchange.md`.
- [ ] Spec version stays `0.3.0` (fix-in-place); §"Versioning and compatibility" states v0.4 is permanently skipped.
- [ ] `file_ref` `transfer` object explicitly documented as staying flat.
- [ ] Implementation tracked in #74; #74 is blocked on this issue.
