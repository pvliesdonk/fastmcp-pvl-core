# Content-Digest pipeline restructure (root-cause response to #169 finding cluster)

> **Status:** Design record. Process artifact for the in-flight #146
> chain (slice 3 / PR #169 was paused to address this).

## Why this design exists

Across rounds 1–3 of bot review on PR #169 plus the post-merge passes on
#167 / #168 / #173 / the closed #174, the same area produced ≥12 findings:

| Cluster | Findings |
|---|---|
| **Hand-rolled SF parser edges** | RFC 8941 SF-parameter handling; multi-algorithm entry ordering; case-insensitivity of algorithm labels; whitespace tolerance; base64 validation |
| **Policy layered on parser** | `requireDigest` case sensitivity; `requireDigest=[""]` Pydantic/route drift; parser fallback semantics; caller-site rejection logic split between parser and route |
| **Coverage gaps in the verify branch** | `_rehash_file` happy path; non-sha-256 mismatch; consume-after-store observability |
| **Cross-source drift** | Schema vs. Pydantic vs. route logic disagreements ("triple source of truth"); the closed #174 attempt to tighten Pydantic beyond the vendored spec |

That cluster size is exactly the signal `CLAUDE.md`'s Test-driven discipline
section names: *"the moment you notice multiple defects pointing at the
same underlying design hole, that is the signal."* Continuing to apply
findings one-by-one is the whack-a-mole the discipline exists to prevent.

The root cause is **a hand-rolled RFC 8941 / 9530 parser interleaved with
`requireDigest`-policy and bytes-verification concerns in a single
function**. The library handles the parsing grammar perfectly; the policy
is small but currently shares its module and its tests with the parser.
Each spec edge has been discovered as a route-level bug rather than a
parser-level contract.

## Architecture: four layers, library handles layer 1

| Layer | Concern | Where it lives |
|---|---|---|
| 1. SF parsing | RFC 8941 grammar — dictionary, item parameters, byte-sequence framing, whitespace, case | [`http-sf`](https://pypi.org/project/http-sf/) library (`http_sf.parse(header, tltype="dictionary")`) |
| 2. Algorithm selection | Filter parsed dictionary to supported algorithms; honour caller's `preferred=` list; fall back to first supported when no preferred is present | `_content_digest.py` — `parse_header` |
| 3. Requirement membership | Case-insensitive set check: is the selected algorithm in `requireDigest`? | `_content_digest.py` — `satisfies_requirement` |
| 4. Bytes verification | sha-256: compare against the streaming-hash captured during body read. sha-384/512: rehash from the staged temp file via `_rehash_file`. | Inline in `_upload.py` route handler + `_rehash_file` in `_staging.py` |

Each layer has a single responsibility and a well-defined input/output
contract. The route handler is then a thin orchestrator:

```
cd_entry = parse_header(cd_header, preferred=required)   # layer 1+2
if cd_entry is None:
    return 400
algo, raw = cd_entry
if not satisfies_requirement(algo, required):            # layer 3
    return 400
if not verify_against_bytes(algo, raw, hasher, tmp_path):  # layer 4
    return 400
```

## Module layout

### New: `src/fastmcp_pvl_core/_file_exchange/_content_digest.py`

```python
"""RFC 9530 Content-Digest header parser + policy helpers.

Parsing delegates to the http_sf library (RFC 8941 Structured Fields);
this module adds the spec-9530 policy layered on top: which algorithms
pvl-core supports, how a multi-algorithm dictionary is reduced to a
single selected entry, and whether a selected entry satisfies a
receiver's ``requireDigest`` constraint.

Bytes verification (computing or rehashing the body's digest) is NOT
in this module — that's the route's concern because it owns the
staging temp-file lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterable

import http_sf

SUPPORTED_ALGORITHMS = frozenset({"sha-256", "sha-384", "sha-512"})


def parse_header(
    header: str, *, preferred: Iterable[str] | None = None
) -> tuple[str, bytes] | None:
    """Parse a Content-Digest header into ``(algo, raw_digest_bytes)``.

    RFC 9530 §3 MUST-ignore: unsupported algorithms are skipped, not
    rejected. ``preferred`` (if given) is the caller's algorithm
    preference list — a matching entry is returned in preference to any
    other supported entry. When no preferred entry is present (or no
    ``preferred`` was given), the first supported well-formed entry is
    returned. ``None`` only when the dictionary parses but contains no
    supported entry, or when the header value is malformed at the
    RFC 8941 layer.

    Parameter dictionaries on entries (``algo=:bytes:;p=v``) are accepted
    and ignored per RFC 9530 §3.
    """
    ...


def satisfies_requirement(algo: str, required: Iterable[str] | None) -> bool:
    """Case-insensitive membership: does ``algo`` appear in ``required``?

    ``required=None`` or empty means no constraint — always True.
    ``algo`` and the ``required`` entries are compared after
    ``str.strip().lower()`` normalisation (RFC-aligned: algorithm labels
    are case-insensitive in the structured-fields grammar).
    """
    ...


def format_header(algo: str, raw: bytes) -> str:
    """Serialise ``(algo, raw)`` to a Content-Digest header value.

    Delegates to http_sf to produce the canonical RFC 8941 form.
    """
    ...
```

### Extended: `src/fastmcp_pvl_core/_file_exchange/_staging.py`

Add `_rehash_file` (currently in `_upload.py`):

```python
def _rehash_file(path: str, hashlib_name: str) -> bytes:
    """Synchronously compute ``hashlib_name``'s digest of the file at ``path``.

    Designed to be called via a single ``asyncio.to_thread`` dispatch
    from the route's non-sha-256 verify branch (the entire open + chunked
    read + close runs in one worker thread).
    """
    ...
```

### Simplified: `src/fastmcp_pvl_core/_file_exchange/_upload.py`

Delete:
- `_content_digest_parse` (replaced by `_content_digest.parse_header`)
- `_content_digest_format` (replaced by `_content_digest.format_header`)
- The inline `_rehash_file` (moved to `_staging.py`)
- The route's lowercase-`required`-set comprehension (moved to `satisfies_requirement`)

Keep:
- `UPLOAD_PREFIX`, `_UPLOAD_METHOD` constants
- `upload_receiver_mint`
- `_media_range_matches` (RFC 7231, separate concern)
- The route handler, simplified to orchestrate the four layers
- `upload_sender_consume` (slice 5 — uses `_content_digest.format_header`)

### Tests

- **New:** `tests/_file_exchange/test_content_digest.py` — exhaustive contract tests for `parse_header`, `satisfies_requirement`, `format_header`. Tests include every spec edge previously found as a route-level bug.
- **Shrunk:** `tests/_file_exchange/test_upload_helpers.py` — keeps only the `_media_range_matches` table.
- **Unchanged behaviour:** `tests/_file_exchange/test_upload_route.py` — every existing route-level test continues to pass; only the function names invoked inside the route change.

## Dependency: `http-sf`

- Package: [`http-sf`](https://pypi.org/project/http-sf/) on PyPI.
- Upstream: github.com/mnot/http-sf (Mark Nottingham, IETF HTTPbis editor).
- Pure-Python; no transitive deps.
- Python ≥ 3.8 supported (pvl-core targets 3.10+, well above).
- Minimum version: pin to whatever currently exposes the `tltype="dictionary"` form. Resolve concretely during PR-A.

The lock-in risk is low: the library wraps an IETF Proposed Standard that
is itself frozen, and the surface we touch (`parse(bytes, tltype="dictionary")`
+ `serialize(dict, tltype="dictionary")`) is the textbook RFC 8941 entry point.
If the library is ever removed from PyPI, swapping back to a hand-rolled
parser is a one-module change because the policy layers stay put.

## PR shape

Two PRs in sequence, then the existing slice chain rebases:

### PR-A — new, against `main`

- Add `http-sf>=<min>` to `pyproject.toml`'s runtime deps.
- Create `_content_digest.py` with the three functions above.
- Create `tests/_file_exchange/test_content_digest.py` with comprehensive contract tests including every prior route-level edge:
  - Empty header → `None`.
  - Single supported algorithm, well-formed → returns it.
  - Multiple supported algorithms, no `preferred` → first one.
  - Multiple supported, `preferred=["sha-256"]` with sha-256 second → sha-256 returned.
  - Unsupported algorithm + supported algorithm → supported returned.
  - All unsupported → `None`.
  - SF parameters on the entry (`sha-256=:b64:;foo=bar`) → ignored, entry still parses.
  - OWS around `,` between dictionary entries → tolerated (RFC 8941 grammar).
  - OWS around `=` within a dictionary entry → **rejected** by `http_sf` per
    strict RFC 8941 grammar (pinned in `test_parse_header_ows_around_equals_rejected`).
  - Malformed base64 → `None`.
  - Uppercase algorithm label (`SHA-256=:...:`) → **rejected** by `http_sf`
    per RFC 8941 lcalpha (pinned in `test_parse_header_uppercase_label_rejected`).
  - `satisfies_requirement` with `required=None`, empty list, uppercase, mixed case.
  - `format_header` round-trips through `parse_header`.
- Does NOT touch `_upload.py` yet — keeps the diff narrow and reviewable.
- After merge: pvl-core has the new module sitting unused, with full test
  coverage. PR-B then wires it in.

### PR-B — rebase of slice 3 (#169) on top of PR-A

- Delete `_content_digest_parse`, `_content_digest_format` from `_upload.py`.
- Move `_rehash_file` from `_upload.py` to `_staging.py`; update the route's call site.
- Route handler: replace the hand-rolled parse with
  `parse_header(cd_header, preferred=required)`; replace the inline
  set-comprehension with `satisfies_requirement(cd_algo, required)`.
- Delete tests that lived on the hand-rolled parser
  (`test_upload_helpers.py`'s Content-Digest section); the same edges
  are now covered in `test_content_digest.py` (PR-A).
- `test_upload_route.py` tests stay — they're route-level and orchestration-level, exactly what should still be route-tested.

### PR-C / PR-D — rebase of #170 and #171

Mechanical rebases on the new slice 3. They don't touch the digest path
directly (slice 4 is the cross-transport registrar; slice 5 is the sender,
which uses `format_header`).

## Error handling

- `_content_digest.parse_header` catches every exception `http_sf.parse`
  can raise (the library raises `ValueError` for malformed input;
  catching broadly so a library version bump's new exception type
  doesn't change pvl-core's observable behaviour) and returns `None`.
  The route treats `None` as `400 digest-mismatch`, matching today.
- `satisfies_requirement` is pure; cannot fail.
- `format_header` round-trips known-supported input; we never feed it
  arbitrary algorithm names, so it cannot fail in practice.
- The route's existing `ArtifactConstraints.model_validate` try/except
  (slice 3's existing fix) is unchanged.

## Spec / vendoring discipline

This restructure does NOT change any wire format. The vendored
`_schema/file-exchange.json` is untouched. The `requireDigest=[""]`
finding from PR #174 was correctly identified as a wire-spec gap rather
than a pvl-core bug — tracked separately as
[`pvliesdonk/mcp-file-exchange-ext#16`](https://github.com/pvliesdonk/mcp-file-exchange-ext/issues/16)
proposing `minLength: 1` on `requireDigest` items for v0.2 of the spec.
When that lands, pvl-core re-pins and the constraint comes through the
schema correctly. Until then, the route's policy layer naturally rejects
`[""]` because `""` is not in `SUPPORTED_ALGORITHMS`.

## What this restructure does NOT address

Two outstanding cross-transport questions remain open and are
intentionally out of scope here:

1. **Per-chunk `asyncio.to_thread` vs. accumulated-buffer writes.** Both
   download and upload do per-chunk `to_thread` dispatch — the symmetry is
   intentional but the buffering trade-off was discussed during spec
   brainstorm and may want revisiting. Track as a separate issue against
   both data planes.
2. **Schema vs. Pydantic vs. route triple-source drift.** Beyond
   `requireDigest`, other wire fields may have similar drift. A separate
   audit pass against the merged data planes (download + upload) is
   warranted once #146 is fully landed.

Both are listed in the EPIC #138 issue map's "out of scope" tracker.

## References

- Bot finding patterns: PR #169 comments rounds 1–3; PR #173 (merged);
  PR #174 (closed for forking vendored spec).
- Memory `feedback_three_abandons_means_scope.md` — informs the
  "stop and design" choice over another iteration round.
- CLAUDE.md — "Test-driven discipline" section's design-revision rule;
  "Spec docs are protocol extensions, not design docs" section's
  vendoring discipline.
- RFC 8941 (Structured Fields), RFC 9530 (Digest Fields, in particular
  §3 MUST-ignore semantics), RFC 7231 §3.1.1.1 (media-range — unaffected).
