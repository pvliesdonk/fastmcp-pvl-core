# File-Exchange #141 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the security-critical filesystem-URI resolution layer — an `exchange://`/`file://` parser, env-driven volume-mapping config, and a canonicalize-and-confine primitive that rejects path-traversal and symlink escapes — as one focused private module plus its public re-exports.

**Architecture:** A single new module `src/fastmcp_pvl_core/_file_exchange/_paths.py` holding a parse → volume-resolve → confine pipeline. The confinement primitive uses `Path.resolve()` (resolves all symlinks + `..`) then `is_relative_to`. Failures return `Path | None` (`None` = skip per §9); a confinement escape additionally logs `WARNING` with only the volume id. The lone raise path is `load_volume_map` (`ConfigurationError` on operator misconfig).

**Tech Stack:** Python 3.10+ (CI matrix 3.10–3.13); stdlib `urllib.parse.urlsplit` + `pathlib`; `hypothesis` (NEW dev dependency) for property-based confinement tests; pytest, ruff, mypy.

**Branch:** `feat/141-exchange-uri-confinement` (already created from `main` at `22405e5`; design-doc commit `9fe19fb` is the tip).

**Spec:** `docs/superpowers/specs/2026-05-22-file-exchange-141-paths-design.md`.

---

### Pre-flight context (read once before Task 1)

```bash
cd /mnt/code/fastmcp-pvl-core
git status                       # on feat/141-exchange-uri-confinement, clean
git log --oneline -2              # 9fe19fb (design) on top of 22405e5 (#140 merge)
uv sync --all-extras              # match CI deps
uv run pytest -q                  # baseline green (658 passed, 1 skipped)
```

Facts verified during planning (rely on them):

- `urllib.parse.urlsplit("exchange://docs/a/b.bin")` → `scheme="exchange"`, `netloc="docs"`, `path="/a/b.bin"`. `urlsplit("file:///mnt/x")` → `scheme="file"`, `netloc=""`, `path="/mnt/x"`. `urlsplit` does NOT normalize `..` in the path (we want the raw path so confinement sees the `..`).
- `pathlib`'s `Path("/mnt/docs") / "/a/b.bin"` == `Path("/a/b.bin")` — an absolute right operand discards the left. The exchange-path leading-`/` strip is therefore security-load-bearing.
- `Path.resolve()` (strict=False default) resolves every symlink in the existing prefix and normalizes `..`; for a non-existent tail it appends lexically. `Path.is_relative_to` (3.9+) is `True` for the root itself and any descendant.
- `ConfigurationError` is defined in `src/fastmcp_pvl_core/_errors.py` and exported from the top-level package.
- `_FS_URI_PATTERN = r"^(exchange://[^/]+/.+|file:///[^/].*)$"` lives in `src/fastmcp_pvl_core/_file_exchange/_wire.py`.
- `hypothesis` is NOT currently a dependency (Task 1 adds it).

Files not modified but depended on: `_wire.py` (`_FS_URI_PATTERN`), `_env.py` (`env`), `_errors.py` (`ConfigurationError`).

---

### Task 1: `canonicalize_and_confine` (security primitive) + `hypothesis` dep

**Why first:** the security core. Everything else composes it. Do it thoroughly with property-based + concrete escape-vector tests.

**Files:**
- Modify: `pyproject.toml` (add `hypothesis` to `[dependency-groups].dev`)
- Create: `src/fastmcp_pvl_core/_file_exchange/_paths.py`
- Create: `tests/_file_exchange/test_paths.py`

- [ ] **Step 1: Add `hypothesis` to the dev group + sync**

In `pyproject.toml`, under `[dependency-groups]` `dev = [...]`, add `"hypothesis>=6"` (alphabetical-ish; placement within the list is not significant). Then:

```bash
cd /mnt/code/fastmcp-pvl-core
uv sync --all-extras
uv run python -c "import hypothesis; print(hypothesis.__version__)"
```
Expected: a version ≥ 6 prints.

- [ ] **Step 2: Write the failing confinement tests**

Create `tests/_file_exchange/test_paths.py`:

```python
"""Tests for filesystem URI parsing, volume config, and path confinement."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fastmcp_pvl_core._file_exchange._paths import canonicalize_and_confine

# --- canonicalize_and_confine: confined cases ---


def test_confine_plain_file_inside(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    f = root / "a.bin"
    f.write_text("x")
    assert canonicalize_and_confine(f, root) == f.resolve()


def test_confine_internal_dotdot_stays_inside(tmp_path):
    root = tmp_path / "vol"
    (root / "sub").mkdir(parents=True)
    (root / "a.bin").write_text("x")
    target = root / "sub" / ".." / "a.bin"
    assert canonicalize_and_confine(target, root) == (root / "a.bin").resolve()


def test_confine_root_itself(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    assert canonicalize_and_confine(root, root) == root.resolve()


def test_confine_nonexistent_tail_inside(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    target = root / "new" / "sink.bin"  # does not exist yet
    assert canonicalize_and_confine(target, root) == target.resolve()


def test_confine_allows_internal_symlink(tmp_path):
    root = tmp_path / "vol"
    (root / "real").mkdir(parents=True)
    (root / "real" / "f.bin").write_text("x")
    (root / "link").symlink_to(root / "real")
    assert (
        canonicalize_and_confine(root / "link" / "f.bin", root)
        == (root / "real" / "f.bin").resolve()
    )


# --- canonicalize_and_confine: escape cases (all must return None) ---


def test_confine_rejects_dotdot_escape(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    assert canonicalize_and_confine(root / ".." / "outside", root) is None


def test_confine_rejects_absolute_escape(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    assert canonicalize_and_confine(outside / "secret", root) is None


def test_confine_rejects_symlink_escape(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (root / "link").symlink_to(outside)
    assert canonicalize_and_confine(root / "link" / "secret", root) is None


def test_confine_rejects_symlink_in_intermediate_component(tmp_path):
    root = tmp_path / "vol"
    (root / "real").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "real" / "esc").symlink_to(outside)
    assert canonicalize_and_confine(root / "real" / "esc" / "x", root) is None


def test_confine_rejects_dotdot_plus_symlink_combo(tmp_path):
    root = tmp_path / "vol"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (root / "sub" / "link").symlink_to(outside)
    # through the symlink, then a sibling — still outside the root
    target = root / "sub" / "link" / ".." / "outside" / "secret"
    assert canonicalize_and_confine(target, root) is None


# --- property: any accepted result is genuinely within the root ---


@pytest.fixture(scope="module")
def _confine_root(tmp_path_factory):
    # Module-scoped so hypothesis (which re-runs the body per example) does
    # not trip the function-scoped-fixture health check.
    return tmp_path_factory.mktemp("confine_root")


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    segments=st.lists(
        st.sampled_from(["..", ".", "a", "b", "sub", "x.bin", "...", "vol"]),
        max_size=10,
    )
)
def test_confine_invariant_accepted_paths_are_within_root(_confine_root, segments):
    root = _confine_root
    candidate = root.joinpath(*segments) if segments else root
    result = canonicalize_and_confine(candidate, root)
    if result is not None:
        assert result.is_relative_to(root.resolve())
```

- [ ] **Step 3: Run tests to verify they fail with ImportError**

```bash
uv run pytest tests/_file_exchange/test_paths.py -q
```
Expected: `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._paths'`

- [ ] **Step 4: Implement `_paths.py` (confinement primitive only)**

Create `src/fastmcp_pvl_core/_file_exchange/_paths.py`:

```python
"""Filesystem-descriptor URI resolution and path confinement.

Security-critical. Turns an untrusted ``filesystem`` descriptor ``uri``
(§7.2.1, §7.2.3) into a confined local path:

- :func:`canonicalize_and_confine` — the confinement primitive
  (resolves symlinks + ``..``, rejects escapes per §10.1.3 / §15).
- :func:`resolve_filesystem_uri` — parse ``exchange://`` / ``file://``,
  look up the volume, confine.
- :func:`load_volume_map` — env-driven volume-to-mount-point config.

All non-usable outcomes return ``None`` (the §9 "skip this descriptor"
signal); a confinement escape additionally logs a ``WARNING`` carrying
only the volume id, never the attacker-controlled raw path/URI.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def canonicalize_and_confine(
    candidate: Path | str, root: Path | str
) -> Path | None:
    """Resolve symlinks + ``..`` and confirm ``candidate`` is within ``root``.

    Returns the fully-resolved candidate path iff it is ``root`` itself
    or a descendant; ``None`` on any escape (the reject signal — §10.1.3
    / §15 "MUST reject escapes, including via symlinks").

    ``Path.resolve()`` resolves every symlink in the path's existing
    prefix and normalises ``.``/``..``; a not-yet-existing tail is
    appended lexically (so a sink target need not exist). Existence /
    readability / writability is a separate concern (the caller's
    ``os.access`` check), not confinement.
    """
    resolved_root = Path(root).resolve()
    resolved_candidate = Path(candidate).resolve()
    if resolved_candidate.is_relative_to(resolved_root):
        return resolved_candidate
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/_file_exchange/test_paths.py -q
```
Expected: all confinement tests pass (10 concrete + 1 property).

- [ ] **Step 6: Format / lint / type-check**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
```
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
cd /mnt/code/fastmcp-pvl-core
git add pyproject.toml uv.lock \
        src/fastmcp_pvl_core/_file_exchange/_paths.py \
        tests/_file_exchange/test_paths.py
git commit -m "$(cat <<'EOF'
feat(_file_exchange): canonicalize_and_confine path-confinement primitive

The security core of #141: resolves symlinks + ``..`` via
``Path.resolve()`` then confirms containment with ``is_relative_to``.
Returns the resolved path when confined, ``None`` on any escape
(``..`` traversal, absolute, symlink, symlink-in-intermediate,
``..``+symlink combinations). Confinement does not require the path to
exist (sink targets may be new).

Adds ``hypothesis`` to the dev group for the confinement invariant
property test (no accepted result ever lies outside the root), alongside
concrete tests for every named escape vector and the confined cases
(plain file, internal ``..``, internal symlink, non-existent tail).

Refs: #141.
EOF
)"
```

---

### Task 2: `_parse_fs_uri` (internal URI parser) + drift guard

**Why:** decodes a URI into `(scheme, volume, path)`; the resolver builds on it. Independent of Task 1.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_paths.py`
- Modify: `tests/_file_exchange/test_paths.py`

- [ ] **Step 1: Add the failing parser tests**

Append to `tests/_file_exchange/test_paths.py`:

```python
# --- _parse_fs_uri ---

from fastmcp_pvl_core._file_exchange._paths import _parse_fs_uri  # noqa: E402
from fastmcp_pvl_core._file_exchange._wire import _FS_URI_PATTERN  # noqa: E402
import re  # noqa: E402


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("exchange://docs/a/b.bin", ("exchange", "docs", "a/b.bin")),
        ("exchange://v/single", ("exchange", "v", "single")),
        ("exchange://docs/a/../etc", ("exchange", "docs", "a/../etc")),
        ("file:///mnt/x", ("file", "", "/mnt/x")),
        ("file:///mnt/../etc", ("file", "", "/mnt/../etc")),
    ],
)
def test_parse_fs_uri_valid(uri, expected):
    assert _parse_fs_uri(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "exchange:///x",          # empty volume
        "exchange://docs",        # no path
        "exchange://docs/",       # empty path
        "exchange://docs/a?q=1",  # query
        "exchange://docs/a#f",    # fragment
        "file://host/x",          # non-empty authority
        "file://x",               # not absolute (authority 'x', empty path)
        "https://example/x",      # unknown scheme
        "not-a-uri",              # no scheme
        "",                       # empty
    ],
)
def test_parse_fs_uri_rejects(uri):
    assert _parse_fs_uri(uri) is None


def test_parse_agrees_with_wire_pattern_on_valid():
    """Every URI _parse_fs_uri accepts must also match the wire pattern."""
    for uri in (
        "exchange://docs/a/b.bin",
        "exchange://v/single",
        "file:///mnt/x",
    ):
        assert re.match(_FS_URI_PATTERN, uri), uri
        assert _parse_fs_uri(uri) is not None, uri


def test_parse_agrees_with_wire_pattern_on_invalid():
    """URIs the wire pattern rejects must also fail to parse."""
    for uri in ("exchange:///x", "exchange://docs/", "file://host/x"):
        assert not re.match(_FS_URI_PATTERN, uri), uri
        assert _parse_fs_uri(uri) is None, uri
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/_file_exchange/test_paths.py -k parse -q
```
Expected: `ImportError: cannot import name '_parse_fs_uri'`

- [ ] **Step 3: Implement `_parse_fs_uri`**

Add to `src/fastmcp_pvl_core/_file_exchange/_paths.py` (imports at top: extend with `from typing import Literal` and `from urllib.parse import urlsplit`):

```python
def _parse_fs_uri(
    uri: str,
) -> tuple[Literal["exchange", "file"], str, str] | None:
    """Decode a filesystem-descriptor URI into ``(scheme, volume, path)``.

    - ``exchange://<volume>/<path>`` → ``("exchange", volume, path)``
      with the path's leading ``/`` stripped (so it joins under a mount;
      ``pathlib`` would discard the mount if the right operand were
      absolute). Volume and a non-empty path are required.
    - ``file:///<abs>`` → ``("file", "", abs_path)``; the authority MUST
      be empty (§10.1.2) and the path absolute.
    - Anything else (query/fragment/userinfo/port present, unknown
      scheme, malformed) → ``None``.

    The wire layer (``_FS_URI_PATTERN``) already validates shape at model
    construction; this re-derives validity structurally for direct
    callers and to honour ``file://``'s empty-authority rule. A
    drift-guard test keeps the two in agreement.
    """
    parts = urlsplit(uri)
    if parts.query or parts.fragment:
        return None
    if parts.scheme == "exchange":
        volume = parts.netloc
        path = parts.path.lstrip("/")
        if not volume or not path:
            return None
        return ("exchange", volume, path)
    if parts.scheme == "file":
        if parts.netloc:
            return None
        path = parts.path
        if not path.startswith("/"):
            return None
        return ("file", "", path)
    return None
```

- [ ] **Step 4: Run to verify pass**

```bash
uv run pytest tests/_file_exchange/test_paths.py -k parse -q
```
Expected: all parse tests pass.

- [ ] **Step 5: Format / lint / type-check**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
```
Expected: all clean. (If ruff flags the mid-file imports in the test, move them to the top of `test_paths.py` with the other imports — `_parse_fs_uri`, `_FS_URI_PATTERN`, and `re`.)

- [ ] **Step 6: Commit**

```bash
cd /mnt/code/fastmcp-pvl-core
git add src/fastmcp_pvl_core/_file_exchange/_paths.py \
        tests/_file_exchange/test_paths.py
git commit -m "$(cat <<'EOF'
feat(_file_exchange): _parse_fs_uri for exchange:// and file:// schemes

Internal parser decoding a filesystem-descriptor URI into
``(scheme, volume, path)`` via ``urlsplit``. Strips the exchange path's
leading ``/`` (security-load-bearing — ``pathlib`` discards the mount on
an absolute right operand). Rejects empty volume/path, non-empty
``file://`` authority, non-absolute ``file://`` path, query/fragment,
and unknown schemes. Drift-guard tests assert agreement with
``_FS_URI_PATTERN``.

Refs: #141.
EOF
)"
```

---

### Task 3: `_parse_volume_map` + `load_volume_map` (config)

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_paths.py`
- Modify: `tests/_file_exchange/test_paths.py`

- [ ] **Step 1: Add the failing config tests**

Append to `tests/_file_exchange/test_paths.py`:

```python
# --- volume-map config ---

from pathlib import Path  # noqa: E402  (move to top with others)

from fastmcp_pvl_core import ConfigurationError  # noqa: E402
from fastmcp_pvl_core._file_exchange._paths import (  # noqa: E402
    _parse_volume_map,
    load_volume_map,
)


def test_parse_volume_map_basic():
    result = _parse_volume_map("docs=/mnt/docs,scratch=/mnt/scratch")
    assert result == {
        "docs": Path("/mnt/docs"),
        "scratch": Path("/mnt/scratch"),
    }


def test_parse_volume_map_trims_and_drops_empty():
    result = _parse_volume_map(" docs = /mnt/docs , , scratch=/mnt/s ")
    assert result == {"docs": Path("/mnt/docs"), "scratch": Path("/mnt/s")}


def test_parse_volume_map_splits_on_first_equals():
    # A path containing '=' (contrived) keeps everything after the first '='.
    result = _parse_volume_map("v=/mnt/a=b")
    assert result == {"v": Path("/mnt/a=b")}


@pytest.mark.parametrize(
    "raw",
    [
        "docs",              # no '='
        "=/mnt/docs",        # empty name
        "docs=",             # empty path
        "docs=relative/x",   # non-absolute path
        "docs=/a,docs=/b",   # duplicate name
    ],
)
def test_parse_volume_map_rejects(raw):
    with pytest.raises(ConfigurationError):
        _parse_volume_map(raw)


def test_load_volume_map_unset_returns_empty(monkeypatch):
    monkeypatch.delenv("FILE_EXCHANGE_VOLUMES", raising=False)
    assert load_volume_map() == {}


def test_load_volume_map_reads_env(monkeypatch):
    monkeypatch.setenv("FILE_EXCHANGE_VOLUMES", "docs=/mnt/docs")
    assert load_volume_map() == {"docs": Path("/mnt/docs")}
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/_file_exchange/test_paths.py -k volume_map -q
```
Expected: `ImportError: cannot import name '_parse_volume_map'`

- [ ] **Step 3: Implement the config functions**

Add to `_paths.py` (extend the top imports with `from collections.abc import Mapping` and `from fastmcp_pvl_core._env import env` and `from fastmcp_pvl_core._errors import ConfigurationError`). Also add the type alias near the top:

```python
VolumeMap = Mapping[str, Path]
"""A mapping from volume identifier to local mount-point path."""


def _parse_volume_map(raw: str) -> dict[str, Path]:
    """Parse ``name=path`` comma-separated pairs into a volume map.

    Raises:
        ConfigurationError: an entry has no ``=``, an empty name, an
            empty path, a non-absolute path, or a duplicate volume name
            — operator misconfiguration that must fail loudly at startup
            rather than silently make ``exchange://`` URIs unresolvable.
    """
    out: dict[str, Path] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, path = entry.partition("=")
        name = name.strip()
        path = path.strip()
        if not sep or not name or not path:
            raise ConfigurationError(
                f"FILE_EXCHANGE_VOLUMES entry must be 'name=path': {entry!r}"
            )
        if not path.startswith("/"):
            raise ConfigurationError(
                f"FILE_EXCHANGE_VOLUMES mount path must be absolute: {path!r}"
            )
        if name in out:
            raise ConfigurationError(
                f"FILE_EXCHANGE_VOLUMES duplicate volume name: {name!r}"
            )
        out[name] = Path(path)
    return out


def load_volume_map(prefix: str = "FILE_EXCHANGE") -> dict[str, Path]:
    """Load the volume map from ``{prefix}_VOLUMES`` in the environment.

    Returns an empty map when the variable is unset/blank — a party with
    no volume mappings resolves no filesystem URIs and skips every
    filesystem descriptor during §9 selection.

    ``prefix`` defaults to the canonical ``FILE_EXCHANGE`` (pvl-core owns
    the env contract); it is overridable only to namespace a multi-server
    deployment, not to change the contract's shape.
    """
    raw = env(prefix, "VOLUMES")
    return _parse_volume_map(raw) if raw else {}
```

- [ ] **Step 4: Run to verify pass**

```bash
uv run pytest tests/_file_exchange/test_paths.py -k volume_map -q
```
Expected: all config tests pass.

- [ ] **Step 5: Format / lint / type-check**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
```
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
cd /mnt/code/fastmcp-pvl-core
git add src/fastmcp_pvl_core/_file_exchange/_paths.py \
        tests/_file_exchange/test_paths.py
git commit -m "$(cat <<'EOF'
feat(_file_exchange): volume-map config (FILE_EXCHANGE_VOLUMES)

``_parse_volume_map`` parses comma-separated ``name=path`` pairs
(first-``=`` split, whitespace trimmed, empty entries dropped) and
raises ``ConfigurationError`` on a malformed entry, empty name/path,
non-absolute mount path, or duplicate volume name — loud startup failure
over silent unresolvable URIs. ``load_volume_map`` reads
``FILE_EXCHANGE_VOLUMES`` via the repo ``env`` helper, returning ``{}``
when unset. Adds the ``VolumeMap`` alias.

Refs: #141.
EOF
)"
```

---

### Task 4: `resolve_filesystem_uri` (pipeline + logging)

**Why:** ties parser + volume lookup + confinement; the public entry point #143 calls. Depends on Tasks 1–3.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_paths.py`
- Modify: `tests/_file_exchange/test_paths.py`

- [ ] **Step 1: Add the failing resolver tests**

Append to `tests/_file_exchange/test_paths.py`:

```python
# --- resolve_filesystem_uri ---

import logging  # noqa: E402  (move to top)

from fastmcp_pvl_core._file_exchange._paths import (  # noqa: E402
    resolve_filesystem_uri,
)


def test_resolve_exchange_confined(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.bin").write_text("x")
    vm = {"docs": root}
    assert resolve_filesystem_uri("exchange://docs/a.bin", volume_map=vm) == (
        root / "a.bin"
    ).resolve()


def test_resolve_exchange_unknown_volume_returns_none(tmp_path):
    vm = {"docs": tmp_path / "docs"}
    assert resolve_filesystem_uri("exchange://other/a.bin", volume_map=vm) is None


def test_resolve_exchange_escape_returns_none_and_warns(tmp_path, caplog):
    root = tmp_path / "docs"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    vm = {"docs": root}
    with caplog.at_level(logging.WARNING):
        result = resolve_filesystem_uri(
            "exchange://docs/../outside", volume_map=vm
        )
    assert result is None
    assert any("escaped its volume root" in r.message for r in caplog.records)
    # The raw path is never logged — only the volume id.
    assert not any("outside" in r.getMessage() for r in caplog.records)


def test_resolve_exchange_unknown_volume_does_not_warn(tmp_path, caplog):
    vm = {"docs": tmp_path / "docs"}
    with caplog.at_level(logging.WARNING):
        resolve_filesystem_uri("exchange://other/a.bin", volume_map=vm)
    assert caplog.records == []


def test_resolve_file_within_a_volume(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.bin").write_text("x")
    vm = {"docs": root}
    uri = f"file://{(root / 'a.bin')}"  # file:///<abs>
    assert resolve_filesystem_uri(uri, volume_map=vm) == (root / "a.bin").resolve()


def test_resolve_file_outside_all_volumes_returns_none_and_warns(tmp_path, caplog):
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    vm = {"docs": root}
    with caplog.at_level(logging.WARNING):
        result = resolve_filesystem_uri(
            f"file://{(outside / 'secret')}", volume_map=vm
        )
    assert result is None
    assert any("not within any configured volume" in r.message for r in caplog.records)


def test_resolve_malformed_uri_returns_none(tmp_path):
    vm = {"docs": tmp_path / "docs"}
    assert resolve_filesystem_uri("https://example/x", volume_map=vm) is None
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/_file_exchange/test_paths.py -k resolve -q
```
Expected: `ImportError: cannot import name 'resolve_filesystem_uri'`

- [ ] **Step 3: Implement `resolve_filesystem_uri`**

Add to `_paths.py`:

```python
def resolve_filesystem_uri(uri: str, *, volume_map: VolumeMap) -> Path | None:
    """Resolve a filesystem-descriptor URI to a confined local path.

    Returns the confined path, or ``None`` when the URI is malformed,
    names an unmapped volume, or escapes confinement. An escape logs a
    ``WARNING`` (with only the volume id — never the raw path/URI);
    benign no-mapping does not log.

    Args:
        uri: The descriptor ``uri`` (``exchange://`` or ``file://``).
        volume_map: Volume id → mount point. The mount points are also
            the universe of exchange directories a ``file://`` path may
            lie within.
    """
    parsed = _parse_fs_uri(uri)
    if parsed is None:
        return None
    scheme, volume, path = parsed

    if scheme == "exchange":
        root = volume_map.get(volume)
        if root is None:
            return None
        confined = canonicalize_and_confine(root / path, root)
        if confined is None:
            logger.warning(
                "file-exchange: exchange:// path escaped its volume root; "
                "rejecting (volume=%r)",
                volume,
            )
        return confined

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

- [ ] **Step 4: Run to verify pass**

```bash
uv run pytest tests/_file_exchange/test_paths.py -k resolve -q
```
Expected: all resolver tests pass.

- [ ] **Step 5: Full file + format / lint / type-check**

```bash
uv run pytest tests/_file_exchange/test_paths.py -q
uv run ruff format .
uv run ruff check .
uv run mypy src
```
Expected: every test in the file passes; all checks clean. (Consolidate the test file's imports at the top now if ruff's `E402` is flagging the appended mid-file imports.)

- [ ] **Step 6: Commit**

```bash
cd /mnt/code/fastmcp-pvl-core
git add src/fastmcp_pvl_core/_file_exchange/_paths.py \
        tests/_file_exchange/test_paths.py
git commit -m "$(cat <<'EOF'
feat(_file_exchange): resolve_filesystem_uri pipeline

Ties _parse_fs_uri + volume lookup + canonicalize_and_confine into the
public entry point. exchange:// confines to its resolved volume root;
file:// confines to any configured volume mount point (the single-config
decision — volume mounts are the exchange-directory universe). Returns
None for malformed/unmapped/escaped; an escape logs WARNING carrying
only the volume id (never the attacker-controlled raw path/URI, per the
URL-redaction discipline). caplog tests pin both the warn-on-escape and
no-warn-on-benign-no-mapping behaviors.

Refs: #141.
EOF
)"
```

---

### Task 5: Public namespace re-exports

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Modify: `tests/test_file_exchange_namespace.py`

- [ ] **Step 1: Add the failing namespace test**

Append to `tests/test_file_exchange_namespace.py`:

```python
def test_path_helpers_exposed():
    from fastmcp_pvl_core import file_exchange

    assert callable(file_exchange.canonicalize_and_confine)
    assert callable(file_exchange.resolve_filesystem_uri)
    assert callable(file_exchange.load_volume_map)
    assert hasattr(file_exchange, "VolumeMap")
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_file_exchange_namespace.py::test_path_helpers_exposed -q
```
Expected: AttributeError on `canonicalize_and_confine`.

- [ ] **Step 3: Add imports + `__all__` entries in the subpackage `__init__.py`**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, add the import block (alphabetical among the `from ._...` blocks — `_paths` sorts after `_errors`/`_codes` and before `_selection`):

```python
from fastmcp_pvl_core._file_exchange._paths import (
    VolumeMap,
    canonicalize_and_confine,
    load_volume_map,
    resolve_filesystem_uri,
)
```

Add these four names to `__all__` (keep it sorted; capitals before lowercase): `"VolumeMap"` goes after `"UploadSink"`/`"VERSION_PATTERN"` per existing order — insert it so the list stays in the file's established sort. `"canonicalize_and_confine"`, `"load_volume_map"`, `"resolve_filesystem_uri"` go in the lowercase section. After editing, verify with:

```bash
uv run python -c "import fastmcp_pvl_core._file_exchange as m; assert sorted(m.__all__)==list(m.__all__); print('sorted ok', len(m.__all__))"
```
Expected: `sorted ok <N>`. If it prints a mismatch, reorder `__all__` until `sorted(__all__) == __all__`.

- [ ] **Step 4: Mirror in the public `file_exchange.py`**

In `src/fastmcp_pvl_core/file_exchange.py`, add the same four names to the single `from fastmcp_pvl_core._file_exchange import (...)` block and to its `__all__`, keeping both sorted. Verify:

```bash
uv run python -c "import fastmcp_pvl_core.file_exchange as m; assert sorted(m.__all__)==list(m.__all__); print('sorted ok', len(m.__all__))"
```
Expected: `sorted ok <N>` (same N as the subpackage).

- [ ] **Step 5: Run the namespace tests + full suite**

```bash
uv run pytest tests/test_file_exchange_namespace.py -q
uv run pytest -q
```
Expected: namespace tests pass; full suite green (baseline 658 + new `test_paths.py` tests + 1 namespace test; 1 skipped for the GITHUB_TOKEN-gated sync test).

- [ ] **Step 6: Format / lint / type-check**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
```
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
cd /mnt/code/fastmcp-pvl-core
git add src/fastmcp_pvl_core/_file_exchange/__init__.py \
        src/fastmcp_pvl_core/file_exchange.py \
        tests/test_file_exchange_namespace.py
git commit -m "$(cat <<'EOF'
feat(file_exchange): expose path helpers in public namespace

Re-exports ``canonicalize_and_confine``, ``resolve_filesystem_uri``,
``load_volume_map``, and the ``VolumeMap`` alias from
``fastmcp_pvl_core.file_exchange`` (and the private subpackage
``__all__``), mirroring the explicit-re-export pattern from #139/#140.

Refs: #141 (closes via the wrapping PR).
EOF
)"
```

---

### Final pre-push sweep (immediately before opening the PR)

- [ ] **Step 1: Confirm the commit range**

```bash
git fetch origin main
git log --oneline origin/main..HEAD
git status
```

Expected commits in order: `9fe19fb` (design doc), then Task 1–5 commits. Clean tree.

- [ ] **Step 2: Final local checks**

```bash
uv sync --all-extras
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```
All clean. CI runs the same on 3.10–3.13.

- [ ] **Step 3: Invoke `preflight-circus`**

Mandatory per the user-global CLAUDE.md PR workflow. Expect the security-relevant lenses to engage: **lens 6 is NOT applicable** (no normative-standard file is authored — `_paths.py` is implementation, the design doc is informative), but `pr-review-toolkit:silent-failure-hunter` (the `None`-return + `WARNING` paths), `type-design-analyzer` (the `VolumeMap` alias + return types), `pr-test-analyzer` (the property + escape-vector tests), and `code-reviewer` all apply. **When dispatching the lens subagents, forbid stateful git ops in every prompt** (`checkout`/`switch`/`reset`/`stash`/`restore`/`commit`) — they share this checkout. Score findings with Haiku; do not push until clean at ≥80. If a finding survives, fix it and re-run the FULL circus.

- [ ] **Step 4: Open the PR as draft, then flip to ready once bots LGTM**

```bash
git push -u origin feat/141-exchange-uri-confinement
gh pr create --draft --base main \
  --title "feat: file-exchange exchange:// URI + path confinement (closes #141)" \
  --body "$(cat <<'EOF'
## Summary

- `canonicalize_and_confine(candidate, root)` — security primitive resolving symlinks + `..` and rejecting escapes (`..`, absolute, symlink, intermediate-symlink, combinations).
- `resolve_filesystem_uri(uri, *, volume_map)` — parse `exchange://`/`file://` → volume lookup → confine. `None` on malformed/unmapped/escaped; escape logs `WARNING` with only the volume id.
- `load_volume_map()` — env-driven (`FILE_EXCHANGE_VOLUMES`, comma `name=path`); `ConfigurationError` on misconfig.
- Single config: volume mounts are the exchange-directory universe; `file://` confines to any one of them.

Design: `docs/superpowers/specs/2026-05-22-file-exchange-141-paths-design.md`.

## Test plan

- [x] `uv run pytest` — green on 3.10 locally (CI verifies 3.10–3.13), incl. property-based confinement invariant (`hypothesis`) + concrete escape vectors.
- [x] `ruff format --check`, `ruff check`, `mypy src` — clean.
- [x] `preflight-circus` — clean at ≥80.

Closes #141.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Watch `claude-review`, read its body (not just the green check), address within the one-round cap, then `gh pr ready`.

---

## Self-review against the spec

**1. Spec coverage:**

| Spec section | Task |
|---|---|
| Module layout (`_paths.py`, public surface) | Tasks 1–5 |
| Failure signaling (`Path | None`, WARN on escape, `ConfigurationError` lone raise) | Tasks 1, 3, 4 |
| URI parser (`_parse_fs_uri`) + drift guard + leading-`/` strip | Task 2 |
| Volume-map config (env format, parser, loader) | Task 3 |
| `canonicalize_and_confine` (resolve + is_relative_to) | Task 1 |
| `resolve_filesystem_uri` (pipeline, single-config, logging discipline) | Task 4 |
| Tests (parser, config, confinement property + concrete vectors) | Tasks 1–4 |
| `hypothesis` dev dep | Task 1 |
| Public namespace re-exports | Task 5 |

Every spec requirement maps to a task. No gaps.

**2. Placeholder scan:** No "TBD"/"TODO"/"add error handling"/"similar to Task N". Every step has concrete code, exact paths, and expected output.

**3. Type consistency:** `canonicalize_and_confine(candidate, root) -> Path | None`, `_parse_fs_uri(uri) -> tuple[Literal["exchange","file"], str, str] | None`, `_parse_volume_map(raw) -> dict[str, Path]`, `load_volume_map(prefix="FILE_EXCHANGE") -> dict[str, Path]`, `resolve_filesystem_uri(uri, *, volume_map: VolumeMap) -> Path | None`, `VolumeMap = Mapping[str, Path]` — all consistent across the tasks that define and consume them. `ConfigurationError` and `_FS_URI_PATTERN` are existing symbols (verified). The `_paths.py` import block accumulates across tasks: `logging`, `pathlib.Path`, `typing.Literal`, `urllib.parse.urlsplit`, `collections.abc.Mapping`, `fastmcp_pvl_core._env.env`, `fastmcp_pvl_core._errors.ConfigurationError` — an implementer adding them per-task should consolidate at the top (ruff `I001` will enforce ordering).
