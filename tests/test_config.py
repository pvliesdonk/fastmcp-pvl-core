"""Tests for ServerConfig."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core import ServerConfig


class TestServerConfigDefaults:
    def test_default_transport_is_stdio(self):
        config = ServerConfig()
        assert config.transport == "stdio"

    def test_default_host_port(self):
        config = ServerConfig()
        assert config.host == "127.0.0.1"
        assert config.port == 8000

    def test_auth_fields_default_to_none(self):
        config = ServerConfig()
        assert config.bearer_token is None
        assert config.oidc_config_url is None
        assert config.oidc_client_id is None
        assert config.oidc_required_scopes == ()

    def test_bearer_tokens_file_defaults_to_none(self):
        assert ServerConfig().bearer_tokens_file is None

    def test_bearer_default_subject_default(self):
        assert ServerConfig().bearer_default_subject == "bearer-anon"

    def test_blank_bearer_default_subject_normalises_at_construction(self):
        # The non-blank invariant lives on the dataclass (``__post_init__``),
        # so direct construction with an empty / whitespace-only value
        # must produce a config carrying the package default.  These
        # assertions are observable on the construction surface itself,
        # without relying on downstream consumers (``build_bearer_auth``)
        # to paper over a blank subject.
        assert ServerConfig(bearer_default_subject="").bearer_default_subject == (
            "bearer-anon"
        )
        assert ServerConfig(bearer_default_subject="   ").bearer_default_subject == (
            "bearer-anon"
        )


class TestServerConfigFromEnv:
    def test_reads_transport(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_TRANSPORT", "http")
        config = ServerConfig.from_env("MYAPP")
        assert config.transport == "http"

    def test_reads_host_port(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_HOST", "0.0.0.0")
        monkeypatch.setenv("MYAPP_PORT", "9000")
        config = ServerConfig.from_env("MYAPP")
        assert config.host == "0.0.0.0"
        assert config.port == 9000

    def test_reads_bearer_token(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_BEARER_TOKEN", "secret")
        config = ServerConfig.from_env("MYAPP")
        assert config.bearer_token == "secret"

    def test_reads_oidc_vars(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_BASE_URL", "https://x.example")
        monkeypatch.setenv(
            "MYAPP_OIDC_CONFIG_URL",
            "https://idp.example/.well-known/openid-configuration",
        )
        monkeypatch.setenv("MYAPP_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("MYAPP_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("MYAPP_OIDC_AUDIENCE", "aud.example")
        monkeypatch.setenv("MYAPP_OIDC_JWT_SIGNING_KEY", "sigkey")
        monkeypatch.setenv("MYAPP_OIDC_REQUIRED_SCOPES", "openid profile")
        config = ServerConfig.from_env("MYAPP")
        assert config.base_url == "https://x.example"
        assert (
            config.oidc_config_url
            == "https://idp.example/.well-known/openid-configuration"
        )
        assert config.oidc_client_id == "cid"
        assert config.oidc_client_secret == "csecret"
        assert config.oidc_audience == "aud.example"
        assert config.oidc_jwt_signing_key == "sigkey"
        assert config.oidc_required_scopes == ("openid", "profile")

    def test_reads_event_store_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_EVENT_STORE_URL", "file:///data/events")
        config = ServerConfig.from_env("MYAPP")
        assert config.event_store_url == "file:///data/events"

    def test_reads_app_domain(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_APP_DOMAIN", "mcp.example.com")
        assert ServerConfig.from_env("MYAPP").app_domain == "mcp.example.com"

    def test_oidc_verify_access_token_defaults_to_false(self):
        assert ServerConfig().oidc_verify_access_token is False

    def test_reads_oidc_verify_access_token(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_OIDC_VERIFY_ACCESS_TOKEN", "true")
        assert ServerConfig.from_env("MYAPP").oidc_verify_access_token is True

    def test_reads_auth_mode(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_AUTH_MODE", "oidc-proxy")
        assert ServerConfig.from_env("MYAPP").auth_mode == "oidc-proxy"

    def test_auth_mode_defaults_to_none(self):
        assert ServerConfig().auth_mode is None

    def test_invalid_transport_falls_back_to_stdio(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MYAPP_TRANSPORT", "websocket")
        assert ServerConfig.from_env("MYAPP").transport == "stdio"

    def test_is_frozen(self):
        config = ServerConfig()
        with pytest.raises(AttributeError):
            config.transport = "http"  # type: ignore[misc]

    def test_reads_bearer_tokens_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        token_file = tmp_path / "tokens.toml"
        token_file.write_text("[tokens]\n", encoding="utf-8")
        monkeypatch.setenv("MYAPP_BEARER_TOKENS_FILE", str(token_file))
        config = ServerConfig.from_env("MYAPP")
        assert config.bearer_tokens_file == token_file

    def test_reads_bearer_default_subject(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_BEARER_DEFAULT_SUBJECT", "service:bot")
        config = ServerConfig.from_env("MYAPP")
        assert config.bearer_default_subject == "service:bot"

    def test_bearer_default_subject_falls_back_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("MYAPP_BEARER_DEFAULT_SUBJECT", raising=False)
        config = ServerConfig.from_env("MYAPP")
        assert config.bearer_default_subject == "bearer-anon"
