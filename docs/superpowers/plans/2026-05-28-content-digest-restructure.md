# Content-Digest pipeline restructure — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-rolled RFC 8941/9530 parser inside `_upload.py` with the `http-sf` library wrapped by a new `_content_digest.py` module that splits Layer-2 (algorithm selection), Layer-3 (`requireDigest` membership), and Layer-4 (bytes verification) into named, separately-testable concerns.

**Architecture:** Two PRs in sequence: **PR-A** lands the new library dependency and the new pure-function module against `main` (route untouched, narrow review surface). **PR-B** rebases the in-flight slice 3 (#169) on PR-A, deletes the hand-rolled parser, and migrates the route to call into the new module. Slices 4 (#170) and 5 (#171) then rebase mechanically.

**Tech Stack:** Python 3.10–3.13, [`http-sf`](https://pypi.org/project/http-sf/) v1.3+ (Mark Nottingham's RFC 8941 reference implementation; pure-Python, no transitive deps), pytest with `asyncio_mode = "auto"`, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-05-28-content-digest-restructure.md` (commit `f2f81e2`).

**Branch hygiene at every Task boundary:**

```bash
uv run pytest tests/_file_exchange -q
uv run --python 3.10 pytest tests/_file_exchange -q
uv run --python 3.13 pytest tests/_file_exchange -q
uv run ruff format --check .
uv run ruff check src tests
uv run mypy src
```

All six must pass before the commit lands.

---

## PR-A — `_content_digest.py` (new module against `main`)

Branch: `feat/146-content-digest-module` off `main`. No route changes. Self-contained, reviewable in one bot round.

### Task A1 — Add `http-sf` runtime dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock` (regenerated)

- [ ] **Step A1.1: Add the dependency.**

Find the `dependencies = [...]` array in `pyproject.toml`'s `[project]` table and add `"http-sf>=1.3"` in alphabetical position. After the edit, the relevant section should contain a line like:

```toml
    "http-sf>=1.3",
```

(Match the surrounding quoting style and comma placement of the existing entries; if entries are sorted alphabetically, place this between `httpx` and `jsonschema` or wherever `h*` lands.)

- [ ] **Step A1.2: Regenerate the lock + sync the env.**

```bash
uv sync --all-extras
```

Expected: `http-sf` resolves to a 1.3.x version with zero transitive runtime deps.

- [ ] **Step A1.3: Smoke-import.**

```bash
uv run python -c "from http_sf import parse, ser_dictionary; print('ok')"
```
Expected output: `ok`.

- [ ] **Step A1.4: Commit.**

```bash
git add pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
build(deps): add http-sf for RFC 8941 Structured Fields parsing (#146)

http-sf is Mark Nottingham's RFC 8941 reference implementation
(pure-Python, no transitive runtime deps). Used by the upcoming
_content_digest.py module to replace the hand-rolled Content-Digest
parser whose finding cluster on PR #169 motivated the restructure.

Refs #146.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A2 — `_content_digest.py` skeleton + first contract test

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_content_digest.py`
- Create: `tests/_file_exchange/test_content_digest.py`

- [ ] **Step A2.1: Write the first failing test.**

```python
# tests/_file_exchange/test_content_digest.py
"""Contract tests for the Content-Digest parse + policy module.

Every spec edge previously surfaced as a route-level bug on PR #169
gets its own test here so the route layer can rely on the contract
without re-deriving it.
"""

import base64
import hashlib

import pytest

from fastmcp_pvl_core._file_exchange import _content_digest


def test_supported_algorithms_set():
    assert _content_digest.SUPPORTED_ALGORITHMS == frozenset(
        {"sha-256", "sha-384", "sha-512"}
    )
```

- [ ] **Step A2.2: Run — expect ModuleNotFoundError.**

```bash
uv run pytest tests/_file_exchange/test_content_digest.py -v
```
Expected: `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._content_digest'`.

- [ ] **Step A2.3: Create the module skeleton.**

```python
# src/fastmcp_pvl_core/_file_exchange/_content_digest.py
"""RFC 9530 Content-Digest header parser + policy helpers.

Parsing delegates to the ``http_sf`` library (RFC 8941 Structured
Fields); this module adds the spec-9530 policy layered on top:
which algorithms pvl-core supports, how a multi-algorithm dictionary
is reduced to a single selected entry, and whether a selected entry
satisfies a receiver's ``requireDigest`` constraint.

Bytes verification (computing or rehashing the body's digest) is NOT
in this module — that's the route's concern because it owns the
staging temp-file lifecycle.
"""

from __future__ import annotations

SUPPORTED_ALGORITHMS = frozenset({"sha-256", "sha-384", "sha-512"})
```

- [ ] **Step A2.4: Run — expect PASS.**

```bash
uv run pytest tests/_file_exchange/test_content_digest.py -v
```

- [ ] **Step A2.5: Lint + commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_content_digest.py \
        tests/_file_exchange/test_content_digest.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): _content_digest.py skeleton + SUPPORTED_ALGORITHMS (#146)

Container module for the upload route's Content-Digest parse + policy
layers. This commit lands the docstring + the supported-algorithm
frozenset; parse_header, satisfies_requirement, and format_header
land in subsequent commits per the TDD micro-cycle.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A3 — `parse_header` happy path (single supported entry)

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_content_digest.py`
- Modify: `tests/_file_exchange/test_content_digest.py`

- [ ] **Step A3.1: Write the failing test.**

Append to `tests/_file_exchange/test_content_digest.py`:

```python
def test_parse_header_single_sha256_entry():
    payload = b"hello"
    raw = hashlib.sha256(payload).digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(f"sha-256=:{b64}:")
    assert parsed == ("sha-256", raw)
```

- [ ] **Step A3.2: Run — expect AttributeError.**

```bash
uv run pytest tests/_file_exchange/test_content_digest.py::test_parse_header_single_sha256_entry -v
```

- [ ] **Step A3.3: Implement the minimal happy path.**

Append to `src/fastmcp_pvl_core/_file_exchange/_content_digest.py`:

```python
from collections.abc import Iterable

import http_sf


def parse_header(
    header: str, *, preferred: Iterable[str] | None = None
) -> tuple[str, bytes] | None:
    """Parse a Content-Digest header into ``(algo, raw_digest_bytes)``.

    Returns ``None`` if the header is empty, malformed at the RFC 8941
    layer, or contains no supported algorithm. Otherwise returns the
    first supported entry, preferring ``preferred`` algorithms (if any
    are present and parse) and falling back to the first supported
    entry the dictionary lists.

    Unsupported algorithms within a multi-algorithm dictionary are
    silently skipped (RFC 9530 §3 MUST-ignore). Parameter dictionaries
    on entries (``algo=:bytes:;p=v``) are accepted and ignored.
    """
    if not header:
        return None
    try:
        parsed = http_sf.parse(header.encode("ascii"), tltype="dictionary")
    except Exception:
        return None
    preferred_set = (
        {p.strip().lower() for p in preferred} if preferred else None
    )
    fallback: tuple[str, bytes] | None = None
    for raw_label, (value, _params) in parsed.items():
        label = raw_label.lower()
        if label not in SUPPORTED_ALGORITHMS:
            continue
        if not isinstance(value, bytes):
            continue
        if preferred_set is not None and label in preferred_set:
            return label, value
        if fallback is None:
            fallback = (label, value)
    return fallback
```

- [ ] **Step A3.4: Run — expect PASS.**

```bash
uv run pytest tests/_file_exchange/test_content_digest.py -v
```
Expected: both tests pass.

- [ ] **Step A3.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_content_digest.py \
        tests/_file_exchange/test_content_digest.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): _content_digest.parse_header — single-entry happy path (#146)

Delegates RFC 8941 parsing to http_sf; layered policy (supported-algo
filter, preferred-algo preference, fallback to first supported)
implemented as a thin loop. The function's full edge surface lands in
subsequent commits one matrix row at a time.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A4 — `parse_header` edge surface (every prior route-level finding)

For each sub-step below: add the test, run it (expect PASS since the implementation from Task A3 already covers each edge structurally), and commit. The discipline is to **prove** each historical finding is covered, not to add code that no test exercises.

**Files:** `tests/_file_exchange/test_content_digest.py` only.

- [ ] **Step A4.1: Empty header → None.**

```python
def test_parse_header_empty_returns_none():
    assert _content_digest.parse_header("") is None
```

- [ ] **Step A4.2: Malformed (StructuredFieldError) → None.**

```python
def test_parse_header_malformed_returns_none():
    assert _content_digest.parse_header("not a structured field!!!") is None
```

- [ ] **Step A4.3: All-unsupported dictionary → None.**

```python
def test_parse_header_all_unsupported_returns_none():
    assert (
        _content_digest.parse_header("md5=:YWJjZA==:, sha-3=:YWJjZA==:")
        is None
    )
```

- [ ] **Step A4.4: Skip unsupported, return supported (RFC 9530 §3 MUST-ignore).**

```python
def test_parse_header_skips_unsupported_takes_supported():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(f"md5=:YWJjZA==:, sha-256=:{b64}:")
    assert parsed == ("sha-256", raw)
```

- [ ] **Step A4.5: SF parameters on entry are ignored.**

```python
def test_parse_header_ignores_sf_parameters():
    raw = hashlib.sha256(b"hello").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(f"sha-256=:{b64}:;foo=bar;baz=42")
    assert parsed == ("sha-256", raw)
```

- [ ] **Step A4.6: Case folding on the algorithm label.**

```python
def test_parse_header_lowercases_algorithm_label():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(f"SHA-256=:{b64}:")
    # Note: http_sf normalizes keys per RFC 8941 (lowercase); we still
    # apply .lower() defensively so the post-condition is pinned here.
    assert parsed == ("sha-256", raw)
```

**Verify expected outcome first:** RFC 8941 §3.2 says dictionary keys are lcalpha — `http_sf.parse` will raise `StructuredFieldError` on `SHA-256` (uppercase). If the test reports `None` rather than `("sha-256", raw)`, that is acceptable behaviour (the wire format rejects uppercase keys; the route then returns 400). In that case, rewrite the assertion to:

```python
    # Per RFC 8941 dictionary keys are lcalpha; uppercase is rejected at
    # the wire layer (http_sf raises StructuredFieldError -> None). Pin
    # the rejection rather than the case-folding behaviour.
    assert parsed is None
```

Run the test against the implementation; choose whichever assertion matches the library's actual behaviour and commit that one.

- [ ] **Step A4.7: Whitespace tolerance.**

```python
def test_parse_header_tolerates_optional_whitespace():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    # OWS around the equals sign and the colon framing.
    parsed = _content_digest.parse_header(f"sha-256= :{b64}:")
    assert parsed == ("sha-256", raw)
```

(If this also reports `None`, then OWS around `=` is rejected by the library; rewrite the test docstring to pin the rejection, same as A4.6.)

- [ ] **Step A4.8: Multi-supported dictionary, no `preferred` → first entry.**

```python
def test_parse_header_multi_supported_no_preferred_returns_first():
    raw256 = hashlib.sha256(b"x").digest()
    raw512 = hashlib.sha512(b"x").digest()
    b256 = base64.b64encode(raw256).decode("ascii")
    b512 = base64.b64encode(raw512).decode("ascii")
    parsed = _content_digest.parse_header(f"sha-256=:{b256}:, sha-512=:{b512}:")
    assert parsed == ("sha-256", raw256)
```

- [ ] **Step A4.9: Multi-supported, `preferred=["sha-256"]` with sha-512 listed first → sha-256 selected.**

```python
def test_parse_header_preferred_overrides_order():
    raw256 = hashlib.sha256(b"x").digest()
    raw512 = hashlib.sha512(b"x").digest()
    b256 = base64.b64encode(raw256).decode("ascii")
    b512 = base64.b64encode(raw512).decode("ascii")
    parsed = _content_digest.parse_header(
        f"sha-512=:{b512}:, sha-256=:{b256}:", preferred=["sha-256"]
    )
    assert parsed == ("sha-256", raw256)
```

- [ ] **Step A4.10: Fallback when no `preferred` entry is present.**

```python
def test_parse_header_preferred_absent_falls_back_to_first_supported():
    raw512 = hashlib.sha512(b"x").digest()
    b512 = base64.b64encode(raw512).decode("ascii")
    parsed = _content_digest.parse_header(
        f"sha-512=:{b512}:", preferred=["sha-256"]
    )
    assert parsed == ("sha-512", raw512)
```

- [ ] **Step A4.11: `preferred` case-insensitive.**

```python
def test_parse_header_preferred_is_case_insensitive():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(
        f"sha-512=:{base64.b64encode(hashlib.sha512(b'x').digest()).decode('ascii')}:, "
        f"sha-256=:{b64}:",
        preferred=["SHA-256"],
    )
    assert parsed == ("sha-256", raw)
```

- [ ] **Step A4.12: Commit (one commit for the whole edge sweep).**

```bash
uv run pytest tests/_file_exchange/test_content_digest.py -v
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add tests/_file_exchange/test_content_digest.py
git commit -m "$(cat <<'EOF'
test(file-exchange): pin parse_header edges against historical findings (#146)

Add contract tests for every edge that previously surfaced as a
route-level bug on PR #169:

- empty header / malformed input -> None
- all-unsupported algorithms -> None
- skip-unsupported-take-supported (RFC 9530 §3 MUST-ignore)
- SF parameters on entries are ignored (RFC 8941 §3.2)
- algorithm-label case behaviour (library rejects uppercase per
  RFC 8941 lcalpha; pinned with whichever observable behaviour
  http_sf actually produces)
- whitespace handling (same library-defers note)
- multi-supported dictionary fallback when no preferred is given
- preferred=[algo] overrides dictionary entry order
- preferred case-insensitive matching
- fallback to first supported when preferred not present

These tests are the contract slice 3's route can rely on without
re-deriving the parsing rules.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A5 — `satisfies_requirement`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_content_digest.py`
- Modify: `tests/_file_exchange/test_content_digest.py`

- [ ] **Step A5.1: Write failing tests.**

```python
def test_satisfies_requirement_none_is_always_true():
    assert _content_digest.satisfies_requirement("sha-256", None) is True
    assert _content_digest.satisfies_requirement("anything", None) is True


def test_satisfies_requirement_empty_is_always_true():
    assert _content_digest.satisfies_requirement("sha-256", []) is True


def test_satisfies_requirement_exact_match():
    assert (
        _content_digest.satisfies_requirement("sha-256", ["sha-256"]) is True
    )


def test_satisfies_requirement_case_insensitive():
    assert (
        _content_digest.satisfies_requirement("sha-256", ["SHA-256"]) is True
    )
    assert (
        _content_digest.satisfies_requirement("SHA-256", ["sha-256"]) is True
    )


def test_satisfies_requirement_not_in_list():
    assert (
        _content_digest.satisfies_requirement("sha-512", ["sha-256"]) is False
    )


def test_satisfies_requirement_normalises_whitespace_in_required():
    assert (
        _content_digest.satisfies_requirement("sha-256", [" sha-256 "])
        is True
    )
```

- [ ] **Step A5.2: Run — expect failure.**

- [ ] **Step A5.3: Implement.**

Append to `_content_digest.py`:

```python
def satisfies_requirement(
    algo: str, required: Iterable[str] | None
) -> bool:
    """Case-insensitive: does ``algo`` appear in ``required``?

    ``required=None`` or empty means no constraint — always True.
    ``algo`` and the ``required`` entries are compared after
    ``str.strip().lower()`` normalisation, so callers can pass an
    unvalidated wire-derived list without pre-normalising.
    """
    if not required:
        return True
    needle = algo.strip().lower()
    return needle in {r.strip().lower() for r in required}
```

- [ ] **Step A5.4: Run — expect PASS.**

- [ ] **Step A5.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_content_digest.py \
        tests/_file_exchange/test_content_digest.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): _content_digest.satisfies_requirement (#146)

Pure case-insensitive set-membership check for ArtifactConstraints.
requireDigest. Both algorithm name and required entries are normalised
via str.strip().lower() so the route can pass unvalidated wire-derived
input without pre-normalisation. None / empty list means "no
constraint, always True".

Replaces the inline lowercased-set-comprehension previously living in
the route handler.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A6 — `format_header`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_content_digest.py`
- Modify: `tests/_file_exchange/test_content_digest.py`

- [ ] **Step A6.1: Write failing test.**

```python
def test_format_header_round_trips_via_parse():
    raw = hashlib.sha256(b"hello world").digest()
    header = _content_digest.format_header("sha-256", raw)
    parsed = _content_digest.parse_header(header)
    assert parsed == ("sha-256", raw)


def test_format_header_shape_matches_rfc_9530():
    raw = hashlib.sha256(b"x").digest()
    header = _content_digest.format_header("sha-256", raw)
    expected = "sha-256=:" + base64.b64encode(raw).decode("ascii") + ":"
    assert header == expected
```

- [ ] **Step A6.2: Run — expect failure.**

- [ ] **Step A6.3: Implement.**

Append to `_content_digest.py`:

```python
def format_header(algo: str, raw: bytes) -> str:
    """Serialise ``(algo, raw)`` as an RFC 9530 Content-Digest value.

    Delegates to http_sf.ser_dictionary for the canonical RFC 8941
    form. ``algo`` is the lowercase algorithm label
    (``sha-256``/``sha-384``/``sha-512``); ``raw`` is the digest bytes.
    """
    return http_sf.ser_dictionary({algo: (raw, {})})
```

- [ ] **Step A6.4: Run — expect PASS.**

If `test_format_header_shape_matches_rfc_9530` fails because `http_sf.ser_dictionary` adds optional whitespace (it shouldn't, but check the library output empirically), adjust the test to a `parse_header` round-trip plus an `assert "=" in header and ":" in header` shape check rather than an exact-string equality. Prefer keeping the exact-string assertion because it pins the canonical form.

- [ ] **Step A6.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_content_digest.py \
        tests/_file_exchange/test_content_digest.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): _content_digest.format_header (#146)

RFC 9530 Content-Digest serialiser via http_sf.ser_dictionary.
Round-trips with parse_header. Used by the upload sender (slice 5) to
attach Content-Digest to outgoing PUTs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task A7 — Multi-Python sanity + push + open PR-A

- [ ] **Step A7.1: Run all checks on min + max Python.**

```bash
uv run pytest tests/_file_exchange -q
uv run --python 3.10 pytest tests/_file_exchange -q
uv run --python 3.13 pytest tests/_file_exchange -q
uv run ruff format --check .
uv run ruff check src tests
uv run mypy src
```

Every command must succeed. Expected file-exchange suite count: previous baseline (~558) + the new `test_content_digest.py` count.

- [ ] **Step A7.2: Push the branch.**

```bash
git push -u origin feat/146-content-digest-module
```

- [ ] **Step A7.3: Open PR-A.**

```bash
gh pr create --title "feat(file-exchange): _content_digest.py + http-sf dependency (#146)" \
  --body "$(cat <<'EOF'
## Summary

Root-cause response to the ≥12-finding cluster on PR #169 / #173 / #174 (closed). Introduces a new pure-function module \`_content_digest.py\` wrapping the [\`http-sf\`](https://pypi.org/project/http-sf/) library (Mark Nottingham's RFC 8941 reference implementation, pure-Python, zero transitive deps) plus a comprehensive contract-test suite covering every Content-Digest edge previously surfaced as a route-level bug.

The route itself is untouched — the new module sits unused after this PR. **PR-B** (rebased slice 3 / #169) deletes the hand-rolled parser and migrates the route to call into this module.

## Design

\`docs/superpowers/specs/2026-05-28-content-digest-restructure.md\` — 4-layer architecture (library handles RFC 8941 grammar; \`_content_digest.py\` owns algorithm selection + requireDigest membership; the route still owns bytes verification because that's tied to the staging temp-file lifecycle).

## Surface added

- \`_content_digest.SUPPORTED_ALGORITHMS\` — frozenset of algorithm labels pvl-core supports
- \`_content_digest.parse_header(header, *, preferred=None) -> (algo, bytes) | None\` — RFC 9530 §3 MUST-ignore semantics; honours \`preferred=\`; falls back to first supported entry
- \`_content_digest.satisfies_requirement(algo, required) -> bool\` — case-insensitive membership against \`ArtifactConstraints.requireDigest\`
- \`_content_digest.format_header(algo, raw) -> str\` — RFC 9530 serialiser via \`http_sf.ser_dictionary\`

## Test plan

- [x] \`uv run pytest tests/_file_exchange\` — 558 baseline + N new \`test_content_digest.py\` tests, all green
- [x] \`uv run --python 3.10 pytest tests/_file_exchange\` — green
- [x] \`uv run --python 3.13 pytest tests/_file_exchange\` — green
- [x] \`uv run ruff format --check .\` clean
- [x] \`uv run ruff check src tests\` clean
- [x] \`uv run mypy src\` — no issues

Refs #146, PR #169 (paused pending this).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR-B — Slice 3 (#169) rebase + route migration onto PR-A

Branch: existing `feat/146-upload-route` (PR #169), rebased onto PR-A's branch. Per the spec, this PR replaces #169's current hand-rolled parser with calls into `_content_digest.py` and moves `_rehash_file` to `_staging.py`.

This whole PR is one logical change ("migrate the route to the new module") and a single commit is acceptable, since the existing PR #169 commits already provide the route's matrix-row coverage — the migration is mechanical.

### Task B1 — Rebase the branch onto PR-A's tip

- [ ] **Step B1.1: Sync local main.**

```bash
git checkout main
git pull --ff-only
```

- [ ] **Step B1.2: Fetch PR-A's branch tip.**

If PR-A is not yet merged: rebase onto its branch tip.

```bash
git fetch origin feat/146-content-digest-module
git checkout feat/146-upload-route
```

If PR-A is already merged: rebase onto main.

- [ ] **Step B1.3: Rebase.**

Identify the last commit on `feat/146-upload-route` that was a slice-3 commit (not a slice-2-duplicate). Use the same `git rebase --onto` pattern we used in prior slice rebases:

```bash
# If PR-A is unmerged: target is origin/feat/146-content-digest-module
git rebase --onto origin/feat/146-content-digest-module \
    <last-slice-2-commit-on-branch> feat/146-upload-route
# Or if PR-A is merged into main:
git rebase --onto origin/main <last-slice-2-commit-on-branch> feat/146-upload-route
```

Resolve any conflicts in `_upload.py` / `_content_digest.py` (none expected since PR-A added only new files).

- [ ] **Step B1.4: Verify the rebase is clean.**

```bash
git log --oneline main..HEAD | head -20
uv run pytest tests/_file_exchange -q
```

All existing route tests must still pass before the migration commit lands.

### Task B2 — Move `_rehash_file` to `_staging.py`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_staging.py`
- Modify: `src/fastmcp_pvl_core/_file_exchange/_upload.py`

- [ ] **Step B2.1: Cut + paste the function.**

In `src/fastmcp_pvl_core/_file_exchange/_upload.py`, find the `_rehash_file` definition (currently above `register_upload_route`) and copy its full body. Delete it from `_upload.py`.

Append the body to `src/fastmcp_pvl_core/_file_exchange/_staging.py` after `_write_chunk`:

```python
def _rehash_file(path: str, hashlib_name: str) -> bytes:
    """Synchronously compute ``hashlib_name``'s digest of the file at ``path``.

    Designed to be called via a single ``asyncio.to_thread`` dispatch
    from the upload route's non-sha-256 verify branch — the entire
    open + chunked-read + close runs in one worker thread rather than
    spawning a thread per chunk.
    """
    h = hashlib.new(hashlib_name)
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(_CHUNK)
            if not buf:
                break
            h.update(buf)
    return h.digest()
```

- [ ] **Step B2.2: Update the route's call site to import from `_staging`.**

In `src/fastmcp_pvl_core/_file_exchange/_upload.py`, update the `_staging` import block (already imports `_CHUNK`, `_HASHLIB_BY_LABEL`, `_write_chunk`) to also include `_rehash_file`:

```python
from fastmcp_pvl_core._file_exchange._staging import (
    _CHUNK,
    _HASHLIB_BY_LABEL,
    _rehash_file,
    _write_chunk,
)
```

- [ ] **Step B2.3: Run tests — expect PASS (no behaviour change).**

```bash
uv run pytest tests/_file_exchange/test_upload_route.py tests/_file_exchange/test_staging.py -v
```

- [ ] **Step B2.4: Add a `_staging.py` unit test for `_rehash_file`.**

The function previously had no direct test (route-level tests covered it transitively). Move that coverage onto the module itself:

```python
# Append to tests/_file_exchange/test_staging.py
def test_rehash_file_sha384(tmp_path):
    payload = b"the rehash helper now lives in _staging"
    target = tmp_path / "blob"
    target.write_bytes(payload)
    import hashlib
    expected = hashlib.sha384(payload).digest()
    assert _staging._rehash_file(str(target), "sha384") == expected


def test_rehash_file_sha512(tmp_path):
    payload = b"x" * (3 * _staging._CHUNK + 7)  # multiple chunks + tail
    target = tmp_path / "blob"
    target.write_bytes(payload)
    import hashlib
    expected = hashlib.sha512(payload).digest()
    assert _staging._rehash_file(str(target), "sha512") == expected
```

(Adjust `import hashlib` if `test_staging.py` already imports it; deduplicate.)

- [ ] **Step B2.5: Run, lint, type-check, commit.**

```bash
uv run pytest tests/_file_exchange -q
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_staging.py \
        src/fastmcp_pvl_core/_file_exchange/_upload.py \
        tests/_file_exchange/test_staging.py
git commit -m "$(cat <<'EOF'
refactor(file-exchange): move _rehash_file to _staging.py (#146)

The rehash helper is a chunk-read primitive that fits naturally
alongside _write_chunk in _staging.py. The route imports it from
there; no behaviour change. Two direct unit tests (sha-384,
sha-512 across multiple chunks + tail) move the coverage onto
the module itself rather than relying on the route's transitive
exercise.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B3 — Migrate the route to use `_content_digest`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_upload.py`

- [ ] **Step B3.1: Delete the hand-rolled helpers.**

In `src/fastmcp_pvl_core/_file_exchange/_upload.py`:

1. Delete the `_content_digest_parse` function (the one with the `preferred=` kwarg).
2. Delete the `_content_digest_format` function.
3. Delete the unused imports they pulled in:
   - `import base64 as _b64`
   - `import binascii`
   
   (Keep `import hashlib` — the streaming hasher still uses it.)
4. Delete the module-docstring inventory bullets for those two helpers.

- [ ] **Step B3.2: Add the new module import.**

Add to `_upload.py`'s import block:

```python
from fastmcp_pvl_core._file_exchange import _content_digest
```

(Place it alphabetically among the other `from fastmcp_pvl_core._file_exchange._foo import ...` lines — likely between `_codes` and `_errors`.)

- [ ] **Step B3.3: Migrate the route's parse + selection + requirement-check branch.**

Find the route handler's Content-Digest verification block (currently uses `_content_digest_parse(cd_header, preferred=required)` and the inline lowercase-set comprehension). Replace with:

```python
            cd_header = request.headers.get("content-digest")
            required = expected.requireDigest if expected is not None else None
            if cd_header is not None:
                parsed = _content_digest.parse_header(
                    cd_header, preferred=required
                )
                if parsed is None:
                    return Response(status_code=400)
                cd_algo, cd_raw = parsed
                if not _content_digest.satisfies_requirement(cd_algo, required):
                    # The parser tried ``preferred=required`` first, so
                    # arriving here means the client's header contained no
                    # entry whose algorithm is in ``required`` — only a
                    # fallback entry in a non-required algorithm.
                    return Response(status_code=400)
                if cd_algo == "sha-256":
                    if hasher.digest() != cd_raw:
                        return Response(status_code=400)
                else:
                    try:
                        rehash_digest = await asyncio.to_thread(
                            _rehash_file, tmp_path, _HASHLIB_BY_LABEL[cd_algo]
                        )
                    except OSError:
                        logger.exception(
                            "file-exchange: upload rehash read failed"
                        )
                        return Response(status_code=500)
                    if rehash_digest != cd_raw:
                        return Response(status_code=400)
            elif required is not None:
                return Response(status_code=400)
```

The verify-against-bytes branch (sha-256 streaming compare; sha-384/512 rehash) stays inline because it's tied to the staging temp-file lifecycle.

- [ ] **Step B3.4: Update the module docstring inventory.**

Replace the previous \``_content_digest_parse` / `_content_digest_format` / `_media_range_matches`\` bullet with:

```
- ``_media_range_matches`` — RFC 7231 §3.1.1.1 media-range matcher
  used by the route to enforce ``acceptMimeTypes``. (The Content-Digest
  parse + policy lives in :mod:`._content_digest`.)
```

- [ ] **Step B3.5: Run tests — expect PASS.**

```bash
uv run pytest tests/_file_exchange/test_upload_route.py -v
```

Every existing route-level test must still pass — the migration is behaviour-preserving by design (`_content_digest.parse_header` + `satisfies_requirement` together produce the same observable contract as the prior hand-rolled `_content_digest_parse` + inline set check).

If any test fails, the most likely cause is a behaviour drift at the parser boundary (whitespace handling, malformed-input semantics). Fix by reading the test's docstring (every route test names its matrix row) and adjusting the call site, not by re-introducing hand-rolled parsing.

- [ ] **Step B3.6: Lint, type-check, commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_upload.py
git commit -m "$(cat <<'EOF'
refactor(file-exchange): route uses _content_digest module (#146)

Replace the hand-rolled RFC 8941 parser + inline requirement-set
comprehension in the upload route handler with calls to
_content_digest.parse_header and _content_digest.satisfies_requirement.

Deletes _content_digest_parse and _content_digest_format from
_upload.py (and their now-unused base64/binascii imports) — those
edges are now contract-tested in tests/_file_exchange/test_content_digest.py
rather than scattered across the route's tests.

Bytes verification (sha-256 streaming compare; non-sha-256 rehash via
_rehash_file) stays inline in the route — it's tied to the staging
temp-file lifecycle.

Closes the root-cause restructure tracked in
docs/superpowers/specs/2026-05-28-content-digest-restructure.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B4 — Trim the now-redundant tests + verify on 3.10/3.13

**Files:**
- Modify: `tests/_file_exchange/test_upload_helpers.py`

The previous Content-Digest tests in `test_upload_helpers.py` exercised the deleted hand-rolled parser. The equivalent contract is now in `test_content_digest.py` (Task A4). Trim `test_upload_helpers.py` so it covers only its remaining concern (`_media_range_matches`).

- [ ] **Step B4.1: Delete the Content-Digest test block.**

In `tests/_file_exchange/test_upload_helpers.py`, delete every test whose name starts with `test_content_digest_*` and `test_content_digest_format_round_trips`. Keep:

- `test_media_range_matches_table` (parametrised)

Update the module docstring to reflect the narrower scope:

```python
"""Matrix rows F3, F4: ``_media_range_matches`` (RFC 7231 §3.1.1.1).

Content-Digest helper tests moved to ``test_content_digest.py`` when
the parse + policy was extracted from ``_upload.py`` into
``_content_digest.py``.
"""
```

Drop unused imports (`base64`, `hashlib`, etc.) that the Content-Digest tests used; keep `pytest`.

Also remove the `_content_digest_parse` / `_content_digest_format` names from the import block at the top of the file.

- [ ] **Step B4.2: Verify nothing else imports the deleted names.**

```bash
grep -rn "_content_digest_parse\|_content_digest_format" src tests
```

Expected: zero matches (every consumer now goes through `_content_digest.parse_header` / `_content_digest.format_header`).

- [ ] **Step B4.3: Run all checks on min + max Python.**

```bash
uv run pytest tests/_file_exchange -q
uv run --python 3.10 pytest tests/_file_exchange -q
uv run --python 3.13 pytest tests/_file_exchange -q
uv run ruff format --check .
uv run ruff check src tests
uv run mypy src
```

- [ ] **Step B4.4: Commit.**

```bash
git add tests/_file_exchange/test_upload_helpers.py
git commit -m "$(cat <<'EOF'
test(file-exchange): trim test_upload_helpers.py to media-range only (#146)

The Content-Digest tests in this file exercised the hand-rolled parser
that was just removed. The equivalent contract is now in
test_content_digest.py. _media_range_matches (RFC 7231) is unrelated
and stays.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task B5 — Force-push slice 3 and flip back to ready

- [ ] **Step B5.1: Force-push.**

```bash
git push --force-with-lease origin feat/146-upload-route
```

- [ ] **Step B5.2: Flip PR #169 back to ready and post the diagnosis-resolved comment.**

```bash
gh pr ready 169
gh pr comment 169 --body "$(cat <<'EOF'
## Restructure complete; flipping back to ready

The digest-pipeline restructure landed across two PRs as planned:

- **PR-A** (\`feat/146-content-digest-module\` / #<NUMBER>) added the new \`_content_digest.py\` module wrapping http-sf, plus a contract-test suite covering every prior route-level edge.
- **PR-B** (this branch, rebased): deleted the hand-rolled \`_content_digest_parse\` / \`_content_digest_format\`; moved \`_rehash_file\` to \`_staging.py\`; route now orchestrates \`parse_header\` + \`satisfies_requirement\` + the existing verify-against-bytes branch.

Every prior route test still passes. The matrix's row count for slice 3 is unchanged. The Content-Digest edge surface that produced ≥12 findings over three review rounds is now contract-tested at the module level, with the route's role narrowed to orchestration.

The wire-spec gap that surfaced as PR #174 (\`requireDigest=[""]\`) is tracked separately at [\`pvliesdonk/mcp-file-exchange-ext#16\`](https://github.com/pvliesdonk/mcp-file-exchange-ext/issues/16); pvl-core does not fork the vendored spec.
EOF
)"
```

Replace `<NUMBER>` with PR-A's actual number once it's opened.

---

## PR-C / PR-D — Rebase slices 4 + 5 mechanically

After PR-B is up to date with the rewritten slice 3:

- [ ] **Step C1: Rebase #170 onto the new slice 3.**

```bash
git checkout feat/146-cross-transport-registrar
git fetch origin feat/146-upload-route
git rebase --onto feat/146-upload-route <last-slice-3-commit> \
    feat/146-cross-transport-registrar
uv run pytest tests/_file_exchange -q
git push --force-with-lease
```

`<last-slice-3-commit>` is the slice-3 head on the slice-4 branch *before* the rebase (use `git log --oneline feat/146-cross-transport-registrar ^feat/146-upload-route | tail -1`'s parent).

- [ ] **Step D1: Rebase #171 onto the new slice 4.**

```bash
git checkout feat/146-sender-e2e
git rebase --onto feat/146-cross-transport-registrar <last-slice-4-commit> \
    feat/146-sender-e2e
uv run pytest tests/_file_exchange -q
uv run --python 3.13 pytest tests/_file_exchange -q
uv run mypy src
git push --force-with-lease
```

No code changes expected — the sender (slice 5) imports `_content_digest.format_header` from the new module; it was always going to use that name once the module landed, so this is a mechanical update of the import and the call site.

Actually, the sender on slice 5 currently calls `_content_digest_format` (the deleted hand-rolled helper). The rebase will conflict on the sender's body. Resolve by updating the call:

```python
cd_header = _content_digest.format_header("sha-256", hasher.digest())
```

(Add the `from fastmcp_pvl_core._file_exchange import _content_digest` import to `_upload.py` if not already present from PR-B's rebase.)

---

## Self-review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Architecture: 4 layers | Task A2–A6 (layers 1–3); Task B3 (layer 4 orchestration) |
| `_content_digest.py` module creation | Task A2 (skeleton), A3–A4 (`parse_header`), A5 (`satisfies_requirement`), A6 (`format_header`) |
| `_staging.py` extension (`_rehash_file`) | Task B2 |
| `_upload.py` simplification | Task B3 (delete hand-rolled helpers + migrate route), Task B4 (trim helper tests) |
| `test_content_digest.py` contract tests | Task A4 (every prior route-level finding has a row) |
| `test_upload_helpers.py` shrunk | Task B4 |
| `http-sf` dependency | Task A1 |
| Two-PR shape | PR-A: A1–A7; PR-B: B1–B5 |
| Slices 4 + 5 mechanical rebase | PR-C / PR-D |
| Wire-format discipline (no schema edits) | No task touches `_schema/file-exchange.json` — explicit by omission. |

**Placeholder scan:** One conditional in Task A4.6/A4.7 ("if the test reports None, rewrite the assertion to..."). This is intentional — the http_sf library's exact case-folding and OWS behaviour at the dictionary-key layer needs empirical verification at implementation time, and the test should pin whichever behaviour actually fires. The plan provides both forms of the assertion concretely. Not a TBD; a conditional with both branches spelled out.

**Type consistency:** `parse_header` returns `tuple[str, bytes] | None` everywhere it's used (A3, A4, B3). `satisfies_requirement` returns `bool` everywhere (A5, B3). `format_header` returns `str` everywhere (A6, slice 5 rebase). `_rehash_file` returns `bytes` everywhere (B2, B3).

`SUPPORTED_ALGORITHMS` is a `frozenset[str]` containing lowercase labels (`"sha-256"`, `"sha-384"`, `"sha-512"`) — every test and call site matches.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-28-content-digest-restructure.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session with `superpowers:executing-plans`, batch with checkpoints.

Which approach?
