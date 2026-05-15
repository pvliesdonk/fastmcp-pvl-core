# http_upload sender `source` spec fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strip the `http_upload` sender-side `source` tagged-union machinery from `docs/specs/file-exchange.md` (it is implementation detail, not wire format), replacing it with an opaque `origin_id` parameter, and fold in a clarification of the HTTP-server-capability asymmetry.

**Architecture:** Spec-document-only change — verbatim edits to one file, `docs/specs/file-exchange.md`. Three substantive tasks (the capability-shape strip; the `upload`-tool-section strip; the HTTP-capability clarification), one commit each, then a verification + draft-PR task. The spec version stays `0.3.0` — fix in place, no bump (pre-release draft spec; the sender side has zero implementations).

**Tech Stack:** Markdown. No code, no tests. The implementation of the sender `upload` tool lands separately in #85.

**Design doc:** `docs/superpowers/specs/2026-05-15-http-upload-source-spec-fix-design.md` (issue #93).

---

## File Structure

- `docs/specs/file-exchange.md` — the only file changed. Edits land in three section areas: §"Transfer Methods / `http_upload`" (the capability-shape examples and the sender-tool subsection), and §"Transfer Methods / `http`" + §"Transfer Methods / `http_upload`" (the HTTP-capability clarification).

Every edit below gives the exact `old_string` to find and the exact `new_string`. The `old_string`s are quoted verbatim from the current file. If any does not match, STOP and report BLOCKED — the file may have drifted.

---

## Task 1: Remove `source_variants` from the `http_upload` capability shape

**Files:**
- Modify: `docs/specs/file-exchange.md` — §"Transfer Methods / `http_upload`", the capability examples and the role-field list.

- [ ] **Step 1: Sender-only capability example — drop `source_variants`**

Find:

````
A sender-only server (the sender side is optional):

```json
"http_upload": {
  "source": {
    "tool": "upload",
    "source_variants": ["path", "exchange_uri", "http_url", "inline_b64"]
  }
}
```
````

Replace with:

````
A sender-only server (the sender side is optional):

```json
"http_upload": {
  "source": {"tool": "upload"}
}
```
````

- [ ] **Step 2: Both-roles capability example — drop `source_variants`**

Find:

```
  "source": {"tool": "upload", "source_variants": ["path"]},
```

Replace with:

```
  "source": {"tool": "upload"},
```

- [ ] **Step 3: Remove the `source_variants` field-list bullet**

Find (the `source_variants` bullet, including the preceding bullet's trailing text as an anchor):

```
- `accepts` / `max_bytes` / `max_ttl_seconds` (`sink` only) — the receiver's admission policy: accepted `Content-Type` filter, body-size ceiling, TTL ceiling.
- `source_variants` (SHOULD, `source` only) — array of the `source` tagged-union variants the sender's tool implements. Allows callers to pre-filter and avoid round-trips on unsupported variants. If omitted, callers MUST assume only `path` is supported (the lowest-common-denominator variant per the `source` table below).
```

Replace with:

```
- `accepts` / `max_bytes` / `max_ttl_seconds` (`sink` only) — the receiver's admission policy: accepted `Content-Type` filter, body-size ceiling, TTL ceiling.
```

- [ ] **Step 4: Delete the `source` role-key vs `source` parameter disambiguation paragraph**

Find:

```
The role is identified by **sub-key presence** (`source` vs `sink`), not by the tool-name string (which is implementation-defined). A server that implements both roles advertises both sub-keys — the `source`/`sink` structure expresses dual-role servers without ambiguity.

The `source` role sub-key here is a capability-declaration construct. It is distinct from the `source` *parameter* on the `upload` tool (the tagged-union payload variant, defined below): same word, different protocol levels — one names a server-wide role, the other a per-invocation argument.

**Receiver-side tool: `create_upload_link`**
```

Replace with:

```
The role is identified by **sub-key presence** (`source` vs `sink`), not by the tool-name string (which is implementation-defined). A server that implements both roles advertises both sub-keys — the `source`/`sink` structure expresses dual-role servers without ambiguity.

**Receiver-side tool: `create_upload_link`**
```

- [ ] **Step 5: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "$(cat <<'EOF'
docs(spec): remove source_variants from the http_upload capability shape (refs #93)

source_variants advertised the sender tool's byte-acquisition
variants — implementation detail, not wire format. The
http_upload.source sub-object becomes {tool: "upload"}, structurally
identical to every other tool-based role. The paragraph
disambiguating the capability source role-key from the (now removed)
source tool parameter goes with it.

Refs #93.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Replace the `upload` tool's `source` union with `origin_id`

**Files:**
- Modify: `docs/specs/file-exchange.md` — §"Transfer Methods / `http_upload`", the "Sender-side tool: `upload`" subsection and its worked example.

- [ ] **Step 1: Replace the `source` parameter row with `origin_id`**

Find:

```
| Param | Cardinality | Description |
|---|---|---|
| `url` | MUST | The receiver-issued POST endpoint (returned from `create_upload_link`). |
| `source` | MUST | Tagged union: exactly one of `{ "path": "<local path>" }`, `{ "exchange_uri": "exchange://..." }`, `{ "http_url": "https://..." }`, or `{ "inline_b64": "<base64 content>" }`. Implementations MAY support a subset; `path` is the lowest-common-denominator. |
| `content_type` | SHOULD | The MIME type the sender will declare in the POST `Content-Type` header. If omitted, the sender SHOULD sniff or default. |
```

Replace with:

```
| Param | Cardinality | Description |
|---|---|---|
| `url` | MUST | The receiver-issued POST endpoint (returned from `create_upload_link`). |
| `origin_id` | MUST | The sender's opaque stable handle for the bytes to push. Same raw-JSON validation rules as `origin_id` in the `http` method's `create_download_link` (no path separators `/` or `\`; not `.` or `..`; no null bytes / control characters; no leading or trailing whitespace). The sender resolves it to bytes by its own domain logic — a file, a database row, an in-memory object, anything; callers treat it as opaque. |
| `content_type` | SHOULD | The MIME type the sender will declare in the POST `Content-Type` header. If omitted, the sender SHOULD sniff or default. |
```

- [ ] **Step 2: Remove the `unsupported_source_variant` paragraph and envelope**

Find:

````
On 4xx with a structured `transfer_failed` envelope, the sender tool SHOULD unwrap and re-raise as a tool error, mirroring how the existing `http` method's `fetch` tool propagates `transfer_failed`.

When called with a `source` variant the sender tool does not implement, the tool MUST return an in-band failure envelope with `error: "unsupported_source_variant"` (a defined error code distinct from the generic `transfer_failed`-with-message form). The envelope MUST include the `url` parameter the caller passed (so a unified handler can correlate the error to the in-flight upload), SHOULD include a `requested_variant` field naming the variant the caller asked for, and SHOULD include a `supported_variants` field listing the variants the tool *does* implement — equivalent in content to the `source_variants` capability field if advertised:

```json
{
  "error": "unsupported_source_variant",
  "method": "http_upload",
  "url": "https://receiver.example/uploads/<token>",
  "requested_variant": "exchange_uri",
  "supported_variants": ["path"],
  "message": "this sender only implements 'path'; caller requested 'exchange_uri'"
}
```

Callers that pre-checked against `source_variants` in the capability declaration will normally avoid this error; the envelope exists for the case where the capability was unavailable or stale.

**Worked example — agent push:**
````

Replace with:

````
On 4xx with a structured `transfer_failed` envelope, the sender tool SHOULD unwrap and re-raise as a tool error, mirroring how the existing `http` method's `fetch` tool propagates `transfer_failed`.

**Worked example — agent push:**
````

- [ ] **Step 3: Update the "MCP-mediated push" worked example**

Find:

```
// step 2: mover.upload
{
  "url": "https://vault-mcp.example/uploads/<token>",
  "source": {"exchange_uri": "exchange://hades-01/mover-mcp/moved-from-vault.bin"},
  "content_type": "application/octet-stream"
}
```

Replace with:

```
// step 2: mover.upload
{
  "url": "https://vault-mcp.example/uploads/<token>",
  "origin_id": "moved-from-vault",
  "content_type": "application/octet-stream"
}
```

- [ ] **Step 4: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "$(cat <<'EOF'
docs(spec): http_upload upload tool takes origin_id, not a source union (refs #93)

The upload tool's `source` tagged union (path / exchange_uri /
http_url / inline_b64) prescribed how the sender locates its bytes —
sender-side implementation detail. Replace it with an opaque
origin_id parameter, mirroring the http method's create_download_link:
both are the source-side "share this resource" operation and take an
opaque, server-resolved handle. The unsupported_source_variant error
code (meaningless with no variants) is removed; the MCP-mediated-push
worked example uses origin_id.

Refs #93.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Fold in the HTTP-server-capability clarification

**Files:**
- Modify: `docs/specs/file-exchange.md` — §"Transfer Methods / `http`" and §"Transfer Methods / `http_upload`".

- [ ] **Step 1: §`http` — state the producer-serves / consumer-outbound-only rule**

Find:

```
The `http` method serves double duty: the generated URL can be used for server-to-server transfer (consumer calls its fetch tool with the URL) or for **direct human download** (the LLM includes the URL in its response for the user to click). This means the `http` method is useful even without a consuming server: a producer can generate a download link that the LLM presents to the user as a clickable link in the conversation.

In a file reference:
```

Replace with:

```
The `http` method serves double duty: the generated URL can be used for server-to-server transfer (consumer calls its fetch tool with the URL) or for **direct human download** (the LLM includes the URL in its response for the user to click). This means the `http` method is useful even without a consuming server: a producer can generate a download link that the LLM presents to the user as a clickable link in the conversation.

**HTTP-server capability.** The `http` method requires the *producer* to be reachable as an HTTP server — it mints and serves the download URL. The consumer needs only the ability to make an *outbound* HTTP request; it does not accept inbound connections and need not itself be an HTTP-transport MCP server. A stdio MCP server can therefore be the consumer for `http` (it issues an outbound `GET`), but cannot be the producer. The spec cares about the *capability* — can a side serve a URL, can it make outbound requests — not how the server obtains HTTP access.

In a file reference:
```

- [ ] **Step 2: §`http_upload` — tighten the motivating paragraph into the symmetric form**

Find:

```
The reverse of the `http` method: the *receiver* mints a one-time POST URL; any party with the URL pushes bytes. The sender can be an LLM/agent, another MCP server, or a human with an HTTP client (`curl`, browser, custom script) — the spec does not constrain who pushes. The motivating use case is when the *sender* cannot serve an HTTP endpoint: with the `http` (download) method the consumer must pull bytes from a producer-served URL, which fails if the sender is a local agent, a `curl` invocation, or any client that can make outbound requests but cannot receive inbound connections. `http_upload` inverts the direction — the receiver issues the URL, the sender only needs outbound HTTP access to reach it.
```

Replace with:

```
The reverse of the `http` method: the *receiver* mints a one-time POST URL; any party with the URL pushes bytes. The sender can be an LLM/agent, another MCP server, or a human with an HTTP client (`curl`, browser, custom script) — the spec does not constrain who pushes.

**HTTP-server capability.** `http_upload` mirrors `http` with the roles inverted: the method requires the *receiver* to be reachable as an HTTP server — it mints and serves the upload URL. The sender needs only the ability to make an *outbound* HTTP request; it does not accept inbound connections and need not itself be an HTTP-transport MCP server. A stdio MCP server can therefore be the sender for `http_upload` (it issues an outbound `POST`), but cannot be the receiver. This is the method's reason to exist: the `http` (download) method requires the *producer* to serve the URL, so it cannot move bytes out of a producer that has no HTTP server; `http_upload` puts the URL-serving on the receiver instead. Between the two methods, whichever side can serve HTTP, one method places the URL-serving there; `exchange` (shared volume) covers the case where neither side can.
```

- [ ] **Step 3: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "$(cat <<'EOF'
docs(spec): clarify the http-family HTTP-server-capability asymmetry (refs #93)

The http and http_upload methods each require an HTTP server on
exactly one side — the side that mints and serves the URL. The other
side needs only outbound HTTP and may be a stdio MCP server. §http
gains an explicit producer-serves / consumer-outbound-only statement;
the §http_upload motivating paragraph is tightened into the crisp
symmetric form, noting the two methods are complementary.

Refs #93.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Verify, preflight-circus, draft PR

**Files:** none — verification and PR mechanics.

- [ ] **Step 1: Verify the strip is complete**

Run:

```bash
grep -nE 'source_variants|unsupported_source_variant|inline_b64|exchange_uri.*http_url|tagged union' docs/specs/file-exchange.md
```

Expected: no matches for `source_variants`, `unsupported_source_variant`, or the `source` tagged-union variant names in the sender context. (A surviving match means an edit was missed — fix it.) Note: `exchange_uri` may legitimately appear elsewhere if the spec references `exchange://` URIs by that phrase — inspect any match and confirm it is not the removed sender machinery.

- [ ] **Step 2: Verify the version line and JSON examples**

Confirm `**Version:** 0.3.0` is unchanged (line 3). Eyeball every JSON block touched by Tasks 1–3 — the `http_upload` capability examples and the `mover.upload` example — for balanced braces and valid JSON. Confirm the `upload` tool parameter table has exactly three rows: `url`, `origin_id`, `content_type`.

- [ ] **Step 3: Invoke the `preflight-circus` skill**

Run the `preflight-circus` skill on the cumulative diff (`git diff origin/main...HEAD`). Address every finding at confidence ≥ 80 before opening the PR. Re-run until the skill reports clean.

- [ ] **Step 4: Open the PR as draft**

```bash
git push -u origin spec/http-upload-source-fix-issue-93
gh pr create --draft --title "spec: http_upload sender source union → origin_id; HTTP-capability clarification (#93)" --body "$(cat <<'EOF'
## Summary

Strips the `http_upload` sender-side `source` tagged-union machinery from the file-exchange spec — it was implementation detail, not wire format — and folds in a clarification of the HTTP-server-capability asymmetry.

- The `upload` tool's `source` tagged union (`path` / `exchange_uri` / `http_url` / `inline_b64`) is replaced by an opaque `origin_id` parameter, mirroring the `http` method's `create_download_link`: both are the source-side "share this resource" operation and take an opaque, server-resolved handle.
- `source_variants` (capability field) and `unsupported_source_variant` (error code) are removed — with no variants, there is nothing to advertise or mismatch. `http_upload.source` becomes `{tool: "upload"}`.
- §`http` / §`http_upload` now state crisply that each method requires an HTTP server on exactly one side (the URL-minting side); the other side needs only outbound HTTP and may be a stdio MCP server.

Spec version stays `0.3.0` — fix-in-place of a pre-release draft spec; the sender side has zero implementations. The `http_upload` wire contract and the receiver side are unchanged.

## Test plan

- [ ] No `source_variants` / `unsupported_source_variant` / `source` tagged-union survives (grep).
- [ ] `upload` tool parameter table is `url` / `origin_id` / `content_type`.
- [ ] `**Version:**` line is `0.3.0`.
- [ ] All touched JSON examples parse.

Closes #93.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Watch CI + claude-review; flip ready when clean**

Per the project PR workflow: read `claude-review`'s body (not just the check status), address findings within the one-round iteration cap, flip ready (`gh pr ready <N>`) once CI is green and the bot body says LGTM.

---

## Summary

Four tasks, three substantive commits to one file. Task 1 removes `source_variants` from the capability shape. Task 2 replaces the `upload` tool's `source` union with `origin_id` and removes `unsupported_source_variant`. Task 3 folds in the HTTP-server-capability clarification. Task 4 verifies and opens the PR.

## What changes

- `docs/specs/file-exchange.md` — the `upload` tool parameter contract, the `http_upload` capability shape, and clarifying prose in §`http` / §`http_upload`.

## What does NOT change

- The `http_upload` wire contract (POST raw bytes + `Content-Type`; status codes; `transfer_failed` envelope).
- The receiver side (`create_upload_link`, the POST contract, `http_upload.sink`).
- The `http` and `exchange` methods' behaviour and wire contracts.
- The spec version (`0.3.0`).

## Local review

`preflight-circus` runs on the cumulative diff; clean at the ≥80 confidence bar before the PR opens.

## Test plan

- [ ] `grep` finds no `source_variants` / `unsupported_source_variant` / sender `source` tagged-union.
- [ ] The `upload` tool parameter table is exactly `url` / `origin_id` / `content_type`.
- [ ] `**Version:**` line is `0.3.0`.
- [ ] Touched JSON examples parse.
- [ ] CI green; bot review clean.

## Out of scope

- The sender-side `upload` tool **implementation** — #85 (resolves `origin_id` → a file-like object via a downstream domain hook, then POSTs).
- Any change to the receiver side, the `http` method, or `exchange`.

## Acceptance (from #93 / the design doc)

- [ ] The `upload` tool's `source` tagged union is replaced by an opaque `origin_id` parameter; `url` and `content_type` retained.
- [ ] `source_variants` removed from the capability shape (examples + field list).
- [ ] The `unsupported_source_variant` error code and envelope removed.
- [ ] The `source` role-key vs `source` parameter disambiguation paragraph removed.
- [ ] The "MCP-mediated push" worked example uses `origin_id`.
- [ ] §`http` and §`http_upload` state the HTTP-server-capability asymmetry crisply.
- [ ] The spec version stays `0.3.0`.
- [ ] The `http_upload` wire contract and the receiver side are unchanged.
