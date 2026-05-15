# Design: strip the http_upload sender `source` union from the spec (issue #93)

**Status**: approved (brainstorm 2026-05-15)
**Issue**: [pvliesdonk/fastmcp-pvl-core#93](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/93)
**Umbrella**: #75
**Blocks**: #85 (the sender-side `http_upload` implementation)

## Problem

The v0.3.0 file-exchange spec's §"Sender-side tool: `upload`" defines the
tool's `source` parameter as a tagged union — `{path}` / `{exchange_uri}`
/ `{http_url}` / `{inline_b64}` — with a companion `source_variants` field
in the `http_upload.source` capability sub-object and an
`unsupported_source_variant` error code.

This is an implementation detail that leaked into the wire spec. The
`source` union prescribes *how a sender locates the bytes it is about to
push* — which is the sender server's own domain, not a wire-format interop
concern. It is the same class of defect the #75 umbrella exists to undo.

## The corrected model

The protocol already has the right pattern, and uses it for the
counterpart operation.

`create_download_link` is the `http` method's **source-side** operation:
the client/LLM says "share this resource" and passes `origin_id` — an
**opaque handle the producing server resolves itself**. The spec is
explicit (§"File Reference", `origin_id` row): the producer "MAY interpret
it as a path, document id, image id, HMAC token, or any other
internally-meaningful handle; clients and consumers MUST treat it as
opaque." The protocol never learns whether the resource is a file, an
in-memory image, or a database row.

`http` and `http_upload` are simply two byte-transfer mechanisms.
`create_download_link` and `upload` are *both* the source-side "share this
resource" MCP operation — `create_download_link` shares the resource by
minting a pull URL, `upload` shares it by pushing to a receiver-issued
URL. They must have the **same input model**: an opaque, server-resolved
`origin_id`. The `source` tagged union was a second, wrong way of saying
"where are the bytes" that only `http_upload` was saddled with.

The fix is to *replace* the `source` union with `origin_id`, not merely
delete it. The `upload` tool stays normative spec — standardising
per-method tool parameter names is explicitly the spec's job (§"Design
Decisions / Standardised parameter names per method"). Only the resolution
*mechanics* were the leak.

## The design

### 1. The `upload` tool parameter table

§"Sender-side tool: `upload`" — the parameter table becomes:

| Param | Cardinality | Description |
|---|---|---|
| `url` | MUST | The receiver-issued POST endpoint (returned from `create_upload_link`). |
| `origin_id` | MUST | The sender's opaque stable handle for the bytes to push. Same opaque-handle semantics and segment rules as the `http` method's `create_download_link` `origin_id` (§"Security and Path Resolution"). The sender resolves it to bytes by its own domain logic; callers treat it as opaque. |
| `content_type` | SHOULD | The MIME type the sender declares in the POST `Content-Type` header. If omitted, the sender SHOULD sniff or default. |

The `source` tagged-union row is removed.

The tool's return — `{status, body}` — and the rule that a `4xx` carrying
a `transfer_failed` envelope SHOULD be unwrapped and re-raised as a tool
error are unchanged: those are the wire/error contract, correctly placed.

### 2. Remove `unsupported_source_variant`

The entire `unsupported_source_variant` paragraph and its JSON envelope
example are deleted. With no variants, there is no variant to mismatch on,
so the error code has nothing to describe.

### 3. Capability shape — remove `source_variants`

The `http_upload.source` capability sub-object becomes `{"tool": "upload"}`
— structurally identical to `http.source` and every other tool-based role.

- §"Transfer Methods / `http_upload`" — the sender-only example and the
  both-roles example drop `source_variants`.
- The "Fields within each role sub-object" list — the `source_variants`
  bullet is removed.
- The paragraph that disambiguates "the `source` role sub-key" from "the
  `source` *parameter* on the `upload` tool (the tagged-union payload
  variant)" is **deleted entirely** — with the `source` tool parameter
  gone, there is no longer a name collision to disambiguate.

§"Discovery" carries no sender-side capability example, so its worked
examples need no change.

### 4. The worked example

§"Transfer Methods / `http_upload`" → "Worked example — MCP-mediated push"
— step 2's `mover.upload` call changes from
`{"url": ..., "source": {"exchange_uri": "exchange://..."}, "content_type": ...}`
to `{"url": ..., "origin_id": "<mover's resource handle>", "content_type": ...}`.
The "agent push" example is a raw `curl` POST and is unaffected.

### 5. Security and Path Resolution

The `upload` tool's `origin_id` is governed by the existing `origin_id`
segment rules (§"Security and Path Resolution"). The `upload` parameter
table's `origin_id` row references those rules, exactly as the
`create_upload_link` `origin_id` row already does ("Same rules as
`origin_id` in the `http` method's `create_download_link`"). No change to
§"Security and Path Resolution" itself is required.

### What does NOT change

- The `http_upload` **wire contract** — the sender POSTs raw bytes plus a
  `Content-Type` header to the receiver-issued URL; the URL-token rules;
  the status-code class table; the `transfer_failed` envelope. This was
  always correct.
- The receiver side (`create_upload_link`, the POST contract,
  `http_upload.sink`) — entirely untouched.
- The `http` and `exchange` methods.

## Version framing — fix in place, stays 0.3.0

The spec version stays `0.3.0`. The file-exchange spec is a **pre-release
draft**: `**Status:** experimental`, internal to this repo, with no spec
release or tag and no external consumer implementing a released spec
version. The `source` union, `source_variants`, and the sender `upload`
tool have **zero implementations** anywhere — the sender side is exactly
what #85 will build, blocked on this fix. #74 shipped the v0.3.0
*receiver*, which this change does not touch (the receiver contract —
`create_upload_link`, the POST route, `http_upload.sink` — is unchanged),
so no implementation needs realignment and the `SPEC_VERSION` constant
stays `"0.3"`.

This is the same fix-in-place reasoning as #83: a still-draft, in-repo
spec corrected before first implementation of the affected surface. The
"`0.4` permanently skipped" note is unaffected and stays.

## Backward compatibility

None at stake. No server advertises an `http_upload.source` capability or
implements an `upload` tool; no `source_variants` is emitted by any
implementation. The change is to un-implemented draft spec text.

## Out of scope

- The sender-side `upload` tool **implementation** — #85. #85's pvl-core
  helper resolves the opaque `origin_id` to a file-like object through a
  domain hook the downstream supplies (the downstream knows whether the
  resource is a file, a database blob, or an in-memory image), then POSTs
  it — mirroring how the download-producer side already treats
  `origin_id` → bytes resolution as the server's own concern.
- Any change to the receiver side, the `http` method, or `exchange`.

## Acceptance (from #93)

- [ ] The `upload` tool's `source` tagged union is replaced by an opaque
  `origin_id` parameter; `url` and `content_type` retained.
- [ ] `source_variants` is removed from the capability declaration shape
  (examples + field list).
- [ ] The `unsupported_source_variant` error code and envelope are removed.
- [ ] The `source` role-key vs `source` parameter disambiguation paragraph
  is removed.
- [ ] The "MCP-mediated push" worked example uses `origin_id`.
- [ ] The spec version stays `0.3.0`.
- [ ] The `http_upload` wire contract and the receiver side are unchanged.
