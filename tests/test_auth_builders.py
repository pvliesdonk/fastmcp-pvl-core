"""Tests for individual auth builders."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from fastmcp_pvl_core import (
    ServerConfig,
    build_bearer_auth,
    build_oidc_proxy_auth,
    build_remote_auth,
)


def _oidc_config(**overrides: object) -> ServerConfig:
    """Build a fully-populated OIDC proxy config with optional overrides."""
    base: dict[str, object] = {
        "base_url": "https://mcp.example.com",
        "oidc_config_url": "https://idp.example/.well-known/openid-configuration",
        "oidc_client_id": "test-client",
        "oidc_client_secret": "test-secret",
    }
    base.update(overrides)
    return ServerConfig(**base)  # type: ignore[arg-type]


class TestBuildBearerAuth:
    def test_returns_none_when_no_token(self):
        assert build_bearer_auth(ServerConfig()) is None

    def test_returns_none_when_token_is_whitespace(self):
        assert build_bearer_auth(ServerConfig(bearer_token="   ")) is None

    def test_returns_verifier_when_token_set(self):
        from fastmcp.server.auth import StaticTokenVerifier

        auth = build_bearer_auth(ServerConfig(bearer_token="secret"))
        assert isinstance(auth, StaticTokenVerifier)

    def test_token_mapped_with_read_write_scopes(self):
        auth = build_bearer_auth(ServerConfig(bearer_token="secret"))
        assert auth is not None
        # StaticTokenVerifier exposes a ``tokens`` dict for introspection.
        entry = auth.tokens["secret"]
        assert entry["client_id"] == "bearer-anon"
        assert entry["scopes"] == ["read", "write"]


class TestBuildOIDCProxyAuth:
    def test_returns_none_when_all_vars_missing(self):
        assert build_oidc_proxy_auth(ServerConfig()) is None

    def test_returns_none_when_base_url_only(self):
        assert build_oidc_proxy_auth(ServerConfig(base_url="https://x")) is None

    def test_returns_none_when_secret_missing(self):
        cfg = ServerConfig(
            base_url="https://x.example",
            oidc_config_url="https://idp.example/.well-known/openid-configuration",
            oidc_client_id="cid",
        )
        assert build_oidc_proxy_auth(cfg) is None

    def test_returns_proxy_when_all_vars_set(self):
        mock_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_cls):
            auth = build_oidc_proxy_auth(_oidc_config())
        assert auth is mock_cls.return_value
        mock_cls.assert_called_once()

    def test_passes_correct_kwargs(self):
        mock_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_cls):
            build_oidc_proxy_auth(_oidc_config())
        kw = mock_cls.call_args.kwargs
        assert kw["base_url"] == "https://mcp.example.com"
        assert kw["client_id"] == "test-client"
        assert kw["client_secret"] == "test-secret"
        assert kw["config_url"] == (
            "https://idp.example/.well-known/openid-configuration"
        )
        assert kw["required_scopes"] == ["openid"]
        assert kw["verify_id_token"] is True
        assert kw["require_authorization_consent"] is False

    def test_defaults_required_scopes_to_openid(self):
        mock_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_cls):
            build_oidc_proxy_auth(_oidc_config())
        assert mock_cls.call_args.kwargs["required_scopes"] == ["openid"]

    def test_custom_scopes_passed_through(self):
        mock_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_cls):
            build_oidc_proxy_auth(
                _oidc_config(oidc_required_scopes=("openid", "profile"))
            )
        assert mock_cls.call_args.kwargs["required_scopes"] == [
            "openid",
            "profile",
        ]

    def test_verify_id_token_false_when_access_token_opt_in(self):
        mock_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_cls):
            build_oidc_proxy_auth(_oidc_config(oidc_verify_access_token=True))
        assert mock_cls.call_args.kwargs["verify_id_token"] is False

    def test_scope_warning_when_verify_id_token_without_openid(
        self, caplog: pytest.LogCaptureFixture
    ):
        mock_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_cls):
            build_oidc_proxy_auth(_oidc_config(oidc_required_scopes=("profile",)))
        assert any(
            "scope_warning" in r.message and r.levelname == "WARNING"
            for r in caplog.records
        )

    def test_linux_ephemeral_key_warning(self, caplog: pytest.LogCaptureFixture):
        mock_cls = MagicMock()
        with (
            patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_cls),
            patch("fastmcp_pvl_core._auth.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            build_oidc_proxy_auth(_oidc_config())
        assert any(
            "ephemeral_signing_key" in r.message and r.levelname == "WARNING"
            for r in caplog.records
        )


class TestBuildRemoteAuth:
    def test_returns_none_when_config_missing(self):
        assert build_remote_auth(ServerConfig()) is None

    def test_returns_none_when_only_base_url(self):
        assert build_remote_auth(ServerConfig(base_url="https://x")) is None

    def test_raises_configuration_error_when_httpx_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from fastmcp_pvl_core import ConfigurationError

        monkeypatch.setitem(sys.modules, "httpx", None)
        with pytest.raises(ConfigurationError, match="remote-auth"):
            build_remote_auth(
                ServerConfig(
                    base_url="https://x",
                    oidc_config_url="https://idp/.well-known/openid-configuration",
                )
            )

    def test_returns_provider_when_discovery_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import httpx
        from fastmcp.server.auth import RemoteAuthProvider

        class _StubResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return {
                    "jwks_uri": "https://idp.example/jwks.json",
                    "issuer": "https://idp.example/",
                }

        def _fake_get(url: str, timeout: int = 10) -> _StubResponse:
            return _StubResponse()

        monkeypatch.setattr(httpx, "get", _fake_get)
        auth = build_remote_auth(
            ServerConfig(
                base_url="https://x.example",
                oidc_config_url=(
                    "https://idp.example/.well-known/openid-configuration"
                ),
            )
        )
        assert isinstance(auth, RemoteAuthProvider)

    def test_raises_when_discovery_missing_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import httpx

        from fastmcp_pvl_core import ConfigurationError

        class _StubResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return {}  # missing jwks_uri/issuer

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _StubResponse())
        with pytest.raises(ConfigurationError, match="incomplete"):
            build_remote_auth(
                ServerConfig(
                    base_url="https://x.example",
                    oidc_config_url=(
                        "https://idp.example/.well-known/openid-configuration"
                    ),
                )
            )

    def test_raises_when_discovery_raises(self, monkeypatch: pytest.MonkeyPatch):
        import httpx

        from fastmcp_pvl_core import ConfigurationError

        def _raise(*a, **kw):
            raise httpx.ConnectError("network down")

        monkeypatch.setattr(httpx, "get", _raise)
        with pytest.raises(ConfigurationError, match="discovery"):
            build_remote_auth(
                ServerConfig(
                    base_url="https://x.example",
                    oidc_config_url=(
                        "https://idp.example/.well-known/openid-configuration"
                    ),
                )
            )

    def test_raises_when_discovery_json_malformed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import httpx

        from fastmcp_pvl_core import ConfigurationError

        class _BadJSONResp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> object:
                raise ValueError("not json")

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _BadJSONResp())
        with pytest.raises(ConfigurationError, match="discovery"):
            build_remote_auth(
                ServerConfig(
                    base_url="https://x.example",
                    oidc_config_url=(
                        "https://idp.example/.well-known/openid-configuration"
                    ),
                )
            )
