# #141 — `exchange://` URI scheme + path confinement (design)

> **Status:** contemporaneous design record. The implementation in the
> same PR is the source of truth; this document captures the shape
> agreed before implementation and the rationale for the non-obvious
> (especially security-critical) choices.

EPIC: [#138](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/138).
Issue: [#141](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/141).
Depends on: [#139](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/139)
(wire format, merged) — uses the `FilesystemSource`/`FilesystemSink`
`uri` field and the `_FS_URI_PATTERN` it is validated against.
Wire-spec authority: `pvliesdonk/mcp-file-exchange-ext` at pinned commit
[`5f50a4e16a33a6bbc0888c142baec7fdfe858cb6`](https://github.com/pvliesdonk/mcp-file-exchange-ext/commit/5f50a4e16a33a6bbc0888c142baec7fdfe858cb6),
sections §10.1.1 (`exchange` scheme), §10.1.2 (`file` scheme), §10.1.3
(obligations), §15 (filesystem path-traversal mitigation).

## Goal

Provide the **security-critical** machinery to turn an untrusted
`filesystem`-descriptor URI into a confined local path: an
`exchange://` / `file://` parser, env-driven volume-mapping config, and
a canonicalize-and-confine primitive that rejects path-traversal and
symlink escapes. This is consumed by #143's role helpers (to build the
§9 `is_accessible` selection callback) and #148's register helpers (to
load the volume map at startup). #141 ships no transport wiring itself.

## Module layout

One new private module:

```
src/fastmcp_pvl_core/_file_exchange/
└── _paths.py   # URI parse → volume resolve → canonicalize + confine
```

Public surface (re-exported via `src/fastmcp_pvl_core/file_exchange.py`,
mirrored in the subpackage `__init__.py`, both `__all__` lists updated):

- `canonicalize_and_confine(candidate, root) -> Path | None`
- `resolve_filesystem_uri(uri, *, volume_map) -> Path | None`
- `load_volume_map(env_prefix) -> dict[str, Path]`
- `VolumeMap` type alias (`Mapping[str, Path]`)

The URI parser (`_parse_fs_uri`) and the volume-map string parser
(`_parse_volume_map`) are **internal** — the wire layer's
`_FS_URI_PATTERN` already validates URI shape at model-construction
time, so downstream holds typed descriptors and calls
`resolve_filesystem_uri(descriptor.uri, volume_map=...)` rather than
parsing raw strings.

Tests: `tests/_file_exchange/test_paths.py`.

## Failure signaling (decided up front)

Every non-usable outcome returns `None`, never raises (except
`load_volume_map`, see below):

- unknown volume → `None` (benign skip, per §9 / §10.1.1),
- malformed URI → `None` (the wire layer normally pre-rejects these),
- confinement escape (`..` / symlink out of root) → `None` **plus a
  `WARNING` log**.

`None` is the §9 "skip this descriptor" signal, so #143's
`is_accessible` callback treats it uniformly without exception control
flow. The escape case additionally logs because an untrusted path
escaping its root is a likely-malicious event an operator should see.

The single exception to "never raises": `load_volume_map` /
`_parse_volume_map` raise `ConfigurationError` (the repo's existing
startup-config error type, already exported) on a malformed volume map —
an operator misconfiguration that should fail loudly at startup, not
silently make every `exchange://` URI unresolvable.

## URI parser (`_parse_fs_uri`, internal)

```python
# ("exchange", volume, path)  |  ("file", "", abs_path)  |  None
def _parse_fs_uri(uri: str) -> tuple[Literal["exchange", "file"], str, str] | None: ...
```

Uses `urllib.parse.urlsplit` (clean handling of custom schemes).

`exchange://<volume>/<path>` — `urlsplit("exchange://docs/a/b.bin")`
yields scheme `exchange`, netloc `docs`, path `/a/b.bin`. Returns
`("exchange", "docs", "a/b.bin")` with the leading `/` stripped. This
strip is **security-load-bearing, not cosmetic**: `pathlib`'s `root /
"/a/b.bin"` discards `root` and yields `/a/b.bin` (absolute right-hand
operands reset the join), so passing the unstripped path to
`root / path` would silently escape the volume before confinement even
runs. The parser hands the resolver a relative path; the resolver joins
it under the mount. (`..` segments are intentionally left intact — the
raw `/../etc` becomes `../etc`, which the join + confine step then
catches.) Rejected (→ `None`):

- empty volume / netloc (`exchange:///x`),
- empty path or bare `/` (`exchange://docs`, `exchange://docs/`),
- any query or fragment component (userinfo/port in the netloc are *not*
  rejected — the wire's `[^/]+` accepts them, so they are absorbed into the
  volume id, which then fails the volume lookup and is skipped benignly),
- a non-lowercase scheme (`urlsplit` lowercases it, but the wire pattern
  is case-sensitive, so `EXCHANGE://…` must be rejected),
- embedded ASCII control characters (`\x00`, `\t`, `\n`, `\r`): `urlsplit`
  silently strips tab/newline/CR (WHATWG), so without an up-front guard the
  parser would act on a `urlsplit`-mutated string the descriptor never
  carried. The parser rejects all four so it only acts on the exact bytes
  received; the null byte additionally must go because `Path.resolve()`
  raises on it (the never-raise contract). The drift-guard tests pin that
  the parser never accepts a URI the wire rejects.

`file:///<abs>` — `urlsplit("file:///mnt/x")` yields scheme `file`,
netloc `""`, path `/mnt/x`. Returns `("file", "", "/mnt/x")`. Rejected:

- non-empty authority (`file://host/x`) — §10.1.2 mandates empty
  authority,
- non-absolute path, or any query/fragment.

Unknown scheme or no scheme → `None`.

**Consistency with `_FS_URI_PATTERN`:** the parser re-derives validity
structurally rather than reusing the regex (match and decompose are
different jobs). A drift-guard test asserts the two agree on the key
cases, so a future spec bump that loosens one without the other is
caught.

## Volume-map config

Env var `{env_prefix}_FILE_EXCHANGE_VOLUMES`, read via
`env(env_prefix, "FILE_EXCHANGE_VOLUMES")`. Format: comma-separated
`name=path`:

```
SCHOLAR_FILE_EXCHANGE_VOLUMES="docs=/mnt/docs,scratch=/mnt/scratch"
```

Pure parser (testable without env):

```python
def _parse_volume_map(raw: str, var_name: str) -> dict[str, Path]: ...
```

- split on `,`, drop empty entries (the `parse_list` discipline);
- split each entry on the **first** `=` only (`name`, `path`);
- strip `name`; strip `path` and wrap in `Path`;
- raise `ConfigurationError` on: an entry with no `=`, empty name,
  empty path, duplicate volume name, or a non-absolute mount path.
  `var_name` is the resolved env var name, interpolated into the error
  message so the operator knows which variable to fix.

Public loader:

```python
def load_volume_map(env_prefix: str) -> dict[str, Path]:
    var_name = f"{env_prefix.rstrip('_')}_FILE_EXCHANGE_VOLUMES"
    raw = env(env_prefix, "FILE_EXCHANGE_VOLUMES")
    return _parse_volume_map(raw, var_name) if raw else {}
```

Unset/empty var → `{}`: a server with no volume mappings resolves no
`exchange://` (and, per the single-config decision below, no `file://`
either), so all filesystem descriptors are skipped during §9 selection
— the correct "this party doesn't do filesystem" posture, consistent
with #140's `is_accessible=None`.

`env_prefix` is **required** and threaded from the downstream server's
own env prefix (e.g. `SCHOLAR`), exactly as for every other pvl-core env
reader (`ServerConfig.from_env`, `build_event_store`,
`build_instructions`, `maybe_start_debugpy`). pvl-core owns the
`_FILE_EXCHANGE_VOLUMES` suffix (the contract's *shape*); the prefix is
the operator's per-server *namespace*, so two pvl servers on one host
never collide on a single shared variable. There is deliberately no
default — a baked-in default that downstream could override would be the
forbidden "default but overridable" kwarg bucket, and the per-server
prefix is not pvl-core's to guess. Mount paths are validated absolute at
load; existence is **not** checked (a volume may mount lazily) —
non-existence surfaces later as a confinement/accessibility failure.

## `canonicalize_and_confine` (security primitive)

```python
def canonicalize_and_confine(
    candidate: Path | str, root: Path | str
) -> Path | None:
    resolved_root = Path(root).resolve()
    resolved_candidate = Path(candidate).resolve()
    if resolved_candidate.is_relative_to(resolved_root):
        return resolved_candidate
    return None
```

Correctness:

- `Path.resolve()` (strict=False, the default) resolves **every**
  symlink in the path's existing prefix and normalizes `.`/`..` — a
  symlink anywhere along the path that points outside `root` is followed
  to its real target, which then fails `is_relative_to`. Catches
  symlink escapes, including via intermediate components.
- For a not-yet-existing tail (a sink target not yet created),
  `resolve()` resolves the existing prefix and appends the remainder
  lexically; `..` is still normalized (`/root/../etc/x` → `/etc/x` →
  rejected). Confinement does **not** require the full path to exist;
  existence / readability / writability is #143's `os.access` concern.
- `root` is itself `resolve()`d so a root under a symlink compares
  canonically.
- `is_relative_to` (3.9+; floor is 3.10) is `True` for the root itself
  and any descendant.

**Known limitation (documented, not solved here):** this is a
resolution-time check. A symlink swapped between the check and a later
`open()` (classic TOCTOU) is not defended here — the data-plane open in
#143 should re-confine or use `O_NOFOLLOW` / `openat`. Out of scope for
#141.

## `resolve_filesystem_uri` (pipeline)

```python
def resolve_filesystem_uri(uri: str, *, volume_map: VolumeMap) -> Path | None:
    parsed = _parse_fs_uri(uri)
    if parsed is None:
        return None  # malformed — skip
    scheme, volume, path = parsed

    if scheme == "exchange":
        root = volume_map.get(volume)
        if root is None:
            return None  # no mapping — skip, per §9 / §10.1.1
        confined = canonicalize_and_confine(root / path, root)
        if confined is None:
            logger.warning(
                "file-exchange: exchange:// path escaped its volume root; "
                "rejecting (volume=%r)",
                volume,
            )
        return confined

    # scheme == "file": confine against ANY configured volume mount point
    for root in volume_map.values():
        confined = canonicalize_and_confine(path, root)
        if confined is not None:
            return confined
    logger.warning(
        "file-exchange: file:// path is not within any configured volume; "
        "rejecting"
    )
    return None
```

**Single-config decision:** there is exactly one config — the volume
map. An `exchange://` URI confines to its resolved volume root; a
`file://` path is accepted iff it canonicalizes within *any* configured
volume mount point. The set of volume mounts is the universe of
exchange directories. §10.1.2's "a configured exchange directory" is
satisfied by the volume mounts; a separate `file://`-only dirs config is
not introduced (YAGNI — `file://` is the niche shared-mount-namespace
case, and a path outside every shared volume is exactly what should be
rejected). If a downstream later needs `file://` dirs that are not named
volumes, that is a pvl-core shape change at that point.

**Logging discipline (per the URL-redaction memory):** a confinement
rejection logs at `WARNING`, but the line carries **only the volume id**
(`exchange://`) or nothing identifying (`file://`) — never the raw URI
or the resolved path, which are attacker-controlled (log-injection /
filesystem-layout-disclosure vector). The benign "no mapping for this
volume" case is not logged.

## Tests (`tests/_file_exchange/test_paths.py`)

**1. `_parse_fs_uri` (parametrized):** valid exchange/file forms decode
to the expected tuples; the rejection cases above all return `None`.
Plus a drift-guard test asserting agreement with `_FS_URI_PATTERN`.

**2. `_parse_volume_map` / `load_volume_map`:** happy-path parse;
whitespace trimming; empty-entry dropping; first-`=` split; the five
`ConfigurationError` cases (no `=`, empty name, empty path, duplicate
name, non-absolute path); `load_volume_map` returns `{}` on unset env
(`monkeypatch.delenv`) and the parsed dict on set env.

**3. `canonicalize_and_confine` + `resolve_filesystem_uri` (security
core, property-based via `hypothesis` + concrete vectors, real
`tmp_path` + `os.symlink`):**

- **Escape vectors → `None`:** `..` traversal, absolute-path escape
  (`file://` outside every volume), symlink escape (symlink inside root
  → sibling outside), symlink in an intermediate component, combined
  `..`+symlink.
- **Confined cases → resolved path:** plain file under root; internal
  `..` that stays inside (`root/a/../b`); a symlink inside root pointing
  elsewhere inside root.
- **Property (hypothesis):** for generated relative segments (including
  `..`, `.`, dotted names) joined under a `tmp_path` root, the security
  invariant holds — *whenever the function returns non-`None`, the
  result `is_relative_to` the resolved root.* The generator deliberately
  includes `..`-heavy and symlink-target strings.

**New dependency:** `hypothesis` is added to `[dependency-groups].dev`
(not currently present). The issue mandates property-based confinement
tests, and security-critical confinement is exactly where property
testing earns its keep. CI's `uv sync --all-extras` picks it up.

## Risks and non-risks

**Risk (security):** confinement correctness is the whole point.
Mitigation: the `Path.resolve()` + `is_relative_to` approach resolves
symlinks and `..` before the containment check; the property test
asserts the invariant across generated inputs, and concrete tests pin
every named escape vector.

**Risk:** TOCTOU between confinement and a later `open()`. Mitigation:
documented as a limitation; the data-plane open (#143) re-confines /
uses `O_NOFOLLOW`. Not solved in #141.

**Non-risk:** the comma/`name=path` env format breaks on a mount path
containing a literal comma. Mitigation: paths with commas are
vanishingly rare and operator-controlled; first-`=` splitting already
tolerates `=` in paths. If it ever bites, the format can move to JSON
without changing the resolver.

**Non-risk:** single config (no separate `file://` dirs). Mitigation:
covered above — volume mounts are the exchange-directory universe;
expand to a second config only if a real downstream need appears.
