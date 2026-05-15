# dual-role capability shape — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change the file-exchange spec's capability declaration so a server implementing both roles of a bidirectional method can advertise both — by giving each tool-based method `source` / `sink` role sub-objects.

**Architecture:** Spec-only edit. One file changed (`docs/specs/file-exchange.md`). Each task is a focused prose/JSON edit with the exact old and new text. No Python, no tests. **The spec version stays `0.3.0`** — this is a fix-in-place of an unimplemented spec version, not a version bump. Implementation (the capability-builder rework) lands in #74.

**Tech Stack:** Markdown (spec doc).

**Spec:** [`docs/superpowers/specs/2026-05-15-dual-role-capability-shape-design.md`](../specs/2026-05-15-dual-role-capability-shape-design.md) (commit `7989636`).

---

## Task 1: §"Transfer Methods" intro — structural rule + `transfer`/`transfer_methods` distinction

**Files:** Modify `docs/specs/file-exchange.md` (the `### Transfer Methods` intro, currently lines ~171–175).

- [x] **Step 1: Replace the intro**

Find this block:

```markdown
### Transfer Methods

A transfer method defines how a file moves from a producing server to a consuming server. The spec defines two methods; future extensions may add more.

Each method is identified by a string key (e.g. `"exchange"`, `"http"`) and has method-specific metadata in both the file reference and the capability declaration.
```

Replace with:

```markdown
### Transfer Methods

A transfer method defines how a file moves between a *source* server (where the bytes originate) and a *sink* server (where they land). The spec defines three methods (`exchange`, `http`, `http_upload`); future extensions may add more.

Each method is identified by a string key (e.g. `"exchange"`, `"http"`, `"http_upload"`) and has method-specific metadata in both the file reference and the capability declaration.

**Capability-declaration shape.** Within the capability declaration's `transfer_methods` object, every *tool-based* method declares its tool(s) under `source` / `sink` role sub-objects. `source` is the endpoint bytes originate from; `sink` is the endpoint bytes land at. Within each role sub-object, `tool` is the one mandatory field; any further fields are method-specific metadata a caller needs up front. A server populates whichever role(s) it implements — both sub-keys for a server that fills both roles of a method, one for a single-role server. The role is identified by sub-key presence, never by the tool-name string (tool names are implementation-defined). `exchange` is the sole *tool-less* method and carries `{}`.

**`transfer` (file reference) vs `transfer_methods` (capability declaration).** These are different objects with different shapes; do not conflate them. The `transfer` object inside a file reference is per-file, producer-emitted, and inherently single-role — it advertises how to retrieve one specific file — so it stays flat (`{tool: ...}`, no `source`/`sink`). The `transfer_methods` object in a capability declaration is server-wide and describes every role the server fills, so it uses the `source`/`sink` sub-objects described above.
```

- [x] **Step 2: Verify**

```bash
cd /mnt/code/fastmcp-pvl-core
sed -n '171,185p' docs/specs/file-exchange.md
```

Expected: the new intro with the structural-rule paragraph and the `transfer`-vs-`transfer_methods` paragraph; the stale "two methods" count is now "three methods".

- [x] **Step 3: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): add source/sink capability structural rule (refs #83)

The §Transfer Methods intro gains the structural rule for the
capability declaration: tool-based methods declare tools under
source/sink role sub-objects (tool mandatory per sub-object);
exchange is the tool-less exception.  Also adds the explicit
transfer (file-ref, flat, single-role) vs transfer_methods
(capability, role-keyed, multi-role) distinction so the shape
asymmetry reads as design.  Stale 'two methods' count corrected
to three.

Refs #83."
```

---

## Task 2: §"`http`" — capability examples → `source`/`sink`

**Files:** Modify `docs/specs/file-exchange.md` (the `#### \`http\` (download URL)` capability-declaration examples, currently lines ~211–225).

- [x] **Step 1: Replace the capability examples**

Find this block:

```markdown
In a capability declaration (producer):

```json
"http": {
  "tool": "create_download_link"
}
```

In a capability declaration (consumer):

```json
"http": {
  "tool": "fetch"
}
```
```

Replace with:

```markdown
In a capability declaration, the `http` method uses `source` / `sink` role sub-objects (see §"Transfer Methods" → "Capability-declaration shape"). The `source` role is the producer (mints the download URL via `create_download_link`); the `sink` role is the consumer (fetches from the URL).

A producer-only server:

```json
"http": {
  "source": {"tool": "create_download_link"}
}
```

A consumer-only server:

```json
"http": {
  "sink": {"tool": "fetch"}
}
```

A server that is both producer and consumer populates both sub-keys:

```json
"http": {
  "source": {"tool": "create_download_link"},
  "sink": {"tool": "fetch"}
}
```
```

- [x] **Step 2: Verify**

```bash
sed -n '197,240p' docs/specs/file-exchange.md
```

Expected: the `http` capability examples now show `source`/`sink` sub-objects, including the both-roles example. The "In a file reference" block above (`"http": {"tool": "create_download_link"}`) is **unchanged** — the file_ref `transfer` object stays flat.

- [x] **Step 3: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): http capability examples use source/sink (refs #83)

The §http capability-declaration examples move from the flat
{tool: <name>} shape to source/sink role sub-objects, with a
both-roles example showing a server that is producer and consumer
simultaneously.  The file-reference 'transfer' block stays flat
(unchanged).

Refs #83."
```

---

## Task 3: §"`http_upload`" — capability examples → `source`/`sink`, rewrite the dual-role paragraph

**Files:** Modify `docs/specs/file-exchange.md` (the `#### \`http_upload\`` capability-declaration block, currently lines ~241–264).

- [x] **Step 1: Replace the capability block + dual-role paragraph**

Find this block (it runs from "In a capability declaration (receiver):" through the paragraph ending "...if simultaneous dual-role advertisement becomes a common need."):

```markdown
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
"http_upload": {
  "tool": "upload",
  "source_variants": ["path", "exchange_uri", "http_url", "inline_b64"]
}
```

- `tool` (MUST) — name of the POST-perform tool.
- `source_variants` (SHOULD) — array of the `source` tagged-union variants the sender's tool implements. Allows callers to pre-filter and avoid round-trips on unsupported variants. If omitted, callers MUST assume only `path` is supported (the lowest-common-denominator variant per the `source` table below).

Both sides advertise the same key (`http_upload`) with the same `{tool: <name>, ...}` shape. The role is identified by **field presence**, not by the tool name (which is implementation-defined): receiver-side blocks carry `accepts` / `max_bytes` / `max_ttl_seconds`; sender-side blocks carry `source_variants`. A client classifying a peer's capability MUST look at these field presences, not at the string value of `tool`.

A single server that implements both roles cannot express both in a single `http_upload` capability block — the JSON object has one value per key, and the receiver/sender shapes are not isomorphic. Such a server advertises only the side relevant to its peer's use case in any given handshake (receiver-side when the peer will push bytes to it; sender-side when it acts as the pusher). A future spec version MAY introduce explicit sub-keying (`http_upload.receiver` / `http_upload.sender`) if simultaneous dual-role advertisement becomes a common need.
```

Replace with:

```markdown
In a capability declaration, the `http_upload` method uses `source` / `sink` role sub-objects (see §"Transfer Methods" → "Capability-declaration shape"). The `sink` role is the receiver (mints the upload URL via `create_upload_link`, accepts the bytes); the `source` role is the sender (POSTs the bytes via `upload`). Note the asymmetry with `http`: for `http` the `source` mints the URL, for `http_upload` the `sink` mints it — the role names track data direction; the mint mechanics are defined per method.

A receiver-only server:

```json
"http_upload": {
  "sink": {
    "tool": "create_upload_link",
    "accepts": ["application/pdf", "text/markdown"],
    "max_bytes": 10485760,
    "max_ttl_seconds": 3600
  }
}
```

A sender-only server (the sender side is optional):

```json
"http_upload": {
  "source": {
    "tool": "upload",
    "source_variants": ["path", "exchange_uri", "http_url", "inline_b64"]
  }
}
```

A server that implements both roles populates both sub-keys:

```json
"http_upload": {
  "source": {"tool": "upload", "source_variants": ["path"]},
  "sink": {
    "tool": "create_upload_link",
    "accepts": ["*/*"],
    "max_bytes": 10485760,
    "max_ttl_seconds": 3600
  }
}
```

Fields within each role sub-object:

- `tool` (MUST, both roles) — name of the MCP tool for that role (`create_upload_link` on the `sink` side, `upload` on the `source` side; the names are implementation-defined).
- `accepts` / `max_bytes` / `max_ttl_seconds` (`sink` only) — the receiver's admission policy: accepted `Content-Type` filter, body-size ceiling, TTL ceiling.
- `source_variants` (SHOULD, `source` only) — array of the `source` tagged-union variants the sender's tool implements. Allows callers to pre-filter and avoid round-trips on unsupported variants. If omitted, callers MUST assume only `path` is supported (the lowest-common-denominator variant per the `source` table below).

The role is identified by **sub-key presence** (`source` vs `sink`), not by the tool-name string (which is implementation-defined). A server that implements both roles advertises both sub-keys — the `source`/`sink` structure expresses dual-role servers without ambiguity.
```

- [x] **Step 2: Verify**

```bash
sed -n '233,290p' docs/specs/file-exchange.md
```

Expected: receiver/sender/both examples use `source`/`sink`; the old "a single server ... cannot express both ... future spec version MAY introduce explicit sub-keying" paragraph is gone, replaced by the sub-key-presence statement. No `http_upload.receiver`/`http_upload.sender` strings remain (the design chose `source`/`sink`, not `receiver`/`sender`).

- [x] **Step 3: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): http_upload capability examples use source/sink (refs #83)

The §http_upload capability-declaration block moves to source/sink
role sub-objects.  The receiver's admission-policy fields (accepts,
max_bytes, max_ttl_seconds) nest under sink; source_variants nests
under source.  The dual-role paragraph that said 'a single server
cannot express both roles ... a future spec version MAY introduce
sub-keying' is rewritten — this change IS that future; source/sink
expresses dual-role servers directly.  Role discrimination moves
from field-presence to sub-key-presence.

Refs #83."
```

---

## Task 4: §"Discovery / Capability declaration" — worked examples + `transfer_methods` field row

**Files:** Modify `docs/specs/file-exchange.md` (the `#### Capability declaration` worked examples + capability field table, currently lines ~519–590).

- [x] **Step 1: Update the producer example's `transfer_methods`**

Find (inside the **Producer example:** JSON block):

```json
        "transfer_methods": {
          "exchange": {},
          "http": {
            "tool": "create_download_link"
          }
        }
```

Replace with:

```json
        "transfer_methods": {
          "exchange": {},
          "http": {
            "source": {"tool": "create_download_link"}
          }
        }
```

- [x] **Step 2: Update the consumer example's `transfer_methods`**

Find (inside the **Consumer example:** JSON block):

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

Replace with:

```json
        "transfer_methods": {
          "exchange": {},
          "http": {
            "sink": {"tool": "fetch"}
          },
          "http_upload": {
            "sink": {
              "tool": "create_upload_link",
              "accepts": ["application/pdf", "text/markdown"],
              "max_bytes": 10485760,
              "max_ttl_seconds": 3600
            }
          }
        }
```

- [x] **Step 3: Update the `transfer_methods` field-table row**

Find:

```markdown
| `transfer_methods` | MUST | Object whose keys are supported transfer method names. Values contain method-specific configuration (e.g. tool names). |
```

Replace with:

```markdown
| `transfer_methods` | MUST | Object whose keys are supported transfer method names. For tool-based methods (`http`, `http_upload`) the value carries `source` / `sink` role sub-objects, each with a `tool` field plus method-specific metadata; a server populates whichever role(s) it fills. `exchange` carries `{}`. See §"Transfer Methods". |
```

- [x] **Step 4: Update the capability-discovery bullet**

Find:

```markdown
- Which transfer methods are available between any two servers (intersection of their `transfer_methods` keys).
```

Replace with:

```markdown
- Which transfer methods are available between any two servers, and in which direction — by matching one server's `source` role for a method against the other server's `sink` role.
```

- [x] **Step 5: Verify**

```bash
sed -n '519,592p' docs/specs/file-exchange.md
```

Expected: both worked examples use `source`/`sink`; the producer advertises `http.source`, the consumer advertises `http.sink` and `http_upload.sink`; the field-table row and the discovery bullet are updated.

- [x] **Step 6: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): capability-declaration worked examples use source/sink (refs #83)

The §Discovery producer and consumer worked examples advertise
transfer_methods with source/sink role sub-objects.  The
transfer_methods field-table row and the capability-discovery
bullet are updated to describe the role-keyed shape.

Refs #83."
```

---

## Task 5: §"Transfer Negotiation / Step 1" — role-aware method selection

**Files:** Modify `docs/specs/file-exchange.md` (`### Step 1: Method selection`, currently line ~654).

- [x] **Step 1: Replace the capability-aware-client paragraph**

Find:

```markdown
**Capability-aware client:** intersect the file reference's `transfer` keys with the consumer's `transfer_methods` keys, restricted to pull-direction methods (those that appear in a file reference's `transfer` object — currently `exchange` and `http`). Pick the highest-priority method that both sides support. Push-direction methods like `http_upload` are NOT part of this intersection because they don't appear in file references; they are selected separately by looking for the receiver-side shape (field-presence test: `accepts` / `max_bytes` / `max_ttl_seconds`) in the destination server's capability declaration.
```

Replace with:

```markdown
**Capability-aware client:** the file reference's `transfer` object lists the methods the *producer* (the `source`) supports for this file. For each, check whether the destination server advertises the matching `sink` role for that method in its `transfer_methods` — for `http`, the consumer needs `transfer_methods.http.sink`; for `exchange`, a matching `exchange_id`. Pick the highest-priority method where the producer's `source` side and the consumer's `sink` side both line up. `http_upload` does not appear in file references (it is push-direction); a client pushing bytes *into* a server instead looks for `transfer_methods.http_upload.sink` in that server's capability declaration.
```

- [x] **Step 2: Verify**

```bash
sed -n '652,658p' docs/specs/file-exchange.md
```

Expected: the capability-aware-client paragraph now describes matching `source` against `sink`; no stale "field-presence test" wording remains.

- [x] **Step 3: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): role-aware method selection in Transfer Negotiation (refs #83)

§Step 1 method selection updated for the source/sink capability
shape: a capability-aware client matches the producer's source
role for a method against the consumer's sink role, rather than
intersecting bare method keys.  The post-#82 field-presence-test
wording for http_upload is replaced with the sink-sub-key lookup.

Refs #83."
```

---

## Task 6: §"Versioning and compatibility" — flat-form reading note + v0.4-skipped note

**Files:** Modify `docs/specs/file-exchange.md` (`### Versioning and compatibility`, currently ends ~line 838, before `### Mixed-OS exchange groups`).

- [x] **Step 1: Append two paragraphs to the end of the section**

Find the last paragraph of `### Versioning and compatibility` (it ends):

```markdown
Otherwise bump minor and document the new constructs. §"Adding future methods" defines the structural recipe for declaring a method; this section governs whether the declaration also requires a spec-version bump.
```

Replace it with that same paragraph followed by the two new paragraphs:

```markdown
Otherwise bump minor and document the new constructs. §"Adding future methods" defines the structural recipe for declaring a method; this section governs whether the declaration also requires a spec-version bump.

**Reading a pre-`source`/`sink` capability declaration.** A server emitting a v0.2.x-era capability declares `transfer_methods.http` as a flat `{tool: <name>}` with no `source` / `sink` sub-objects. A reader encountering a flat `http` block treats it as a single-role declaration and infers the role from the peer's `produces` / `consumes`: a non-empty `produces` means the flat tool is the `source`-side tool; a non-empty `consumes` means it is the `sink`-side tool. A v0.2.x server cannot be both at once — that limitation is exactly what the `source` / `sink` shape resolves.

**Version `0.4` is permanently skipped.** The `0.4` label was used by an earlier set of inline amendments that were later reverted, and by a stale implementation-side version constant; reusing the number would be ambiguous about which `0.4` is meant. The minor release after `0.3` is `0.5`.
```

- [x] **Step 2: Verify**

```bash
sed -n '826,846p' docs/specs/file-exchange.md
```

Expected: the §Versioning section ends with the flat-form reading note and the v0.4-skipped note; `### Mixed-OS exchange groups` follows.

- [x] **Step 3: Commit**

```bash
git add docs/specs/file-exchange.md
git commit -m "docs(spec): versioning notes — flat-form reading + v0.4 skipped (refs #83)

Two additions to §Versioning and compatibility:
- How to read a pre-source/sink (v0.2.x) flat http capability
  block: single-role, role inferred from produces/consumes.
- v0.4 is permanently skipped — the label is burned by the
  reverted inline amendments and a stale version constant; the
  minor release after 0.3 is 0.5.

Refs #83."
```

---

## Task 7: End-to-end verification, preflight-circus, draft PR

**Files:** none modified — verification + PR-mechanics step.

- [x] **Step 1: Read the modified spec end-to-end for the touched sections**

```bash
cd /mnt/code/fastmcp-pvl-core
git diff origin/main...HEAD -- docs/specs/file-exchange.md
```

Confirm:
- The spec version line is still `**Version:** 0.3.0` (no bump — this is fix-in-place).
- No flat `http: {tool: ...}` capability-declaration blocks remain (the file_ref `transfer.http: {tool: ...}` block IS expected to stay flat — verify that one was NOT changed).
- `source` / `sink` used consistently; no stray `receiver`/`sender` sub-keys (the design chose `source`/`sink`).
- The structural rule, the `transfer`/`transfer_methods` distinction, the role-aware method-selection, and both versioning notes are all present.

- [x] **Step 2: Naming-consistency grep**

```bash
grep -nE '"http":\s*\{\s*"tool"|"http_upload":\s*\{\s*"tool"' docs/specs/file-exchange.md
```

Expected: matches ONLY inside the file-reference `transfer` examples (which stay flat), NOT inside any `transfer_methods` capability block. If a `transfer_methods` block still shows a flat `{tool: ...}`, it was missed — fix it.

- [x] **Step 3: Invoke the `preflight-circus` skill**

Per `~/.claude/CLAUDE.md`, the `preflight-circus` skill (`~/.claude/skills/preflight-circus/`) runs before any PR-creating push. Invoke it on `BASE..HEAD` where `BASE = $(git merge-base HEAD origin/main)`. It runs the five core lenses + the `pr-review-toolkit:code-reviewer` supplementary lens, scores findings at ≥80 confidence, and returns clean-or-findings. Address every ≥80 finding (fix, or defend in writing if the lens misread), re-running the affected lens until clean.

- [x] **Step 4: Open the PR as draft**

```bash
git push -u origin spec/dual-role-capability-issue-83
gh pr create --draft --title "docs(spec): capability declaration expresses dual-role servers (closes #83)" --body "$(cat <<'EOF'
Closes #83. Refs #75 (umbrella), #74 (blocked on this — its capability-builder rework targets this corrected shape).

## Summary

Fixes a wire-format gap in the file-exchange capability declaration: `transfer_methods.<method>` held a single `{tool: <name>}` value and could not express a server implementing both roles of a bidirectional method (`http`: producer + consumer; `http_upload`: receiver + sender). v0.4-amendments A10/A11 hinted at this; the v0.3 `http_upload` section explicitly deferred it ("a future spec version MAY introduce sub-keying"). This is that future.

**Spec-only PR. The spec version stays `0.3.0`** — v0.3.0 was never published or implemented (the bug was found by #74, the first implementation attempt), so this is a pre-first-implementation correction, not a post-release amendment.

## What changes

- Tool-based methods (`http`, `http_upload`) declare tools under **`source` / `sink` role sub-objects** in the capability declaration. `source` = bytes originate here; `sink` = bytes land here. A both-roles server populates both; a single-role server populates one; role detection is sub-key presence.
- A stated **structural rule**: `tool` is mandatory per role sub-object; method-specific metadata is additional; `exchange` is the documented tool-less exception (`{}`).
- The `file_ref` `transfer` object stays flat — it is per-file, producer-emitted, single-role. The spec now states the `transfer` vs `transfer_methods` distinction explicitly.
- Method selection (§Transfer Negotiation) becomes role-aware: match a producer's `source` against a consumer's `sink`.
- §Versioning gains: how to read a pre-`source`/`sink` (v0.2.x) flat block, and a note that **v0.4 is permanently skipped** (burned by the reverted amendments + a stale constant; next minor after 0.3 is 0.5).

## What does NOT change

- Spec version stays `0.3.0` — fix-in-place.
- Method *mechanics* (URL minting, POST contract, status codes, tool contracts) — unchanged; this PR is purely the capability-declaration shape.
- The `file_ref` `transfer` object — stays flat.
- No Python — the capability-builder rework lands in #74.

## Local review

`preflight-circus` (five core lenses + `pr-review-toolkit:code-reviewer`) run on the cumulative diff; clean at the ≥80 confidence bar before opening.

## Test plan

- [x] No code; no tests.
- [x] Spec version unchanged at `0.3.0`.
- [x] No flat `{tool: ...}` capability blocks remain; file_ref `transfer` blocks correctly stay flat.
- [x] `source`/`sink` used consistently across §Transfer Methods, §Discovery, §Transfer Negotiation.
- [x] CI green.
- [x] Bot review clean.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [x] **Step 5: Watch CI + claude-review; flip ready when clean**

Per the project PR workflow: watch CI, read claude-review's body (not just the check status), address findings within the one-round iteration cap, flip ready (`gh pr ready <N>`) when CI is green and the bot body says LGTM.

---

## Out of scope

- **Implementation** — the capability-builder rework in `_file_exchange_protocol.py`, `SPEC_VERSION` realignment, the upload helper — all land in #74 against this corrected spec.
- **Method mechanics** — URL minting, POST contract, status codes, tool contracts are unchanged.
- **The `file_ref` `transfer` object** — stays flat; documented as such, not restructured.

## Acceptance (from #83)

- [x] Spec PR: capability declaration expresses dual-role servers for `http` and `http_upload` via `source`/`sink` sub-objects (Tasks 1–4).
- [x] Spec version stays `0.3.0`; §Versioning states v0.4 is permanently skipped (Task 6).
- [x] `file_ref` `transfer` object explicitly documented as staying flat (Task 1).
- [x] Implementation tracked in #74 (out of scope here).
