# reverse HTTP transfer spec — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `http_upload` transfer method to `docs/specs/file-exchange.md` per the approved design at `docs/superpowers/specs/2026-05-15-reverse-http-transfer-spec-design.md`, and bump the spec version from 0.2.5 to 0.3.0.

**Architecture:** Spec-only edit. One file changed (`docs/specs/file-exchange.md`). Each task is a focused prose insertion or rewrite, showing the exact text to add. No Python, no tests; verification is reading the resulting prose end-to-end and checking that cross-references resolve and the new section fits the surrounding voice. Implementation is tracked separately in #74.

**Tech Stack:** Markdown. (Spec doc.)

**Spec:** [`docs/superpowers/specs/2026-05-15-reverse-http-transfer-spec-design.md`](../specs/2026-05-15-reverse-http-transfer-spec-design.md) (commit `5b448a9`).

---

## Task 1: Bump spec version to 0.3.0

**Files:**
- Modify: `docs/specs/file-exchange.md` (line ~3)

- [ ] **Step 1: Locate the version line**

```bash
cd /mnt/code/fastmcp-pvl-core
head -6 docs/specs/file-exchange.md
```

Expected: the title block starting with `# MCP File Exchange Specification`, followed by `**Version:** 0.2.5`, `**Status:** experimental`, etc.

- [ ] **Step 2: Replace the version string**

Edit `docs/specs/file-exchange.md` — change:

```
**Version:** 0.2.5
```

to:

```
**Version:** 0.3.0
```

- [ ] **Step 3: Verify**

```bash
head -6 docs/specs/file-exchange.md
```

Expected: shows `**Version:** 0.3.0`. No other lines in the title block changed.

- [ ] **Step 4: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): bump file-exchange version to 0.3.0

Spec version bump for the new \`http_upload\` transfer method added
by this PR.  v0.3.0 is the first release containing the reverse
HTTP transfer direction.  Backward compat with v0.2 is automatic:
v0.2 peers silently ignore unknown methods (existing spec rule).

Refs #71."
```

---

## Task 2: Add the new `#### http_upload` subsection to "Transfer Methods"

**Files:**
- Modify: `docs/specs/file-exchange.md` — the `### Transfer Methods` section. Insert a new `####` subsection AFTER the existing `#### http (download URL)` subsection and BEFORE the existing `#### Method priority` subsection.

- [ ] **Step 1: Locate the insertion point**

```bash
grep -n '^#### Method priority' docs/specs/file-exchange.md
```

Note the line number. The insertion goes IMMEDIATELY BEFORE this line (so the new subsection appears as a peer of `http` and before the priority discussion).

- [ ] **Step 2: Insert the new subsection**

Insert the following block immediately before the line that contains `#### Method priority`:

````markdown
#### `http_upload` (push to receiver-issued URL)

The reverse of the `http` method: the *receiver* mints a one-time POST URL; any party with the URL pushes bytes. The sender can be an LLM/agent, another MCP server, or a human with an HTTP client (`curl`, browser, custom script) — the spec does not constrain who pushes. The motivating use case is uploading to a receiver that is not publicly reachable: with download-only methods, the receiver cannot mint a URL the sender can `GET` from, and the sender has no way to push without the receiver first issuing a reachable endpoint.

Like the existing `http` (download) method, both the URL-mint tool on the receiver side and the POST-perform tool on the sender side are wire-optional from the spec's perspective. Any HTTP client that can issue a `POST` is a valid sender, just as any HTTP client that can `GET` is a valid consumer of the existing `http` method. The tool definitions exist to standardize MCP-mediated transfer between MCP servers; they are not the only valid implementation of either side.

In a capability declaration (receiver):

```json
"http_upload": {
  "tool": "create_upload_link",
  "accepts": ["application/pdf", "text/markdown"],
  "max_bytes": 10485760,
  "max_ttl_seconds": 3600
}
```

In a capability declaration (sender, optional):

```json
"http_upload": {"tool": "upload"}
```

Both sides advertise the same key (`http_upload`) with the same `{tool: <name>}` shape; the role is implicit based on which tool name the server registers. A single server MAY advertise both sides if it implements both roles.

**Receiver-side tool: `create_upload_link`**

The receiver registers a tool that mints upload URLs given a sender's identifier and (optionally) a destination instruction.

| Param | Cardinality | Rules | Description |
|---|---|---|---|
| `origin_id` | MUST | Same rules as `origin_id` in the `http` method's `create_download_link` (raw-JSON validation; no path separators `/` or `\`; not equal to `.` or `..`; no null bytes / control characters; no leading or trailing whitespace). | The sender's opaque stable handle for the bytes (the *what*). The receiver MAY treat it as a filename, document id, content hash, or any internally-meaningful key, but MUST NOT interpret it as a path component. |
| `destination` | MAY | Forbids only null bytes, control characters (U+0000 through U+001F), and leading/trailing whitespace. Path separators, dots, and traversal-shaped strings are **NOT** spec-rejected. The receiver MUST validate per its own domain rules before any filesystem interaction. | The sender's destination instruction (the *where*). The receiver decides semantics — path, slot, parent document key, anything. The relaxed character rules vs. `origin_id` reflect the asymmetric role: `destination` is consumed only by the receiver's own domain logic and never embedded in a URI by anyone else. |
| `ttl_seconds` | MAY | Positive number of seconds. | Sender's TTL hint for the minted URL. The receiver MAY clamp to its own ceiling (`max_ttl_seconds`); the effective TTL is returned. |
| `max_bytes` | MAY | Positive integer. | Sender's size hint. The receiver MAY clamp to its own ceiling (`max_bytes`); the effective ceiling is returned. |
| `content_type` | MAY | Standard MIME type string. | Sender's hint about what `Content-Type` the upload will declare. The receiver MAY pre-filter against its `accepts` list at link-mint time and surface a `transfer_failed` envelope in-band, sparing the sender a 415 round-trip. |

The tool MUST return:

```json
{
  "url": "https://receiver.example/uploads/<token>",
  "expires_in_seconds": 3600,
  "max_bytes": 10485760
}
```

- `url` (MUST) — the POST endpoint.
- `expires_in_seconds` (MUST) — effective TTL after clamping.
- `max_bytes` (SHOULD) — effective body-size ceiling after clamping.

On in-band failure (invalid `destination`, `content_type` not in `accepts`, quota exhausted, dedup conflict, etc.), the receiver returns a `transfer_failed` envelope:

```json
{
  "error": "transfer_failed",
  "method": "http_upload",
  "origin_server": "<receiver namespace>",
  "origin_id": "<the origin_id passed in>",
  "message": "destination validation failed: ..."
}
```

**POST contract (at the minted URL):**

The sender POSTs raw bytes to the receiver-issued URL.

- **Method**: `POST`.
- **Body**: raw bytes.
- **`Content-Type` header**: MUST be set by the sender. The receiver MAY enforce per its `accepts` list at this point; mismatch yields `415 Unsupported Media Type`.
- **`Content-Length` header**: SHOULD be set by the sender. The receiver MAY require it.

The URL token:

- MUST be cryptographically unguessable (≥128 bits of entropy in the URL path or query).
- MUST be one-time use: the receiver MUST atomically consume the token on the first POST attempt — success OR failure. A retry on the same URL returns `404`, not the original error. Senders that need to retry MUST call `create_upload_link` again.
- MUST be TTL-bounded: the receiver MUST reject expired tokens.

Status code classes (the receiver picks specific codes within each class; senders SHOULD treat the class as the actionable signal):

| Class | When | Spec rule |
|---|---|---|
| `2xx` | bytes accepted | MUST emit one of these on success. |
| `404 Not Found` | token unknown, expired, OR already consumed | MUST NOT distinguish between these three conditions (anti-leak: avoid revealing token-existence to a probing caller). |
| `413 Payload Too Large` | body exceeds the receiver's enforced `max_bytes` (either `Content-Length` declares too much, or the running body total exceeds the cap mid-stream) | MUST emit when the cap is breached. |
| `415 Unsupported Media Type` | `Content-Type` does not match the receiver's `accepts` filter | MUST emit when the filter rejects. |
| Other `4xx` | receiver-domain rejection (invalid destination, quota, dedup conflict, etc.) | The receiver picks the code; the response body MUST carry a `transfer_failed` envelope. |
| `5xx` | server-side error | The body MAY be generic; the receiver MUST NOT echo internal error details (the full traceback is logged server-side). |

Success body: the spec does NOT mandate a shape. Receivers MAY return JSON with domain-specific data (saved-path confirmation, generated id, etc.). Senders SHOULD parse JSON when the response `Content-Type` indicates JSON; otherwise treat the body as opaque acknowledgment.

Failure body (4xx with structured information): a `transfer_failed` envelope, same shape as the in-band failure example above.

**Sender-side tool: `upload` (optional)**

A server that wants to act as an MCP-mediated *pusher* of bytes (e.g., a mover-server that reads from a remote source and POSTs to a receiver-issued upload URL) advertises a sender-side tool. Servers that don't have a push role simply omit this side of the capability.

| Param | Cardinality | Description |
|---|---|---|
| `url` | MUST | The receiver-issued POST endpoint (returned from `create_upload_link`). |
| `source` | MUST | Tagged union: exactly one of `{ "path": "<local path>" }`, `{ "exchange_uri": "exchange://..." }`, `{ "http_url": "https://..." }`, or `{ "inline_b64": "<base64 content>" }`. Implementations MAY support a subset; `path` is the lowest-common-denominator. |
| `content_type` | SHOULD | The MIME type the sender will declare in the POST `Content-Type` header. If omitted, the sender SHOULD sniff or default. |

The tool MUST return:

```json
{
  "status": 201,
  "body": "<receiver's response body, passed through>"
}
```

- `status` (MUST) — the receiver's HTTP status code.
- `body` (MAY) — the receiver's response body, passed through to the caller (opaque to the sender tool itself).

On 4xx with a structured `transfer_failed` envelope, the sender tool SHOULD unwrap and re-raise as a tool error, mirroring how the existing `http` method's `fetch` tool propagates `transfer_failed`.

**Worked example — agent push:**

An LLM agent has a local PDF at `/tmp/draft.pdf` and wants to upload it to a vault server with `namespace: "vault-mcp"`. The agent calls the vault's `create_upload_link` tool:

```jsonc
// request
{
  "origin_id": "draft-2026-05-15.pdf",
  "destination": "projects/research/papers/draft.pdf",
  "content_type": "application/pdf"
}

// response
{
  "url": "https://vault-mcp.example/uploads/8f3a9e2b...",
  "expires_in_seconds": 3600,
  "max_bytes": 10485760
}
```

The agent then pushes the bytes with `curl`:

```
curl -X POST --data-binary @/tmp/draft.pdf \
     -H "Content-Type: application/pdf" \
     https://vault-mcp.example/uploads/8f3a9e2b...
```

The receiver responds `201 Created` with an optional JSON body (e.g., `{"saved_path": "projects/research/papers/draft.pdf"}`).

**Worked example — MCP-mediated push:**

A mover-server is asked to copy bytes from an internal `exchange://` URI into an external vault. The mover calls the vault's `create_upload_link` to obtain the URL, then calls its own `upload` tool to actually POST the bytes:

```jsonc
// step 1: vault.create_upload_link
{
  "origin_id": "moved-from-vault",
  "destination": "incoming/2026-05-15/movement.bin"
}
// -> {"url": "https://vault-mcp.example/uploads/<token>", "expires_in_seconds": 3600, "max_bytes": 10485760}

// step 2: mover.upload
{
  "url": "https://vault-mcp.example/uploads/<token>",
  "source": {"exchange_uri": "exchange://hades-01/mover-mcp/moved-from-vault.bin"},
  "content_type": "application/octet-stream"
}
// -> {"status": 201, "body": {"saved_path": "incoming/2026-05-15/movement.bin"}}
```

````

- [ ] **Step 3: Verify the new subsection sits between `http` and `Method priority`**

```bash
grep -nE '^#### ' docs/specs/file-exchange.md
```

Expected: subsection headings include `#### exchange`, `#### http`, `#### http_upload`, `#### Method priority`, `#### Adding future methods` — in that order under `### Transfer Methods`.

- [ ] **Step 4: Render-check by reading the new subsection end-to-end**

```bash
awk '/^#### `http_upload`/,/^#### Method priority/' docs/specs/file-exchange.md | head -200
```

Read the new content. Confirm:
- The intro paragraph mentions the not-publicly-reachable use case.
- The wire-optional note explicitly says "any HTTP client that can issue a POST is a valid sender."
- The two capability-declaration JSON blocks (receiver + sender) appear before the receiver-side tool details.
- The receiver-side `create_upload_link` table has 5 rows (`origin_id`, `destination`, `ttl_seconds`, `max_bytes`, `content_type`).
- The POST contract has 5 status-code rows (2xx, 404, 413, 415, other 4xx, 5xx — actually 6).
- The sender-side `upload` table has 3 rows (`url`, `source`, `content_type`).
- Both worked examples (agent push, MCP-mediated push) are present.

- [ ] **Step 5: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): add http_upload transfer method (refs #71)

The new \`http_upload\` transfer method defines reverse HTTP transfer:
the receiver mints a one-time POST URL; any party (agent, server,
or human with curl) pushes bytes.  Wire shape:

- Receiver tool \`create_upload_link\` takes \`origin_id\` (WHAT,
  strict origin_id-style rules) + \`destination\` (WHERE,
  receiver-validated, relaxed character rules) + optional ttl /
  max_bytes / content_type hints; returns \`{url, expires_in_seconds,
  max_bytes}\`.
- Sender tool \`upload\` (optional) takes \`url\` + \`source\`
  (tagged union: path / exchange_uri / http_url / inline_b64) +
  optional \`content_type\`; returns \`{status, body}\`.
- POST contract: receiver-issued URL with ≥128-bit token, one-time
  consumption, TTL-bounded, well-defined status-code classes (2xx,
  404 token, 413 size, 415 type, 4xx domain, 5xx server) and
  \`transfer_failed\` envelope on structured 4xx.
- Both worked examples (agent push with curl; MCP-mediated push
  via mover-server) included at the end of the new subsection.

The new method is a top-level peer of \`exchange\` / \`http\`, not
nested under \`http\`.  Naming is explicit about transport
(\`http_upload\`) to leave room for future non-HTTP upload methods
(e.g. \`s3_upload\`).  Backward compat with v0.2 is automatic
(unknown methods silently skipped per the existing spec rule).

Refs #71.  Implementation tracked in #74."
```

---

## Task 3: Update capability-declaration examples to show `http_upload`

**Files:**
- Modify: `docs/specs/file-exchange.md` — the `#### Capability declaration` subsection (inside `### Discovery`). Update the two existing example JSON blocks to (a) advertise `"version": "0.3"` and (b) show a server advertising `http_upload` on the consumer/receiver side.

- [ ] **Step 1: Locate the example blocks**

```bash
grep -n 'Producer example\|Consumer example' docs/specs/file-exchange.md
```

Note the line numbers of both `**Producer example:**` and `**Consumer example:**` labels.

- [ ] **Step 2: Update the producer example**

In the JSON block under `**Producer example:**` (image-mcp), change:

```json
        "version": "0.2",
```

to:

```json
        "version": "0.3",
```

The `transfer_methods` block stays unchanged for the producer example — producers (file_ref publishers) don't usually advertise `http_upload`. (A server CAN advertise both `http`-producer AND `http_upload`-receiver simultaneously; we keep the producer example minimal so it remains a clear "producer-only" pattern. The consumer example, updated next, illustrates the receiver-of-uploads case.)

- [ ] **Step 3: Update the consumer example**

In the JSON block under `**Consumer example:**` (vault-mcp), make TWO changes:

a) Change:

```json
        "version": "0.2",
```

to:

```json
        "version": "0.3",
```

b) In the `transfer_methods` block, change:

```json
        "transfer_methods": {
          "exchange": {},
          "http": {
            "tool": "fetch"
          }
        }
```

to:

```json
        "transfer_methods": {
          "exchange": {},
          "http": {
            "tool": "fetch"
          },
          "http_upload": {
            "tool": "create_upload_link",
            "accepts": ["application/pdf", "text/markdown"],
            "max_bytes": 10485760,
            "max_ttl_seconds": 3600
          }
        }
```

This shows vault-mcp advertising itself as both a download-direction consumer (`http: {tool: "fetch"}`) AND an upload receiver (`http_upload: {tool: "create_upload_link", ...}`).

- [ ] **Step 4: Verify both example blocks**

```bash
awk '/Producer example:/,/Consumer example:/' docs/specs/file-exchange.md | head -30
echo '---'
awk '/Consumer example:/,/^[|]/' docs/specs/file-exchange.md | head -30
```

Expected: producer example has `"version": "0.3"` and unchanged `transfer_methods`; consumer example has `"version": "0.3"` and the `http_upload` block present with `accepts`, `max_bytes`, `max_ttl_seconds` fields.

- [ ] **Step 5: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): update capability examples to show http_upload (refs #71)

Both worked examples in §Discovery / Capability declaration now
advertise \`\"version\": \"0.3\"\`.  The consumer example (vault-mcp)
gains an \`http_upload\` block showing the receiver-side
capability advertisement with concrete \`accepts\`, \`max_bytes\`,
and \`max_ttl_seconds\` values.

The producer example stays focused on the producer-only pattern —
adding \`http_upload\` there would be valid but would muddle the
example's intent.

Refs #71."
```

---

## Task 4: Add `destination` validation rules to "Security and Path Resolution"

**Files:**
- Modify: `docs/specs/file-exchange.md` — the `### Security and Path Resolution` section.

- [ ] **Step 1: Locate the section**

```bash
grep -n '^### Security and Path Resolution' docs/specs/file-exchange.md
```

Note the line. Find the existing paragraph that mentions `origin_id` JSON-RPC parameter validation (look for "When handling direct JSON-RPC parameters"). The new paragraph goes near there, ideally right after the existing JSON-RPC-parameter discussion concludes (before the segment-rules bullet list, or after it — the section's logical flow already has the JSON-RPC vs URI distinction, so the new paragraph extends that).

- [ ] **Step 2: Find the exact insertion point**

```bash
sed -n '/^### Security and Path Resolution/,/^### /p' docs/specs/file-exchange.md
```

Read the section. Identify a clean insertion point: after the segment-validation rules (which apply to URI segments AND JSON-RPC params like `origin_id`), insert a new paragraph specifically about `destination`. The cleanest spot is right BEFORE the existing paragraph that begins with `In addition, \`exchange://\` URIs themselves MUST NOT...`.

- [ ] **Step 3: Insert the new paragraph**

Insert the following block IMMEDIATELY BEFORE the line starting `In addition, \`exchange://\` URIs themselves MUST NOT contain a query component`:

```markdown
The `destination` parameter passed to a receiver's `create_upload_link` tool (new in v0.3, used by the `http_upload` method) is **not** subject to the segment-validation rules above. It is opaque to anyone but the receiver — the spec mandates only minimum safety constraints (no null bytes, no control characters U+0000 through U+001F, no leading or trailing whitespace). Path separators, dots, and traversal-shaped strings are NOT spec-rejected; the receiver MUST validate per its own domain rules before any filesystem interaction. The asymmetric rules vs. `origin_id` reflect the role split: `origin_id` MAY be echoed by the receiver into URIs or filenames (so it must be URI-safe), but `destination` is consumed only by the receiver's own domain logic and never embedded in a URI by anyone else.

```

(Note the trailing blank line to keep the section's paragraph spacing intact.)

- [ ] **Step 4: Verify**

```bash
sed -n '/^### Security and Path Resolution/,/^### /p' docs/specs/file-exchange.md | head -40
```

Read the section end-to-end. Confirm:
- The existing segment-validation rules (no `/`, `.`, `..`, etc.) are intact and unchanged.
- The new `destination` paragraph appears AFTER the segment rules and BEFORE the `exchange://`-URI no-query-or-fragment paragraph.
- The paragraph mentions `create_upload_link`, names `v0.3`, names `http_upload`, and explains the asymmetry with `origin_id`.

- [ ] **Step 5: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): add destination validation rules to Security section (refs #71)

\`destination\` is the new sender-provided WHERE parameter on
\`create_upload_link\` (the \`http_upload\` method's receiver-side
tool).  Unlike \`origin_id\`, which a receiver may echo into URIs
or filenames and therefore must be URI-safe, \`destination\` is
consumed only by the receiver's own domain logic.  The spec
mandates only the minimum safety constraints (no null bytes / no
control characters / no leading-trailing whitespace); the receiver
validates per its own domain rules before any filesystem
interaction.

The asymmetric rules are stated inline so future contributors don't
\"correct\" the relaxed form by mistake.

Refs #71."
```

---

## Task 5: Add method-priority note for `http_upload`

**Files:**
- Modify: `docs/specs/file-exchange.md` — the `#### Method priority` subsection (inside `### Transfer Methods`).

- [ ] **Step 1: Locate the section**

```bash
grep -n '^#### Method priority' docs/specs/file-exchange.md
```

Read the existing content. It currently lists the priority order (`exchange > http`) and says future methods slot in by latency / cost / privacy properties.

- [ ] **Step 2: Append the note**

At the end of the `#### Method priority` subsection (immediately before the next `#### Adding future methods` subsection), append:

```markdown

The `http_upload` method introduced in v0.3 is NOT included in the priority list. The priority list compares methods for the same transfer *direction* (consumer pull from a producer-side endpoint); `http_upload` is the inverse direction (sender push to a receiver-side endpoint). It is selected by a different mechanism — the sender looks for `http_upload` in a receiver's capability declaration and uses it when the use case calls for an upload, not when comparing alternative consumer-pull methods.
```

(Leading blank line to separate from the prior paragraph; trailing blank line to keep section spacing intact.)

- [ ] **Step 3: Verify**

```bash
sed -n '/^#### Method priority/,/^#### Adding future methods/p' docs/specs/file-exchange.md | head -30
```

Read the result. Confirm:
- The original priority-list paragraph (numbered list `1. exchange (zero-cost...) / 2. http (network transfer...)`) is intact.
- The original "Future methods slot into this priority list by convention" paragraph is intact.
- The new note about `http_upload` not being in the priority list appears as the last paragraph in the subsection, before the next `####` heading.

- [ ] **Step 4: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): note that http_upload is not in method-priority list (refs #71)

\`http_upload\` (introduced in v0.3) is the inverse-direction
transfer method: sender push, not consumer pull.  The priority
list compares methods for the same direction (consumer pull); it
isn't meaningful to slot a push method into a pull-priority list.
The spec calls this out explicitly so future contributors don't
try to fit it in.

Refs #71."
```

---

## Task 6: Final read-through + cross-reference check

**Files:** none modified — verification-only step.

- [ ] **Step 1: Read the new `http_upload` section end-to-end**

```bash
awk '/^#### `http_upload`/,/^#### Method priority/' docs/specs/file-exchange.md
```

Read the whole subsection as if encountering it for the first time. Look for:
- Awkward transitions.
- Forward references to symbols defined further down (e.g. a mention of `transfer_failed` before the reader has been pointed at where it's defined — the existing v0.2.5 spec defines it in §Transfer Negotiation, so a cross-reference is fine but the new section shouldn't redefine it).
- Tables that read clearly column-by-column.
- Worked examples that match the prose's claims about parameter cardinality / semantics.

- [ ] **Step 2: Check all `http_upload` mentions are consistent**

```bash
grep -n 'http_upload\|create_upload_link\|destination' docs/specs/file-exchange.md
```

Verify across all hits:
- `http_upload` is consistently the method key.
- `create_upload_link` is consistently the receiver-side tool name.
- `upload` is consistently the sender-side tool name.
- `destination` is consistently the sender-provided WHERE parameter (not `target_path`, `target`, or other alternative names — the brainstorm settled on `destination`).
- `origin_id` is consistently the sender-issued WHAT parameter (re-using the existing name; not a new name).

If any hit shows a name drift, fix it (likely a typo introduced during one of the edits).

- [ ] **Step 3: Verify the version bump is reflected**

```bash
grep -n 'v0\.2\.5\|v0\.3\.0\|0\.2"\|0\.3"' docs/specs/file-exchange.md
```

Expected: 
- The title block says `**Version:** 0.3.0`.
- The two capability example blocks both say `"version": "0.3"`.
- Any in-prose references to "v0.3" (in the new subsection, in §Security's new paragraph, in §Method priority's new note) are correct.
- There may still be lingering `v0.2.5` references in *historical* prose (e.g. discussing what v0.2.5 did) — those are intentional and should stay. Distinguish "historical reference to what v0.2.5 said" from "claim about the current spec version."

- [ ] **Step 4: Run `markdownlint` if available**

```bash
which markdownlint && markdownlint docs/specs/file-exchange.md || echo "(markdownlint not installed; skipping)"
```

If markdownlint is available, address any new warnings. If not, skip.

- [ ] **Step 5: Confirm no other source-tree file references `0.2.5` as the current spec version**

```bash
grep -rnE '\bv?0\.2\.5\b' src/ tests/ docs/ README.md CLAUDE.md CHANGELOG.md 2>&1 | head -30
```

Expected results:
- `src/fastmcp_pvl_core/file_exchange.py` mentions `(spec v0.2.5)` in the module docstring (post-#72 polish). This is now STALE because the spec doc is at 0.3.0. However: the implementation may not yet emit the 0.3 nested-shape capability (that realignment is #74's job). The docstring describing the spec the implementation targets is therefore now an inconsistency.
- Decide: either (a) leave the docstring as-is and let #74 update it when the implementation realigns, or (b) update the docstring now to `(spec v0.3.0)` while flagging that the *implementation* still emits a v0.4-amendments-era shape pending #74.
- The plan recommends (a): the spec PR is spec-only, and updating implementation prose to match a spec the implementation doesn't yet implement is misleading. Note the inconsistency in the PR body so #74 picks it up.
- Other matches (CHANGELOG, etc.) should be left intact — they record what v0.2.5 said historically.

No commit at this task; verification only. If any of Steps 1–3 turn up an issue, return to the relevant earlier task to fix.

---

## Task 7: Local review circus + open draft PR

**Files:** none modified by the agent; this is the harness-level step that delegates to subagents.

- [ ] **Step 1: Sync branch state**

```bash
git fetch origin main
git log --oneline origin/main..HEAD
```

Confirm: the commit list shows the design doc (`5b448a9`), then the per-task commits from Tasks 1–5 in this plan.

- [ ] **Step 2: Dispatch the primary reviewer**

Use the Agent tool with `subagent_type: "pr-review-toolkit:code-reviewer"`. Prompt includes:

- Goal of the PR (spec evolution for `http_upload`, issue #71).
- Design doc path: `docs/superpowers/specs/2026-05-15-reverse-http-transfer-spec-design.md`.
- Cumulative diff via `git diff origin/main...HEAD`.
- Explicit instruction to read `docs/specs/file-exchange.md` end-to-end as a *whole document* (not just the diff hunks). A spec doc has to read coherently top-to-bottom; diff-only review will miss flow problems.
- Note that this PR is spec-only; no Python code; the implementation lives in #74.

- [ ] **Step 3: Dispatch the second-opinion reviewer**

Use the Agent tool with `subagent_type: "feature-dev:code-reviewer"`. Different focus:

- Apply the framing principle (from `CLAUDE.md` / `README.md`) to the new spec content. Specifically the classification test ("would pvl-core be wrong to make this decision itself?") doesn't apply directly to a wire-format spec, but the related principle does: "the spec describes wire format; implementation choices belong elsewhere." Verify the new section doesn't sneak in implementation choices (e.g., "use SQLite for token storage", "buffer the body in memory", etc.).
- Verify the new section doesn't contradict the existing v0.2.5 wording elsewhere in the spec doc (forward-compat statements, segment-rules section, etc.).
- Verify the asymmetric `destination` vs `origin_id` validation is consistent across §`create_upload_link` (Task 2), §Security (Task 4), and the worked examples (Task 2's examples).
- Verify the worked examples are syntactically valid (the JSON parses, the curl invocation is well-formed) — paste them into a JSON parser / dry-run them mentally.

- [ ] **Step 4: Address findings**

For any blocker or should-fix from either reviewer:
- Fix the prose; re-dispatch the relevant reviewer.
- OR defend in writing in a PR comment if the reviewer is wrong (per `superpowers:receiving-code-review`).

Iterate until both reviewers return clean.

- [ ] **Step 5: Open the PR as draft**

```bash
gh pr create --draft --title "docs(spec)!: add http_upload transfer method, bump to v0.3.0 (closes #71)" --body "$(cat <<'EOF'
Closes #71.  Refs #75 (umbrella), #74 (the matching implementation that lands after this spec ships).

## Summary

Adds the new \`http_upload\` transfer method to the MCP File Exchange spec.  Reverse-direction HTTP transfer: the receiver mints a one-time POST URL; any party with the URL (LLM/agent, server, human with curl) pushes bytes.  Bumps spec version to v0.3.0.

Spec-only PR.  No Python code, no tests.  Implementation tracked in #74.

## What's in the spec

- New \`#### http_upload\` subsection under \`### Transfer Methods\`, peer of the existing \`#### http\`.
- Receiver-side tool \`create_upload_link(origin_id, destination?, ttl_seconds?, max_bytes?, content_type?)\` returning \`{url, expires_in_seconds, max_bytes}\`.
- Sender-side tool \`upload(url, source, content_type?)\` (optional) returning \`{status, body}\`.
- POST contract: one-time URL with ≥128-bit token, status-code classes (2xx success, 404 token, 413 size, 415 type, 4xx domain, 5xx server), \`transfer_failed\` envelope on structured 4xx.
- Two worked examples: agent-push (curl) and MCP-mediated push (mover-server pattern).

## Key design decisions

- **WHAT/WHERE split**: \`origin_id\` (sender's opaque WHAT, strict origin_id rules) + \`destination\` (sender's WHERE, receiver-validated, relaxed character rules).  Solves the \"vault-root-only\" pain point A11 created by collapsing both roles into one segment-rule-strict identifier.
- **Independent peer method**: \`http_upload\` is a top-level transfer method, not nested under \`http\`.  Backward compat with v0.2 is automatic (unknown methods silently skipped per the existing spec rule).
- **Wire-optional sender tool**: the \`upload\` tool standardizes MCP-mediated push between MCP servers, but the spec explicitly notes any HTTP client (curl, browser, custom code) is a valid sender — mirroring how \`fetch\` is wire-optional on the existing download side.

## What's NOT in this PR

- **Implementation** of \`register_file_exchange_upload\` against the new spec — that's #74.
- **markdown-vault-mcp migration** — tracked in pvliesdonk/markdown-vault-mcp#488 (the downstream issue whose maintainer drove the WHAT/WHERE framing in the brainstorm).
- **Template scaffold update** — folds into pvliesdonk/fastmcp-server-template#131 alongside #74.
- **\`SPEC_VERSION = \"0.4\"\` in the implementation** — known stale (the v0.4 amendments were reverted in #77 / PR #77).  The implementation will realign to advertise \`\"0.3\"\` when #74 lands.  The implementation docstring in \`src/fastmcp_pvl_core/file_exchange.py\` still says \`(spec v0.2.5)\` post-#72; that's now also stale and will be corrected by #74.

## Local review

Two-subagent local review circus run on the cumulative diff before opening:
- \`pr-review-toolkit:code-reviewer\` — see review thread.
- \`feature-dev:code-reviewer\` — see review thread.

## Test plan

- [x] No Python code; no tests added by this PR.
- [x] Spec self-review clean (per the brainstorming-skill self-review checklist).
- [x] All \`http_upload\` / \`create_upload_link\` / \`destination\` references in the spec are consistent (single naming).
- [x] Asymmetric validation rules (\`origin_id\` strict vs \`destination\` relaxed) consistent across the new \`#### http_upload\` subsection and the §Security addition.
- [x] Capability examples in §Discovery updated to advertise \`\"version\": \"0.3\"\` and include the new method on the receiver-side example.
- [x] Method-priority note added explaining why \`http_upload\` isn't in the priority list.
- [ ] CI green (verify after push — only the markdown lint / link-check workflows, if any, will exercise this change).
- [ ] Bot review clean.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Wait for CI + claude-review, address findings, flip ready**

Per the project's standard PR workflow (in `~/.claude/CLAUDE.md`):
- Watch CI completion.
- Read claude-review's full body (not just the check status).
- If clean: flip ready (`gh pr ready <N>`) — gemini auto-runs on flip per the repo's `.gemini/config.yaml`.
- If bot finds something: address per the one-round iteration cap; surface to user if a third round is needed.

---

## Out of scope (explicit)

- **Python implementation** of `register_file_exchange_upload` against this new spec → tracked in #74. The spec PR does NOT touch any file under `src/` or `tests/`.
- **`src/fastmcp_pvl_core/file_exchange.py` module docstring** still saying `(spec v0.2.5)` — now stale relative to the spec doc, but updating it requires coordinating with the implementation's capability advertisement (`SPEC_VERSION = "0.4"`), which is #74's job. The PR body notes this inconsistency explicitly.
- **markdown-vault-mcp migration** → pvliesdonk/markdown-vault-mcp#488.
- **Template scaffold updates** for the new method → folds into pvliesdonk/fastmcp-server-template#131 alongside #74.
- **CHANGELOG.md entry** for v0.3.0 of the spec. The spec doc is versioned independently from the Python package (which is at 3.0.0 post-#72). A spec-version bump does not require a CHANGELOG entry on the Python-package side; the spec file's own version field is the authoritative record. *If* the package release notes should mention the spec bump, that's covered when the next package release ships (post-#74) and is out of scope here.

## Acceptance (from issue #71)

- [x] Design discussion completed with input from a downstream implementor (markdown-vault-mcp via pvliesdonk/markdown-vault-mcp#488). [User is the markdown-vault-mcp maintainer; drove the WHAT/WHERE framing in the brainstorm.]
- [ ] Spec PR merges the new prose into `docs/specs/file-exchange.md` as a clean addition. **No "amendment" or "proposal" framing.** [Task 2 adds a `####` subsection peer of the existing `#### http`; no amendments framing.]
- [ ] Spec version bumped to v0.3.0; PR includes the bump. [Task 1.]
- [ ] Implementation tracked separately in #74; the spec PR does not include the implementation. [Out-of-scope section above.]
- [ ] **Template impact**: scaffolds may need new defaults; tracked in pvliesdonk/fastmcp-server-template#131. [Out-of-scope section above.]
