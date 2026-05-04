"""Tests for build_auth dispatcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fastmcp_pvl_core import ServerConfig, build_auth


def _oidc_proxy_config(**overrides: object) -> ServerConfig:
    """Fully-populated OIDC proxy config."""
    base: dict[str, object] = {
        "base_url": "https://mcp.example.com",
        "oidc_config_url": "https://idp.example/.well-known/openid-configuration",
        "oidc_client_id": "test-client",
        "oidc_client_secret": "test-secret",
    }
    base.update(overrides)
    return ServerConfig(**base)  # type: ignore[arg-type]


def _remote_only_config(**overrides: object) -> ServerConfig:
    """Config that triggers remote mode (no client_id/secret)."""
    base: dict[str, object] = {
        "base_url": "https://mcp.example.com",
        "oidc_config_url": "https://idp.example/.well-known/openid-configuration",
    }
    base.update(overrides)
    return ServerConfig(**base)  # type: ignore[arg-type]


def _mock_discovery() -> MagicMock:
    """MagicMock httpx.Response with a valid OIDC discovery payload."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "jwks_uri": "https://idp.example/jwks",
        "issuer": "https://idp.example/",
    }
    return resp


class TestBuildAuth:
    def test_none_when_no_auth_configured(self):
        assert build_auth(ServerConfig()) is None

    def test_returns_bearer_verifier_alone(self):
        from fastmcp.server.auth import StaticTokenVerifier

        auth = build_auth(ServerConfig(bearer_token="x"))
        assert isinstance(auth, StaticTokenVerifier)

    def test_returns_oidc_proxy_alone(self):
        # OIDCProxy reaches out to config_url at construction time; stub
        # the class so we only exercise the dispatcher.
        mock_proxy_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_proxy_cls):
            auth = build_auth(_oidc_proxy_config())
        assert auth is mock_proxy_cls.return_value
        mock_proxy_cls.assert_called_once()

    def test_returns_remote_auth_alone(self):
        from fastmcp.server.auth import RemoteAuthProvider

        with patch("httpx.get", return_value=_mock_discovery()):
            auth = build_auth(_remote_only_config())
        assert isinstance(auth, RemoteAuthProvider)

    def test_returns_multi_auth_with_empty_required_scopes(self):
        """The load-bearing invariant: required_scopes=[] in multi mode."""
        from fastmcp.server.auth import MultiAuth

        mock_proxy_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_proxy_cls):
            auth = build_auth(_oidc_proxy_config(bearer_token="x"))
        assert isinstance(auth, MultiAuth)
        # CRITICAL: required_scopes must be [] to prevent OIDC's ["openid"]
        # from propagating to RequireAuthMiddleware and rejecting bearer
        # tokens with 403 insufficient_scope (MV PR #249).
        assert auth.required_scopes == []

    def test_multi_places_oidcproxy_as_server_not_verifier(self):
        """OIDCProxy MUST live in server=, not verifiers=."""
        from fastmcp.server.auth import MultiAuth, StaticTokenVerifier

        mock_proxy_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_proxy_cls):
            auth = build_auth(_oidc_proxy_config(bearer_token="x"))
        assert isinstance(auth, MultiAuth)
        # Bearer verifier lives in verifiers=.
        assert any(isinstance(v, StaticTokenVerifier) for v in auth.verifiers)
        # OIDCProxy (the mock instance) lives in server=.
        assert auth.server is mock_proxy_cls.return_value
        # And is NOT in verifiers=.
        assert mock_proxy_cls.return_value not in auth.verifiers

    def test_multi_with_bearer_and_remote(self):
        """Multi mode also works when the OIDC side is RemoteAuthProvider."""
        from fastmcp.server.auth import MultiAuth, RemoteAuthProvider

        with patch("httpx.get", return_value=_mock_discovery()):
            auth = build_auth(_remote_only_config(bearer_token="x"))
        assert isinstance(auth, MultiAuth)
        assert auth.required_scopes == []
        assert isinstance(auth.server, RemoteAuthProvider)

    def test_multi_hard_fails_when_oidc_discovery_fails(self):
        """OIDC discovery failure in multi mode must abort startup.

        Regression guard for issue #41: the previous behaviour was to
        log a ``multi_auth_degraded`` warning and silently fall back
        to bearer-only — a security-relevant silent failure where the
        operator believes OIDC is enforcing identity but it is not.
        """
        import httpx

        from fastmcp_pvl_core import ConfigurationError

        def _boom(*_args: object, **_kw: object) -> MagicMock:
            raise httpx.ConnectError("boom")

        with patch("httpx.get", side_effect=_boom):
            with pytest.raises(ConfigurationError, match="discovery"):
                build_auth(_remote_only_config(bearer_token="x"))

    def test_multi_hard_fails_when_bearer_builder_returns_none(self):
        """Defense-in-depth: bearer_auth=None in multi mode → ConfigurationError.

        Currently unreachable via ``build_auth`` because
        ``resolve_auth_mode`` only picks ``"multi"`` when bearer config is
        present, so ``build_bearer_auth`` is guaranteed to return a
        verifier.  This test forces the dispatcher into the multi branch
        with no bearer config (via a mocked resolver) to lock in the
        contract that the explicit ``bearer_auth is None`` guard fires
        rather than silently constructing a half-auth ``MultiAuth``.
        Catches a future refactor that desyncs the resolver from the
        builder preconditions.
        """
        from fastmcp_pvl_core import ConfigurationError

        cfg = _oidc_proxy_config()  # OIDC fully configured, no bearer
        mock_proxy_cls = MagicMock()
        with (
            patch("fastmcp_pvl_core._auth.resolve_auth_mode", return_value="multi"),
            patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_proxy_cls),
            pytest.raises(ConfigurationError, match="bearer"),
        ):
            build_auth(cfg)


class TestBuildAuthMapped:
    def test_returns_verifier_in_bearer_mapped_mode(self, tmp_path: Path) -> None:
        from fastmcp.server.auth import StaticTokenVerifier

        token_file = tmp_path / "tokens.toml"
        token_file.write_text('[tokens]\n"k1" = "user:alice"\n', encoding="utf-8")
        auth = build_auth(ServerConfig(bearer_tokens_file=token_file))
        assert isinstance(auth, StaticTokenVerifier)
        assert auth.tokens["k1"]["client_id"] == "user:alice"


class TestBuildAuthMultiWithMapped:
    def test_multi_with_mapped_bearer_and_oidc_proxy(self, tmp_path):
        from fastmcp.server.auth import MultiAuth, StaticTokenVerifier

        token_file = tmp_path / "tokens.toml"
        token_file.write_text('[tokens]\n"k1" = "user:alice"\n', encoding="utf-8")
        cfg = ServerConfig(
            bearer_tokens_file=token_file,
            base_url="https://x.example",
            oidc_config_url=("https://idp.example/.well-known/openid-configuration"),
            oidc_client_id="cid",
            oidc_client_secret="csecret",
        )
        mock_proxy_cls = MagicMock()
        with patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_proxy_cls):
            auth = build_auth(cfg)
        assert isinstance(auth, MultiAuth)
        assert auth.required_scopes == []
        bearer = next(v for v in auth.verifiers if isinstance(v, StaticTokenVerifier))
        assert bearer.tokens["k1"]["client_id"] == "user:alice"

    def test_multi_warns_when_both_bearer_inputs_set(self, tmp_path, caplog):
        import logging

        token_file = tmp_path / "tokens.toml"
        token_file.write_text('[tokens]\n"k1" = "user:alice"\n', encoding="utf-8")
        cfg = ServerConfig(
            bearer_token="single-token",
            bearer_tokens_file=token_file,
            base_url="https://x.example",
            oidc_config_url=("https://idp.example/.well-known/openid-configuration"),
            oidc_client_id="cid",
            oidc_client_secret="csecret",
        )
        mock_proxy_cls = MagicMock()
        with (
            caplog.at_level(logging.WARNING),
            patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", mock_proxy_cls),
        ):
            build_auth(cfg)
        assert any(
            "bearer_tokens_file_takes_precedence" in r.message
            and r.levelname == "WARNING"
            for r in caplog.records
        )

    def test_multi_with_mapped_bearer_hard_fails_when_oidc_discovery_fails(
        self, tmp_path, monkeypatch
    ):
        """Same hard-fail as the single-bearer case — across both bearer flavors.

        The audit's silent-failure-hunter flagged this exact scenario
        (mapped bearer + remote OIDC, discovery fails) as the canonical
        operator foot-gun: the deployment looks fine, audit logs show
        per-user mapped subjects from the bearer side, but OIDC isn't
        actually enforcing anything.  Hard-fail at startup instead.
        """
        import httpx

        from fastmcp_pvl_core import ConfigurationError

        def _raise(*a, **kw):
            raise httpx.ConnectError("network down")

        monkeypatch.setattr(httpx, "get", _raise)

        token_file = tmp_path / "tokens.toml"
        token_file.write_text('[tokens]\n"k1" = "user:alice"\n', encoding="utf-8")
        cfg = ServerConfig(
            bearer_tokens_file=token_file,
            base_url="https://x.example",
            oidc_config_url=("https://idp.example/.well-known/openid-configuration"),
            # Only base_url + oidc_config_url set → remote mode (not proxy)
        )
        with pytest.raises(ConfigurationError, match="discovery"):
            build_auth(cfg)
