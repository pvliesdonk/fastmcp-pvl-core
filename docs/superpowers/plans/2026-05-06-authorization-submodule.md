# Authorization Submodule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in authorization layer to `fastmcp-pvl-core` that gives downstream MCP servers a reusable `(subject, required_scope) → allow/deny` primitive — middleware + annotation convention + read-only ACL TOML loader — closing issue #37.

**Architecture:** A single new private module `src/fastmcp_pvl_core/_authorization.py` exposing six public symbols re-exported from the package root. Per-component hooks (`on_call_tool`, `on_read_resource`, `on_get_prompt`) for enforcement; matching `on_list_*` hooks for list filtering. `check_authorization` reads an ambient `Authorizer` from a package-internal `ContextVar` set by the middleware at install time, mirroring the `_current_auth_mode` pattern in `_subject.py`. Tools opt in via `meta={"required_scope": "<scope>"}`; absence of the key means unrestricted.

**Tech Stack:** Python 3.10+, fastmcp ≥3.2.4, `tomllib` (with `tomli` backport for 3.10), `contextvars.ContextVar`, pytest+pytest-asyncio (asyncio_mode=auto), mypy --strict, ruff (Google docstring convention).

**Spec reference:** `docs/specs/authorization-submodule.md` (committed in this branch's first commit).

---

## File structure

| File | Action | Purpose |
|---|---|---|
| `src/fastmcp_pvl_core/_authorization.py` | Create | All authz logic in one module — ~250 lines: `AuthzDenied`, `Authorizer` alias, `_current_authorizer` ContextVar + `set_current_authorizer`, `load_acl`, `make_acl_authorizer`, `check_authorization`, `AuthorizationMiddleware`. |
| `src/fastmcp_pvl_core/__init__.py` | Modify | Add six public symbols to imports + `__all__`. |
| `tests/conftest.py` | Modify | Add `_reset_authorizer` autouse fixture mirroring `_reset_auth_mode`. |
| `tests/test_authorization_loader.py` | Create | `load_acl` happy path + every validation error. |
| `tests/test_authorization_authorizer.py` | Create | `make_acl_authorizer` semantics. |
| `tests/test_authorization_check.py` | Create | `check_authorization` + `AuthzDenied`. |
| `tests/test_authorization_middleware.py` | Create | All middleware hook behaviors. |
| `README.md` | Modify | New "Authorization" section after "Identifying the caller — `get_subject`". |
| `docs/specs/auth-subject-authz.md` | Modify | Update broken "See also" pointer that the deleted-then-redrafted authorization-submodule.md created. |

Existing `_authorization.py` precedents to follow:
- **TOML loading + `Path.expanduser()` single-expansion-site pattern**: `src/fastmcp_pvl_core/_auth.py:_load_bearer_tokens` (lines 132-190).
- **`ContextVar` plumbing + `set_current_*` helper**: `src/fastmcp_pvl_core/_subject.py` (lines 43-57).
- **Tests that patch ambient context rather than spinning up fastmcp**: `tests/test_subject.py` (patches `fastmcp.server.dependencies.get_access_token`).
- **`ConfigurationError` for fail-fast loader errors**: `src/fastmcp_pvl_core/_errors.py` + usage throughout `_auth.py`.

---

## Task 1: Module skeleton — `AuthzDenied`, `Authorizer`, ContextVar

**Files:**
- Create: `src/fastmcp_pvl_core/_authorization.py`
- Test: `tests/test_authorization_check.py` (just for `AuthzDenied` here; full check_authorization tests come in Task 5)

This task creates the module file with the type alias, exception, and ambient-context plumbing. No middleware, no loader, no helper yet — just the foundations everything else builds on.

- [ ] **Step 1: Write the failing test for `AuthzDenied`**

Create `tests/test_authorization_check.py`:

```python
"""Tests for AuthzDenied and check_authorization (added in later tasks)."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core._authorization import AuthzDenied


def test_authz_denied_carries_subject_and_required_scope() -> None:
    exc = AuthzDenied(subject="user:alice@example.com", required_scope="write")
    assert exc.subject == "user:alice@example.com"
    assert exc.required_scope == "write"


def test_authz_denied_subject_can_be_none() -> None:
    exc = AuthzDenied(subject=None, required_scope="read")
    assert exc.subject is None
    assert exc.required_scope == "read"


def test_authz_denied_is_an_exception() -> None:
    with pytest.raises(AuthzDenied):
        raise AuthzDenied(subject="x", required_scope="y")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_authorization_check.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fastmcp_pvl_core._authorization'`.

- [ ] **Step 3: Write the module skeleton**

Create `src/fastmcp_pvl_core/_authorization.py`:

```python
"""Authorization primitives: middleware, annotation convention, ACL loader.

Downstream MCP servers that need to enforce per-subject access control
on their tools, resources, or prompts can opt in by:

1. Annotating components with ``meta={"required_scope": "<scope>"}``.
2. Building an :data:`Authorizer` (typically via :func:`load_acl` +
   :func:`make_acl_authorizer`).
3. Installing :class:`AuthorizationMiddleware` after
   :func:`fastmcp_pvl_core.wire_middleware_stack`.

See ``docs/specs/authorization-submodule.md`` for the design rationale.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import TypeAlias

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

Authorizer: TypeAlias = Callable[[str | None, str], bool]
"""Decision callable: ``(subject, required_scope) -> bool``.

Returns ``True`` to allow, ``False`` to deny.  ``None`` subject means
"no caller identity available" — typical authorizers deny that case.

This is a :class:`TypeAlias`, not a ``Protocol``.  The Protocol upgrade
across this and other Callable seams in the package is tracked in
issue #60.
"""


# ---------------------------------------------------------------------------
# AuthzDenied exception
# ---------------------------------------------------------------------------


class AuthzDenied(Exception):
    """Raised by :func:`check_authorization` when the authorizer denies.

    The :class:`AuthorizationMiddleware` catches this around
    ``call_next`` and re-raises as the per-operation MCP error
    (:class:`fastmcp.exceptions.ToolError` for a tool body,
    :class:`~fastmcp.exceptions.ResourceError` for a resource handler,
    :class:`~fastmcp.exceptions.PromptError` for a prompt handler).

    If the middleware is *not* installed, this propagates as a plain
    :class:`Exception` and surfaces as a generic MCP error.
    """

    subject: str | None
    required_scope: str

    def __init__(self, *, subject: str | None, required_scope: str) -> None:
        super().__init__(
            f"authorization denied: subject={subject!r} "
            f"required_scope={required_scope!r}"
        )
        self.subject = subject
        self.required_scope = required_scope


# ---------------------------------------------------------------------------
# Ambient authorizer (ContextVar plumbing)
# ---------------------------------------------------------------------------


_current_authorizer: ContextVar[Authorizer | None] = ContextVar(
    "fastmcp_pvl_core_current_authorizer",
    default=None,
)
"""Per-context pointer to the active authorizer.

Set by :class:`AuthorizationMiddleware.__init__`; read by
:func:`check_authorization` when its ``authorizer=`` kwarg is omitted.
Same pattern as ``_current_auth_mode`` in :mod:`_subject`; same
composition caveat (last writer wins; operators wishing to compose
multiple :class:`AuthorizationMiddleware` instances on distinct
contexts must wrap each install in
``contextvars.copy_context().run(...)``).
"""


def set_current_authorizer(authorizer: Authorizer | None) -> None:
    """Record the active authorizer for the current context.

    Called by :class:`AuthorizationMiddleware.__init__`.  Tests that
    exercise :func:`check_authorization` without going through the
    middleware may call this directly.  Passing ``None`` resets the
    pointer (useful between tests).
    """
    _current_authorizer.set(authorizer)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_authorization_check.py -v`
Expected: PASS (3 tests passing).

- [ ] **Step 5: Run mypy + ruff**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py`
Expected: both pass with no findings.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_check.py
git commit -m "feat(authorization): add AuthzDenied + ambient authorizer plumbing (refs #37)"
```

---

## Task 2: `load_acl` happy path

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (add `load_acl`)
- Create: `tests/test_authorization_loader.py`

Minimal happy-path-only loader. Validation errors come in Task 3.

- [ ] **Step 1: Write the failing happy-path test**

Create `tests/test_authorization_loader.py`:

```python
"""Tests for the load_acl TOML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from fastmcp_pvl_core._authorization import load_acl


def test_load_acl_happy_path(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text(
        '''
[subjects]
"user:alice@example.com" = ["read", "write"]
"user:admin@example.com" = ["*"]
"service:ci-bot"         = ["read"]
        ''',
        encoding="utf-8",
    )
    acl = load_acl(p)
    assert acl == {
        "user:alice@example.com": frozenset({"read", "write"}),
        "user:admin@example.com": frozenset({"*"}),
        "service:ci-bot": frozenset({"read"}),
    }
    # Values are frozensets, not lists.
    assert all(isinstance(v, frozenset) for v in acl.values())


def test_load_acl_empty_subjects_table_permitted(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text("[subjects]\n", encoding="utf-8")
    assert load_acl(p) == {}


def test_load_acl_expands_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Create the file at a real location, then point HOME at tmp_path
    # so that ``~/acl.toml`` resolves there.
    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / "acl.toml"
    real.write_text('[subjects]\n"user:x" = ["read"]\n', encoding="utf-8")
    acl = load_acl(Path("~/acl.toml"))
    assert acl == {"user:x": frozenset({"read"})}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_authorization_loader.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_acl'`.

- [ ] **Step 3: Add minimal `load_acl` to `_authorization.py`**

Add the import block at the top of `_authorization.py` (extend the existing imports):

```python
from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, AbstractSet
from contextvars import ContextVar
from pathlib import Path
from typing import TypeAlias

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - fallback for Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]

from fastmcp_pvl_core._errors import ConfigurationError
```

Then append the loader after the ContextVar block:

```python
# ---------------------------------------------------------------------------
# ACL TOML loader
# ---------------------------------------------------------------------------


def load_acl(path: Path) -> dict[str, frozenset[str]]:
    """Load an ACL TOML file into a ``{subject: frozenset[scope]}`` dict.

    The path is normalised with :meth:`Path.expanduser` first.  This is
    the single expansion site for both env-loaded paths (which keep a
    leading ``~`` literal) and direct-construction paths.

    Schema:

    .. code-block:: toml

        [subjects]
        "user:alice@example.com" = ["read", "write"]
        "user:admin@example.com" = ["*"]

    The ``*`` scope is interpreted by :func:`make_acl_authorizer` as
    "any required scope passes".  No subject-side wildcard.

    Args:
        path: Path to the ACL TOML file.

    Returns:
        A ``dict`` mapping each subject to a ``frozenset`` of granted
        scope strings.

    Raises:
        ConfigurationError: file missing, unreadable, malformed,
            schema-invalid, or containing an empty / whitespace /
            ``"*"`` subject key.
    """
    path = path.expanduser()
    if not path.is_file():
        raise ConfigurationError(
            f"ACL file not found or not a regular file: {path}"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(
            f"ACL file at {path} could not be read: {exc}"
        ) from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            f"ACL file at {path} could not be parsed: {exc}"
        ) from exc

    subjects = data.get("subjects")
    if not isinstance(subjects, dict):
        raise ConfigurationError(
            f"ACL file at {path} must define a [subjects] table"
        )

    result: dict[str, frozenset[str]] = {}
    for subject, scopes in subjects.items():
        result[subject] = frozenset(scopes)
    return result
```

- [ ] **Step 4: Run tests to verify happy path passes**

Run: `uv run pytest tests/test_authorization_loader.py -v`
Expected: PASS (3 tests passing).

- [ ] **Step 5: Run mypy + ruff**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_loader.py
git commit -m "feat(authorization): add load_acl happy path (refs #37)"
```

---

## Task 3: `load_acl` validation errors

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py:load_acl` (add validation branches)
- Modify: `tests/test_authorization_loader.py` (add error tests)

Each validation rule from the spec gets a test + branch.

- [ ] **Step 1: Write the failing validation tests**

Append to `tests/test_authorization_loader.py`:

```python
def test_load_acl_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "nope.toml"
    with pytest.raises(ConfigurationError, match="not found"):
        load_acl(p)


def test_load_acl_directory_not_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found or not a regular file"):
        load_acl(tmp_path)  # a directory


def test_load_acl_invalid_utf8(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_bytes(b"\xff\xfe\xfd not utf-8")
    with pytest.raises(ConfigurationError, match="could not be read"):
        load_acl(p)


def test_load_acl_malformed_toml(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text("[subjects\nbroken =", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="could not be parsed"):
        load_acl(p)


def test_load_acl_missing_subjects_table(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[other]\nkey = "val"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"\[subjects\] table"):
        load_acl(p)


def test_load_acl_subjects_not_a_table(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('subjects = "scalar"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"\[subjects\] table"):
        load_acl(p)


def test_load_acl_blank_subject_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"" = ["read"]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="subject key is empty"):
        load_acl(p)


def test_load_acl_whitespace_subject_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"   " = ["read"]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="subject key is empty"):
        load_acl(p)


def test_load_acl_subject_wildcard_rejected(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"*" = ["read"]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r'"\*" as a subject key'):
        load_acl(p)


def test_load_acl_non_list_value(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"user:x" = "read"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must be an array"):
        load_acl(p)


def test_load_acl_non_string_scope(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"user:x" = ["read", 42]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="scope must be a string"):
        load_acl(p)


def test_load_acl_blank_scope(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"user:x" = ["read", "  "]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="scope is empty"):
        load_acl(p)
```

Add the matching import to the top of the test file:

```python
from fastmcp_pvl_core._errors import ConfigurationError
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_authorization_loader.py -v`
Expected: 12 new tests fail (the existing 3 happy-path tests still pass).

- [ ] **Step 3: Add validation branches to `load_acl`**

Replace the loop body in `load_acl` (the part starting at `for subject, scopes in subjects.items():`) with:

```python
    result: dict[str, frozenset[str]] = {}
    for subject, scopes in subjects.items():
        if not subject.strip():
            raise ConfigurationError(
                f"ACL file at {path}: subject key is empty or whitespace-only"
            )
        if subject == "*":
            raise ConfigurationError(
                f'ACL file at {path}: "*" as a subject key is not allowed '
                "(global subject wildcards collapse the model)"
            )
        if not isinstance(scopes, list):
            raise ConfigurationError(
                f"ACL file at {path}: subject {subject!r} value must be an "
                f"array of scope strings; got {type(scopes).__name__}"
            )
        cleaned: set[str] = set()
        for scope in scopes:
            if not isinstance(scope, str):
                raise ConfigurationError(
                    f"ACL file at {path}: subject {subject!r}: scope must "
                    f"be a string; got {type(scope).__name__}"
                )
            if not scope.strip():
                raise ConfigurationError(
                    f"ACL file at {path}: subject {subject!r}: scope is "
                    "empty or whitespace-only"
                )
            cleaned.add(scope)
        result[subject] = frozenset(cleaned)
    return result
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest tests/test_authorization_loader.py -v`
Expected: 15 tests passing (12 new + 3 happy-path).

- [ ] **Step 5: Run mypy + ruff**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py tests/test_authorization_loader.py`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_loader.py
git commit -m "feat(authorization): add load_acl validation (refs #37)"
```

---

## Task 4: `make_acl_authorizer`

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (add bridge)
- Create: `tests/test_authorization_authorizer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_authorization_authorizer.py`:

```python
"""Tests for make_acl_authorizer."""

from __future__ import annotations

from fastmcp_pvl_core._authorization import make_acl_authorizer


def test_subject_in_acl_with_required_scope_allowed() -> None:
    authorize = make_acl_authorizer(
        {"user:alice": frozenset({"read", "write"})}
    )
    assert authorize("user:alice", "read") is True
    assert authorize("user:alice", "write") is True


def test_subject_in_acl_missing_required_scope_denied() -> None:
    authorize = make_acl_authorizer({"user:alice": frozenset({"read"})})
    assert authorize("user:alice", "write") is False


def test_unknown_subject_denied() -> None:
    authorize = make_acl_authorizer({"user:alice": frozenset({"read"})})
    assert authorize("user:bob", "read") is False


def test_subject_none_denied() -> None:
    authorize = make_acl_authorizer({"user:alice": frozenset({"read"})})
    assert authorize(None, "read") is False


def test_wildcard_scope_grants_anything() -> None:
    authorize = make_acl_authorizer({"user:admin": frozenset({"*"})})
    assert authorize("user:admin", "read") is True
    assert authorize("user:admin", "write") is True
    assert authorize("user:admin", "anything:project-foo") is True


def test_wildcard_alongside_specific_scopes() -> None:
    authorize = make_acl_authorizer(
        {"user:admin": frozenset({"*", "read"})}
    )
    assert authorize("user:admin", "anything") is True


def test_acl_captured_by_reference_not_copied() -> None:
    acl: dict[str, frozenset[str]] = {"user:alice": frozenset({"read"})}
    authorize = make_acl_authorizer(acl)
    assert authorize("user:bob", "read") is False
    # Mutate the ACL after the bridge was created.
    acl["user:bob"] = frozenset({"read"})
    assert authorize("user:bob", "read") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_authorization_authorizer.py -v`
Expected: FAIL with `ImportError: cannot import name 'make_acl_authorizer'`.

- [ ] **Step 3: Add `make_acl_authorizer` to `_authorization.py`**

Append after `load_acl`:

```python
# ---------------------------------------------------------------------------
# ACL → Authorizer bridge
# ---------------------------------------------------------------------------


def make_acl_authorizer(acl: Mapping[str, AbstractSet[str]]) -> Authorizer:
    """Bridge a ``{subject: scopes}`` mapping to an :data:`Authorizer`.

    Allow rules:

    - ``subject is None`` → deny.
    - Subject not in ``acl`` → deny.
    - ``"*"`` in the subject's grants → allow any required scope.
    - Otherwise → allow iff ``required_scope`` is in the grants.

    The mapping is captured by reference, not copied.  A downstream that
    mutates the dict in place sees the change reflected by the closure
    (intentional; the recommended pattern is rebuild + reassign, but
    reference-capture lets advanced consumers wire reload semantics
    without changing this signature).

    Args:
        acl: Mapping from subject string to a set of granted scope strings.

    Returns:
        An :data:`Authorizer` callable.
    """

    def authorize(subject: str | None, required_scope: str) -> bool:
        if subject is None:
            return False
        granted = acl.get(subject)
        if granted is None:
            return False
        return "*" in granted or required_scope in granted

    return authorize
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest tests/test_authorization_authorizer.py -v`
Expected: 7 tests passing.

- [ ] **Step 5: Run mypy + ruff**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py tests/test_authorization_authorizer.py`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_authorizer.py
git commit -m "feat(authorization): add make_acl_authorizer bridge (refs #37)"
```

---

## Task 5: `check_authorization`

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (add helper)
- Modify: `tests/test_authorization_check.py` (add full-coverage tests)
- Modify: `tests/conftest.py` (add `_reset_authorizer` autouse fixture)

This task adds the ambient-or-explicit `check_authorization` helper *and* the conftest fixture that keeps test isolation working — the two are coupled because the helper reads the ContextVar and the fixture resets it.

- [ ] **Step 1: Add the conftest fixture**

Replace `tests/conftest.py` with:

```python
"""Shared pytest fixtures for fastmcp-pvl-core tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from fastmcp_pvl_core._authorization import set_current_authorizer
from fastmcp_pvl_core._subject import set_current_auth_mode


@pytest.fixture(autouse=True)
def _reset_auth_mode() -> Iterator[None]:
    """Reset the auth-mode contextvar between tests.

    ``set_current_auth_mode`` writes to a :class:`ContextVar` whose
    visible value crosses test boundaries when tests share the same
    asyncio task / module run.  Lifted suite-wide here so that any
    test calling ``build_auth`` (which mutates the var as a startup
    side effect) does not leak the resolved mode into the next test
    that reads via ``get_subject``.
    """
    set_current_auth_mode(None)
    yield
    set_current_auth_mode(None)


@pytest.fixture(autouse=True)
def _reset_authorizer() -> Iterator[None]:
    """Reset the authorizer contextvar between tests.

    Mirrors ``_reset_auth_mode``; same rationale.
    """
    set_current_authorizer(None)
    yield
    set_current_authorizer(None)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip all env vars whose name starts with a common test prefix."""
    prefixes = ("TEST_", "PVLCORE_TEST_")
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    yield
```

- [ ] **Step 2: Add the failing `check_authorization` tests**

Append to `tests/test_authorization_check.py`:

```python
from unittest.mock import patch

from fastmcp_pvl_core._authorization import (
    Authorizer,
    check_authorization,
    set_current_authorizer,
)


def _allow_all(_subject: str | None, _required_scope: str) -> bool:
    return True


def _deny_all(_subject: str | None, _required_scope: str) -> bool:
    return False


def test_check_authorization_uses_explicit_authorizer_allow() -> None:
    # No subject lookup needed when the authorizer says yes.
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = check_authorization("read", authorizer=_allow_all)
    assert result is None


def test_check_authorization_uses_explicit_authorizer_deny() -> None:
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(AuthzDenied) as exc_info:
            check_authorization("write", authorizer=_deny_all)
    assert exc_info.value.subject == "user:alice"
    assert exc_info.value.required_scope == "write"


def test_check_authorization_reads_ambient_authorizer() -> None:
    set_current_authorizer(_allow_all)
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        check_authorization("read")  # no authorizer kwarg


def test_check_authorization_explicit_overrides_ambient() -> None:
    # Ambient says allow, explicit says deny — explicit wins.
    set_current_authorizer(_allow_all)
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(AuthzDenied):
            check_authorization("read", authorizer=_deny_all)


def test_check_authorization_no_authorizer_anywhere_raises_runtime_error() -> None:
    # Both ambient and explicit absent.
    with pytest.raises(RuntimeError, match="install AuthorizationMiddleware"):
        check_authorization("read")


def test_check_authorization_subject_kwarg_overrides_get_subject() -> None:
    captured: dict[str, object] = {}

    def authorize(subject: str | None, required_scope: str) -> bool:
        captured["subject"] = subject
        captured["required_scope"] = required_scope
        return True

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:fromcontext"
    ):
        check_authorization("read", authorizer=authorize, subject="user:explicit")
    assert captured == {"subject": "user:explicit", "required_scope": "read"}


def test_check_authorization_omitted_subject_falls_through_to_get_subject() -> None:
    """When ``subject`` is omitted (or None), ``get_subject()`` is consulted."""
    captured: dict[str, object] = {}

    def authorize(subject: str | None, required_scope: str) -> bool:
        captured["subject"] = subject
        return True

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:from_context"
    ):
        check_authorization("read", authorizer=authorize)
    assert captured == {"subject": "user:from_context"}


def test_check_authorization_get_subject_returning_none_denied_by_authorizer() -> None:
    """When no ambient subject and the authorizer denies None, AuthzDenied carries None."""
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value=None
    ):
        with pytest.raises(AuthzDenied) as exc_info:
            check_authorization("read", authorizer=_deny_all)
    assert exc_info.value.subject is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_authorization_check.py -v`
Expected: 7 new tests fail with `ImportError: cannot import name 'check_authorization'`. (The 3 `AuthzDenied` tests from Task 1 still pass.)

- [ ] **Step 4: Add `check_authorization` to `_authorization.py`**

Append after `make_acl_authorizer`:

```python
# ---------------------------------------------------------------------------
# check_authorization (escape-hatch helper)
# ---------------------------------------------------------------------------

# Local import to keep ``_authorization`` importable from ``_subject`` if
# that direction ever becomes needed; today the dependency is one-way.
from fastmcp_pvl_core._subject import get_subject  # noqa: E402


def check_authorization(
    required_scope: str,
    *,
    authorizer: Authorizer | None = None,
    subject: str | None = None,
) -> None:
    """Imperative authz check for use inside a tool / resource / prompt body.

    Resolution order:

    1. ``authorizer`` argument if given.
    2. The ambient :data:`_current_authorizer` (set by
       :class:`AuthorizationMiddleware.__init__`).
    3. Otherwise raise :class:`RuntimeError`.

    Subject resolution:

    - ``subject`` used as-is when a non-``None`` value is passed.
    - When omitted (or ``None``), :func:`fastmcp_pvl_core.get_subject`
      is called.  ``get_subject`` itself may return ``None`` if no auth
      context is available — that ``None`` is then forwarded to the
      authorizer, which typically denies it.

    Args:
        required_scope: Scope string to require, e.g. ``"write"`` or
            ``"read:project-foo"``.
        authorizer: Override the ambient authorizer.  Useful when the
            middleware isn't installed but a code path still wants the
            check.
        subject: Override the ``get_subject()`` lookup.  ``None``
            (the default) means "look up via :func:`get_subject`".

    Raises:
        AuthzDenied: when the authorizer returns ``False``.
        RuntimeError: when no authorizer is reachable (neither ambient
            nor explicit).
    """
    if authorizer is None:
        authorizer = _current_authorizer.get()
        if authorizer is None:
            raise RuntimeError(
                "no authorizer in context; install AuthorizationMiddleware "
                "or pass authorizer= explicitly to check_authorization()"
            )

    resolved_subject = subject if subject is not None else get_subject()

    if not authorizer(resolved_subject, required_scope):
        raise AuthzDenied(
            subject=resolved_subject, required_scope=required_scope
        )
```

- [ ] **Step 5: Run tests to verify all pass**

Run: `uv run pytest tests/test_authorization_check.py -v`
Expected: 10 tests passing (3 from Task 1 + 7 new).

- [ ] **Step 6: Run full test suite to ensure no regression**

Run: `uv run pytest -q`
Expected: all existing tests + new authz tests pass.

- [ ] **Step 7: Run mypy + ruff**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py tests/conftest.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py tests/test_authorization_check.py tests/conftest.py`
Expected: both pass.

- [ ] **Step 8: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_check.py tests/conftest.py
git commit -m "feat(authorization): add check_authorization + reset fixture (refs #37)"
```

---

## Task 6: `AuthorizationMiddleware` skeleton + `on_call_tool` static deny

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (add middleware class)
- Create: `tests/test_authorization_middleware.py`

The middleware grows in four tasks (6, 7, 8, 9); this one creates the class and the tool-call hook with static-meta deny only. AuthzDenied catching, list filtering, and lookup-error tolerance come next.

- [ ] **Step 1: Write the failing tool-call tests**

Create `tests/test_authorization_middleware.py`:

```python
"""Tests for AuthorizationMiddleware."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from fastmcp_pvl_core._authorization import AuthorizationMiddleware


def _make_context(
    *,
    tool_name: str = "do_thing",
    tool_meta: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a minimal MiddlewareContext-shaped mock for tool calls."""
    tool = SimpleNamespace(meta=tool_meta or {})
    fastmcp_obj = SimpleNamespace(get_tool=AsyncMock(return_value=tool))
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    message = SimpleNamespace(name=tool_name, arguments={})
    return MagicMock(
        message=message,
        fastmcp_context=fastmcp_context,
    )


def _allow_all(_subject: str | None, _required_scope: str) -> bool:
    return True


def _deny_all(_subject: str | None, _required_scope: str) -> bool:
    return False


@pytest.mark.asyncio
async def test_on_call_tool_no_meta_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_context(tool_meta={})  # no required_scope
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_call_tool(ctx, call_next)
    assert result == "result"
    call_next.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_on_call_tool_with_meta_allowed_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_call_tool(ctx, call_next)
    assert result == "result"
    call_next.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_on_call_tool_with_meta_denied_raises_tool_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "write"}
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_call_tool_publishes_authorizer_to_contextvar() -> None:
    """__init__ sets the ambient authorizer so check_authorization works."""
    from fastmcp_pvl_core._authorization import _current_authorizer

    AuthorizationMiddleware(authorizer=_allow_all)
    assert _current_authorizer.get() is _allow_all
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_authorization_middleware.py -v`
Expected: FAIL with `ImportError: cannot import name 'AuthorizationMiddleware'`.

- [ ] **Step 3: Add the middleware class to `_authorization.py`**

Append after `check_authorization`:

```python
# ---------------------------------------------------------------------------
# AuthorizationMiddleware
# ---------------------------------------------------------------------------

import json  # noqa: E402  (kept inline next to the deny-formatter)
import logging  # noqa: E402

from fastmcp.exceptions import ToolError  # noqa: E402
from fastmcp.server.middleware import Middleware, MiddlewareContext  # noqa: E402

logger = logging.getLogger(__name__)


class AuthorizationMiddleware(Middleware):
    """fastmcp middleware that enforces ``meta["required_scope"]`` on components.

    Tools, resources, and prompts opt in by setting
    ``meta={"required_scope": "<scope>"}`` at registration.  Components
    without the meta key are unrestricted.

    See ``docs/specs/authorization-submodule.md`` for the full design.
    """

    def __init__(
        self,
        *,
        authorizer: Authorizer,
        expose_subject_in_error: bool = False,
    ) -> None:
        """Construct the middleware and publish the authorizer ambient.

        Args:
            authorizer: Decision callable.  Saved on the instance and
                also written to the package-internal
                ``_current_authorizer`` :class:`ContextVar` so that
                :func:`check_authorization` calls inside tool bodies
                find it without an explicit ``authorizer=`` kwarg.
            expose_subject_in_error: When ``True``, the wire-side deny
                payload includes the ``subject`` key.  Defaults to
                ``False`` (multi-user disclosure risk).  The subject is
                always logged at WARNING regardless.
        """
        self._authorizer = authorizer
        self._expose_subject = expose_subject_in_error
        set_current_authorizer(authorizer)

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _format_deny_payload(
        self, *, subject: str | None, required_scope: str
    ) -> str:
        """Render the JSON-encoded deny payload for the wire."""
        body: dict[str, Any] = {
            "code": "authz_denied",
            "required_scope": required_scope,
        }
        if self._expose_subject:
            body["subject"] = subject
        return json.dumps(body)

    def _log_deny(
        self, *, kind: str, name: str, subject: str | None, required_scope: str
    ) -> None:
        """Log an authz denial at WARNING (subject always included in logs)."""
        logger.warning(
            "authz_denied kind=%s name=%s subject=%r required_scope=%r",
            kind, name, subject, required_scope,
        )

    # -------------------------------------------------------------------
    # Hooks (tool-call only in this task)
    # -------------------------------------------------------------------

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Enforce ``required_scope`` on tool calls."""
        tool = await context.fastmcp_context.fastmcp.get_tool(
            context.message.name
        )
        meta = getattr(tool, "meta", None) or {}
        required = meta.get("required_scope")
        if not isinstance(required, str) or not required.strip():
            # No requirement: pass through.
            return await call_next(context)

        subject = get_subject()
        if not self._authorizer(subject, required):
            self._log_deny(
                kind="tool",
                name=context.message.name,
                subject=subject,
                required_scope=required,
            )
            raise ToolError(
                self._format_deny_payload(
                    subject=subject, required_scope=required
                )
            )
        return await call_next(context)
```

Note on imports: the late `import` block (`json`, `logging`, fastmcp imports) is grouped with the middleware to keep the section self-contained. The `# noqa: E402` suppresses ruff's "module-level imports at top" rule for these specific lines; alternatively move them to the top of the file in a final cleanup pass.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_authorization_middleware.py -v`
Expected: 4 tests passing.

- [ ] **Step 5: Run mypy + ruff on the module**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py`
Expected: both pass. If ruff complains about E402 even with the `# noqa`, hoist the imports to the top of the file (the per-section import comment is a stylistic preference, not a requirement).

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py
git commit -m "feat(authorization): add AuthorizationMiddleware tool-call hook (refs #37)"
```

---

## Task 7: `on_read_resource` and `on_get_prompt`

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (add two parallel hooks)
- Modify: `tests/test_authorization_middleware.py` (add resource and prompt tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_authorization_middleware.py`:

```python
from fastmcp.exceptions import PromptError, ResourceError


def _make_resource_context(
    *, uri: str = "vault://doc-1", resource_meta: dict[str, Any] | None = None
) -> MagicMock:
    resource = SimpleNamespace(meta=resource_meta or {})
    fastmcp_obj = SimpleNamespace(get_resource=AsyncMock(return_value=resource))
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    message = SimpleNamespace(uri=uri)
    return MagicMock(message=message, fastmcp_context=fastmcp_context)


def _make_prompt_context(
    *, prompt_name: str = "the_prompt", prompt_meta: dict[str, Any] | None = None
) -> MagicMock:
    prompt = SimpleNamespace(meta=prompt_meta or {})
    fastmcp_obj = SimpleNamespace(get_prompt=AsyncMock(return_value=prompt))
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    message = SimpleNamespace(name=prompt_name, arguments={})
    return MagicMock(message=message, fastmcp_context=fastmcp_context)


@pytest.mark.asyncio
async def test_on_read_resource_with_meta_denied_raises_resource_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_resource_context(resource_meta={"required_scope": "read"})
    call_next = AsyncMock(return_value="contents")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ResourceError) as exc_info:
            await middleware.on_read_resource(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "read"}
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_read_resource_no_meta_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_resource_context(resource_meta={})
    call_next = AsyncMock(return_value="contents")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_read_resource(ctx, call_next)
    assert result == "contents"


@pytest.mark.asyncio
async def test_on_get_prompt_with_meta_denied_raises_prompt_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_prompt_context(prompt_meta={"required_scope": "read"})
    call_next = AsyncMock(return_value="messages")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(PromptError) as exc_info:
            await middleware.on_get_prompt(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "read"}
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_get_prompt_no_meta_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_prompt_context(prompt_meta={})
    call_next = AsyncMock(return_value="messages")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_get_prompt(ctx, call_next)
    assert result == "messages"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_authorization_middleware.py -v`
Expected: 4 new tests fail with `AttributeError: 'AuthorizationMiddleware' object has no attribute 'on_read_resource'`.

- [ ] **Step 3: Add the parallel hooks**

Add to `_authorization.py`'s import block (top of file or grouped near the existing fastmcp import):

```python
from fastmcp.exceptions import PromptError, ResourceError, ToolError  # noqa: E402
```

(Replacing the bare `from fastmcp.exceptions import ToolError` line.)

Add the two methods to `AuthorizationMiddleware` after `on_call_tool`:

```python
    async def on_read_resource(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Enforce ``required_scope`` on resource reads."""
        resource = await context.fastmcp_context.fastmcp.get_resource(
            context.message.uri
        )
        meta = getattr(resource, "meta", None) or {}
        required = meta.get("required_scope")
        if not isinstance(required, str) or not required.strip():
            return await call_next(context)

        subject = get_subject()
        if not self._authorizer(subject, required):
            self._log_deny(
                kind="resource",
                name=str(context.message.uri),
                subject=subject,
                required_scope=required,
            )
            raise ResourceError(
                self._format_deny_payload(
                    subject=subject, required_scope=required
                )
            )
        return await call_next(context)

    async def on_get_prompt(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Enforce ``required_scope`` on prompt retrievals."""
        prompt = await context.fastmcp_context.fastmcp.get_prompt(
            context.message.name
        )
        meta = getattr(prompt, "meta", None) or {}
        required = meta.get("required_scope")
        if not isinstance(required, str) or not required.strip():
            return await call_next(context)

        subject = get_subject()
        if not self._authorizer(subject, required):
            self._log_deny(
                kind="prompt",
                name=context.message.name,
                subject=subject,
                required_scope=required,
            )
            raise PromptError(
                self._format_deny_payload(
                    subject=subject, required_scope=required
                )
            )
        return await call_next(context)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_authorization_middleware.py -v`
Expected: 8 tests passing.

- [ ] **Step 5: Run mypy + ruff**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py
git commit -m "feat(authorization): add resource and prompt hooks (refs #37)"
```

---

## Task 8: `AuthzDenied` translation in per-call hooks

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (wrap `call_next` in try/except)
- Modify: `tests/test_authorization_middleware.py` (add `AuthzDenied`-from-body tests)

The middleware needs to catch `AuthzDenied` raised from inside a tool/resource/prompt body (via `check_authorization`) and convert it to the per-operation MCP error.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_authorization_middleware.py`:

```python
from fastmcp_pvl_core._authorization import AuthzDenied


@pytest.mark.asyncio
async def test_on_call_tool_body_authz_denied_becomes_tool_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_context(tool_meta={})  # static check passes (no meta)

    async def body_raises(_ctx: object) -> str:
        raise AuthzDenied(subject="user:alice", required_scope="write:foo")

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(ctx, body_raises)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "write:foo"}


@pytest.mark.asyncio
async def test_on_read_resource_body_authz_denied_becomes_resource_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_resource_context(resource_meta={})

    async def body_raises(_ctx: object) -> str:
        raise AuthzDenied(subject="user:alice", required_scope="read:foo")

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ResourceError) as exc_info:
            await middleware.on_read_resource(ctx, body_raises)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "read:foo"}


@pytest.mark.asyncio
async def test_on_get_prompt_body_authz_denied_becomes_prompt_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_prompt_context(prompt_meta={})

    async def body_raises(_ctx: object) -> str:
        raise AuthzDenied(subject="user:alice", required_scope="read:foo")

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(PromptError) as exc_info:
            await middleware.on_get_prompt(ctx, body_raises)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "read:foo"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_authorization_middleware.py -v -k authz_denied_becomes`
Expected: 3 tests fail (the bodies' `AuthzDenied` propagates as `Exception` rather than `ToolError`/`ResourceError`/`PromptError`).

- [ ] **Step 3: Wrap each per-call hook's `call_next` in a try/except**

In `_authorization.py`, modify `on_call_tool` so that the `return await call_next(context)` lines (both branches: with and without meta) become:

```python
        try:
            return await call_next(context)
        except AuthzDenied as exc:
            self._log_deny(
                kind="tool",
                name=context.message.name,
                subject=exc.subject,
                required_scope=exc.required_scope,
            )
            raise ToolError(
                self._format_deny_payload(
                    subject=exc.subject, required_scope=exc.required_scope
                )
            ) from None
```

Apply the analogous pattern to `on_read_resource` (raising `ResourceError`, `kind="resource"`, `name=str(context.message.uri)`) and `on_get_prompt` (raising `PromptError`, `kind="prompt"`, `name=context.message.name`).

The pattern collapses well into a small helper. Refactor `on_call_tool`'s body to:

```python
    async def on_call_tool(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        tool = await context.fastmcp_context.fastmcp.get_tool(
            context.message.name
        )
        await self._enforce_static(
            kind="tool",
            name=context.message.name,
            meta=getattr(tool, "meta", None) or {},
            error_cls=ToolError,
        )
        return await self._call_with_authz_translation(
            kind="tool",
            name=context.message.name,
            error_cls=ToolError,
            call_next=call_next,
            context=context,
        )
```

And add the two helpers after `_log_deny`:

```python
    async def _enforce_static(
        self,
        *,
        kind: str,
        name: str,
        meta: Mapping[str, Any],
        error_cls: type[Exception],
    ) -> None:
        """Run the static `meta["required_scope"]` check.

        Raises `error_cls` (constructed with the JSON deny payload) on
        deny.  Does nothing when meta has no requirement.
        """
        required = meta.get("required_scope")
        if not isinstance(required, str) or not required.strip():
            return
        subject = get_subject()
        if not self._authorizer(subject, required):
            self._log_deny(
                kind=kind, name=name, subject=subject, required_scope=required
            )
            raise error_cls(
                self._format_deny_payload(
                    subject=subject, required_scope=required
                )
            )

    async def _call_with_authz_translation(
        self,
        *,
        kind: str,
        name: str,
        error_cls: type[Exception],
        call_next: Any,
        context: MiddlewareContext,
    ) -> Any:
        """Run ``call_next`` and translate AuthzDenied to ``error_cls``."""
        try:
            return await call_next(context)
        except AuthzDenied as exc:
            self._log_deny(
                kind=kind, name=name,
                subject=exc.subject, required_scope=exc.required_scope,
            )
            raise error_cls(
                self._format_deny_payload(
                    subject=exc.subject, required_scope=exc.required_scope,
                )
            ) from None
```

Then collapse `on_read_resource` and `on_get_prompt` to mirror `on_call_tool`'s shape. Final form:

```python
    async def on_read_resource(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        resource = await context.fastmcp_context.fastmcp.get_resource(
            context.message.uri
        )
        await self._enforce_static(
            kind="resource",
            name=str(context.message.uri),
            meta=getattr(resource, "meta", None) or {},
            error_cls=ResourceError,
        )
        return await self._call_with_authz_translation(
            kind="resource",
            name=str(context.message.uri),
            error_cls=ResourceError,
            call_next=call_next,
            context=context,
        )

    async def on_get_prompt(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        prompt = await context.fastmcp_context.fastmcp.get_prompt(
            context.message.name
        )
        await self._enforce_static(
            kind="prompt",
            name=context.message.name,
            meta=getattr(prompt, "meta", None) or {},
            error_cls=PromptError,
        )
        return await self._call_with_authz_translation(
            kind="prompt",
            name=context.message.name,
            error_cls=PromptError,
            call_next=call_next,
            context=context,
        )
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest tests/test_authorization_middleware.py -v`
Expected: 11 tests passing (8 prior + 3 new).

- [ ] **Step 5: Run mypy + ruff**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py
git commit -m "feat(authorization): translate AuthzDenied from component bodies (refs #37)"
```

---

## Task 9: Component-lookup error tolerance

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (defensive try/except around `get_tool`/`get_resource`/`get_prompt`)
- Modify: `tests/test_authorization_middleware.py` (add fall-through tests)

When the inner-component lookup raises (mounted-server edge cases), the middleware logs a warning and falls through rather than denying.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_authorization_middleware.py`:

```python
@pytest.mark.asyncio
async def test_on_call_tool_get_tool_failure_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    fastmcp_obj = SimpleNamespace(
        get_tool=AsyncMock(side_effect=KeyError("tool not found"))
    )
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    ctx = MagicMock(
        message=SimpleNamespace(name="missing", arguments={}),
        fastmcp_context=fastmcp_context,
    )
    call_next = AsyncMock(return_value="result")
    caplog.set_level("WARNING", logger="fastmcp_pvl_core._authorization")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_call_tool(ctx, call_next)
    assert result == "result"
    call_next.assert_awaited_once_with(ctx)
    assert any("tool lookup failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_on_read_resource_get_resource_failure_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    fastmcp_obj = SimpleNamespace(
        get_resource=AsyncMock(side_effect=KeyError("nope"))
    )
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    ctx = MagicMock(
        message=SimpleNamespace(uri="vault://x"),
        fastmcp_context=fastmcp_context,
    )
    call_next = AsyncMock(return_value="contents")
    caplog.set_level("WARNING", logger="fastmcp_pvl_core._authorization")
    result = await middleware.on_read_resource(ctx, call_next)
    assert result == "contents"
    assert any("resource lookup failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_on_get_prompt_get_prompt_failure_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    fastmcp_obj = SimpleNamespace(
        get_prompt=AsyncMock(side_effect=KeyError("nope"))
    )
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    ctx = MagicMock(
        message=SimpleNamespace(name="missing", arguments={}),
        fastmcp_context=fastmcp_context,
    )
    call_next = AsyncMock(return_value="messages")
    caplog.set_level("WARNING", logger="fastmcp_pvl_core._authorization")
    result = await middleware.on_get_prompt(ctx, call_next)
    assert result == "messages"
    assert any("prompt lookup failed" in rec.message for rec in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_authorization_middleware.py -v -k lookup_failure`
Expected: 3 tests fail (the `KeyError` propagates).

- [ ] **Step 3: Wrap the lookups in defensive try/except**

In `_authorization.py`, modify `on_call_tool` to wrap the `get_tool` call:

```python
    async def on_call_tool(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        try:
            tool = await context.fastmcp_context.fastmcp.get_tool(
                context.message.name
            )
        except Exception as exc:  # noqa: BLE001 — defensive, logged
            logger.warning(
                "tool lookup failed during authz check; falling through "
                "name=%s exc=%r",
                context.message.name, exc,
            )
            return await self._call_with_authz_translation(
                kind="tool",
                name=context.message.name,
                error_cls=ToolError,
                call_next=call_next,
                context=context,
            )
        await self._enforce_static(
            kind="tool",
            name=context.message.name,
            meta=getattr(tool, "meta", None) or {},
            error_cls=ToolError,
        )
        return await self._call_with_authz_translation(
            kind="tool",
            name=context.message.name,
            error_cls=ToolError,
            call_next=call_next,
            context=context,
        )
```

Apply the analogous wrap to `on_read_resource` (`get_resource`, `"resource lookup failed"`, `name=str(context.message.uri)`, `error_cls=ResourceError`) and `on_get_prompt` (`get_prompt`, `"prompt lookup failed"`, `name=context.message.name`, `error_cls=PromptError`).

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest tests/test_authorization_middleware.py -v`
Expected: 14 tests passing.

- [ ] **Step 5: Run mypy + ruff**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py
git commit -m "feat(authorization): tolerate component-lookup failures (refs #37)"
```

---

## Task 10: List-filtering hooks

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (add four `on_list_*` hooks)
- Modify: `tests/test_authorization_middleware.py` (add list-filter tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_authorization_middleware.py`:

```python
@pytest.mark.asyncio
async def test_on_list_tools_filters_denied_tools() -> None:
    """Tools with denied required_scope are dropped; unannotated kept."""
    tool_open = SimpleNamespace(name="open_tool", meta={})
    tool_write = SimpleNamespace(
        name="write_tool", meta={"required_scope": "write"}
    )
    tool_read = SimpleNamespace(
        name="read_tool", meta={"required_scope": "read"}
    )

    def authorize(_subject: str | None, required: str) -> bool:
        return required == "read"  # only "read" passes

    middleware = AuthorizationMiddleware(authorizer=authorize)
    call_next = AsyncMock(return_value=[tool_open, tool_write, tool_read])
    ctx = MagicMock()
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_list_tools(ctx, call_next)
    assert result == [tool_open, tool_read]


@pytest.mark.asyncio
async def test_on_list_resources_filters_denied() -> None:
    res_open = SimpleNamespace(uri="vault://1", meta={})
    res_locked = SimpleNamespace(uri="vault://2", meta={"required_scope": "x"})
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    call_next = AsyncMock(return_value=[res_open, res_locked])
    ctx = MagicMock()
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_list_resources(ctx, call_next)
    assert result == [res_open]


@pytest.mark.asyncio
async def test_on_list_resource_templates_filters_denied() -> None:
    tmpl_open = SimpleNamespace(uri="vault://{a}", meta={})
    tmpl_locked = SimpleNamespace(
        uri="vault://locked/{a}", meta={"required_scope": "x"}
    )
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    call_next = AsyncMock(return_value=[tmpl_open, tmpl_locked])
    ctx = MagicMock()
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_list_resource_templates(ctx, call_next)
    assert result == [tmpl_open]


@pytest.mark.asyncio
async def test_on_list_prompts_filters_denied() -> None:
    p_open = SimpleNamespace(name="open", meta={})
    p_locked = SimpleNamespace(name="locked", meta={"required_scope": "x"})
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    call_next = AsyncMock(return_value=[p_open, p_locked])
    ctx = MagicMock()
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_list_prompts(ctx, call_next)
    assert result == [p_open]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_authorization_middleware.py -v -k on_list`
Expected: 4 tests fail with `AttributeError: ... has no attribute 'on_list_tools'`.

- [ ] **Step 3: Add the four list-filter hooks**

In `_authorization.py`, add a private helper and the four hooks:

```python
    def _filter_components(
        self, components: list[Any]
    ) -> list[Any]:
        """Drop components whose ``meta["required_scope"]`` denies the caller."""
        subject = get_subject()
        kept: list[Any] = []
        for component in components:
            meta = getattr(component, "meta", None) or {}
            required = meta.get("required_scope")
            if not isinstance(required, str) or not required.strip():
                kept.append(component)
                continue
            if self._authorizer(subject, required):
                kept.append(component)
        return kept

    async def on_list_tools(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Filter tool listings by what the caller can call."""
        tools = await call_next(context)
        return self._filter_components(tools)

    async def on_list_resources(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Filter resource listings by what the caller can read."""
        resources = await call_next(context)
        return self._filter_components(resources)

    async def on_list_resource_templates(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Filter resource template listings by what the caller can read."""
        templates = await call_next(context)
        return self._filter_components(templates)

    async def on_list_prompts(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Filter prompt listings by what the caller can retrieve."""
        prompts = await call_next(context)
        return self._filter_components(prompts)
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest tests/test_authorization_middleware.py -v`
Expected: 18 tests passing.

- [ ] **Step 5: Run mypy + ruff**

Run: `uv run mypy src/fastmcp_pvl_core/_authorization.py && uv run ruff check src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_middleware.py
git commit -m "feat(authorization): filter list-* responses by authz (refs #37)"
```

---

## Task 11: `expose_subject_in_error` flag tests

**Files:**
- Modify: `tests/test_authorization_middleware.py` (add expose-subject tests)

The flag and the WARNING-log behavior were already implemented in Tasks 6 and 8. This task locks them in with explicit assertions.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_authorization_middleware.py`:

```python
@pytest.mark.asyncio
async def test_default_payload_does_not_include_subject() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert "subject" not in payload


@pytest.mark.asyncio
async def test_expose_subject_in_error_includes_subject() -> None:
    middleware = AuthorizationMiddleware(
        authorizer=_deny_all, expose_subject_in_error=True
    )
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert payload == {
        "code": "authz_denied",
        "required_scope": "write",
        "subject": "user:alice",
    }


@pytest.mark.asyncio
async def test_subject_always_logged_at_warning_regardless_of_flag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Subject appears in WARNING log even when expose_subject_in_error=False."""
    caplog.set_level("WARNING", logger="fastmcp_pvl_core._authorization")
    middleware = AuthorizationMiddleware(
        authorizer=_deny_all, expose_subject_in_error=False
    )
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError):
            await middleware.on_call_tool(ctx, call_next)
    assert any(
        "user:alice" in rec.message and "authz_denied" in rec.message
        for rec in caplog.records
    )
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_authorization_middleware.py -v -k "expose_subject or default_payload or always_logged"`
Expected: 3 tests passing (these behaviors were already implemented; the tests just lock them in).

If a test fails, the implementation in Tasks 6 and 8 didn't match the assertions — go back and fix the implementation rather than weakening the test.

- [ ] **Step 3: Run full middleware test suite**

Run: `uv run pytest tests/test_authorization_middleware.py -v`
Expected: 21 tests passing.

- [ ] **Step 4: Commit**

```bash
git add tests/test_authorization_middleware.py
git commit -m "test(authorization): lock in expose_subject_in_error + log behavior (refs #37)"
```

---

## Task 12: Wire up package exports

**Files:**
- Modify: `src/fastmcp_pvl_core/__init__.py`

- [ ] **Step 1: Add the imports**

In `src/fastmcp_pvl_core/__init__.py`, after the existing `from fastmcp_pvl_core._auth import ...` block (around line 14-21), insert:

```python
from fastmcp_pvl_core._authorization import (
    Authorizer,
    AuthorizationMiddleware,
    AuthzDenied,
    check_authorization,
    load_acl,
    make_acl_authorizer,
)
```

- [ ] **Step 2: Add to `__all__`**

In the same file's `__all__` list, insert these entries (alphabetical placement; the list is sorted):

```python
    "AuthorizationMiddleware",
    "Authorizer",
    "AuthzDenied",
    ...
    "check_authorization",
    ...
    "load_acl",
    ...
    "make_acl_authorizer",
```

The exact insertion points depend on the alphabetical order; the existing `__all__` is sorted, so place each new symbol at the right spot. After editing, run a quick check that `__all__` stays sorted:

Run: `python -c "import fastmcp_pvl_core; assert fastmcp_pvl_core.__all__ == sorted(fastmcp_pvl_core.__all__), 'unsorted'; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Smoke-test the public API**

Run: `python -c "from fastmcp_pvl_core import AuthorizationMiddleware, AuthzDenied, Authorizer, check_authorization, load_acl, make_acl_authorizer; print('imports ok')"`
Expected: `imports ok`.

- [ ] **Step 4: Run full suite**

Run: `uv run pytest -q && uv run mypy src tests && uv run ruff check src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/__init__.py
git commit -m "feat(authorization): export public symbols (refs #37)"
```

---

## Task 13: README addition

**Files:**
- Modify: `README.md` (add an "Authorization" section after "Identifying the caller — `get_subject`")

- [ ] **Step 1: Add the new section**

In `README.md`, after the closing of the "Identifying the caller — `get_subject`" section (after the resolution-order block), and before the "Remote debugging in containers" section, insert:

````markdown
### Authorization (opt-in) — `AuthorizationMiddleware`

Tools, resources, and prompts can opt into per-subject access control by
setting `meta={"required_scope": "<scope>"}` at registration. A
configured `AuthorizationMiddleware` enforces the static check and
filters `list_*` responses to what the caller can use:

```python
from pathlib import Path
from fastmcp_pvl_core import (
    AuthorizationMiddleware, load_acl, make_acl_authorizer, check_authorization,
)

authorizer = make_acl_authorizer(load_acl(Path("/etc/my-app/acl.toml")))
mcp.add_middleware(AuthorizationMiddleware(authorizer=authorizer))

@mcp.tool(meta={"required_scope": "write"})
async def edit_document(project_id: str, doc_id: str, body: str) -> None:
    # Coarse "write" gate already passed at middleware. Per-project gate here:
    check_authorization(f"write:{project_id}")
    ...
```

ACL TOML schema (loaded by `load_acl`):

```toml
[subjects]
"user:alice@example.com" = ["read", "write"]
"user:admin@example.com" = ["*"]              # wildcard scope
"service:ci-bot"         = ["read"]
"local"                  = ["*"]              # stdio mode
```

Key properties:

- **Opt-in per component.** Tools / resources / prompts without
  `meta["required_scope"]` are unrestricted regardless of caller.
- **`*` is the only library-treated special scope** ("any required
  scope passes"). All other scopes are opaque strings; downstream chooses
  the vocabulary.
- **Subject-side wildcards (`*` as an ACL key) are rejected at load
  time.**
- **`load_acl` fails fast** with `ConfigurationError` on every malformed
  condition — never silent denial.
- **ACL is loaded once at startup.** Restart to pick up changes.
- **Authorization scopes are application-level** and distinct from the
  OAuth scopes carried in tokens.
- **Subject is logged on every deny** at WARNING. The wire-side payload
  *omits* the subject by default to limit cross-user info disclosure;
  pass `AuthorizationMiddleware(..., expose_subject_in_error=True)` to
  include it (e.g. for internal-only servers).

For the full design rationale and deviations from the originating
issue, see
[`docs/specs/authorization-submodule.md`](docs/specs/authorization-submodule.md).
````

- [ ] **Step 2: Verify README still renders cleanly**

Run: `python -c "import pathlib; t = pathlib.Path('README.md').read_text(); print('len=', len(t), 'has authz section=', 'AuthorizationMiddleware' in t)"`
Expected: `len=...` (longer than before), `has authz section= True`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): add Authorization section (refs #37)"
```

---

## Task 14: Cross-link cleanup in `auth-subject-authz.md`

**Files:**
- Modify: `docs/specs/auth-subject-authz.md` (the `## See also` block at the bottom + the "follow-on" paragraph in the Problem section)

The existing spec has two stale references to the deleted authorization-submodule draft:

- Lines 23-27 (Problem section): "A follow-on optional `authorization` submodule ... is described separately in `authorization-submodule.md` — that work was attempted in 2026-05 and abandoned mid-implementation; issue #37 remains open."
- Lines 277-281 (Driving consumers): "The downstream `authorization` submodule (issue #37) ... is described in `authorization-submodule.md`. That spec is currently DRAFT and not implemented; treat it as forward design only."
- Lines 283-286 (See also): the link annotated as DRAFT.

Each of these now points at a real, implemented spec rather than a deleted draft.

- [ ] **Step 1: Read the current state**

Run: `grep -n "authorization-submodule" docs/specs/auth-subject-authz.md`
Expected: 3 line numbers showing the references to update.

- [ ] **Step 2: Update Problem-section paragraph**

In `docs/specs/auth-subject-authz.md`, replace the paragraph that starts "A follow-on optional `authorization` submodule" with:

```markdown
A follow-on optional `authorization` submodule (subject + scope →
allow/deny middleware, ACL TOML loader, and `check_authorization`
helper) is described separately in
[`authorization-submodule.md`](authorization-submodule.md). The
implementation closes [issue #37](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/37).
```

- [ ] **Step 3: Update the Driving-consumers paragraph**

Replace the paragraph that starts "The downstream `authorization` submodule (issue #37)" with:

```markdown
The downstream `authorization` submodule — the optional middleware /
loader / `check_authorization` helper — is described in
[`authorization-submodule.md`](authorization-submodule.md) and closes
issue #37.
```

- [ ] **Step 4: Update the "See also" link annotation**

Replace the `authorization-submodule.md` line in the `## See also` block with:

```markdown
- [`authorization-submodule.md`](authorization-submodule.md) — the
  optional authorization submodule design (issue #37).
```

(Remove the "DRAFT and not implemented" suffix — it's no longer accurate.)

- [ ] **Step 5: Verify no stale references remain**

Run: `grep -n -E "(DRAFT|abandoned|not implemented)" docs/specs/auth-subject-authz.md`
Expected: no output (or only output that's about something else, not authorization-submodule).

- [ ] **Step 6: Commit**

```bash
git add docs/specs/auth-subject-authz.md
git commit -m "docs(spec): refresh auth-subject-authz cross-links to live authz spec (refs #37)"
```

---

## Task 15: Verification sweep + final commit

**Files:** none new — this task verifies everything works together.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass, no warnings about authz tests.

- [ ] **Step 2: Run mypy in strict mode across the project**

Run: `uv run mypy src tests`
Expected: `Success: no issues found`.

- [ ] **Step 3: Run ruff**

Run: `uv run ruff check src tests`
Expected: `All checks passed!`.

- [ ] **Step 4: Smoke-test the public API end-to-end**

Run:

```bash
python <<'EOF'
from pathlib import Path
import tempfile
from fastmcp_pvl_core import (
    AuthorizationMiddleware, AuthzDenied, Authorizer,
    check_authorization, load_acl, make_acl_authorizer,
)

with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
    f.write('[subjects]\n"user:alice" = ["read", "write"]\n"user:bob" = ["read"]\n')
    f.flush()
    acl = load_acl(Path(f.name))

authorize = make_acl_authorizer(acl)
assert authorize("user:alice", "write") is True
assert authorize("user:bob", "write") is False
assert authorize(None, "read") is False

mw = AuthorizationMiddleware(authorizer=authorize)

try:
    check_authorization("write", subject="user:bob")
    raise AssertionError("expected AuthzDenied")
except AuthzDenied as exc:
    assert exc.subject == "user:bob"
    assert exc.required_scope == "write"

print("smoke ok")
EOF
```

Expected: `smoke ok`.

- [ ] **Step 5: Verify git history is clean**

Run: `git log --oneline origin/main..HEAD`
Expected: ~14 commits (one per task plus the initial spec commit), each with a `(refs #37)` trailer.

- [ ] **Step 6: Map verification list from spec**

Open `docs/specs/authorization-submodule.md` "Verification list" section. For each `[ ]` checkbox, point to the test that covers it. Update the spec's list to `[x]` only via a follow-up commit if the user wants — otherwise leave the spec's list as the design-time intent.

- [ ] **Step 7: Final review preparation**

The branch is now ready for the local review circus:

1. Dispatch `pr-review-toolkit:code-reviewer` on the cumulative diff against `main`.
2. Dispatch `superpowers:code-reviewer` on the same diff.
3. Targeted reviewers: `silent-failure-hunter` (the middleware has try/except / fall-through paths), `type-design-analyzer` (the `Authorizer` alias + the `subject: ... = ...` sentinel), `pr-test-analyzer` (asserts the test list against the spec's verification matrix), `comment-analyzer` (the spec is a long doc that the comment-analyzer should sanity-check).

After both primary reviewers return *nothing flagged at any severity*, open the PR as draft against `main` with body that explicitly closes #37.

After CI green and bot LGTM bodies (read the bodies, not just the check status), flip ready.

- [ ] **Step 8: Post-merge follow-up**

The spec's "Documentation → Template-side" section lists three stub issues to file against `pvliesdonk/fastmcp-server-template`. File these *after* this PR merges, each with a `Depends-on:` pointer back to issue #37 / this PR, per the spec.

---

## Self-review

**Spec coverage check:** every section of `docs/specs/authorization-submodule.md` maps to one or more tasks above:

| Spec section | Covered by |
|---|---|
| Module layout and public API | Tasks 1, 12 |
| `AuthorizationMiddleware` (per-call hooks, list filtering, ContextVar publish, lookup error tolerance) | Tasks 6, 7, 8, 9, 10 |
| Annotation convention | Tasks 6, 7, 13 (docs) |
| `check_authorization` and `AuthzDenied` | Tasks 1, 5 |
| Error shape (default + `expose_subject_in_error`, log on deny) | Tasks 6, 11 |
| TOML loader + bridge | Tasks 2, 3, 4 |
| Wiring example | Task 13 |
| What this submodule does NOT do | Implicit — anything not implemented in Tasks 1-14 isn't shipped |
| Testing (file-by-file, scenarios, verification list) | Tasks 2-11 + 15 |
| Documentation (library-side) | Tasks 13, 14 |
| Documentation (template-side) | Task 15 step 8 (post-merge follow-up) |
| Implementation phasing | This whole plan + the "single PR" framing in Task 15 |
| Driving consumers | Mentioned in spec; nothing to implement here |
| Local review discipline | Task 15 step 7 |
| Deviations from issue #37 | Spec section; nothing to implement |

**Placeholder scan:** none. Each step has the actual code, the actual command, the actual expected output.

**Type consistency:** the `Authorizer` alias is defined in Task 1 and used identically in Tasks 4, 5, 6, 7, 8, 10, 11, 12. `AuthzDenied`'s constructor signature (`subject=`, `required_scope=`) is consistent across Tasks 1, 5, 8. The middleware's `_format_deny_payload` and `_log_deny` helper signatures are consistent across all per-call hooks (Tasks 6-9). The `_enforce_static` and `_call_with_authz_translation` helpers introduced in Task 8 are reused unchanged in Task 9.

---

**End of plan.**
