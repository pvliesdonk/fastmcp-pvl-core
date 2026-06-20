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
    ctx = _ctx(
        _FakeToken(client_id="user:alice", claims={}), {"required_scope": "write"}
    )
    assert check(ctx) is True


def test_no_token_denies() -> None:
    check = make_acl_check({"user:alice": frozenset({"write"})})
    assert check(_ctx(None, {"required_scope": "write"})) is False


def test_no_required_scope_meta_is_unrestricted() -> None:
    check = make_acl_check({})  # empty ACL would deny everyone if a scope were required
    ctx = _ctx(_FakeToken(claims={"sub": "user:bob"}), meta={})
    assert check(ctx) is True


def test_unrestricted_component_allowed_without_token() -> None:
    # No required_scope => unrestricted regardless of caller, even with no token.
    check = make_acl_check({})
    assert check(_ctx(None, meta={})) is True


def test_invalid_meta_treated_unrestricted(caplog: pytest.LogCaptureFixture) -> None:
    check = make_acl_check({})
    ctx = _ctx(_FakeToken(claims={"sub": "x"}), {"required_scope": "   "})
    with caplog.at_level("WARNING", logger="fastmcp_pvl_core._authorization"):
        assert check(ctx) is True
    assert "authz_meta_invalid" in caplog.text
