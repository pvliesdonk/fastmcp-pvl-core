"""Tests for ServerConfig."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from fastmcp_pvl_core import (
    ConfigurationError,
    ServerConfig,
    build_bearer_auth,
    domain_env_suffixes,
)
from fastmcp_pvl_core._env import env, env_int

# ---------------------------------------------------------------------------
# Module-level fixture classes for TestDomainEnvSuffixes
# (defined at module scope so typing.get_type_hints can resolve them —
# the primary discovery path used by real production configs).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ModuleSection:
    n: int = 0

    @classmethod
    def from_env(cls, prefix: str) -> _ModuleSection:
        return cls(n=env_int(prefix, "MODULE_SECTION_N", 0))


@dataclass(frozen=True)
class _ModuleComposed:
    section: _ModuleSection = field(default_factory=_ModuleSection)
    server: ServerConfig = field(default_factory=ServerConfig)

    @classmethod
    def from_env(cls, prefix: str = "X") -> _ModuleComposed:
        _ = env(prefix, "MODULE_TOP")
        return cls(
            section=_ModuleSection.from_env(prefix),
            server=ServerConfig.from_env(prefix),
        )


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

    def test_port_unset_defaults_to_8000(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MYAPP_PORT", raising=False)
        assert ServerConfig.from_env("MYAPP").port == 8000

    def test_malformed_port_raises_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MYAPP_PORT", "abc")
        with pytest.raises(ConfigurationError) as exc:
            ServerConfig.from_env("MYAPP")
        assert "MYAPP_PORT" in str(exc.value)

    @pytest.mark.parametrize("raw", ["0", "-1", "70000"])
    def test_out_of_range_port_raises_configuration_error(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MYAPP_PORT", raw)
        with pytest.raises(ConfigurationError) as exc:
            ServerConfig.from_env("MYAPP")
        assert "MYAPP_PORT" in str(exc.value)

    @pytest.mark.parametrize("raw", ["1", "65535"])
    def test_boundary_ports_accepted(self, raw: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_PORT", raw)
        assert ServerConfig.from_env("MYAPP").port == int(raw)

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

    def test_reads_kv_store_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_KV_STORE_URL", "redis://localhost:6379/0")
        config = ServerConfig.from_env("MYAPP")
        assert config.kv_store_url == "redis://localhost:6379/0"

    def test_kv_store_url_defaults_to_none(self):
        assert ServerConfig().kv_store_url is None

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

    def test_bearer_tokens_file_keeps_tilde_literal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # ``from_env`` no longer expands ``~`` — the loader is the single
        # expansion site.  Verifies the load-bearing change in this PR:
        # the env-driven path stays literal on the dataclass and only
        # resolves to the on-disk file when it reaches the loader.
        token_file = tmp_path / "tokens.toml"
        token_file.write_text('[tokens]\n"k1" = "user:alice"\n', encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("MYAPP_BEARER_TOKENS_FILE", "~/tokens.toml")
        config = ServerConfig.from_env("MYAPP")
        # Field is the literal ``~/tokens.toml`` — not expanded yet.
        assert str(config.bearer_tokens_file) == "~/tokens.toml"
        # Expanding by hand (with the patched ``$HOME``) lands on the
        # actual file the loader will touch.
        assert config.bearer_tokens_file is not None
        assert config.bearer_tokens_file.expanduser() == token_file
        # End-to-end: the loader resolves the tilde and returns a verifier
        # carrying the mapped subject.  Symmetric with the loader-side test
        # in ``test_auth_bearer_tokens_file.py::test_tilde_path_expands_at_load_time``.
        auth = build_bearer_auth(config)
        assert auth is not None
        assert auth.tokens["k1"]["client_id"] == "user:alice"

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


def _suffixes_read_by_from_env() -> set[str]:
    """The literal env suffixes ``ServerConfig.from_env`` actually reads.

    Statically extracts the second positional argument of each
    ``env``/``env_int``/``env_float`` call in ``from_env``'s source **whose
    suffix is a string literal** (calls with a variable, keyword, or
    attribute-form suffix are skipped), so the test reflects the literal read
    surface rather than a hand-copied list.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(ServerConfig.from_env))
    read_funcs = {"env", "env_int", "env_float"}
    found: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in read_funcs
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.add(node.args[1].value)
    return found


class TestServerConfigEnvSuffixes:
    def test_returns_frozenset_with_known_suffixes(self):
        from fastmcp_pvl_core import server_config_env_suffixes

        suffixes = server_config_env_suffixes()
        assert isinstance(suffixes, frozenset)
        # Representative members across the categories from_env reads.
        assert {
            "TRANSPORT",
            "HOST",
            "PORT",
            "BASE_URL",
            "BEARER_TOKEN",
            "OIDC_CONFIG_URL",
            "KV_STORE_URL",
            "AUTH_MODE",
        } <= suffixes

    def test_matches_what_from_env_actually_reads(self):
        """Anti-drift: the declared set must equal the literal suffixes from_env reads.

        A literal-string read added/removed/renamed in ``from_env`` without
        updating the declared set fails here. (A suffix built from a variable or
        passed by keyword is invisible to the scan — see
        ``_suffixes_read_by_from_env``.)
        """
        from fastmcp_pvl_core import server_config_env_suffixes

        assert server_config_env_suffixes() == _suffixes_read_by_from_env()


class TestDomainEnvSuffixes:
    def test_flat_config_returns_own_reads(self):
        """A config that reads only in its own from_env (no sub-configs)."""
        from dataclasses import dataclass, field

        from fastmcp_pvl_core import ServerConfig, domain_env_suffixes, env

        @dataclass(frozen=True)
        class Flat:
            server: ServerConfig = field(default_factory=ServerConfig)

            @classmethod
            def from_env(cls, prefix: str = "X") -> Flat:
                _ = env(prefix, "WIDGET")
                _ = env(prefix, "GADGET")
                return cls()

        assert domain_env_suffixes(Flat) == frozenset({"WIDGET", "GADGET"})

    def test_recurses_into_subconfigs_and_excludes_server(self):
        """Sub-config reads are collected; the ServerConfig field is not."""
        from dataclasses import dataclass, field

        from fastmcp_pvl_core import ServerConfig, domain_env_suffixes, env, env_int

        @dataclass(frozen=True)
        class Section:
            n: int = 0

            @classmethod
            def from_env(cls, prefix: str) -> Section:
                return cls(n=env_int(prefix, "SECTION_N", 0))

        @dataclass(frozen=True)
        class Composed:
            section: Section = field(default_factory=Section)
            server: ServerConfig = field(default_factory=ServerConfig)

            @classmethod
            def from_env(cls, prefix: str = "X") -> Composed:
                _ = env(prefix, "TOP_LEVEL")
                return cls(
                    section=Section.from_env(prefix),
                    server=ServerConfig.from_env(prefix),
                )

        result = domain_env_suffixes(Composed)
        assert {"TOP_LEVEL", "SECTION_N"} <= result
        # ServerConfig's own suffixes are NOT folded in here.
        assert "TRANSPORT" not in result

    def test_cycle_safe(self):
        """A reference cycle between sections does not infinite-loop."""
        from dataclasses import dataclass, field

        from fastmcp_pvl_core import domain_env_suffixes, env

        @dataclass(frozen=True)
        class A:
            b: B | None = None

            @classmethod
            def from_env(cls, prefix: str = "X") -> A:
                _ = env(prefix, "A_VAR")
                return cls()

        @dataclass(frozen=True)
        class B:
            a: A = field(default_factory=A)

            @classmethod
            def from_env(cls, prefix: str = "X") -> B:
                _ = env(prefix, "B_VAR")
                return cls()

        assert {"A_VAR", "B_VAR"} <= domain_env_suffixes(B)

    def test_primary_get_type_hints_path_module_level(self) -> None:
        """Module-level config: recursion resolves via get_type_hints, not the
        default_factory fallback. Proves the production path (real configs are
        module-level) and ServerConfig exclusion through that path."""
        result = domain_env_suffixes(_ModuleComposed)
        assert {"MODULE_TOP", "MODULE_SECTION_N"} <= result
        assert "TRANSPORT" not in result
