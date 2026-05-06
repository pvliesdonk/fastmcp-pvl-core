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
    acl["user:bob"] = frozenset({"read"})
    assert authorize("user:bob", "read") is True


def test_local_subject_treated_as_normal_subject() -> None:
    """Spec checklist: ``"local"`` works exactly like any other subject."""
    authorize = make_acl_authorizer({"local": frozenset({"read", "write"})})
    assert authorize("local", "read") is True
    assert authorize("local", "write") is True
    assert authorize("local", "admin") is False
    # And not in ACL → denied like any unknown subject.
    other = make_acl_authorizer({"user:alice": frozenset({"read"})})
    assert other("local", "read") is False
