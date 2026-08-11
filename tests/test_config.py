"""Tests for ServerConfig."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from fastmcp_pvl_core import (
    ConfigField,
    ConfigurationError,
    DomainEnvVar,
    ServerConfig,
    build_bearer_auth,
    domain_env_suffixes,
    domain_env_surface,
    env,
    env_float,
    env_int,
    server_config_env_suffixes,
    server_config_surface,
)
from fastmcp_pvl_core._config import DEFAULT_BEARER_SUBJECT, _config_field_from

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


# ---------------------------------------------------------------------------
# Extra fixtures for TestDomainEnvSurface: a section whose prefixed suffixes
# are tied to metadata-carrying fields via constructor keywords (the realistic
# shape the surface resolves), and a config that composes it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MetaSec:
    token: str | None = field(
        default=None,
        metadata={
            "help": "Secret token.",
            "tags": ("meta",),
            "wizard": {"secret": True},
        },
    )
    count: int = field(
        default=3,
        metadata={"help": "How many.", "tags": ("meta",)},
    )

    @classmethod
    def from_env(cls, prefix: str = "X") -> _MetaSec:
        return cls(
            token=env(prefix, "META_TOKEN"),
            count=env_int(prefix, "META_COUNT", 3),
        )


@dataclass(frozen=True)
class _MetaComposed:
    label: str = field(default="", metadata={"help": "A label.", "tags": ("top",)})
    meta: _MetaSec = field(default_factory=_MetaSec)
    server: ServerConfig = field(default_factory=ServerConfig)

    @classmethod
    def from_env(cls, prefix: str = "X") -> _MetaComposed:
        return cls(
            label=env(prefix, "LABEL", ""),
            meta=_MetaSec.from_env(prefix),
            server=ServerConfig.from_env(prefix),
        )


@dataclass(frozen=True)
class _ReqSec:
    endpoint: str  # no default -> a required var

    @classmethod
    def from_env(cls, prefix: str = "X") -> _ReqSec:
        return cls(endpoint=env(prefix, "REQ_ENDPOINT", "fallback"))


# ---------------------------------------------------------------------------
# Fixtures for the field-name resolution fallback (issue #243): reads consumed
# via a local before cls(...) are not keyword-mapped, but a top-level field
# whose name.upper() equals the suffix must still carry its metadata.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LocalRead:
    read_only: bool = field(
        default=False,
        metadata={"help": "Read-only mode.", "tags": ("mode",)},
    )
    api_key: str | None = field(
        default=None,
        metadata={"help": "API key.", "tags": ("auth",), "wizard": {"secret": True}},
    )

    @classmethod
    def from_env(cls, prefix: str = "X") -> _LocalRead:
        # Both reads land in a local first, so neither cls(...) keyword carries
        # the literal; only the name fallback can attach their metadata.
        read_only_raw = env(prefix, "READ_ONLY")
        api_key = env(prefix, "API_KEY")
        return cls(read_only=bool(read_only_raw), api_key=api_key)


@dataclass(frozen=True)
class _TwoLiteralKeyword:
    host: str = field(default="", metadata={"help": "Host.", "tags": ("net",)})
    port: str = field(default="", metadata={"help": "Port.", "tags": ("net",)})
    addr: str = field(default="", metadata={"help": "Combined.", "tags": ("net",)})

    @classmethod
    def from_env(cls, prefix: str = "X") -> _TwoLiteralKeyword:
        # Two literals in one keyword -> not keyword-mapped; each resolves by
        # field name instead.
        return cls(addr=f"{env(prefix, 'HOST')}:{env(prefix, 'PORT')}")


@dataclass(frozen=True)
class _SectionLocalRead:
    # A section-style field: its read carries a section prefix, so the prefixed
    # suffix is not name.upper() of any field. Read via a local -> unresolved.
    ttl_s: float = field(default=1.0, metadata={"help": "TTL.", "tags": ("sec",)})

    @classmethod
    def from_env(cls, prefix: str = "X") -> _SectionLocalRead:
        raw = env(prefix, "SECTION_TTL_S")
        return cls(ttl_s=float(raw) if raw else 1.0)


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

    def test_reads_tools_allow(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_TOOLS_ALLOW", "search, get_item ,fetch")
        config = ServerConfig.from_env("MYAPP")
        assert config.tools_allow == ("search", "get_item", "fetch")
        assert config.tools_deny == ()

    def test_reads_tools_deny(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_TOOLS_DENY", "delete_item,, purge ")
        config = ServerConfig.from_env("MYAPP")
        assert config.tools_deny == ("delete_item", "purge")
        assert config.tools_allow == ()

    def test_tool_lists_default_to_empty(self):
        config = ServerConfig()
        assert config.tools_allow == ()
        assert config.tools_deny == ()

    def test_both_tool_lists_set_raises_naming_the_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MYAPP_TOOLS_ALLOW", "search")
        monkeypatch.setenv("MYAPP_TOOLS_DENY", "purge")
        with pytest.raises(
            ConfigurationError, match="MYAPP_TOOLS_ALLOW and MYAPP_TOOLS_DENY"
        ):
            ServerConfig.from_env("MYAPP")

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


class TestDomainEnvSurface:
    @pytest.mark.parametrize(
        "cls",
        [_Flat, _Composed, _OptComposed, _CycA, _TwoFields, _HasPlain, _ListTypedField],
    )
    def test_suffixes_match_the_frozenset_gate(self, cls: type) -> None:
        """The surface never drops or adds a suffix the flat frozenset carries."""
        assert {v.suffix for v in domain_env_surface(cls)} == domain_env_suffixes(cls)

    def test_records_are_domain_env_var_instances(self) -> None:
        assert all(
            isinstance(v, DomainEnvVar) for v in domain_env_surface(_MetaComposed)
        )

    def test_top_level_read_carries_its_field_metadata(self) -> None:
        surface = domain_env_surface(_MetaComposed)
        label = next(v for v in surface if v.suffix == "LABEL")
        assert label.source == "_MetaComposed"
        assert label.name == "label"
        assert label.help == "A label."
        assert label.tags == ("top",)
        assert label.required is False

    def test_section_read_carries_provenance_and_field_metadata(self) -> None:
        """A composed section's prefixed suffix resolves to its field's metadata."""
        surface = domain_env_surface(_MetaComposed)
        token = next(v for v in surface if v.suffix == "META_TOKEN")
        assert token.source == "_MetaSec"
        assert token.name == "token"
        assert token.help == "Secret token."
        assert token.tags == ("meta",)
        assert token.wizard == {"secret": True}
        assert token.required is False

    def test_default_is_carried_through(self) -> None:
        count = next(
            v for v in domain_env_surface(_MetaComposed) if v.suffix == "META_COUNT"
        )
        assert count.default == 3
        assert count.type_name == "int"

    def test_field_without_default_is_required(self) -> None:
        """A section field with no default reports required=True."""
        endpoint = next(
            v for v in domain_env_surface(_ReqSec) if v.suffix == "REQ_ENDPOINT"
        )
        assert endpoint.name == "endpoint"
        assert endpoint.required is True

    def test_throwaway_read_yields_placeholder_record(self) -> None:
        """A read not tied to a constructor field is still emitted, with name=None."""
        surface = domain_env_surface(_Composed)
        assert {v.suffix for v in surface} == {"TOP", "SEC_TOKEN", "SEC_COUNT"}
        assert all(v.name is None for v in surface)
        top = next(v for v in surface if v.suffix == "TOP")
        assert top.help == ""
        assert top.tags == ()
        assert top.required is False

    def test_order_is_depth_first_root_before_sections(self) -> None:
        """Root's own read precedes the composed section's reads."""
        order = [v.suffix for v in domain_env_surface(_MetaComposed)]
        assert order == ["LABEL", "META_TOKEN", "META_COUNT"]

    def test_server_config_field_is_excluded(self) -> None:
        suffixes = {v.suffix for v in domain_env_surface(_MetaComposed)}
        assert "TRANSPORT" not in suffixes
        assert "HOST" not in suffixes

    def test_server_config_as_root_returns_empty(self) -> None:
        assert domain_env_surface(ServerConfig) == ()

    def test_non_dataclass_input_raises_typeerror(self) -> None:
        with pytest.raises(TypeError, match="dataclass"):
            domain_env_surface(int)

    def test_dataclass_instance_raises_typeerror(self) -> None:
        with pytest.raises(TypeError, match="dataclass"):
            domain_env_surface(_Flat())  # type: ignore[arg-type]

    def test_source_unavailable_raises_oserror_with_context(self) -> None:
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
            domain_env_surface(ns["ExecCfg"])  # type: ignore[arg-type]

    def test_unresolvable_forward_ref_raises_nameerror(self) -> None:
        with pytest.raises(NameError, match="domain_env_surface"):
            domain_env_surface(_BadRef)

    def test_order_is_stable_under_hash_randomisation(self) -> None:
        """Byte-stability guard: record order must not vary between processes."""
        program = (
            "from tests.test_config import _MetaComposed;"
            "from fastmcp_pvl_core import domain_env_surface;"
            "print(','.join(v.suffix for v in domain_env_surface(_MetaComposed)))"
        )
        outputs = set()
        for seed in ("1", "2", "3"):
            result = subprocess.run(
                [sys.executable, "-c", program],
                capture_output=True,
                text=True,
                check=True,
                cwd=Path(__file__).resolve().parent.parent,
                env={**os.environ, "PYTHONHASHSEED": seed},
            )
            outputs.add(result.stdout.strip())
        assert outputs == {"LABEL,META_TOKEN,META_COUNT"}


class TestDomainEnvSurfaceNameFallback:
    """Issue #243: a read consumed via a local (or assembled from several reads)
    is not keyword-mapped, but a field whose name.upper() equals the suffix must
    still carry its metadata — restoring the pre-4.6 field-name resolution."""

    def test_local_read_resolves_to_its_field(self) -> None:
        by_suffix = {v.suffix: v for v in domain_env_surface(_LocalRead)}
        read_only = by_suffix["READ_ONLY"]
        assert read_only.name == "read_only"
        assert read_only.help == "Read-only mode."
        assert read_only.tags == ("mode",)
        assert read_only.required is False

    def test_secret_wizard_hint_survives_the_local_read(self) -> None:
        """The regression lost isSecret masking on API keys; it must be back."""
        api_key = next(
            v for v in domain_env_surface(_LocalRead) if v.suffix == "API_KEY"
        )
        assert api_key.name == "api_key"
        assert api_key.wizard == {"secret": True}

    def test_suffixes_still_match_the_frozenset_gate(self) -> None:
        assert {
            v.suffix for v in domain_env_surface(_LocalRead)
        } == domain_env_suffixes(_LocalRead)

    def test_keyword_mapping_still_wins_when_present(self) -> None:
        """Inline reads keep their keyword resolution; the fallback is a fallback."""
        endpoint = next(
            v for v in domain_env_surface(_ReqSec) if v.suffix == "REQ_ENDPOINT"
        )
        assert endpoint.name == "endpoint"
        assert endpoint.required is True

    def test_two_literals_in_one_keyword_resolve_by_field_name(self) -> None:
        """A keyword with two literals is not keyword-mapped; each suffix falls
        back to its same-named field (open question 1 in the issue)."""
        by_suffix = {v.suffix: v for v in domain_env_surface(_TwoLiteralKeyword)}
        assert by_suffix["HOST"].name == "host"
        assert by_suffix["HOST"].help == "Host."
        assert by_suffix["PORT"].name == "port"
        assert by_suffix["PORT"].help == "Port."

    def test_section_field_read_via_local_stays_unresolved(self) -> None:
        """A prefixed section suffix does not equal name.upper() of any field, so
        a section field read via a local stays name=None (documented boundary)."""
        ttl = next(
            v
            for v in domain_env_surface(_SectionLocalRead)
            if v.suffix == "SECTION_TTL_S"
        )
        assert ttl.name is None
        assert ttl.help == ""
        assert ttl.required is False

    def test_throwaway_read_with_no_matching_field_stays_none(self) -> None:
        """The fallback must not invent a field: _Composed's throwaway reads have
        no same-named field, so they remain name=None as before."""
        assert all(v.name is None for v in domain_env_surface(_Composed))


class TestServerConfigSurface:
    def test_surface_returns_config_field_records(self):
        assert all(isinstance(c, ConfigField) for c in server_config_surface())

    def test_covers_every_field_in_declaration_order(self):
        """Declaration order is the contract — it makes generated output stable."""
        surface = server_config_surface()
        assert tuple(c.name for c in surface) == tuple(
            f.name for f in dataclasses.fields(ServerConfig)
        )

    def test_returns_twenty_fields(self):
        assert len(server_config_surface()) == 20

    def test_suffix_is_the_upper_cased_field_name(self):
        assert all(c.suffix == c.name.upper() for c in server_config_surface())

    def test_suffixes_match_the_env_suffix_set(self):
        """The surface and the existing frozenset describe the same 20 vars."""
        assert {
            c.suffix for c in server_config_surface()
        } == server_config_env_suffixes()

    def test_scalar_default_is_carried_through(self):
        host = next(c for c in server_config_surface() if c.name == "host")
        assert host.default == "127.0.0.1"

    def test_default_factory_is_resolved_to_a_value(self):
        """oidc_required_scopes uses default_factory=tuple; the surface reports ()."""
        scopes = next(
            c for c in server_config_surface() if c.name == "oidc_required_scopes"
        )
        assert scopes.default == ()

    def test_type_name_is_the_annotation_string(self):
        port = next(c for c in server_config_surface() if c.name == "port")
        assert port.type_name == "int"

    def test_records_are_frozen(self):
        record = server_config_surface()[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.help = "mutated"  # type: ignore[misc]

    def test_order_is_stable_under_hash_randomisation(self):
        """Guards the generated-output byte-stability failure mode.

        server_config_env_suffixes() returns a frozenset, whose iteration order
        varies between processes because CPython randomises string hashing. The
        surface must not inherit that.
        """
        program = (
            "from fastmcp_pvl_core import server_config_surface;"
            "print(','.join(c.suffix for c in server_config_surface()))"
        )
        outputs = set()
        for seed in ("1", "2", "3"):
            result = subprocess.run(
                [sys.executable, "-c", program],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            )
            outputs.add(result.stdout.strip())
        assert len(outputs) == 1

    def test_every_field_is_documented(self):
        undocumented = [c.name for c in server_config_surface() if not c.help]
        assert undocumented == []

    def test_every_field_is_tagged(self):
        untagged = [c.name for c in server_config_surface() if not c.tags]
        assert untagged == []

    def test_inferred_fields_are_the_expert_overrides(self):
        """AUTH_MODE is an expert override and the tool visibility lists are
        deployment-specific operator knobs, so the wizard offers no control
        for any of them."""
        assert [c.name for c in server_config_surface() if c.inferred] == [
            "tools_allow",
            "tools_deny",
            "auth_mode",
        ]

    def test_inferred_field_carries_no_wizard_hints(self):
        auth_mode = next(c for c in server_config_surface() if c.name == "auth_mode")
        assert auth_mode.wizard == {}

    def test_base_url_carries_several_tags(self):
        """A field can honestly belong to several documentation sections."""
        base_url = next(c for c in server_config_surface() if c.name == "base_url")
        assert set(base_url.tags) == {"server", "oidc", "apps"}

    def test_secret_fields_are_marked(self):
        secrets = {c.suffix for c in server_config_surface() if c.wizard.get("secret")}
        assert secrets == {
            "BEARER_TOKEN",
            "OIDC_CLIENT_SECRET",
            "OIDC_JWT_SIGNING_KEY",
        }

    def test_oidc_fields_share_the_oidc_tag(self):
        tagged = {c.suffix for c in server_config_surface() if "oidc" in c.tags}
        assert tagged == {
            "BASE_URL",
            "OIDC_CONFIG_URL",
            "OIDC_CLIENT_ID",
            "OIDC_CLIENT_SECRET",
            "OIDC_AUDIENCE",
            "OIDC_REQUIRED_SCOPES",
            "OIDC_JWT_SIGNING_KEY",
            "OIDC_VERIFY_ACCESS_TOKEN",
        }

    def test_kv_store_url_is_readme_prominent(self):
        """The consuming README shows a 3-row curated table; this is its core row."""
        kv = next(c for c in server_config_surface() if c.name == "kv_store_url")
        assert "readme" in kv.tags

    def test_defaults_are_unchanged_by_the_metadata_migration(self):
        """Behaviour guard: converting to field(default=...) must not alter values."""
        config = ServerConfig()
        assert config.host == "127.0.0.1"
        assert config.port == 8000
        assert config.transport == "stdio"
        assert config.oidc_required_scopes == ()
        assert config.oidc_verify_access_token is False
        assert config.bearer_default_subject == DEFAULT_BEARER_SUBJECT
        assert config.base_url is None

    def test_unknown_wizard_string_is_rejected(self):
        """A typo like "infered" must fail loudly, not crash on .items()."""

        @dataclass(frozen=True)
        class _BadWizard:
            oops: str = field(default="x", metadata={"wizard": "infered"})

        (bad,) = dataclasses.fields(_BadWizard)
        with pytest.raises(ValueError, match="must be a mapping of hints"):
            _config_field_from(bad)

    def test_non_mapping_non_string_wizard_is_rejected(self):
        """The class is "any non-mapping", not just a mistyped string."""

        @dataclass(frozen=True)
        class _BadWizard:
            oops: str = field(default="x", metadata={"wizard": ["inferred"]})

        (bad,) = dataclasses.fields(_BadWizard)
        with pytest.raises(ValueError, match="must be a mapping of hints"):
            _config_field_from(bad)

    def test_wizard_hints_use_only_documented_keys(self):
        """An unrecognised hint key (e.g. a typo) would be silently ignored."""
        documented = {"group", "when", "secret", "control"}
        offenders = {
            c.name: sorted(set(c.wizard) - documented)
            for c in server_config_surface()
            if set(c.wizard) - documented
        }
        assert offenders == {}

    def test_every_declared_default_is_unchanged(self):
        """Full 20-field guard; a spot check would miss a silent default change."""
        expected = {
            "transport": "stdio",
            "host": "127.0.0.1",
            "port": 8000,
            "base_url": None,
            "bearer_token": None,
            "oidc_config_url": None,
            "oidc_client_id": None,
            "oidc_client_secret": None,
            "oidc_audience": None,
            "oidc_required_scopes": (),
            "oidc_jwt_signing_key": None,
            "oidc_verify_access_token": False,
            "kv_store_url": None,
            "event_store_url": None,
            "app_domain": None,
            "tools_allow": (),
            "tools_deny": (),
            "auth_mode": None,
            "bearer_tokens_file": None,
            "bearer_default_subject": DEFAULT_BEARER_SUBJECT,
        }
        actual = {c.name: c.default for c in server_config_surface()}
        assert actual == expected
