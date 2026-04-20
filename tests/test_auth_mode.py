"""Tests for resolve_auth_mode."""

from __future__ import annotations

from fastmcp_pvl_core import ServerConfig, resolve_auth_mode


def _cfg(**kwargs) -> ServerConfig:
    return ServerConfig(**kwargs)


class TestResolveAuthMode:
    def test_none_when_no_auth_configured(self):
        assert resolve_auth_mode(_cfg()) == "none"

    def test_bearer_only(self):
        assert resolve_auth_mode(_cfg(bearer_token="x")) == "bearer"

    def test_oidc_proxy_when_all_four_oidc_vars_set(self):
        assert (
            resolve_auth_mode(
                _cfg(
                    base_url="https://x",
                    oidc_config_url="https://idp/.well-known/openid-configuration",
                    oidc_client_id="cid",
                    oidc_client_secret="csecret",
                )
            )
            == "oidc-proxy"
        )

    def test_remote_when_only_base_url_and_config_url(self):
        assert (
            resolve_auth_mode(
                _cfg(
                    base_url="https://x",
                    oidc_config_url="https://idp/.well-known/openid-configuration",
                )
            )
            == "remote"
        )

    def test_multi_when_bearer_and_oidc_proxy(self):
        assert (
            resolve_auth_mode(
                _cfg(
                    bearer_token="x",
                    base_url="https://x",
                    oidc_config_url="https://idp/.well-known/openid-configuration",
                    oidc_client_id="cid",
                    oidc_client_secret="csecret",
                )
            )
            == "multi"
        )

    def test_multi_when_bearer_and_remote(self):
        assert (
            resolve_auth_mode(
                _cfg(
                    bearer_token="x",
                    base_url="https://x",
                    oidc_config_url="https://idp/.well-known/openid-configuration",
                )
            )
            == "multi"
        )


class TestExplicitOverride:
    def test_override_bearer_with_no_fields_set(self):
        # Even with no fields configured, an explicit override returns that mode.
        assert resolve_auth_mode(_cfg(auth_mode="bearer")) == "bearer"

    def test_override_none_when_bearer_configured(self):
        # Explicit override trumps auto-detection.
        assert resolve_auth_mode(_cfg(auth_mode="none", bearer_token="x")) == "none"

    def test_override_remote_when_all_four_oidc_vars_set(self):
        # Explicit 'remote' beats auto-detected 'oidc-proxy'.
        assert (
            resolve_auth_mode(
                _cfg(
                    auth_mode="remote",
                    base_url="https://x",
                    oidc_config_url="https://idp/.well-known/openid-configuration",
                    oidc_client_id="cid",
                    oidc_client_secret="csecret",
                )
            )
            == "remote"
        )

    def test_override_is_case_insensitive_and_trims(self):
        assert resolve_auth_mode(_cfg(auth_mode="  OIDC-PROXY  ")) == "oidc-proxy"

    def test_unknown_override_falls_back_to_auto_detection(self):
        # Unknown override string is ignored; auto-detect still runs.
        assert resolve_auth_mode(_cfg(auth_mode="bogus", bearer_token="x")) == "bearer"

    def test_blank_override_falls_back_to_auto_detection(self):
        assert resolve_auth_mode(_cfg(auth_mode="   ")) == "none"
