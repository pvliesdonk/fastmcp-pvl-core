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


def _ctx(
    claims: dict[str, Any] | None, meta: dict[str, Any] | None = None
) -> AuthContext:
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
