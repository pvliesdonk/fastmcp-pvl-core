"""Tests for get_subject helper."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import patch

import pytest

from fastmcp_pvl_core import (
    ServerConfig,
    build_auth,
    get_subject,
)
from fastmcp_pvl_core._subject import set_current_auth_mode


@pytest.fixture(autouse=True)
def _reset_auth_mode() -> Iterator[None]:
    """Reset the process-global auth-mode pointer between tests.

    Without this, a test that doesn't call ``build_auth`` would observe
    the mode set by whichever earlier test ran last — order-dependent
    flakes are easy to introduce as the suite grows.
    """
    set_current_auth_mode(None)
    yield
    set_current_auth_mode(None)


class _FakeAccessToken:
    """Minimal stand-in for fastmcp.server.auth.AccessToken."""

    def __init__(
        self,
        client_id: str | None = None,
        claims: dict[str, Any] | None = None,
    ) -> None:
        self.client_id = client_id
        self.claims = claims or {}


@pytest.fixture
def patch_get_access_token() -> Callable[
    [_FakeAccessToken | None], AbstractContextManager[Any]
]:
    """Patch FastMCP's get_access_token to return a controllable value."""

    def _set(token: _FakeAccessToken | None) -> AbstractContextManager[Any]:
        return patch("fastmcp_pvl_core._subject.get_access_token", return_value=token)

    return _set


class TestGetSubjectAuthModeNone:
    def test_returns_local_when_no_token_and_mode_none(self, patch_get_access_token):
        # Simulate auth_mode=none after build_auth
        build_auth(ServerConfig())
        with patch_get_access_token(None):
            assert get_subject() == "local"


class TestGetSubjectBearerSingle:
    def test_returns_default_subject_from_client_id(self, patch_get_access_token):
        cfg = ServerConfig(bearer_token="x", bearer_default_subject="bearer-anon")
        build_auth(cfg)
        with patch_get_access_token(_FakeAccessToken(client_id="bearer-anon")):
            assert get_subject() == "bearer-anon"


class TestGetSubjectBearerMapped:
    def test_returns_mapped_subject(self, patch_get_access_token, tmp_path):
        token_file = tmp_path / "tokens.toml"
        token_file.write_text(
            '[tokens]\n"k1" = "user:alice@example.com"\n', encoding="utf-8"
        )
        cfg = ServerConfig(bearer_tokens_file=token_file)
        build_auth(cfg)
        with patch_get_access_token(
            _FakeAccessToken(client_id="user:alice@example.com")
        ):
            assert get_subject() == "user:alice@example.com"


class TestGetSubjectOIDC:
    def test_returns_sub_claim(self, patch_get_access_token):
        # Mode pointer is irrelevant here — claims["sub"] always wins.
        with patch_get_access_token(
            _FakeAccessToken(
                client_id="oidc-client-x",
                claims={"sub": "user:bob@example.com"},
            )
        ):
            assert get_subject() == "user:bob@example.com"

    def test_falls_back_to_client_id_when_sub_missing(self, patch_get_access_token):
        with patch_get_access_token(
            _FakeAccessToken(client_id="oidc-client-x", claims={})
        ):
            assert get_subject() == "oidc-client-x"

    def test_empty_sub_falls_back_to_client_id(self, patch_get_access_token):
        # ``isinstance(sub, str) and sub`` rejects empty strings; the
        # fallback to ``client_id`` must engage.
        with patch_get_access_token(
            _FakeAccessToken(client_id="oidc-client-x", claims={"sub": ""})
        ):
            assert get_subject() == "oidc-client-x"

    def test_returns_none_when_sub_and_client_id_both_empty(
        self, patch_get_access_token
    ):
        with patch_get_access_token(_FakeAccessToken(client_id="", claims={"sub": ""})):
            assert get_subject() is None

    def test_returns_none_when_sub_missing_and_client_id_none(
        self, patch_get_access_token
    ):
        with patch_get_access_token(_FakeAccessToken(client_id=None, claims={})):
            assert get_subject() is None


class TestGetSubjectMissing:
    def test_returns_none_when_no_token_and_auth_configured(
        self, patch_get_access_token
    ):
        # Simulate any auth mode != "none"
        build_auth(ServerConfig(bearer_token="t"))
        with patch_get_access_token(None):
            assert get_subject() is None


# Note: ``multi`` mode is not exercised here. When a token is present,
# the bearer-vs-OIDC distinction flows through the token's ``claims`` and
# ``client_id`` (not the resolved auth mode), so the OIDC and bearer
# tests above already cover both runtime paths in ``multi`` deployments.
