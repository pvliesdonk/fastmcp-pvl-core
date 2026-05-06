"""Tests for AuthzDenied and check_authorization (added in later tasks)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fastmcp_pvl_core._authorization import (
    AuthzDenied,
    check_authorization,
    set_current_authorizer,
)


def test_authz_denied_carries_subject_and_required_scope() -> None:
    exc = AuthzDenied(subject="user:alice@example.com", required_scope="write")
    assert exc.subject == "user:alice@example.com"
    assert exc.required_scope == "write"


def test_authz_denied_subject_can_be_none() -> None:
    exc = AuthzDenied(subject=None, required_scope="read")
    assert exc.subject is None
    assert exc.required_scope == "read"


def test_authz_denied_message_contains_subject_and_scope() -> None:
    exc = AuthzDenied(subject="user:alice@example.com", required_scope="write")
    msg = str(exc)
    assert "user:alice@example.com" in msg
    assert "write" in msg


def test_authz_denied_is_an_exception() -> None:
    with pytest.raises(AuthzDenied):
        raise AuthzDenied(subject="x", required_scope="y")


def _allow_all(_subject: str | None, _required_scope: str) -> bool:
    return True


def _deny_all(_subject: str | None, _required_scope: str) -> bool:
    return False


def test_check_authorization_uses_explicit_authorizer_allow() -> None:
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        check_authorization("read", authorizer=_allow_all)


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
        check_authorization("read")


def test_check_authorization_explicit_overrides_ambient() -> None:
    set_current_authorizer(_allow_all)
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(AuthzDenied):
            check_authorization("read", authorizer=_deny_all)


def test_check_authorization_no_authorizer_anywhere_raises_runtime_error() -> None:
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
    captured: dict[str, object] = {}

    def authorize(subject: str | None, required_scope: str) -> bool:
        captured["subject"] = subject
        return True

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:from_context"
    ):
        check_authorization("read", authorizer=authorize)
    assert captured == {"subject": "user:from_context"}


def test_check_authorization_explicit_subject_none_uses_get_subject() -> None:
    """Explicit ``subject=None`` is equivalent to omitting the kwarg."""
    captured: dict[str, object] = {}

    def authorize(subject: str | None, required_scope: str) -> bool:
        captured["subject"] = subject
        return True

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:from_context"
    ):
        check_authorization("read", authorizer=authorize, subject=None)
    assert captured == {"subject": "user:from_context"}


def test_check_authorization_strips_required_scope() -> None:
    captured: dict[str, object] = {}

    def authorize(_subject: str | None, required_scope: str) -> bool:
        captured["required_scope"] = required_scope
        return True

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        check_authorization("  write  ", authorizer=authorize)
    assert captured == {"required_scope": "write"}


def test_check_authorization_empty_required_scope_raises_value_error() -> None:
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ValueError, match="non-empty"):
            check_authorization("   ", authorizer=_allow_all)


def test_check_authorization_get_subject_returning_none_denied_by_authorizer() -> None:
    with patch("fastmcp_pvl_core._authorization.get_subject", return_value=None):
        with pytest.raises(AuthzDenied) as exc_info:
            check_authorization("read", authorizer=_deny_all)
    assert exc_info.value.subject is None
