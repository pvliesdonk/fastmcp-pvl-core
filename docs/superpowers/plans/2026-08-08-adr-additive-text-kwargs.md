# ADR 0001 amendment — additive-domain-text kwargs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Amend ADR 0001 to ratify a third kwarg category — additive-domain-text kwargs — so that Part B (issue #248) has a decision record to cite instead of contradicting.

**Architecture:** Docs-only. Three amendments to `docs/adr/0001-transfer-lift.md` (§2 item 1, §10 item 2, §5 table row), each using the repo's established post-hoc correction-block form rather than rewriting shipped ADR text. No source files change; no tests change.

**Tech Stack:** Markdown. The repo's `pytest` / `ruff` / `mypy` gate still runs, but only to prove nothing broke.

## Global Constraints

- **Do not rewrite shipped ADR prose in place.** ADR 0001 is a published decision record. Amendments use the `> **Correction (post-implementation):** …` blockquote form already present at `docs/adr/0001-transfer-lift.md:211-213`. The original sentence stays readable; the correction sits beside it.
- No source files are touched in this plan. If a step seems to require one, stop — that belongs to Part B.
- Markdown line length: match the file's existing wrapping (~76 chars). The repo's ruff config governs Python only, but the ADR is hand-wrapped and should stay so.
- Run the gate with `uv run` (`uv run pytest`, `uv run ruff check .`), never bare commands.
- Branch off `main`. One PR, closing the ADR-amendment issue.

## Why this is a separate PR from Part B

The first Part B attempt shipped the two kwargs *and* cited ADR §10 item 2 as their authority, while that clause said the only kwargs are `sink` and `validate`. The pre-flight gate returned `structural` — 11 findings at ≥80, nine traceable to that single unamended premise. A PR that both changes a rule and relies on it gives review nothing stable to check against, so the rule change lands first and alone.

## The category being ratified

Copy this wording verbatim where the tasks call for it:

> **Additive-domain-text kwargs** — optional strings a downstream appends to
> text pvl-core owns. They may only *add*: the core text always survives as
> the prefix, so the shape stays pvl-core's. Not a shape override, and not
> operator config.

---

### Task 1: Amend §10 item 2 (the guardrail the code cites)

**Files:**
- Modify: `docs/adr/0001-transfer-lift.md` (§10 item 2, currently at lines 433-436)

**Interfaces:**
- Consumes: nothing.
- Produces: the ratified category wording that Task 2 and Task 3 cross-reference, and that Part B's docstrings will cite.

- [ ] **Step 1: Read the current text to confirm it is unchanged**

Run: `sed -n '425,447p' docs/adr/0001-transfer-lift.md`

Expected — item 2 currently reads exactly:

```
2. **No shape-override kwargs.** Tool names, route path, status codes, and the
   scheme allowlist are pvl-core's. The only kwargs are the two hooks
   (`sink`, `validate`); the only tuning is env config. A reviewer rejects any
   kwarg that overrides a shape decision.
```

If it differs, stop and report — the plan was written against this text.

- [ ] **Step 2: Append the correction block**

Leave those four lines exactly as they are. Insert immediately after them (before the `3. **Opaque handle.**` item), indented to sit inside item 2:

```markdown
   > **Correction (post-implementation):** "the only kwargs are the two
   > hooks" is superseded. A third category is ratified:
   > **additive-domain-text kwargs** — optional strings a downstream appends
   > to text pvl-core owns. They may only *add*: the core text always
   > survives as the prefix, so the shape stays pvl-core's. Not a shape
   > override, and not operator config. `register_transfer_routes`'
   > `download_note` / `upload_note` are the first of these; they append a
   > domain sentence to a tool description whose generic text pvl-core still
   > owns and always emits first. The guardrail this item exists for is
   > unchanged: a kwarg that *replaces* pvl-core text, or changes a tool
   > name, route, or status code, is still rejected.
```

- [ ] **Step 3: Verify the rendering and the surrounding items**

Run: `sed -n '425,462p' docs/adr/0001-transfer-lift.md`

Check: the four original lines are intact and unedited; the blockquote is indented three spaces so it belongs to item 2; item 3 (`**Opaque handle.**`) still follows and is not absorbed into the quote.

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0001-transfer-lift.md
git commit -m "docs(adr): ratify additive-domain-text kwargs in §10 item 2

The guardrail said the only kwargs are sink and validate. Ratifies a
third category for optional strings a downstream appends to text
pvl-core owns — add-only, so the shape stays pvl-core's. The
shape-override prohibition the item exists for is unchanged."
```

---

### Task 2: Amend §2 item 1 (the decision summary)

**Files:**
- Modify: `docs/adr/0001-transfer-lift.md` (§2 item 1, currently at lines 64-71)

**Interfaces:**
- Consumes: the category ratified in Task 1.
- Produces: nothing further; Task 3 is independent of this one.

- [ ] **Step 1: Read the current text to confirm it is unchanged**

Run: `sed -n '62,73p' docs/adr/0001-transfer-lift.md`

Expected — item 1 currently ends with:

```
   resolution rather than owning them. Downstream implements **only** two
   domain hooks: a `TransferSink` (where bytes land) and a
   `TransferValidator` (what bytes are acceptable).
```

and its first line reads `register_transfer_routes(mcp, config, *, sink, validate)`.

If it differs, stop and report.

- [ ] **Step 2: Append the correction block**

Leave item 1's existing lines exactly as they are. Insert immediately after them (before `2. **\`fetch_url\` and \`decode_base64_capped\` are also first-class standalone`), indented three spaces:

```markdown
   > **Correction (post-implementation):** the signature shown omits later
   > additions. `register_transfer_routes` also accepts the optional
   > `download_note` / `upload_note` additive-domain-text kwargs (§10 item 2's
   > correction). "Downstream implements **only** two domain hooks" remains
   > true as written — a note is text, not an implementation — and the two
   > hooks are still the only things a downstream must supply.
```

- [ ] **Step 3: Verify**

Run: `sed -n '62,80p' docs/adr/0001-transfer-lift.md`

Check: item 1's original lines are intact; the blockquote is indented three spaces; item 2 of §2 still starts a new numbered item and is not swallowed by the quote.

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0001-transfer-lift.md
git commit -m "docs(adr): note the additive kwargs in §2 item 1's signature

The decision summary shows register_transfer_routes with sink and
validate only. Records the two optional note kwargs alongside it, and
confirms the 'only two domain hooks' claim still holds — a note is
text, not an implementation."
```

---

### Task 3: Amend the §5 module table row

**Files:**
- Modify: `docs/adr/0001-transfer-lift.md` (§5 table, `register.py` row, currently line 209)

**Interfaces:**
- Consumes: the category ratified in Task 1.
- Produces: nothing further.

- [ ] **Step 1: Read the current row**

Run: `sed -n '200,215p' docs/adr/0001-transfer-lift.md`

Expected — the `register.py` row currently reads:

```
| `register.py` | `register_transfer_routes(mcp, config, *, sink, validate)` — owns route + both link tools + TTL clamp + `base_url` guard, wiring one shared store | `register_transfer_routes` |
```

Note there is already a `> **Correction (post-implementation):** …` block below this table (for the `store.py` row). Your addition is a second bullet inside that same existing block — do not start a new block.

- [ ] **Step 2: Extend the existing correction block**

Find the existing correction block below the table. It currently begins:

```
> **Correction (post-implementation):** the `store.py` row's "Public export" originally
```

Leave its text intact. Append this as a new paragraph at the end of that same blockquote:

```markdown
>
> The `register.py` row's signature is also incomplete: `register_transfer_routes`
> additionally accepts the optional `download_note` / `upload_note`
> additive-domain-text kwargs (§10 item 2's correction). Its "Public export"
> column is unchanged and still correct.
```

- [ ] **Step 3: Verify the block reads as one unit**

Run: `sed -n '200,228p' docs/adr/0001-transfer-lift.md`

Check: there is exactly one correction block under the table, containing both the original `store.py` paragraph and your new `register.py` paragraph, separated by a `>` line. The table itself is unedited.

- [ ] **Step 4: Run the full gate**

The ADR is documentation, so nothing should move — this proves it.

```bash
uv sync --all-extras
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: `748 passed` (verified on `main` at `e4d72c6` while writing this plan), and all three static checks clean. This plan changes no code, so a differing count means something else is wrong — stop and report rather than adjusting the expectation.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0001-transfer-lift.md
git commit -m "docs(adr): note the additive kwargs in the §5 module table

Extends the existing post-implementation correction block under the
table rather than opening a second one."
```

---

### Task 4: Gate and PR

**Files:** none (verification only).

- [ ] **Step 1: Re-read the whole amended ADR for coherence**

Run: `grep -n "Correction (post-implementation)" docs/adr/0001-transfer-lift.md`

Expected: three hits — the pre-existing one under the §5 table (now extended), one in §2 item 1, one in §10 item 2.

Then read each in context and check they agree with each other: all three describe the same category, none claims the note kwargs can replace pvl-core text, and none contradicts §10 item 2's surviving shape-override prohibition.

- [ ] **Step 2: Confirm no source file changed**

Run: `git diff --name-only main..HEAD`

Expected: exactly one path — `docs/adr/0001-transfer-lift.md`. Anything else means scope leaked; stop and report.

- [ ] **Step 3: Run the preflight circus**

Use the `preflight-circus` skill over `main..HEAD`. Resolve findings in the artifact, not by arguing them away in a reply. A docs-only diff is not exempt — the skill's own guidance says so explicitly, and this particular diff changes how a rule reads, which is exactly what a lens will check.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin <branch>
```

PR body: `Closes <the ADR-amendment issue>`, links the design doc §4.4, and states plainly that Part B (#248) is blocked on this merging. Note that no code changes and the test count is unmoved.

---

## Self-Review

**Spec coverage (design §4.4):**

| §4.4 requirement | Task |
|---|---|
| Amend §10 item 2 | 1 |
| Amend §2 item 1 | 2 |
| Amend §5 table row | 3 |
| Use the established `> **Correction (post-implementation):**` form, not silent rewriting | 1, 2, 3 (each step says "leave the original intact") |
| Ratify the category with the design's exact wording | 1 |
| Lands before Part B, as its own change | Stated in the header and Task 4 step 4 |

No gaps.

**Placeholder scan:** none — every step carries the literal markdown to insert and the exact command to verify it.

**Type consistency:** N/A (no code). The category name "additive-domain-text kwargs" and the kwarg names `download_note` / `upload_note` are spelled identically in all three tasks and match the design doc and the Part B plan.
