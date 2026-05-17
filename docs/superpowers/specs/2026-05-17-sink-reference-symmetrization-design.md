# Design: symmetrize the file-exchange sink-side reference (`path` → `destination`)

**Issue:** [#114](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/114)
**Date:** 2026-05-17
**Spec target:** file-exchange v0.6

## Problem

file-exchange's transfer surface has a *source* side and a *sink* side. The
source side, after v0.5, has exactly one reference name — `origin_id` — used
by every source tool (`create_download_link`, the `upload` sender,
`make_file_ref`). It is an opaque domain reference: the producing server
interprets it however its domain dictates; pvl-core never parses it.

The sink side has **two names for one concept**:

| Sink tool | Transfer method | Sink-side reference | State |
|---|---|---|---|
| `fetch_file` | `http`, `exchange` | `path` | filesystem-framed — spec says "auto-generate a safe **local path**" |
| `create_upload_link` | `http_upload` | `destination` | v0.5-opaque — "path, slot, parent document key, MCP resource URI, anything" |

Both parameters carry the identical role: *where and how this consuming
server stores the received bytes, interpreted by its own domain*. The only
difference between them is the transfer mechanism (pull vs. pushed-in) — and
mechanism detail must not leak into a reference's name or shape.

The v0.3 dual-role pass and the v0.5 `origin_id` ⇄ `destination` symmetry
pass each *asserted* source/sink symmetry without auditing `fetch_file`'s
`path`. It slipped through both. `SinkContext`'s own docstring records the
split outright: "`params`: Caller-supplied parameters (e.g. `path` on
`fetch_file`, `destination` on an upload)".

## Goal

Complete the symmetry: the sink side has **one** opaque domain reference,
`destination`, mirroring `origin_id` on the source side — one name, one
shape, across both sink tools.

This is a focused spec evolution (v0.5 → v0.6). It is not a new feature; it
finishes a job the prior two revisions left incomplete.

## Design

### The reference

`fetch_file`'s `path` parameter becomes `destination`. `create_upload_link`
keeps `destination` unchanged. Both are then governed by one definition,
identical to `origin_id`'s v0.5 definition but sink-side:

- **Opaque domain reference.** The consuming server interprets it however its
  domain dictates; pvl-core never parses, splits, or derives structure from
  it.
- **Validated against the minimal-safety floor only** — non-empty, no null
  bytes, no control characters U+0000–U+001F, no leading/trailing whitespace
  (the existing `validate_reference`). No path-segment rules.
- **Optional (`MAY`).** If omitted, the consumer auto-places — the existing
  `fetch_file` fallback carries over, generalised from "auto-generate a safe
  local path" to *the consumer auto-places per its own domain* (a
  domain-default location, a UUID-derived name, etc.; the rationale —
  "prevents LLMs hallucinating invalid directory structures" — is unchanged).
  If `destination` is *supplied but* fails the minimal-safety floor,
  `fetch_file` returns its `invalid_input` error envelope (the tool's existing
  shape for malformed input) — surfaced, not a silent fallback.

The spec enumerates, non-normatively, the range of valid downstream
interpretations — the same freedom `origin_id` has on the source side:

- interpreted **directly** — e.g. as a path or storage slot;
- a **handle minted by a downstream "prepare-receive" domain tool** (the
  sink-side dual of a producer's domain tool minting an `origin_id`);
- a **(possibly parametrized) resource URI**.

pvl-core picks none of these — the consuming server does.

### `SinkContext`

`destination` becomes a first-class `SinkContext` field, symmetric with the
existing first-class `origin_id` field. It is populated on every sink path —
the `fetch_file` consume path (`http` and `exchange`) and the `http_upload`
receive path. Today the sink reference reaches the sink hook only through the
generic `params` dict; after this change `SinkContext` carries both
references explicitly:

- `origin_id` — the *source-side* reference (the producer's handle), when the
  call carries one.
- `destination` — the *sink-side* reference (this consumer's placement
  handle), when the caller supplied one.

`params` reverts to what its name says — caller extras — and its docstring
stops citing `path`/`destination` as examples.

(The `SinkContext` type is also the subject of [#106](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/106),
type-design hardening. The `destination` field is intrinsic to *this*
symmetrization and belongs here; #106 covers separate hardening.)

### Scope boundary

file-exchange's sink job ends at: *bytes received; the sink hook stored them
at and according to `destination`.* Anything domain-rich beyond placement —
markdown-vault-mcp stamping YAML front matter onto a received document, for
instance — is **postprocessing performed by separate follow-up tools**, off
file-exchange's surface.

This boundary is why a fourth option once floated — domain-specific keyword
arguments appended to `fetch_file` — stays rejected: it would re-domain-couple
the shared tool surface and collapse the opacity of the reference. A consumer
that needs to act on richer per-receive metadata either encodes it into the
opaque `destination` (via its own prepare-receive tool) or applies it
afterward with its own domain tools.

The markdown-vault front-matter use case motivated this work but is **not a
deliverable of it**: the symmetrization unblocks it (markdown-vault may mint a
`destination` that encodes front-matter intent, or postprocess after receipt
— its choice), and requires no further pvl-core surface.

## Spec edits (`docs/specs/file-exchange.md` → v0.6)

- **`http` consumer tool** (the `Consumer tool MUST accept …` bullet): the
  optional placement parameter `path` → `destination`. Replace the
  "auto-generate a safe local path" filesystem framing with the
  opaque-domain-reference definition — reuse the wording of the
  `create_upload_link` `destination` row. Keep the omitted → auto-place
  fallback.
- **Consuming-server requirements** section: the `SHOULD … accept … `path``
  bullet → `destination`.
- **Worked example / step list**: every reference to the consumer placement
  parameter `path` → `destination`.
- **Version** field `0.5` → `0.6`. This changes a tool-contract parameter and
  its validation regime, which per the spec's own §"Versioning and
  compatibility" bump-trigger checklist warrants a minor bump.

The `exchange://` URI section's `{id}.{ext}` text and §"Security and Path
Resolution" already use the word "path" for genuine path components — those
are unrelated and unchanged. Only the consumer *tool parameter* is renamed.

## Implementation (pvl-core)

- `fetch_file` tool: rename the `path` parameter to `destination`; validate it
  with `validate_reference` (the minimal-safety floor), exactly as
  `create_upload_link` already validates its `destination`.
- `SinkContext`: add `destination: str | None`; populate it in
  `_consume_exchange`, `_consume_http`, and the `http_upload` receive route.
  Update the `SinkContext` docstring; drop the `path`/`destination` examples
  from the `params` field doc.
- `SPEC_VERSION` → `"0.6"`; update spec-version literals/examples.
- The `fetch_file` auto-place fallback logic is retained; it now keys off
  `destination` being omitted.

## Breaking change

`fetch_file`'s `path` → `destination` is a tool-parameter rename. No
downstream server has adopted file-exchange, so the cost is nil — it ships as
a direct replacement with no compatibility alias, consistent with how v0.5's
breaking changes shipped.

## Out of scope

- Any domain postprocessing surface (front matter, tagging, etc.) — that is
  downstream follow-up tooling by design.
- Broader `SinkContext` type-design hardening — [#106](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/106).
- The `ArtifactStore` → `DownloadStore` rename — [#113](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/113).

## Verification

- `fetch_file` accepts `destination` and no longer accepts `path`.
- `SinkContext.destination` is populated on the `fetch_file` (`http` /
  `exchange`) and `http_upload` sink paths; `SinkContext.origin_id` and
  `SinkContext.destination` are both first-class fields.
- The spec's consumer-tool surface has no placement parameter named `path`;
  `docs/specs/file-exchange.md` is at v0.6 and `SPEC_VERSION == "0.6"`.
- A supplied `destination` that violates the minimal-safety floor yields
  `fetch_file`'s `invalid_input` error envelope; an omitted `destination`
  triggers the auto-place fallback.
- Full suite + `ruff` + `mypy` green.
