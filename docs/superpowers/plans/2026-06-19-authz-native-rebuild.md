# Authorization Native-Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace pvl-core's bespoke `AuthorizationMiddleware` + ACL with thin factories over FastMCP 3.3's native `AuthCheck` surface, and add claim-based authorization.

**Architecture:** pvl-core stops shipping an authorization middleware. It ships five functions that return native `AuthCheck` callables (`Callable[[AuthContext], bool]`), which downstream wires via FastMCP's own `AuthMiddleware(auth=...)` / `@mcp.tool(auth=...)`. Checks read `ctx.token` and `ctx.component.meta["required_scope"]` directly. The `_subject.py` identity extractor is untouched.

**Tech Stack:** Python 3.10–3.13, FastMCP ≥3.3.1, pytest, mypy (strict), ruff, uv.

## Global Constraints

- `fastmcp>=3.3.1,<4` — native auth symbols (`AuthCheck`, `AuthContext`, `AuthMiddleware`) come from `fastmcp.server.auth` / `fastmcp.server.middleware`.
- Python 3.10–3.13. `tomllib` is stdlib only on 3.11+; `load_acl` keeps the existing `tomli` fallback block for 3.10.
- mypy strict + ruff are gates. `from __future__ import annotations` is in the module (annotations are stringized — import `AuthCheck`/`AuthContext` under `TYPE_CHECKING`).
- Before declaring local checks clean: `uv sync --all-extras` then `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`.
- Conventional commits. The cutover (Task 4) removes public symbols → carries a `BREAKING CHANGE:` footer so PSR cuts a **major**.
- Every commit ends with the trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Tests import from the private module `fastmcp_pvl_core._authorization` (matching the existing test convention); public re-exports are wired in Task 4.
- Spec: `docs/superpowers/specs/2026-06-19-authz-native-rebuild-design.md`.

---

### Task 0: Tracking issue and branch

**Files:** none (GitHub + git only).

- [ ] **Step 1: File the tracking issue** (GitHub write — append the operator's agent-attribution signature line to the body per personal workflow rules)

Title: `Rebuild authorization on FastMCP-native auth checks`
Body (summary): replace bespoke `AuthorizationMiddleware`/ACL with native `AuthCheck` factories (`make_acl_check`, `make_claims_check`, `any_check`, `parse_claim_grants`; keep `load_acl`); delete `AuthorizationMiddleware`, `AuthzDenied`, `check_authorization`, `Authorizer`, `make_acl_authorizer`, and `docs/specs/authorization-submodule.md`. Breaking (major). Links the spec.

- [ ] **Step 2: Create the branch**

```bash
git checkout -b feat/authz-native-rebuild
```

---

### Task 1: `make_acl_check`

Subject→scope native check. Reuses the kept `load_acl`. Adds two private helpers (`_resolve_required`, `_subject_of`) that later tasks also use.

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (add helpers + `make_acl_check`; leave old code in place for now)
- Test: `tests/test_authorization_acl_check.py` (create)

**Interfaces:**
- Consumes: `fastmcp.server.auth.AuthContext` (dataclass: `.token`, `.component`).
- Produces:
  - `make_acl_check(acl: Mapping[str, AbstractSet[str]], required: str | None = None) -> AuthCheck`
  - `_resolve_required(required: str | None, component: object) -> str | None`
  - `_subject_of(token: object) -> str | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_authorization_acl_check.py`:

```python
"""Tests for make_acl_check (subject->scope native AuthCheck)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastmcp.server.auth import AuthContext

from fastmcp_pvl_core._authorization import make_acl_check


@dataclass
class _FakeToken:
    client_id: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeComponent:
    meta: dict[str, Any] = field(default_factory=dict)


def _ctx(token: object, meta: dict[str, Any] | None = None) -> AuthContext:
    return AuthContext(token=token, component=_FakeComponent(meta=meta or {}))


def test_allows_when_subject_has_required_scope() -> None:
    check = make_acl_check({"user:alice": frozenset({"read", "write"})})
    ctx = _ctx(_FakeToken(claims={"sub": "user:alice"}), {"required_scope": "write"})
    assert check(ctx) is True


def test_denies_when_scope_absent() -> None:
    check = make_acl_check({"user:alice": frozenset({"read"})})
    ctx = _ctx(_FakeToken(claims={"sub": "user:alice"}), {"required_scope": "write"})
    assert check(ctx) is False


def test_wildcard_scope_allows_anything() -> None:
    check = make_acl_check({"user:admin": frozenset({"*"})})
    ctx = _ctx(_FakeToken(claims={"sub": "user:admin"}), {"required_scope": "delete"})
    assert check(ctx) is True


def test_unknown_subject_denied() -> None:
    check = make_acl_check({"user:alice": frozenset({"write"})})
    ctx = _ctx(_FakeToken(claims={"sub": "user:bob"}), {"required_scope": "write"})
    assert check(ctx) is False


def test_falls_back_to_client_id_when_no_sub_claim() -> None:
    # Bearer mode: no "sub" claim; subject is the client_id.
    check = make_acl_check({"user:alice": frozenset({"write"})})
    ctx = _ctx(_FakeToken(client_id="user:alice", claims={}), {"required_scope": "write"})
    assert check(ctx) is True


def test_no_token_denies() -> None:
    check = make_acl_check({"user:alice": frozenset({"write"})})
    assert check(_ctx(None, {"required_scope": "write"})) is False


def test_no_required_scope_meta_is_unrestricted() -> None:
    check = make_acl_check({})  # empty ACL would deny everyone if a scope were required
    ctx = _ctx(_FakeToken(claims={"sub": "user:bob"}), meta={})
    assert check(ctx) is True


def test_explicit_required_arg_overrides_meta() -> None:
    check = make_acl_check({"user:alice": frozenset({"admin"})}, required="admin")
    ctx = _ctx(_FakeToken(claims={"sub": "user:alice"}), {"required_scope": "write"})
    assert check(ctx) is True


def test_invalid_meta_treated_unrestricted(caplog: pytest.LogCaptureFixture) -> None:
    check = make_acl_check({})
    ctx = _ctx(_FakeToken(claims={"sub": "x"}), {"required_scope": "   "})
    with caplog.at_level("WARNING", logger="fastmcp_pvl_core._authorization"):
        assert check(ctx) is True
    assert "authz_meta_invalid" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_authorization_acl_check.py -q`
Expected: FAIL — `ImportError: cannot import name 'make_acl_check'`.

- [ ] **Step 3: Add helpers and `make_acl_check` to `_authorization.py`**

Add `import inspect` is **not** needed yet. Ensure these imports exist at the top of the module (some already do): `import logging`, `from collections.abc import Mapping`, `from collections.abc import Set as AbstractSet`, `from typing import TYPE_CHECKING`. Under the existing `if TYPE_CHECKING:` block (add one if absent) add:

```python
if TYPE_CHECKING:
    from fastmcp.server.auth import AuthCheck, AuthContext
```

Add the following near the bottom of the module (before the to-be-removed bespoke code is fine; it will be the only code left after Task 4):

```python
def _resolve_required(required: str | None, component: object) -> str | None:
    """Resolve the required scope: explicit arg, else component meta.

    Returns ``None`` when no scope is required (component is
    unrestricted). An invalid ``meta["required_scope"]`` (present but not
    a non-empty string) is logged and treated as unrestricted, matching
    the opt-in posture.
    """
    if required is not None:
        required = required.strip()
        return required or None
    meta = getattr(component, "meta", None) or {}
    value = meta.get("required_scope")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        logger.warning(
            "authz_meta_invalid required_scope=%r — expected non-empty "
            "string; treating as unrestricted",
            value,
        )
        return None
    return value.strip()


def _subject_of(token: object) -> str | None:
    """Resolve the caller subject from a token.

    Prefers the OIDC ``sub`` claim; falls back to ``client_id`` (the
    bearer-mode subject). Mirrors ``fastmcp_pvl_core.get_subject``'s
    resolution rule so the ACL keys identically across modes. (Kept local
    to avoid coupling to the ambient ``get_access_token`` path that
    ``get_subject`` uses.)
    """
    claims = getattr(token, "claims", None)
    if isinstance(claims, dict):
        sub = claims.get("sub")
        if isinstance(sub, str) and sub:
            return sub
    client_id = getattr(token, "client_id", None)
    if isinstance(client_id, str) and client_id:
        return client_id
    return None


def make_acl_check(
    acl: Mapping[str, AbstractSet[str]],
    required: str | None = None,
) -> AuthCheck:
    """Build a native ``AuthCheck`` enforcing a subject→scope ACL.

    The returned check reads the caller subject from ``ctx.token``
    (``sub`` claim, else ``client_id``) and the required scope from
    ``required=`` or ``ctx.component.meta["required_scope"]``. This is
    the only authz primitive usable in bearer modes, which carry no
    OIDC claims. ``acl`` is captured by reference.
    """

    def check(ctx: AuthContext) -> bool:
        token = ctx.token
        if token is None:
            return False
        scope = _resolve_required(required, ctx.component)
        if scope is None:
            return True
        subject = _subject_of(token)
        if subject is None:
            return False
        granted = acl.get(subject)
        if granted is None:
            return False
        return "*" in granted or scope in granted

    return check
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_authorization_acl_check.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_acl_check.py
git commit -m "$(cat <<'EOF'
feat(authorization): add make_acl_check native AuthCheck

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `make_claims_check` and `parse_claim_grants`

Claim→scope native check (OIDC modes) plus the strict inline-JSON grants loader.

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (add `_extract_claim_values`, `make_claims_check`, `parse_claim_grants`)
- Test: `tests/test_authorization_claims_check.py` (create), `tests/test_authorization_grants_parser.py` (create)

**Interfaces:**
- Consumes: `_resolve_required` (Task 1), `ConfigurationError` (already imported in the module).
- Produces:
  - `make_claims_check(claim: str, grants: Mapping[str, AbstractSet[str]] | None = None, required: str | None = None) -> AuthCheck`
  - `parse_claim_grants(raw: str) -> dict[str, frozenset[str]]`
  - `_extract_claim_values(claims: object, claim: str) -> set[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_authorization_claims_check.py`:

```python
"""Tests for make_claims_check (claim->scope native AuthCheck)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastmcp.server.auth import AuthContext

from fastmcp_pvl_core._authorization import make_claims_check


@dataclass
class _FakeToken:
    claims: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeComponent:
    meta: dict[str, Any] = field(default_factory=dict)


def _ctx(claims: dict[str, Any] | None, meta: dict[str, Any] | None = None) -> AuthContext:
    token = None if claims is None else _FakeToken(claims=claims)
    return AuthContext(token=token, component=_FakeComponent(meta=meta or {}))


def test_blank_claim_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        make_claims_check("   ")


def test_identity_allows_when_claim_contains_scope() -> None:
    check = make_claims_check("groups")
    ctx = _ctx({"groups": ["read", "write"]}, {"required_scope": "write"})
    assert check(ctx) is True


def test_identity_denies_when_claim_missing_scope() -> None:
    check = make_claims_check("groups")
    ctx = _ctx({"groups": ["read"]}, {"required_scope": "write"})
    assert check(ctx) is False


def test_translation_maps_group_to_scopes() -> None:
    check = make_claims_check("groups", {"app-writers": frozenset({"read", "write"})})
    ctx = _ctx({"groups": ["app-writers"]}, {"required_scope": "write"})
    assert check(ctx) is True


def test_translation_unions_across_values() -> None:
    grants = {"g1": frozenset({"read"}), "g2": frozenset({"write"})}
    check = make_claims_check("groups", grants)
    ctx = _ctx({"groups": ["g1", "g2"]}, {"required_scope": "write"})
    assert check(ctx) is True


def test_translation_wildcard() -> None:
    check = make_claims_check("groups", {"admins": frozenset({"*"})})
    ctx = _ctx({"groups": ["admins"]}, {"required_scope": "delete"})
    assert check(ctx) is True


def test_string_scalar_claim_is_single_value_not_split() -> None:
    # "openid write" must NOT be split; it's one value that won't match "write".
    check = make_claims_check("scope")
    ctx = _ctx({"scope": "openid write"}, {"required_scope": "write"})
    assert check(ctx) is False
    check2 = make_claims_check("role")
    ctx2 = _ctx({"role": "write"}, {"required_scope": "write"})
    assert check2(ctx2) is True


def test_mixed_list_keeps_only_strings() -> None:
    check = make_claims_check("groups")
    ctx = _ctx({"groups": ["write", 5, None]}, {"required_scope": "write"})
    assert check(ctx) is True


@pytest.mark.parametrize("value", [42, True, None, {"a": 1}, []])
def test_non_usable_claim_values_deny(value: Any) -> None:
    check = make_claims_check("groups")
    ctx = _ctx({"groups": value}, {"required_scope": "write"})
    assert check(ctx) is False


def test_absent_claim_denies() -> None:
    check = make_claims_check("groups")
    ctx = _ctx({"other": ["write"]}, {"required_scope": "write"})
    assert check(ctx) is False


def test_no_token_denies() -> None:
    check = make_claims_check("groups")
    assert check(_ctx(None, {"required_scope": "write"})) is False


def test_no_required_scope_is_unrestricted() -> None:
    check = make_claims_check("groups")
    assert check(_ctx({"groups": []}, meta={})) is True
```

Create `tests/test_authorization_grants_parser.py`:

```python
"""Tests for parse_claim_grants (inline-JSON claim-value->scopes loader)."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core._authorization import parse_claim_grants
from fastmcp_pvl_core._errors import ConfigurationError


def test_happy_path() -> None:
    result = parse_claim_grants('{"writers": ["read", "write"], "admins": ["*"]}')
    assert result == {
        "writers": frozenset({"read", "write"}),
        "admins": frozenset({"*"}),
    }


def test_empty_object_permitted() -> None:
    assert parse_claim_grants("{}") == {}


def test_scopes_stripped() -> None:
    assert parse_claim_grants('{"g": [" write "]}') == {"g": frozenset({"write"})}


def test_invalid_json() -> None:
    with pytest.raises(ConfigurationError, match="could not be parsed"):
        parse_claim_grants("{not json")


def test_top_level_not_object() -> None:
    with pytest.raises(ConfigurationError, match="must be a JSON object"):
        parse_claim_grants('["a", "b"]')


def test_blank_key() -> None:
    with pytest.raises(ConfigurationError, match="empty or whitespace"):
        parse_claim_grants('{"  ": ["read"]}')


def test_star_key_rejected() -> None:
    with pytest.raises(ConfigurationError, match="not allowed"):
        parse_claim_grants('{"*": ["read"]}')


def test_value_not_array() -> None:
    with pytest.raises(ConfigurationError, match="must be an array"):
        parse_claim_grants('{"g": "read"}')


def test_non_string_scope() -> None:
    with pytest.raises(ConfigurationError, match="must be a string"):
        parse_claim_grants('{"g": ["read", 5]}')


def test_blank_scope() -> None:
    with pytest.raises(ConfigurationError, match="empty or whitespace"):
        parse_claim_grants('{"g": ["read", "  "]}')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_authorization_claims_check.py tests/test_authorization_grants_parser.py -q`
Expected: FAIL — `ImportError` for `make_claims_check` / `parse_claim_grants`.

- [ ] **Step 3: Add `_extract_claim_values`, `make_claims_check`, `parse_claim_grants`**

Ensure `import json` is present at the top of the module (it already is — used by the old code; keep it). Add:

```python
def _extract_claim_values(claims: object, claim: str) -> set[str]:
    """Normalise a claim's value to a set of strings (lenient).

    Scalar string -> ``{value}`` (never whitespace-split). List/tuple/set
    -> its string elements only. Any other shape (absent, int, bool,
    None, dict, empty list) -> empty set. A request is never failed on an
    unexpected claim shape.
    """
    if not isinstance(claims, dict):
        return set()
    value = claims.get(claim)
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {v for v in value if isinstance(v, str)}
    return set()


def make_claims_check(
    claim: str,
    grants: Mapping[str, AbstractSet[str]] | None = None,
    required: str | None = None,
) -> AuthCheck:
    """Build a native ``AuthCheck`` enforcing a claim→scope grant.

    Reads ``claim`` from ``ctx.token.claims``. With ``grants=None`` the
    caller is granted exactly the string values in the claim (identity).
    With ``grants`` supplied, those values are mapped through the table
    and unioned. OIDC modes only — bearer tokens carry no usable claims.
    Required scope resolves from ``required=`` or
    ``ctx.component.meta["required_scope"]``.
    """
    claim = claim.strip()
    if not claim:
        raise ValueError("claim must be a non-empty string")

    def check(ctx: AuthContext) -> bool:
        token = ctx.token
        if token is None:
            return False
        scope = _resolve_required(required, ctx.component)
        if scope is None:
            return True
        values = _extract_claim_values(getattr(token, "claims", None), claim)
        if grants is None:
            granted: set[str] = set(values)
        else:
            granted = set()
            for value in values:
                mapped = grants.get(value)
                if mapped is not None:
                    granted |= set(mapped)
        return "*" in granted or scope in granted

    return check


def parse_claim_grants(raw: str) -> dict[str, frozenset[str]]:
    """Parse an inline-JSON claim-value→scopes map. Fail-fast.

    Schema: a JSON object mapping each claim value (e.g. a group name) to
    an array of scope strings. Empty object is permitted (deny-everyone).
    Mirrors ``load_acl``'s value validation. Raises
    :class:`ConfigurationError` on every malformed condition.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigurationError(
            f"claim grants could not be parsed as JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigurationError(
            "claim grants must be a JSON object mapping claim values to "
            f"scope arrays; got {type(data).__name__}"
        )
    result: dict[str, frozenset[str]] = {}
    for key, scopes in data.items():
        # JSON object keys are always strings.
        if not key.strip():
            raise ConfigurationError(
                "claim grants: claim-value key is empty or whitespace-only"
            )
        if key == "*":
            raise ConfigurationError(
                'claim grants: "*" as a claim-value key is not allowed '
                "(global key wildcards collapse the model)"
            )
        if not isinstance(scopes, list):
            raise ConfigurationError(
                f"claim grants: value for {key!r} must be an array of scope "
                f"strings; got {type(scopes).__name__}"
            )
        cleaned: set[str] = set()
        for scope in scopes:
            if not isinstance(scope, str):
                raise ConfigurationError(
                    f"claim grants: {key!r}: scope must be a string; got "
                    f"{type(scope).__name__}"
                )
            if not scope.strip():
                raise ConfigurationError(
                    f"claim grants: {key!r}: scope is empty or whitespace-only"
                )
            cleaned.add(scope.strip())
        result[key] = frozenset(cleaned)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_authorization_claims_check.py tests/test_authorization_grants_parser.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_claims_check.py tests/test_authorization_grants_parser.py
git commit -m "$(cat <<'EOF'
feat(authorization): add make_claims_check and parse_claim_grants

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `any_check`

OR-combinator for `multi` mode (native combines checks with AND).

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (add `import inspect`; add `any_check`)
- Test: `tests/test_authorization_any_check.py` (create)

**Interfaces:**
- Produces: `any_check(*checks: AuthCheck) -> AuthCheck` (returns an async check).

- [ ] **Step 1: Write the failing test**

Create `tests/test_authorization_any_check.py`:

```python
"""Tests for any_check (OR-combinator over native AuthChecks)."""

from __future__ import annotations

import pytest
from fastmcp.server.auth import AuthContext

from fastmcp_pvl_core._authorization import any_check


def _ctx() -> AuthContext:
    return AuthContext(token=object(), component=object())


def test_zero_checks_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        any_check()


async def test_true_when_any_passes() -> None:
    check = any_check(lambda ctx: False, lambda ctx: True)
    assert await check(_ctx()) is True


async def test_false_when_all_fail() -> None:
    check = any_check(lambda ctx: False, lambda ctx: False)
    assert await check(_ctx()) is False


async def test_short_circuits_on_first_true() -> None:
    calls: list[int] = []

    def first(ctx: AuthContext) -> bool:
        calls.append(1)
        return True

    def second(ctx: AuthContext) -> bool:
        calls.append(2)
        return True

    check = any_check(first, second)
    assert await check(_ctx()) is True
    assert calls == [1]


async def test_awaits_async_sub_checks() -> None:
    async def async_true(ctx: AuthContext) -> bool:
        return True

    check = any_check(lambda ctx: False, async_true)
    assert await check(_ctx()) is True
```

Note: these tests are async; the repo's pytest config runs async tests (anyio/asyncio mode). If a test needs an explicit marker, the existing `tests/` suite shows the convention — match it.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_authorization_any_check.py -q`
Expected: FAIL — `ImportError: cannot import name 'any_check'`.

- [ ] **Step 3: Add `any_check`**

Add `import inspect` to the top-of-module imports. Add:

```python
def any_check(*checks: AuthCheck) -> AuthCheck:
    """Combine checks with OR (native ``AuthMiddleware`` uses AND).

    Returns an async check that passes if any sub-check passes,
    short-circuiting on the first ``True``. Sub-checks may be sync or
    async; coroutine results are awaited. Used for ``multi`` mode where a
    bearer caller satisfies the ACL check and an OIDC caller satisfies
    the claims check.
    """
    if not checks:
        raise ValueError("any_check requires at least one check")

    async def combined(ctx: AuthContext) -> bool:
        for check in checks:
            result = check(ctx)
            if inspect.isawaitable(result):
                result = await result
            if result:
                return True
        return False

    return combined
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_authorization_any_check.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_authorization.py tests/test_authorization_any_check.py
git commit -m "$(cat <<'EOF'
feat(authorization): add any_check OR-combinator

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Cutover — delete bespoke authz, rewire exports, docs

Coordinated removal: the old symbols, their tests, the conftest fixture, the `__init__` exports, the README section, and the obsolete spec all go together so the suite stays green and imports resolve.

**Files:**
- Modify: `src/fastmcp_pvl_core/_authorization.py` (delete bespoke code; keep only `load_acl` + the new functions/helpers)
- Modify: `src/fastmcp_pvl_core/__init__.py:17-86` (re-exports)
- Modify: `tests/conftest.py:11,31-39` (drop `_reset_authorizer` + its import)
- Modify: `README.md:253-308` (rewrite Authorization section)
- Delete: `tests/test_authorization_authorizer.py`, `tests/test_authorization_check.py`, `tests/test_authorization_middleware.py`
- Delete: `docs/specs/authorization-submodule.md`
- Keep: `tests/test_authorization_loader.py` (`load_acl` unchanged)

**Interfaces:**
- Consumes: all symbols from Tasks 1–3.
- Produces: public re-exports `make_acl_check`, `make_claims_check`, `any_check`, `parse_claim_grants`, `load_acl` (kept).

- [ ] **Step 1: Delete bespoke code from `_authorization.py`**

Remove entirely: the `Authorizer` type alias, `AuthzDenied`, `_current_authorizer` ContextVar, `set_current_authorizer`, `check_authorization`, `make_acl_authorizer`, and the `AuthorizationMiddleware` class. After this edit the module contains only: module docstring (update it to describe native checks), imports, `logger`, `load_acl`, `parse_claim_grants`, `_resolve_required`, `_subject_of`, `_extract_claim_values`, `make_acl_check`, `make_claims_check`, `any_check`. Drop now-unused imports (`json` is still used by `parse_claim_grants`; remove `Callable`, `ContextVar`, `PromptError`/`ResourceError`/`ToolError`, `Middleware`/`MiddlewareContext`, `TypeAlias` if no longer referenced).

- [ ] **Step 2: Rewrite the `__init__.py` re-exports**

In `src/fastmcp_pvl_core/__init__.py`, change the import block (lines ~17-23) from:

```python
from fastmcp_pvl_core._authorization import (
    AuthorizationMiddleware,
    Authorizer,
    AuthzDenied,
    check_authorization,
    load_acl,
    make_acl_authorizer,
)
```

to:

```python
from fastmcp_pvl_core._authorization import (
    any_check,
    load_acl,
    make_acl_check,
    make_claims_check,
    parse_claim_grants,
)
```

In `__all__`, remove `"AuthorizationMiddleware"`, `"Authorizer"`, `"AuthzDenied"`, `"check_authorization"`, `"make_acl_authorizer"`; add `"any_check"`, `"make_acl_check"`, `"make_claims_check"`, `"parse_claim_grants"` (keep `"load_acl"`). Maintain alphabetical ordering of `__all__`.

- [ ] **Step 3: Update `conftest.py`**

Remove the `set_current_authorizer` import (line 11) and the entire `_reset_authorizer` fixture (lines ~31-39). Leave `_reset_auth_mode` and `_fastmcp_logger_propagates` intact.

- [ ] **Step 4: Delete obsolete test files and spec**

```bash
git rm tests/test_authorization_authorizer.py tests/test_authorization_check.py tests/test_authorization_middleware.py docs/specs/authorization-submodule.md
```

- [ ] **Step 5: Rewrite the README Authorization section**

Replace `README.md` lines 253-308 (the `### Authorization (opt-in) — \`AuthorizationMiddleware\`` block through the `docs/specs/authorization-submodule.md` link) with:

````markdown
### Authorization (opt-in) — native auth checks

pvl-core builds on FastMCP's native authorization (`AuthCheck` +
`AuthMiddleware`). It ships factories for the two checks the framework
has no built-in for — subject→scope (the only per-token authz available
in bearer modes) and claim→scope (group/role authz for OIDC modes) —
plus an OR-combinator for `multi` mode. Scope- and tag-based patterns
use FastMCP's own `require_scopes` / `restrict_tag`.

Components opt in with `meta={"required_scope": "<scope>"}`; the checks
read it. Components without it are unrestricted.

```python
import os
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.server.middleware import AuthMiddleware
from fastmcp_pvl_core import (
    make_acl_check, make_claims_check, any_check, load_acl, parse_claim_grants,
)

# OIDC mode — claim-based (identity: name IdP groups to match scopes)
mcp = FastMCP(..., middleware=[AuthMiddleware(auth=make_claims_check("groups"))])

# bearer mode — static subject ACL
mcp = FastMCP(..., middleware=[AuthMiddleware(auth=make_acl_check(load_acl(Path("/etc/my-app/acl.toml"))))])

# multi mode — OR of both
raw = os.environ.get("MY_APP_AUTHZ_GRANTS")
grants = parse_claim_grants(raw) if raw else None
mcp = FastMCP(..., middleware=[AuthMiddleware(auth=any_check(
    make_acl_check(load_acl(Path("/etc/my-app/acl.toml"))),
    make_claims_check(os.environ["MY_APP_AUTHZ_CLAIM"], grants),
))])

@mcp.tool(meta={"required_scope": "write"})
async def edit_document(...): ...
```

ACL TOML schema (`load_acl`) and inline-JSON grants (`parse_claim_grants`):

```toml
[subjects]
"user:alice@example.com" = ["read", "write"]
"user:admin@example.com" = ["*"]          # wildcard scope
```

```json
{"app-writers": ["read", "write"], "app-admins": ["*"]}
```

Key properties:

- **Claim vs scope.** Claim-based authz reads OIDC *claims* (`groups`,
  `roles`) — the user's IdP-issued permissions — not OAuth *scopes*
  (which describe the client/token grant). Bearer tokens carry no usable
  claims, so use `make_acl_check` there.
- **Opt-in per component** via `meta["required_scope"]`; absent ⇒
  unrestricted.
- **`*` is the only special scope** ("any required scope passes").
- **Loaders fail fast** with `ConfigurationError`; never silent denial.
- **Loaded once at startup.** Restart to pick up changes.
- **stdio/`none` mode skips checks** (no token) — authz is meaningful
  only under an `AuthProvider`.
````

- [ ] **Step 6: Run the full suite + removal verification + gates**

```bash
uv sync --all-extras
uv run pytest
# Removal verification — each must print nothing and exit 0:
! rg -n 'AuthorizationMiddleware|AuthzDenied|check_authorization|make_acl_authorizer|set_current_authorizer|_current_authorizer|expose_subject_in_error|authz_denied' src tests
! rg -n '\bAuthorizer\b' src tests
! test -e docs/specs/authorization-submodule.md
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: all tests pass; the three removal checks succeed (symbols gone, spec gone); ruff and mypy clean.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(authorization)!: rebuild on FastMCP-native auth checks

Replace the bespoke AuthorizationMiddleware + Authorizer/ACL with thin
factories over FastMCP 3.3 native AuthChecks (make_acl_check,
make_claims_check, any_check; keep load_acl, add parse_claim_grants).
Delete AuthorizationMiddleware, AuthzDenied, check_authorization,
the Authorizer alias, make_acl_authorizer, and the deny envelope.
Wiring is now native AuthMiddleware(auth=...). _subject.py is unchanged.

BREAKING CHANGE: removes AuthorizationMiddleware, AuthzDenied,
check_authorization, Authorizer, and make_acl_authorizer. Downstream
wires authz via fastmcp.server.middleware.AuthMiddleware(auth=...) with
make_acl_check / make_claims_check.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- `make_acl_check` (subject→scope, `sub`→`client_id`, bearer) → Task 1 ✅
- `make_claims_check` (claim→scope, identity/translation, extraction matrix) → Task 2 ✅
- `any_check` (OR, multi mode) → Task 3 ✅
- `parse_claim_grants` (strict inline-JSON) → Task 2 ✅
- `load_acl` kept → unchanged, `test_authorization_loader.py` retained ✅
- `meta["required_scope"]` convention survives (read by checks) → `_resolve_required`, Task 1 ✅
- Deletions + removal verification → Task 4 (Steps 1-4, 6) ✅
- `_subject.py` untouched → no task modifies it ✅
- stdio/none behavior change → README key property (Task 4 Step 5); design captured in spec ✅
- Native wiring + per-mode coverage docs → README (Task 4 Step 5) ✅
- Major version (BREAKING CHANGE footer) → Task 4 Step 7 ✅
- Template-side stub issues → tracked in spec; filed at land time (not a code task) ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows assertions. ✅

**Type consistency:** `make_acl_check`/`make_claims_check`/`any_check`/`parse_claim_grants`/`_resolve_required`/`_subject_of`/`_extract_claim_values` signatures are identical across the task that defines them and the Interfaces blocks that reference them. ✅
