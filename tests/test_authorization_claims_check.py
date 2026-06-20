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


def test_star_grants_key_rejected_at_construction() -> None:
    # A hand-built grants dict must not be able to map a literal "*" claim
    # value to scopes (would mirror the escalation parse_claim_grants blocks).
    with pytest.raises(ValueError, match="not a valid grants key"):
        make_claims_check("groups", {"*": frozenset({"admin"})})


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


def test_star_claim_value_does_not_grant_universal_access() -> None:
    # Identity mode: a literal "*" in the claim must NOT trigger the
    # wildcard — that sentinel is honoured only from operator grants.
    check = make_claims_check("groups")
    ctx = _ctx({"groups": ["*"]}, {"required_scope": "write"})
    assert check(ctx) is False


def test_star_grant_value_still_grants_via_operator_table() -> None:
    # Translation mode: "*" supplied by the operator grants table IS the
    # wildcard and passes any required scope.
    check = make_claims_check("groups", {"admins": frozenset({"*"})})
    ctx = _ctx({"groups": ["admins"]}, {"required_scope": "write"})
    assert check(ctx) is True


def test_token_present_but_claims_not_a_dict_denies() -> None:
    # A token exists but its `.claims` is None (some providers) — the
    # non-dict guard yields no values, so a scoped component is denied.
    check = make_claims_check("groups")
    token = _FakeToken(claims=None)  # type: ignore[arg-type]
    ctx = AuthContext(
        token=token, component=_FakeComponent(meta={"required_scope": "read"})
    )
    assert check(ctx) is False
