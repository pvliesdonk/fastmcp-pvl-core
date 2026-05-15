# Design: reverse HTTP transfer spec (`http_upload`, issue #71)

**Status**: approved (brainstorm 2026-05-15)
**Issue**: [pvliesdonk/fastmcp-pvl-core#71](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/71)
**Umbrella**: #75
**Implementation tracker**: #74

## Problem

`docs/specs/file-exchange.md` v0.2.5 defines server-to-server file
transfer in one direction: a producer publishes a file via
`origin_id`, a consumer pulls via `exchange://` URI or via the
producer-minted download URL (`http` method). The reverse direction
is missing — and is a real, legitimate transfer method.

The motivating use case (one of several): the *sender* cannot serve
an HTTP endpoint. With the existing `http` (download) method the
consumer must pull bytes from a producer-served URL, which fails
if the bytes-haver is a local agent, a `curl` invocation, or any
client that can make outbound requests but cannot receive inbound
connections. `http_upload` inverts the direction: the receiver
issues the URL, the sender only needs outbound HTTP access to
reach it.

The previous attempt captured the missing direction as Amendment 11
inside the same spec doc as the v0.2.5 wording, presented as a
proposal and merged without spec-evolution review. It collapsed
"what is being uploaded" and "where to write it" into a single
`target_id` with path-segment-strict character rules, which forced
downstream consumers (notably `markdown-vault-mcp`) to ship
vault-root-only upload primitives.

PR #77 reverted A11 along with the other v0.4 amendments. This
spec lands the reverse-HTTP method properly: a real spec release
with the version bumped to v0.3.0, designed bottom-up from the
interop need rather than carried forward from A11.

## Framing principle

Every transfer carries **two identifiers**:

- **WHAT** — a stable handle for the bytes (the bytes-haver's
  choice; opaque to anyone else).
- **WHERE** — a destination instruction for the receiving side
  (chosen by whoever decides destination semantics).

In v0.2.5 download:

- WHAT = `origin_id`, picked by the producer (bytes-haver).
- WHERE = `path` parameter on the consumer's `fetch` tool — picked
  by the consumer (the receiving side decides where to land).

In v0.3 reverse-HTTP upload, the roles flip but the framing holds:

- WHAT = `origin_id`, picked by the sender (now the bytes-haver).
- WHERE = `destination` parameter on the receiver's
  `create_upload_link` tool — picked by the sender, validated by
  the receiver (the receiving side enforces destination semantics).

A11's mistake was collapsing both roles into a single `target_id`
with the same character rules as `origin_id`, which forced any
receiver wanting hierarchical destinations into a vault-root-only
shape. The corrected design keeps them separate, with asymmetric
character rules justified by their different roles.

## Out of scope

- **Implementation** — lives in #74. This PR is spec-only. The PR
  contains the spec wording, the version bump, and any worked
  examples; it does NOT contain Python code.
- **`register_file_exchange_upload`** rename/redesign — #74's job.
- **Template scaffold update** — folds into
  [pvliesdonk/fastmcp-server-template#131](https://github.com/pvliesdonk/fastmcp-server-template/issues/131)
  alongside the #74 implementation.
- **markdown-vault-mcp migration** — tracked in
  [pvliesdonk/markdown-vault-mcp#488](https://github.com/pvliesdonk/markdown-vault-mcp/issues/488).

## Design decisions

### 1. Method name and capability shape

New transfer method **`http_upload`**, an independent top-level peer
of the existing `exchange` and `http` methods. Not nested under
`http`.

Rationale:

- Each method is a top-level key in `transfer_methods`; the existing
  `exchange` / `http` precedent is "key = method name." Adding
  `http_upload` as a peer matches.
- Naming explicit about transport (`http_`) leaves room for future
  non-HTTP upload methods (e.g. `s3_upload`) without naming
  collisions.
- Backward compat is automatic: v0.2 servers and clients silently
  skip unknown methods (existing spec rule); no version-negotiation
  gymnastics needed.

### 2. Direction and tool naming

`http_upload` defines two tool roles, mirroring how `http` defines
both `create_download_link` (producer side) and `fetch` (consumer
side):

- **Receiver side** — tool `create_upload_link`. Mints a one-time
  POST URL given a sender's `origin_id` + optional `destination`.
- **Sender side** — tool `upload`. Optional. POSTs bytes to a
  receiver-issued URL.

Both sides are advertised in `transfer_methods.http_upload` with
the same `{tool: <name>}` shape; the role is implicit based on
which name the server uses (same pattern as `http`).

The sender-side tool is **wire-optional**. Any HTTP client (`curl`,
a browser, a custom script) is a valid party. The `upload` tool
only exists to standardize MCP-mediated push between MCP servers.
The corresponding optionality of `fetch` on the download side is
called out in the new spec text for consistency, even though the
existing spec already permits any HTTP client.

### 3. `create_upload_link` tool contract

**Inputs:**

| Param | Cardinality | Rules | Description |
|---|---|---|---|
| `origin_id` | MUST | Same rules as `origin_id` in v0.2.5 download (raw-JSON validation; no `/`, `\`, `.`, `..`, null bytes, control chars, leading/trailing whitespace). | Sender's opaque stable handle for the bytes. |
| `destination` | MAY | Relaxed: forbids only null bytes, control characters, leading/trailing whitespace. Slashes, dots, etc. are allowed. The receiver validates per its own domain rules. | Sender's destination instruction. Receiver decides semantics (path, slot, parent doc, anything). |
| `ttl_seconds` | MAY | Positive number. | Sender's TTL hint. Receiver MAY clamp to its own ceiling. |
| `max_bytes` | MAY | Positive integer. | Sender's size hint. Receiver MAY clamp to its own ceiling. |
| `content_type` | MAY | Standard MIME type string. | Sender's MIME hint. Receiver MAY pre-filter against its `accepts` list at link-mint time, surfacing the error in-band rather than at POST time. |

**Returns:**

```json
{
  "url": "https://receiver.example/uploads/<token>",
  "ttl_seconds": 3600,
  "max_bytes": 10485760
}
```

- `url` (MUST) — the POST endpoint.
- `ttl_seconds` (MUST) — effective TTL after clamping.
- `max_bytes` (SHOULD) — effective body-size ceiling after
  clamping. Sender uses this to decide whether to abort early.

**Failure shape** (in-band tool error): `transfer_failed` envelope
matching the existing download spec:

```json
{
  "error": "transfer_failed",
  "method": "http_upload",
  "receiver_server": "<receiver namespace>",
  "origin_id": "<the origin_id passed in>",
  "message": "destination validation failed: ..."
}
```

In-band failure reasons: `destination` invalid per receiver's
rules, `content_type` not in `accepts`, receiver-domain rejection
(quota, dedup conflict, etc.).

### 4. POST contract (receiver-side, at the minted URL)

**Request:**

- Method: `POST`.
- Body: raw bytes.
- `Content-Type` header: MUST be set by the sender. Receiver MAY
  enforce per its `accepts` list at this point; mismatch yields
  `415`.
- `Content-Length` header: SHOULD be set. Receiver MAY require it.

**Token semantics:**

- Cryptographically unguessable (≥128 bits of entropy in the URL
  path or query).
- One-time use: receiver MUST atomically consume the token on
  first POST attempt — success OR failure. A retry on the same URL
  returns `404`, not the original error. Senders that need to retry
  call `create_upload_link` again.
- TTL-bounded: receiver MUST reject expired tokens.

**Status code classes:**

| Class | When | Spec rule |
|---|---|---|
| `2xx` | bytes accepted | MUST emit one of these on success |
| `404 Not Found` | token unknown, expired, OR already consumed | MUST NOT distinguish (anti-leak) |
| `413 Payload Too Large` | body exceeds enforced `max_bytes` (either via `Content-Length` or mid-stream running total) | MUST emit this when the cap is breached |
| `415 Unsupported Media Type` | `Content-Type` doesn't match `accepts` | MUST emit this when filter rejects |
| Other `4xx` | receiver-domain rejection | receiver picks code; body carries `transfer_failed` envelope |
| `5xx` | server error | body MAY be generic; receiver MUST NOT echo internal error details (log full traceback server-side) |

**Success body:** spec does NOT mandate a shape. Receivers MAY
return JSON with domain-specific data (saved-path confirmation,
generated ID, etc.). Senders SHOULD parse JSON if the response
`Content-Type` indicates JSON; otherwise treat as opaque
acknowledgment.

**Failure body (4xx with structured info):** `transfer_failed`
envelope, same shape as in Section 3.

### 5. `upload` tool contract (sender-side, optional)

**Inputs:**

| Param | Cardinality | Description |
|---|---|---|
| `url` | MUST | The receiver-issued POST endpoint. |
| `source` | MUST | Tagged-union: one of `{ "path": "<local path>" }`, `{ "exchange_uri": "exchange://..." }`, `{ "http_url": "https://..." }`, `{ "inline_b64": "<base64>" }`. Implementations MAY support a subset; `path` is the lowest common denominator. |
| `content_type` | SHOULD | MIME type the sender will declare in the POST. If omitted, the sender SHOULD sniff or default. |

**Returns:**

```json
{
  "status": 201,
  "body": "<receiver's success body, passed through>"
}
```

- `status` (MUST) — receiver's HTTP status code.
- `body` (MAY) — receiver's response body, opaque to the sender
  tool (passed through to the caller).

On 4xx with a structured `transfer_failed` envelope, the sender
tool SHOULD unwrap and re-raise as a tool error, mirroring how
download's `fetch` propagates `transfer_failed`.

### 6. Capability declaration

**Receiver advertises:**

```json
"transfer_methods": {
  "http_upload": {
    "tool": "create_upload_link",
    "accepts": ["application/pdf", "text/markdown"],
    "max_bytes": 10485760,
    "max_ttl_seconds": 3600
  }
}
```

- `tool` (MUST) — name of the URL-mint tool.
- `accepts` (SHOULD) — MIME-type filter applied at link-mint
  (`content_type` check) and/or at POST (`Content-Type` header
  check). Defaults to `["*/*"]` if omitted.
- `max_bytes` (SHOULD) — receiver-enforced body-size ceiling.
- `max_ttl_seconds` (SHOULD) — receiver-enforced TTL ceiling.

**Sender (optional) advertises:**

```json
"transfer_methods": {
  "http_upload": {"tool": "upload"}
}
```

Just the tool name. No `accepts` / `max_bytes` / `max_ttl_seconds`
on the sender side; those are receiver-side constraints.

A single server can advertise both sides simultaneously (one as
`create_upload_link`, the other as `upload`) if it implements both
roles — but in practice most servers will pick one.

### 7. Method priority

`http_upload` is **not** added to the existing priority list
(`exchange > http`). The priority list is for choosing among
equivalent methods for the same transfer; `http_upload` is a
different *role* (push, not pull) and isn't equivalent to `exchange`
or `http`. The spec calls this out explicitly so future contributors
don't try to slot it in.

### 8. Security & path resolution

The existing security section (§"Security and Path Resolution") gets
a small addition:

- `origin_id` validation: already covered — raw-JSON-string rules
  apply to both directions.
- `destination` validation: receiver-side only. Spec mandates
  minimum safety (no null bytes, no control characters, no
  leading/trailing whitespace); slashes, dots, and traversal-shaped
  strings are **not** spec-rejected. The receiver MUST validate per
  its own domain rules.
- Receiver-issued POST URL: same unguessability rule as download
  URLs (≥128 bits entropy, opaque to anyone but the receiver).

The asymmetry between `origin_id`'s strict rules and `destination`'s
relaxed rules is documented inline so future readers don't
"correct" the relaxed form by mistake.

### 9. Versioning

- Spec version bumps **0.2.5 → 0.3.0**. The new transfer method is
  additive (existing methods unchanged), but the user's framing for
  #71 is explicit: "Acceptance is a real spec release with the
  version bumped." So a minor bump even though strictly additive.
- Capability `version` field advertises `"0.3"` per the existing
  `MAJOR.MINOR` rule. A server implementing 0.3.0 MUST advertise
  `"0.3"`.
- Backward compat: v0.2 servers/clients silently skip `http_upload`
  in capability declarations (existing spec rule for unknown
  methods). v0.3 servers talking to v0.2 peers don't see
  `http_upload` advertised and don't attempt the new direction.
- The existing "Versioning and compatibility" section's
  additive-within-minor / unknown-method-skip rules cover this;
  no new wording is needed in that section.

### 10. Worked examples

The new spec section ends with two short worked examples (concrete
wire payloads):

- **Agent push** — agent calls `create_upload_link(origin_id=..., destination=...)`,
  receives URL, POSTs bytes from a local file with `curl`,
  receives 201.
- **MCP-mediated push** — server A calls server B's
  `create_upload_link`, then calls its own `upload(url=..., source={"path": ...})`
  to mediate the transfer end-to-end.

Both examples are short and concrete enough to read end-to-end. They
do not appear elsewhere in the spec; they live at the end of the new
`#### \`http_upload\`` subsection.

## File layout in the spec doc

The new content is integrated into `docs/specs/file-exchange.md` as
follows:

- **Title & version** (top of doc): bump version string from `0.2.5`
  to `0.3.0`.
- **§"About this document"**: no change.
- **§"Concepts > File Reference"**: no change (file_ref is
  unchanged; `http_upload` doesn't interact with file_ref).
- **§"Transfer Methods"**: new subsection `#### \`http_upload\` (push
  to receiver-issued URL)` peer of the existing `#### \`http\``.
  Contains §§3, 4, 5 above (the tool contracts and POST contract)
  plus the worked examples.
- **§"Method priority"**: short addition noting that `http_upload`
  is not in the priority list and why.
- **§"Adding future methods"**: no change (the existing extension
  point already accommodates `http_upload`).
- **§"Security and Path Resolution"**: small addition for
  `destination` validation rules (Section 8 above).
- **§"Discovery / Capability declaration"**: example capability
  blocks updated to show a server advertising `http_upload` (both
  receiver and sender variants).
- **§"Versioning and compatibility"**: no new wording (existing
  rules cover the bump).

## Acceptance (from issue #71)

- [ ] Design discussion completed with input from a downstream
      implementor (markdown-vault-mcp via #488). [Captured in this
      brainstorm; the user is the markdown-vault-mcp maintainer
      and provided the WHAT/WHERE framing that drives Section 3.]
- [ ] Spec PR merges the new prose into `docs/specs/file-exchange.md`
      as a clean addition. **No "amendment" or "proposal" framing.**
- [ ] Spec version bumped to v0.3.0; PR includes the bump.
- [ ] Implementation tracked separately in #74; the spec PR does
      not include the implementation.
- [ ] **Template impact**: scaffolds may need new defaults; tracked
      in pvliesdonk/fastmcp-server-template#131 alongside #74.
