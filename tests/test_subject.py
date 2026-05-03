# tests/test_subject.py
"""Tests for get_subject helper."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from fastmcp_pvl_core import (
    ServerConfig,
    build_auth,
    get_subject,
)


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
def patch_get_access_token():
    """Patch FastMCP's get_access_token to return a controllable value."""

    def _set(token):
        return patch("fastmcp_pvl_core._subject.get_access_token", return_value=token)

    return _set


class TestGetSubjectAuthModeNone:
    def test_returns_local_when_no_token_and_mode_none(self, patch_get_access_token):
        # Simulate auth_mode=none after build_auth
        build_auth(ServerConfig())
        with patch_get_access_token(None):
            assert get_subject() == "local"


class TestGetSubjectBearerSingle:
    def test_returns_default_subject_from_client_id(
        self, patch_get_access_token, tmp_path
    ):
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


class TestGetSubjectMissing:
    def test_returns_none_when_no_token_and_auth_configured(
        self, patch_get_access_token
    ):
        # Simulate any auth mode != "none"
        build_auth(ServerConfig(bearer_token="t"))
        with patch_get_access_token(None):
            assert get_subject() is None
