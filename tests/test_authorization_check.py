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
