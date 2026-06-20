# Design: keep pvl-core cleanly foldable for independent forks

**Date:** 2026-06-20
**Status:** approved (brainstorming) — pending spec review
**Scope:** this PR is pvl-core only. The downstream copier-detach story is
filed as a separate comprehensive issue in `fastmcp-server-template` (see
§7) and executed in a parallel template-repo session.

## 1. Motivation

`fastmcp-pvl-core` is the opinionated shared core for the `pvliesdonk/*-mcp`
fleet. It exists so the same cross-cutting concerns are not implemented five
times, and so a new server idea starts from a coherent base.

But it was written for personal use and convenience. The maintainer may not
maintain the fleet — or pvl-core — forever. Two realistic futures need a clean
exit ramp:

- **Succession.** Someone who actively uses *one* server forks it and takes it
  over. They cannot depend on an unmaintained upstream pvl-core, so they fold
  it into their tree.
- **Divergence.** Someone wants their own opinionated implementation. MIT fully
  permits this; folding pvl-core in is the natural way to own it.

A **fork is not a downstream.** The repo's coherence rules ("shape lives in
pvl-core, downstream conforms") govern *members of the family*. A fork has left
the family; releasing it cleanly is orthogonal to family coherence — there is
no override-kwarg and no shape migration involved. Moreover, a credible exit
ramp *lowers* the cost of depending on pvl-core in the first place, and the
seams that make the package foldable are the same seams that keep it a clean
load-bearing layer.

## 2. Goals / non-goals

**Goals**

- Make folding pvl-core into a fork a *directory rename*, not a find-replace.
- Ship a comprehensive, forker-facing guide covering the full disentanglement.
- Encode "keep pvl-core foldable" as a standing contributor directive so the
  property does not regress.

**Non-goals**

- **No pre-flattening of abstractions.** The `Build*`/factory layer, the
  `env(prefix, name)` indirection, parameterized `prog`, and the optional-extra
  split exist because pvl-core serves the whole fleet. Collapsing them is
  *fork-side* work (a single-server fork can flatten them); pvl-core must not
  do it "in case someone forks." It is documented, never pre-done.
- **No vendor-automation script.** Once imports are relative the manual recipe
  is ~5 commands; a `vendor.py` would be over-engineering for a hypothetical.
- **No cosmetic self-name changes.** The `pip install fastmcp-pvl-core[...]`
  hints and the `_server_info.py` version label correctly name the real package
  for the *unforked* case. They are listed as fork-side scrub items in the
  guide, not changed here.

## 3. Current foldability assessment (baseline)

Audited 2026-06-20. pvl-core is already remarkably foldable, mostly via good
hygiene rather than intent:

| Fold-in hazard | Status | Notes |
|---|---|---|
| License | OK — MIT | Vendoring legally trivial. |
| Self-metadata lookups (`importlib.metadata.version`, `resources.files("pkg")`) | OK — none | Every `fastmcp-pvl-core` literal is a human-facing pip hint, not a runtime lookup. |
| Env var prefix | OK — parameterized | `env(prefix, name)`; prefix injected by caller, not baked to `FASTMCP_PVL_CORE_*`. |
| CLI `prog` | OK — parameterized | `make_serve_parser(prog=...)`; no console-script under pvl-core's dist name. |
| Public API surface | OK — narrow | Single `__init__` re-export with `__all__`; internals `_`-prefixed. |
| `__version__` | OK — literal | A string in `__init__.py`, not read from dist metadata; survives vendoring (reports stale name cosmetically). |
| Intra-package imports | **friction — 30 absolute** | `from fastmcp_pvl_core._x import …` (15 in `__init__.py`'s re-export block, 15 across 11 sibling modules), 0 relative. The one real obstacle. |

The single high-leverage change is the last row.

## 4. Deliverable 1 — relative intra-package imports (code)

Convert the 30 absolute self-import statements (across 12 files) inside
`src/fastmcp_pvl_core/` to relative imports:

- `src/fastmcp_pvl_core/__init__.py` — the 15 re-export statements
  (`from fastmcp_pvl_core._apps import …` → `from ._apps import …`).
- The 15 cross-module imports across 11 sibling modules
  (`from fastmcp_pvl_core._x import …` → `from ._x import …`).

**Out of scope of this change:** test files keep their absolute
`fastmcp_pvl_core` imports — tests are *external consumers* of the package and
exercise the public import path. Only `src/` is touched.

**Why this is the enabler:** with relative imports, a fold-in becomes
`cp -r src/fastmcp_pvl_core myfork/src/myfork/_core` plus a directory rename,
with **zero** internal-import edits. With absolute imports it is a 15-site
package-name find-replace.

**No behavior change**, therefore no new tests. The safety net is the existing
27 test files plus the standard local-checks block staying green:

```bash
uv sync --all-extras
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

A pure import-style change that keeps all of the above green is complete.

## 5. Deliverable 2 — `docs/forking.md` (the guide)

Forker/operator-facing. Lives at `docs/forking.md` — **not** `docs/specs/`,
which is reserved for wire-format interop specs. Sections:

1. **When to fold vs. pin.** The two strategies and their tradeoff:
   - *Pin-and-forget* — keep `fastmcp-pvl-core==X.Y.Z` pinned forever, stop
     `copier update`. Zero effort, but cannot modify the core and stops
     receiving upstream dependency/CVE bumps.
   - *Fold-in / vendor* — full ownership and modification freedom, at the cost
     of inheriting the full maintenance burden including transitive CVE
     tracking. Choose deliberately; many forks are better served by pinning.

2. **Fold-in recipe.** Copy the package directory, rename it, update the fork's
   own `from fastmcp_pvl_core import …` call sites to the new name, drop the
   `fastmcp-pvl-core` dependency. Short, because relative imports made it short.

3. **Bring the tests.** Copy `tests/`, rewrite the absolute `fastmcp_pvl_core`
   imports to the new package name. This is the fork's safety net for the
   flattening in §4 of the guide.

4. **Collapsible-seams map.** Which abstractions a *single-server* fork can
   flatten, each with a "why it exists / how to collapse" line. Explicitly
   framed as fork-side work pvl-core cannot pre-do without breaking the family:
   - `env(prefix, name)` → inline your one prefix.
   - `Build*` / factory layer → inline the construction at the single call site.
   - parameterized `prog` → hard-code the fork's program name.
   - optional-dependency extras → keep only the backends the fork actually uses.

5. **Cosmetic scrub list.** The self-name string literals to search-and-replace:
   the `_server_info.py` version label and the `pip install fastmcp-pvl-core[...]`
   optional-extra hints scattered across `_debug.py`, `_auth.py`, `_kv_store.py`,
   and `_icons.py`. None are functional; all are human-facing text.

The guide gets a one-line pointer from `README.md` near the design-principles
section.

## 6. Deliverable 3 — foldability directive in `CLAUDE.md` (project)

Add a directive to the project contributor `CLAUDE.md` (the framing-principles
file) so the property does not regress. Proposed text:

> ### Keep pvl-core cleanly foldable
>
> A fork is not a downstream. MIT lets anyone vendor pvl-core into their own
> tree — to take over a single server when the fleet is no longer maintained,
> or to run their own opinionated variant. We keep that exit ramp cheap:
> credible foldability lowers the cost of depending on pvl-core in the first
> place, and the seams that make the package vendorable are the same seams that
> keep it a clean load-bearing layer. Foldability is a modularity property, not
> a coherence compromise.
>
> Contributors preserve:
> - **Relative intra-package imports** (`from ._x import …`) so a fold-in is a
>   directory rename, not a find-replace.
> - **No self-name lookups** — never resolve pvl-core's own distribution name or
>   package resources at runtime (`importlib.metadata.version(...)`,
>   `importlib.resources.files("fastmcp_pvl_core")`). Package-name string
>   literals stay confined to human-facing hints.
> - **Parameterized identity** — env prefixes, CLI `prog`, and similar
>   caller-facing identity stay arguments, never hard-coded to pvl-core's name.
> - **A narrow public surface** — the `__init__` re-export with `__all__` is the
>   contract; internals stay `_`-prefixed.
>
> This does **not** authorize pre-flattening abstractions "in case someone
> forks." The factory/`Build*` layer, the `env(prefix, name)` indirection, and
> the optional extras exist because pvl-core serves the whole family; collapsing
> them is fork-side work documented in `docs/forking.md`, never done in
> pvl-core.

Keep the README design-principles section aligned per the existing "keep them
aligned" rule.

## 7. Out of scope here — the template detach (separate issue)

Filed as a comprehensive issue in `fastmcp-server-template`, executed in a
parallel template-repo session. It covers the maintainer's points 1 and 3:

- Stop tracking the template: delete `.copier-answers.yml`, stop
  `copier update`.
- Strip template-origin GitHub workflows a standalone fork should not inherit.
- Scrub the opinionated `CLAUDE.md` / `.claude/CLAUDE.md` down to fork-neutral
  contributor guidance.

Written to the issue-writing discipline: separate concerns, explicit
verification commands (grep/find that mechanically confirm detach), test/CI
updates listed as deliverables.

## 8. Plumbing

- One new pvl-core issue: *"Make pvl-core cleanly foldable for independent
  forks"* — closed by this PR. Deliverables 1–3 land together (one coherent
  goal; no removal bundled with addition).
- One new `fastmcp-server-template` issue (§7), not closed by this PR.

## 9. Testing strategy

- **Deliverable 1:** behavior-preserving; full existing suite + ruff + mypy
  green is the contract. No new tests.
- **Deliverables 2–3:** documentation; reviewed for accuracy against the §3
  baseline (every claim in the guide must match the audited code state).
