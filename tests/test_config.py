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
    env,
    env_float,
    env_int,
)

# ---------------------------------------------------------------------------
# Module-level fixture classes for TestDomainEnvSuffixes
# (defined at module scope so typing.get_type_hints can resolve them —
# the only shape real configs take).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Sec:
    @classmethod
    def from_env(cls, prefix: str) -> _Sec:
        _ = env(prefix, "SEC_TOKEN")
        _ = env_int(prefix, "SEC_COUNT", 0)
        return cls()


@dataclass(frozen=True)
class _Flat:
    server: ServerConfig = field(default_factory=ServerConfig)

    @classmethod
    def from_env(cls, prefix: str = "X") -> _Flat:
        _ = env(prefix, "FLAT_S")
        _ = env_int(prefix, "FLAT_I", 0)
        _ = env_float(prefix, "FLAT_F", 1.0)
        return cls()


@dataclass(frozen=True)
class _Composed:
    sec: _Sec = field(default_factory=_Sec)
    server: ServerConfig = field(default_factory=ServerConfig)

    @classmethod
    def from_env(cls, prefix: str = "X") -> _Composed:
        _ = env(prefix, "TOP")
        return cls(sec=_Sec.from_env(prefix), server=ServerConfig.from_env(prefix))


@dataclass(frozen=True)
class _OptComposed:
    sec: _Sec | None = None

    @classmethod
    def from_env(cls, prefix: str = "X") -> _OptComposed:
        _ = env(prefix, "OPT_TOP")
        return cls()


@dataclass(frozen=True)
class _CycA:
    b: _CycB | None = None

    @classmethod
    def from_env(cls, prefix: str = "X") -> _CycA:
        _ = env(prefix, "CYC_A")
        return cls()


@dataclass(frozen=True)
class _CycB:
    a: _CycA | None = None

    @classmethod
    def from_env(cls, prefix: str = "X") -> _CycB:
        _ = env(prefix, "CYC_B")
        return cls()


@dataclass(frozen=True)
class _TwoFields:
    x: _Sec = field(default_factory=_Sec)
    y: _Sec = field(default_factory=_Sec)

    @classmethod
    def from_env(cls, prefix: str = "X") -> _TwoFields:
        return cls()


@dataclass(frozen=True)
class _Plain:
    v: int = 0


@dataclass(frozen=True)
class _HasPlain:
    p: _Plain = field(default_factory=_Plain)

    @classmethod
    def from_env(cls, prefix: str = "X") -> _HasPlain:
        _ = env(prefix, "HP_TOP")
        return cls()


@dataclass(frozen=True)
class _SubForList:
    @classmethod
    def from_env(cls, prefix: str) -> _SubForList:
        _ = env(prefix, "LIST_SUB")
        return cls()


@dataclass(frozen=True)
class _ListTypedField:
    subs: list[_SubForList] = field(default_factory=list)

    @classmethod
    def from_env(cls, prefix: str = "X") -> _ListTypedField:
        _ = env(prefix, "LIST_TOP")
        return cls()


@dataclass(frozen=True)
class _BadRef:
    x: NonExistentType = 0  # type: ignore[name-defined]  # noqa: F821 — unresolvable on purpose

    @classmethod
    def from_env(cls, prefix: str = "X") -> _BadRef:
        _ = env(prefix, "BAD")
        return cls()


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
    def test_flat_exact_surface_and_all_read_funcs(self) -> None:
        """Flat config: env/env_int/env_float all matched, ServerConfig excluded."""
        assert domain_env_suffixes(_Flat) == frozenset({"FLAT_S", "FLAT_I", "FLAT_F"})

    def test_recurses_into_subconfig_excludes_server(self) -> None:
        """Sub-config reads collected exactly; the ServerConfig field is not."""
        assert domain_env_suffixes(_Composed) == frozenset(
            {"TOP", "SEC_TOKEN", "SEC_COUNT"}
        )
        assert "TRANSPORT" not in domain_env_suffixes(_Composed)

    def test_optional_subconfig_discovered_via_get_args(self) -> None:
        """Optional[Sub]/Sub|None resolves to a Union; the inner type is found
        via typing.get_args (no default_factory needed)."""
        assert domain_env_suffixes(_OptComposed) == frozenset(
            {"OPT_TOP", "SEC_TOKEN", "SEC_COUNT"}
        )

    def test_module_scope_cycle_terminates_via_visited(self) -> None:
        """A<->B mutual references resolve via get_type_hints in both directions;
        the visited-set is what terminates the walk."""
        assert domain_env_suffixes(_CycA) == frozenset({"CYC_A", "CYC_B"})

    def test_same_type_two_fields_deduped(self) -> None:
        """A type referenced by two fields is scanned once (visited-set)."""
        assert domain_env_suffixes(_TwoFields) == frozenset({"SEC_TOKEN", "SEC_COUNT"})

    def test_dataclass_field_without_from_env_is_skipped(self) -> None:
        """A dataclass field lacking from_env is not scanned and does not crash."""
        assert domain_env_suffixes(_HasPlain) == frozenset({"HP_TOP"})

    def test_server_config_as_root_returns_empty(self) -> None:
        """ServerConfig as root yields empty; use server_config_env_suffixes."""
        assert domain_env_suffixes(ServerConfig) == frozenset()

    def test_non_dataclass_input_raises_typeerror(self) -> None:
        """A non-dataclass argument is a caller error, not a silent empty result."""
        with pytest.raises(TypeError, match="dataclass"):
            domain_env_suffixes(int)

    def test_dataclass_instance_raises_typeerror(self) -> None:
        """An instance (vs the class) is rejected — is_dataclass alone accepts both."""
        with pytest.raises(TypeError, match="dataclass"):
            domain_env_suffixes(_Flat())  # type: ignore[arg-type]

    def test_list_subconfig_field_traversed(self) -> None:
        """list[Sub] fields are traversed via get_args (one level)."""
        assert domain_env_suffixes(_ListTypedField) == frozenset(
            {"LIST_TOP", "LIST_SUB"}
        )

    def test_source_unavailable_raises_oserror_with_context(self) -> None:
        """A class whose from_env source can't be read raises OSError naming it."""
        ns: dict[str, object] = {}
        exec(  # noqa: S102 — building a source-less class on purpose
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class ExecCfg:\n"
            "    @classmethod\n"
            "    def from_env(cls, prefix='X'):\n"
            "        return cls()\n",
            ns,
        )
        with pytest.raises(OSError, match="ExecCfg.from_env"):
            domain_env_suffixes(ns["ExecCfg"])  # type: ignore[arg-type]

    def test_unresolvable_forward_ref_raises_nameerror(self) -> None:
        """An annotation not importable at module scope propagates as NameError."""
        with pytest.raises(NameError, match="domain_env_suffixes"):
            domain_env_suffixes(_BadRef)
