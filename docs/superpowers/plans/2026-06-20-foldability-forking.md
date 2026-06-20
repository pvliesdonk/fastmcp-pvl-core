# Foldability / Forking Enablement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `fastmcp-pvl-core` cleanly foldable into an independent fork, and document the full disentanglement, so the fleet survives the maintainer stepping back.

**Architecture:** Three deliverables in one PR on pvl-core: (1) convert intra-package absolute imports to relative so a fold-in is a directory rename; (2) add `docs/forking.md`, the forker-facing guide; (3) add a standing "keep pvl-core foldable" directive to the contributor `CLAUDE.md`. A separate comprehensive issue is filed into `fastmcp-server-template` for the copier-detach story (not implemented here).

**Tech Stack:** Python ≥3.10, `uv`, `ruff`, `mypy`, `pytest`, `gh` CLI.

## Global Constraints

- Local checks before any push (verbatim from the repo CLAUDE.md):
  `uv sync --all-extras` → `uv run pytest` → `uv run ruff format --check .` → `uv run ruff check .` → `uv run mypy src`.
- Every PR closes ≥1 issue; merging is human-only (never `gh pr merge`).
- Every GitHub post authored via the token MUST end with the agent-signature line (see Task 0); commits end with the `Co-Authored-By:` trailer.
- Preflight-circus is mandatory before the push that opens/updates the PR.
- Do NOT pre-flatten any pvl-core abstraction (factory/`Build*`, `env(prefix, name)`, parameterized `prog`, optional extras). Those stay; collapsing them is fork-side and documented only.
- Spec authority: `docs/superpowers/specs/2026-06-20-foldability-forking-design.md`.

---

## File Structure

- `src/fastmcp_pvl_core/*.py` — modified: absolute self-imports → relative (12 files).
- `docs/forking.md` — created: the forker-facing guide.
- `README.md` — modified: one pointer to `docs/forking.md` near design principles.
- `CLAUDE.md` (project root, contributor guidance) — modified: new "Keep pvl-core cleanly foldable" directive.

---

### Task 0: Branch and tracking issues

**Files:** none (git + GitHub plumbing).

- [ ] **Step 1: Create the feature branch** (we are on `main`)

```bash
git checkout -b feat/foldability-forking
```

- [ ] **Step 2: Create the pvl-core tracking issue**

```bash
gh issue create --repo pvliesdonk/fastmcp-pvl-core \
  --title "Make pvl-core cleanly foldable for independent forks" \
  --body "$(cat <<'EOF'
## Why

`fastmcp-pvl-core` is the opinionated shared core for the `pvliesdonk/*-mcp`
fleet, written for personal convenience. The maintainer may not maintain it
forever. A **fork is not a downstream**: when someone takes over a single
server (succession) or wants their own opinionated variant (divergence), they
must stop depending on an unmaintained upstream and fold pvl-core into their
tree. MIT permits this; we make the exit ramp cheap. A credible exit ramp also
lowers the cost of depending on pvl-core in the first place.

## Deliverables

1. Convert the 30 absolute intra-package imports in `src/fastmcp_pvl_core/` to
   relative imports, so a fold-in is a directory rename rather than a
   30-site find-replace. Behavior-preserving; tests keep absolute imports.
2. Add `docs/forking.md` — fold-vs-pin tradeoff, fold-in recipe,
   bring-the-tests, collapsible-seams map, cosmetic scrub list.
3. Add a "Keep pvl-core cleanly foldable" directive to the contributor
   `CLAUDE.md` that forbids pre-flattening abstractions.

## Non-goals

No pre-flattening of abstractions; no vendor-automation script; no change to
the human-facing `pip install fastmcp-pvl-core[...]` hints or version label.

Design: `docs/superpowers/specs/2026-06-20-foldability-forking-design.md`.

— 🤖 _Automated post by Claude Code (agent) via the account owner's GitHub token; agent analysis/proposal, not a personal directive from the account owner._
EOF
)"
```

Record the issue number returned as `PVLCORE_ISSUE`.

- [ ] **Step 3: Create the template detach issue** (filed into the template repo, NOT implemented here)

```bash
gh issue create --repo pvliesdonk/fastmcp-server-template \
  --title "Document and enable clean detach from the copier template for independent forks" \
  --body "$(cat <<'EOF'
## Why

A fork that takes over a single server (or wants its own opinionated variant)
must detach from this copier template and from the opinionated fleet guidance
it ships. This is the template-side half of the pvl-core foldability work
(pvliesdonk/fastmcp-pvl-core: "Make pvl-core cleanly foldable for independent
forks"). A fork is not a downstream; we make the detach mechanical and
documented.

## Deliverables

1. **Detach guide** (e.g. `docs/forking.md` in the template's rendered output,
   or a top-level section): stop tracking the template — delete
   `.copier-answers.yml`, stop running `copier update`.
2. **Strip template-origin CI**: identify which rendered `.github/workflows/*`
   a standalone fork should not inherit (template-update automation, fleet-wide
   review wiring) versus keep (the fork's own CI/release). Document the prune.
3. **Scrub opinionated guidance**: reduce the rendered `CLAUDE.md` /
   `.claude/CLAUDE.md` to fork-neutral contributor guidance — remove
   maintainer-personal opinion and fleet-coherence rules that do not apply to a
   standalone fork.
4. **Test/CI updates**: any template smoke tests asserting the old state get
   updated to assert the detached state — rewritten, not deleted.

## Verification (run before closing)

```bash
# After applying the documented detach to a rendered project:
test ! -f .copier-answers.yml && echo "copier answers removed"
grep -rn "copier" .github/ ; echo "<-- expect no template-update workflow refs"
grep -rniE "fleet|downstream conforms|shape lives in pvl-core" CLAUDE.md .claude/CLAUDE.md ; echo "<-- expect none"
```

## Non-goals

Does not touch pvl-core itself (separate issue/PR). Does not remove the
template's own ability to render new fleet members — this documents the
*fork's* exit, not a template teardown.

— 🤖 _Automated post by Claude Code (agent) via the account owner's GitHub token; agent analysis/proposal, not a personal directive from the account owner._
EOF
)"
```

This issue is **not** closed by this PR.

---

### Task 1: Relative intra-package imports

**Files:**
- Modify: all 12 `src/fastmcp_pvl_core/*.py` files containing absolute self-imports (`__init__.py`, `_apps`? no — exact set below).
- Test: existing suite (no new tests; behavior-preserving).

**Interfaces:**
- Consumes: nothing.
- Produces: identical public API; `import fastmcp_pvl_core` and all `from fastmcp_pvl_core import …` from *outside* the package still resolve unchanged.

**The exact set of import statements to convert** (30 statements, 12 files). Two transform rules, both anchored to lines starting (after optional indentation) with `from fastmcp_pvl_core`:

- **Rule A (dotted submodule):** `from fastmcp_pvl_core._X import …` → `from ._X import …`
- **Rule B (package-level):** `from fastmcp_pvl_core import …` → `from . import …`

Rule B covers exactly these 4 deferred/package-level imports (do NOT miss them — Rule A's pattern skips them):
- `_logging.py:80  from fastmcp_pvl_core import SecretMaskFilter`
- `_server_info.py:105  from fastmcp_pvl_core import __version__ as core_version`

(The other Rule-B-shaped lines are dotted and handled by Rule A.)

**MUST NOT touch** — these contain `fastmcp_pvl_core` but are NOT imports (docstrings, a runtime ContextVar name, a JSON example string). Leave them exactly as-is:
- `_factory.py:90` `:class:`~fastmcp_pvl_core.ServerConfig`` (docstring)
- `_authorization.py:169` ``fastmcp_pvl_core.get_subject`` (docstring)
- `_server_info.py:60` `"core_version": "<fastmcp_pvl_core.__version__>"` (JSON example)
- `_config.py:73, _config.py:131` `:func:` docstring refs
- `_subject.py:46` `"fastmcp_pvl_core_current_auth_mode"` (ContextVar name — a runtime identifier)
- `_subject.py:54` `:func:` docstring ref
- `_logging_middleware.py:9` `:func:` docstring ref

- [ ] **Step 1: Snapshot the import lines before changing (baseline for diff review)**

```bash
grep -rnE '^[[:space:]]*from fastmcp_pvl_core' src/fastmcp_pvl_core/*.py | tee /tmp/foldability_imports_before.txt
wc -l < /tmp/foldability_imports_before.txt   # expect 30
```

- [ ] **Step 2: Apply Rule A then Rule B, anchored to import lines only**

```bash
# Rule A: dotted submodule imports -> single-dot relative
sed -i -E 's/^([[:space:]]*)from fastmcp_pvl_core\./\1from ./' src/fastmcp_pvl_core/*.py
# Rule B: package-level imports -> "from . import"
sed -i -E 's/^([[:space:]]*)from fastmcp_pvl_core import/\1from . import/' src/fastmcp_pvl_core/*.py
```

Both `sed` patterns are anchored to `^[[:space:]]*from fastmcp_pvl_core`, so docstring/string occurrences (which never start with `from fastmcp_pvl_core`) are untouched.

- [ ] **Step 3: Verify no absolute self-import statements remain, and preserved refs survived**

```bash
# Expect EMPTY — no import statement references the absolute package name:
grep -rnE '^[[:space:]]*(from fastmcp_pvl_core|import fastmcp_pvl_core)' src/fastmcp_pvl_core/*.py
# Expect the 8 docstring/string refs STILL present (unchanged):
grep -rn 'fastmcp_pvl_core' src/fastmcp_pvl_core/*.py
```

First grep MUST print nothing. Second grep MUST still show the docstring/ContextVar/JSON lines from the MUST-NOT-touch list (proof we did not over-reach).

- [ ] **Step 4: Run the full local-checks block**

```bash
uv sync --all-extras
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: all green. (A behavior-preserving import-style change keeps the existing 27 test files passing; `ruff` may also confirm relative-import preference.)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/
git commit -m "$(cat <<'EOF'
refactor: use relative intra-package imports for foldability

Convert the 30 absolute `from fastmcp_pvl_core...` self-imports in src/ to
relative imports so a fork can vendor the package by renaming the directory
rather than find-replacing the package name across every module. Behavior
preserving; tests (external consumers) keep absolute imports. Docstring
cross-references and the auth-mode ContextVar name are intentionally left
unchanged.

Refs #PVLCORE_ISSUE

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

Replace `#PVLCORE_ISSUE` with the number from Task 0.

---

### Task 2: `docs/forking.md` and README pointer

**Files:**
- Create: `docs/forking.md`
- Modify: `README.md` (one pointer near the design-principles section)

**Interfaces:** documentation only; every factual claim must match the audited code state in the spec §3 baseline.

- [ ] **Step 1: Write `docs/forking.md` with this exact content**

````markdown
# Forking and folding in pvl-core

`fastmcp-pvl-core` is the shared core of the `pvliesdonk/*-mcp` server family.
It is MIT-licensed. If you want to take over a single server when the upstream
is no longer maintained, or run your own opinionated variant, you can **fold
pvl-core into your fork** and cut the upstream dependency entirely. A fork is
not a downstream — none of the family's coherence rules bind you once you fold.

## First decide: pin, or fold?

**Pin and forget.** Keep `fastmcp-pvl-core==X.Y.Z` pinned and stop running
`copier update`. Zero effort. You keep receiving nothing new — including no
dependency or CVE bumps — and you cannot modify the core. Best if you are happy
with the core as-is and only want to freeze it.

**Fold in (vendor).** Copy the package into your tree, rename it, drop the
dependency. You get full ownership and the freedom to modify, at the cost of
owning the full maintenance burden — including tracking transitive CVEs that
upstream used to handle for you. Choose this only if you actually intend to
change the core or cannot rely on upstream at all.

The rest of this guide covers folding in.

## Fold-in recipe

pvl-core uses relative intra-package imports, so folding is a directory rename
— you do not edit the core's internal imports.

```bash
# 1. Copy the package into your fork (rename to your own internal package):
cp -r path/to/fastmcp_pvl_core  src/myfork/_core

# 2. Update YOUR code's imports from the dependency to the vendored package:
#    from fastmcp_pvl_core import build_app   ->   from myfork._core import build_app
grep -rl 'fastmcp_pvl_core' src/myfork --include='*.py'   # find your call sites
#    ...then rewrite those `from fastmcp_pvl_core` references to `from myfork._core`.

# 3. Drop the dependency from pyproject.toml (remove the fastmcp-pvl-core line).

# 4. Reinstall and run your tests.
```

## Bring the tests

Vendor the test suite too — it is your safety net for the flattening below:

```bash
cp -r path/to/tests  tests/_core
# Rewrite the suite's absolute imports to your vendored package name:
#   from fastmcp_pvl_core import X   ->   from myfork._core import X
```

## Collapsible-seams map

pvl-core carries abstractions because it serves *five* servers. Your fork serves
one, so you may collapse them. These are safe to flatten **in a fork** — do not
ask pvl-core to pre-flatten them, that would break the family:

- **`env(prefix, name)` indirection** — pvl-core parameterizes the env-var
  prefix so each server picks its own. Your fork has one prefix; inline it.
- **`Build*` / factory layer** — the factory exists to assemble a server from
  config generically. With one server you can inline the construction at its
  single call site.
- **Parameterized CLI `prog`** — `make_serve_parser(prog=...)` lets each server
  name its own program. Hard-code your fork's name.
- **Optional-dependency extras** — pvl-core splits backends (`[redis]`,
  `[dynamodb]`, `[mongodb]`, `[remote-auth]`, `[debug]`) behind extras. Keep
  only the backends your fork uses and make them hard dependencies.

## Cosmetic scrub list

These reference pvl-core by name but are harmless until you want the fork to
look fully its own. Search-and-replace at your leisure:

- The version label in `_server_info.py` (reports `fastmcp-pvl-core` + version).
- The `pip install fastmcp-pvl-core[...]` hints in `_debug.py`, `_auth.py`,
  `_kv_store.py`, `_icons.py` (shown when an optional extra is missing).
- Sphinx-style docstring cross-references (`:class:`~fastmcp_pvl_core....``) and
  the `fastmcp_pvl_core_current_auth_mode` ContextVar name, if you rename the
  package for real.

None of these are functional couplings — pvl-core performs no runtime lookup of
its own distribution name or package resources, so renaming never breaks
imports or resource loading.
````

- [ ] **Step 2: Add a pointer from `README.md`** near the design-principles section

Find the `## Design principles` section heading in `README.md` and add, immediately after its closing paragraph, this line:

```markdown
> Planning to fork and cut the dependency? See [docs/forking.md](docs/forking.md)
> for the fold-in recipe and what a single-server fork can safely collapse.
```

(Use Grep to locate `## Design principles` first; insert after that section's prose, not mid-paragraph.)

- [ ] **Step 3: Verify the guide's claims against the code**

```bash
# pvl-core performs NO runtime self-name lookup (claim in the guide):
grep -rnE "metadata\.version\(|resources\.files\(" src/fastmcp_pvl_core/ ; echo "<-- expect none"
# The optional extras named in the guide exist:
grep -nE "redis|dynamodb|mongodb|remote-auth|debug" pyproject.toml
```

Both must confirm the guide (no self-name lookups; the named extras exist).

- [ ] **Step 4: Commit**

```bash
git add docs/forking.md README.md
git commit -m "$(cat <<'EOF'
docs: add forking/fold-in guide for independent forks

Document the pin-vs-fold decision, the directory-rename fold-in recipe, the
test-vendoring step, the collapsible-seams map (which abstractions a
single-server fork may flatten), and the cosmetic scrub list. Link it from the
README design-principles section.

Refs #PVLCORE_ISSUE

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `CLAUDE.md` foldability directive

**Files:**
- Modify: `CLAUDE.md` (project root contributor guidance)

**Interfaces:** documentation/policy; aligns with the existing framing principles and the README design-principles section.

- [ ] **Step 1: Insert the directive** as a new subsection under the framing principles

Use Grep to locate the `## Practical consequences` heading in `CLAUDE.md`. Insert the following as a new `###` subsection immediately *before* `## Practical consequences` (i.e., as the last item of the framing-principle group):

```markdown
### Keep pvl-core cleanly foldable

A fork is not a downstream. MIT lets anyone vendor pvl-core into their own tree
— to take over a single server when the fleet is no longer maintained, or to run
their own opinionated variant. We keep that exit ramp cheap: credible
foldability lowers the cost of depending on pvl-core in the first place, and the
seams that make the package vendorable are the same seams that keep it a clean
load-bearing layer. Foldability is a modularity property, not a coherence
compromise.

Contributors preserve:

- **Relative intra-package imports** (`from ._x import …`) so a fold-in is a
  directory rename, not a find-replace.
- **No self-name lookups** — never resolve pvl-core's own distribution name or
  package resources at runtime (`importlib.metadata.version(...)`,
  `importlib.resources.files("fastmcp_pvl_core")`). Package-name string literals
  stay confined to human-facing hints.
- **Parameterized identity** — env prefixes, CLI `prog`, and similar
  caller-facing identity stay arguments, never hard-coded to pvl-core's name.
- **A narrow public surface** — the `__init__` re-export with `__all__` is the
  contract; internals stay `_`-prefixed.

This does **not** authorize pre-flattening abstractions "in case someone forks."
The factory/`Build*` layer, the `env(prefix, name)` indirection, and the
optional extras exist because pvl-core serves the whole family; collapsing them
is fork-side work documented in `docs/forking.md`, never done in pvl-core.
```

- [ ] **Step 2: Verify placement and links**

```bash
grep -n "Keep pvl-core cleanly foldable" CLAUDE.md            # exists once
grep -n "docs/forking.md" CLAUDE.md                           # the cross-link is present
grep -n "## Practical consequences" CLAUDE.md                 # directive sits before it
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: add "keep pvl-core foldable" contributor directive

Encode foldability as a standing modularity property (relative imports, no
self-name lookups, parameterized identity, narrow public surface) so it does not
regress, while explicitly forbidding pre-flattening of abstractions — that stays
fork-side, documented in docs/forking.md.

Refs #PVLCORE_ISSUE

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Preflight-circus and open the PR

**Files:** none (review + PR).

- [ ] **Step 1: Re-run the full local-checks block** (final, on the complete diff)

```bash
uv sync --all-extras && uv run pytest && uv run ruff format --check . && uv run ruff check . && uv run mypy src
```

Expected: all green.

- [ ] **Step 2: Run the preflight-circus skill** against `main..HEAD` and resolve any findings *in the artifacts* (not by reply). Re-run until clean at the push threshold.

- [ ] **Step 3: Push and open the PR** (closes the pvl-core issue)

```bash
git push -u origin feat/foldability-forking
gh pr create --repo pvliesdonk/fastmcp-pvl-core \
  --title "Make pvl-core cleanly foldable for independent forks" \
  --body "$(cat <<'EOF'
Enables a fork to vendor pvl-core by directory rename, and documents the full
disentanglement.

## Changes
- Relative intra-package imports (30 statements, behavior-preserving).
- `docs/forking.md` — pin-vs-fold, fold-in recipe, bring-the-tests,
  collapsible-seams map, cosmetic scrub list.
- `CLAUDE.md` — "keep pvl-core foldable" directive (forbids pre-flattening).

The copier-template detach (the fork's other half) is tracked separately in
fastmcp-server-template.

Closes #PVLCORE_ISSUE

Design: docs/superpowers/specs/2026-06-20-foldability-forking-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)

— 🤖 _Automated post by Claude Code (agent) via the account owner's GitHub token; agent analysis/proposal, not a personal directive from the account owner._
EOF
)"
```

Replace `#PVLCORE_ISSUE`. Do NOT merge (human-only).

---

## Self-Review

**Spec coverage:**
- Spec §4 (relative imports) → Task 1. ✓
- Spec §5 (`docs/forking.md`, all 5 sections + README pointer) → Task 2. ✓
- Spec §6 (CLAUDE.md directive + README alignment) → Task 3 (+ README pointer already in Task 2). ✓
- Spec §7 (template issue) → Task 0 Step 3. ✓
- Spec §8 (plumbing: pvl-core issue, template issue) → Task 0. ✓
- Spec §9 (testing: suite green; doc accuracy) → Task 1 Step 4, Task 2 Step 3, Task 4 Step 1. ✓

**Placeholder scan:** `#PVLCORE_ISSUE` is an intentional fill-in (issue number unknown until Task 0 runs) and is flagged at each use. No TBD/TODO/"handle edge cases". Doc and directive content are complete verbatim.

**Type/identifier consistency:** No new code symbols introduced (refactor preserves the public API). The MUST-NOT-touch list and the two sed rules are mutually exclusive by anchor (`^\s*from fastmcp_pvl_core`), verified in Task 1 Step 3.

**Risk note:** the only behavioral risk is an import that the two sed rules miss or mis-rewrite; Task 1 Step 3's two greps (zero remaining import-refs; preserved docstring refs intact) plus the full suite in Step 4 catch that before commit.
