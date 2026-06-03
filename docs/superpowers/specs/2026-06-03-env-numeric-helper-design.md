# Design: numeric env helpers (`env_int` / `env_float`)

**Date:** 2026-06-03  
**Issues:** pvliesdonk/fastmcp-pvl-core#180, #159 (Closes both — see closure note)  
**Follow-up:** pvliesdonk/fastmcp-pvl-core#181 (`_debug.py` convergence — out of scope here)  
**Related downstream:** pvliesdonk/markdown-vault-mcp#579 (blocked on this landing + a pvl-core release)

---

## Problem

`fastmcp_pvl_core._env` exposes `env` (read) and `parse_bool` / `parse_list` /
`parse_scopes` (convert) — but **no numeric parser**. Downstream servers therefore
hand-roll int/float env parsing per field. `markdown-vault-mcp` does it **nine times
in two shapes**: *warn-and-fall-back-to-default* (×5) and *parse-or-raise* (×4), most
with a post-parse bound. pvl-core is the right place to absorb this once, alongside
the existing `parse_*` family.

## Design

### Shape

Read-and-parse helpers mirroring `env` (not parse-only), because every observed
consumer reads *and* parses — one call replaces the whole read + `try/except` +
fallback stanza. No standalone `parse_int` / `parse_float` (YAGNI: no consumer needs
parse-only today).

```python
def env_int(prefix, name, default=None, *, strict=False, minimum=None, maximum=None) -> int | None
def env_float(prefix, name, default=None, *, strict=False, minimum=None, maximum=None) -> float | None
```

Overloads mirror `env`: `default: int` → returns `int`; `default: None` / omitted →
returns `int | None` (and likewise `float`).

### Dual mode — one function, `strict=` flag

A single function per type expresses both observed shapes via `strict`. `strict` is a
per-field **behavioral selector the caller chooses** (like `default`), not a shape
override and not operator config:

| Input | `strict=False` (soft) | `strict=True` |
|---|---|---|
| unset / blank-after-strip | return `default` (silent) | return `default` (silent) |
| valid & in-bounds | return parsed value | return parsed value |
| set but non-numeric | `WARNING` + return `default` | raise `ConfigurationError` |
| out of `[minimum, maximum]` (inclusive) | `WARNING` + return `default` | raise `ConfigurationError` |
| `env_float` only: `nan` / `±inf` | `WARNING` + return `default` | raise `ConfigurationError` |

The unset/blank case delegates to `env(prefix, name)` (which already strips and treats
blank as unset), so the numeric helpers never re-implement read/strip semantics. Only a
**set, non-blank** value is ever parsed; "unset" and "malformed" stay distinct (the
raise/warn only fires on malformed-when-set, never on a simply-absent var).

### Bounds = reject, never clamp

`minimum` / `maximum` are inclusive and **reject** out-of-range values (treated
identically to malformed: soft warns + default, strict raises). No clamping/coercion —
silently changing an operator's configured value is a footgun. The bounds validate the
**operator's env value only**; the developer-supplied `default` is the trusted fallback
and is returned as-is (never re-validated against the bounds), consistent with the
unset path.

### Errors and messages

- Raise-mode uses **`ConfigurationError`** (the existing operator-visible-misconfig
  type), consistent with #159 / #161. This is also what lets `from_env` reuse the
  helper and inherit the better message.
- Message names the full key and the offending value:
  - non-numeric: `MYAPP_PORT must be an integer; got 'abc'` (`env_float`: `must be a number`)
  - non-finite float: `MYAPP_X must be a finite number; got 'inf'`
  - bound: `MYAPP_PORT must be >= 1; got 0` / `must be <= 65535; got 70000`
- Soft mode logs the **same message text** at `WARNING` via a module logger
  (`logging.getLogger("fastmcp_pvl_core._env")`), suffixed `— using default <default>`.

### Accept-set (shape decision, recorded during review)

The helpers **delegate to Python's `int()` / `float()`** and accept whatever those
accept — including PEP 515 underscore separators (`"1_000"` → `1000`), a leading
sign, and (for `float` only) scientific notation (`"1e3"` → `1000.0`). This is a
deliberate shape decision: rejecting underscores alone would be incoherent while
still accepting `+5`/non-ASCII digits (and, for `float`, `1e3`), and a true "plain
decimal only"
contract would need a regex for no real benefit (readable large numbers like
`10_000_000` are a plus for downstream byte/size limits). The accept-set is
**documented in both docstrings and pinned by characterization tests** so it is an
explicit, regression-protected contract rather than an implicit consequence of the
stdlib. `int()`/`float()` reject leading/trailing/doubled underscores themselves,
so `"_1"` / `"1__0"` are still invalid.

### Small refactor

Extract `_resolve_key(prefix, name) -> str` (`f"{prefix.rstrip('_')}_{name}"`) and use
it in `env` and the new helpers, so reads and error messages share one
key-construction rule (no drift, no `MYAPP__PORT` double-underscore).

### `from_env` adoption (in this PR)

`ServerConfig.from_env` has exactly one numeric cast left on `main`
(`port=int(port_str)`; #159's two file-exchange casts were parked off `main`). Convert it:

```python
port=env_int(env_prefix, "PORT", 8000, strict=True, minimum=1, maximum=65535),
```

(The separate `port_str = env(env_prefix, "PORT", "8000")` line is removed.)

This is a deliberate **behavior change** — the live scope of #159:

- malformed `PORT` (e.g. `abc`) → `ConfigurationError` naming the var (was bare
  `ValueError` from `_config.py`)
- `PORT` of `0`, `-5`, or `70000` → `ConfigurationError` (were **silently accepted**)
- `PORT` unset → `8000`; `1` and `65535` accepted (inclusive bounds)

`port == 0` is rejected: a server *listen* port of 0 (OS-ephemeral) is a
misconfiguration for an addressable MCP server.

### Exports

`env_float`, `env_int` added to `__init__.py` import and `__all__` in alphabetical
order (between `env` and the `parse_*` block).

### Issue closure note

`Closes #180` **and `Closes #159`**. This **supersedes #159's proposed *private*
`_parse_int_env` / `_parse_float_env`** with a public, dual-mode helper used internally
for `port` — #159's only live deliverable (the `port` cast) is satisfied here, and its
two other casts (file-exchange `token_ttl` / `max_artifact_size`) no longer exist on
`main` (parked). The PR body notes this supersession so #159's text-vs-reality gap is
recorded at close time. #161 stays open (its item 2 is an unrelated test tightening).

## Tests (`tests/test_env.py`)

Test-first, one behavior each. `env_int` and `env_float` mirror this list.

| Test | Assertion |
|---|---|
| unset → default | var absent → returns `default` (both `strict` values), **no warning logged** |
| blank → default | `"   "` → returns `default`, no warning (delegates to `env` strip) |
| valid in-bounds | `"42"` → `42` (`env_float`: `"3.14"` → `3.14`; `"5"` → `5.0`) |
| whitespace around value | `"  42  "` → `42` |
| default omitted → None | unset, no `default` arg → `None` |
| malformed, soft | `"abc"` → returns `default` **and** a `WARNING` is logged naming the key |
| malformed, strict | `"abc"`, `strict=True` → raises `ConfigurationError`; message contains key + `'abc'` |
| `env_int` rejects float text | `"42.5"` → invalid (soft default+warn; strict raises) |
| below minimum, soft | `"0"`, `minimum=1` → `default` + WARNING |
| below minimum, strict | `"0"`, `minimum=1`, `strict=True` → `ConfigurationError` containing `>= 1` |
| above maximum, strict | `"70000"`, `maximum=65535`, `strict=True` → `ConfigurationError` containing `<= 65535` |
| boundary inclusive | `minimum`/`maximum` exactly equal to value → accepted |
| no bounds set | `minimum=maximum=None` → any parseable value accepted |
| trailing-underscore prefix | `env_int("MYAPP_", "PORT")` == `env_int("MYAPP", "PORT")`; error key has no `__` |
| `env_float` non-finite | `"nan"` / `"inf"` / `"-inf"` → invalid (soft default+warn; strict raises `finite`) |

### `from_env` port (config tests)

| Test | Assertion |
|---|---|
| unset → 8000 | no `PORT` env → `from_env(...).port == 8000` |
| valid → parsed | `PORT=9000` → `port == 9000` |
| malformed → ConfigurationError | `PORT=abc` → raises `ConfigurationError` naming `<PREFIX>_PORT` (not bare `ValueError`) |
| out of range → ConfigurationError | `PORT=0`, `PORT=70000`, `PORT=-1` each raise `ConfigurationError` |
| boundary accepted | `PORT=1`, `PORT=65535` → accepted |

Existing `from_env` tests that asserted the old bare-`int()` behavior are updated to the
new strict contract (not deleted).

## Out of scope

- **`_debug.py` DEBUG_PORT convergence** — second internal soft-mode consumer, but
  carries a `port == 0`-as-disable sentinel `env_int` doesn't model and would churn the
  `test_debug.py` hotspot. Tracked as #181.
- **`parse_int` / `parse_float`** (parse-only) — no consumer needs them; add when one does.
- **Clamping bounds** — rejected by design (silent value change).
- **Downstream adoption** (MV's nine stanzas, image-gen's one) — happens per-repo after a
  pvl-core release.
