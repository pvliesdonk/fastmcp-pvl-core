"""Universal server configuration.

Downstream projects compose this into their own domain config dataclass
(they do not inherit). Core only owns fields that are universal to any
FastMCP server: transport, host, port, auth credentials, event store URL,
MCP Apps domain.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ._env import env, env_int, parse_bool, parse_scopes

Transport = Literal["stdio", "http", "sse"]

# Default subject string assigned to the single bearer-token mode (and
# the bearer leg of ``multi`` mode when only ``bearer_token`` is set).
# Mapped mode uses per-token subjects from the TOML file and ignores
# this default. Referenced from the ``ServerConfig`` field default,
# the env-loading fallback, and the ``__post_init__`` non-blank guard
# below — three call sites, one source of truth.
DEFAULT_BEARER_SUBJECT = "bearer-anon"


@dataclass(frozen=True)
class ServerConfig:
    """Universal fields every FastMCP server needs.

    Compose into a domain config; never inherit from this class.
    """

    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    base_url: str | None = None

    bearer_token: str | None = None

    oidc_config_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_audience: str | None = None
    oidc_required_scopes: tuple[str, ...] = field(default_factory=tuple)
    oidc_jwt_signing_key: str | None = None
    oidc_verify_access_token: bool = False

    kv_store_url: str | None = None
    # Legacy override, honoured by build_event_store and build_kv_store
    # when ``kv_store_url`` is unset. Set ``kv_store_url`` instead for
    # new deployments — a single URL drives every pvl-core subsystem
    # that needs persistent state.
    event_store_url: str | None = None
    app_domain: str | None = None

    auth_mode: str | None = None

    bearer_tokens_file: Path | None = None
    # Subject for the single-token bearer mode; ignored when
    # ``bearer_tokens_file`` is set (mapped mode uses per-token subjects).
    bearer_default_subject: str = DEFAULT_BEARER_SUBJECT

    def __post_init__(self) -> None:
        """Enforce the non-blank ``bearer_default_subject`` invariant.

        Blank / whitespace-only values are rewritten to
        :data:`DEFAULT_BEARER_SUBJECT` rather than rejected, so that
        direct construction (``ServerConfig(bearer_default_subject="")``)
        stays permissive.  This guard exists specifically for the
        direct-construction call site — the ``from_env`` path never
        reaches it, because :func:`fastmcp_pvl_core._env.env` already
        strips and falls back to its ``default`` argument before the
        ``cls(...)`` call.

        Without this guard, a downstream caller that constructs
        ``ServerConfig`` directly with an empty string would otherwise
        produce a ``StaticTokenVerifier`` entry with an empty
        ``client_id`` — exactly the foot-gun the consumer-side
        defensive fallback in ``_auth.py`` was previously papering over.
        """
        if not self.bearer_default_subject.strip():
            # ``object.__setattr__`` bypasses the frozen-dataclass guard;
            # this is the documented escape hatch for ``__post_init__``
            # normalisation on a frozen dataclass.
            object.__setattr__(self, "bearer_default_subject", DEFAULT_BEARER_SUBJECT)

    @classmethod
    def from_env(cls, env_prefix: str) -> ServerConfig:
        """Load all fields from ``{env_prefix}_*`` environment variables.

        Unknown values for ``TRANSPORT`` silently fall back to ``"stdio"``
        rather than raising — string fields prefer permissive defaults.
        ``PORT``, by contrast, is parsed strictly via :func:`env_int`: a
        non-integer or out-of-``1..65535`` value raises
        :class:`ConfigurationError` naming the var, so an operator typo
        fails fast at load instead of binding an invalid port later.

        Args:
            env_prefix: Env var prefix, no trailing underscore needed.

        Returns:
            A populated :class:`ServerConfig` instance.

        Raises:
            ConfigurationError: If ``{env_prefix}_PORT`` is set to a
                non-integer or out-of-``1..65535`` value.
        """
        transport_raw = env(env_prefix, "TRANSPORT", "stdio")
        transport: Transport
        if transport_raw == "http":
            transport = "http"
        elif transport_raw == "sse":
            transport = "sse"
        else:
            transport = "stdio"

        host = env(env_prefix, "HOST", "127.0.0.1")

        scopes_raw = env(env_prefix, "OIDC_REQUIRED_SCOPES")
        scopes = tuple(parse_scopes(scopes_raw) or ())

        verify_access_raw = env(env_prefix, "OIDC_VERIFY_ACCESS_TOKEN")
        verify_access_token = (
            parse_bool(verify_access_raw) if verify_access_raw is not None else False
        )

        tokens_file_raw = env(env_prefix, "BEARER_TOKENS_FILE")
        # ``Path(...)`` keeps a leading ``~`` literal here.  Expansion is
        # performed once, in :func:`fastmcp_pvl_core._auth._load_bearer_tokens`,
        # so both this env-driven path and a directly-constructed
        # ``ServerConfig(bearer_tokens_file=Path("~/tokens.toml"))`` resolve
        # the tilde at the same call site.
        bearer_tokens_file = Path(tokens_file_raw) if tokens_file_raw else None
        bearer_default_subject = env(
            env_prefix, "BEARER_DEFAULT_SUBJECT", DEFAULT_BEARER_SUBJECT
        )

        return cls(
            transport=transport,
            host=host,
            port=env_int(
                env_prefix, "PORT", 8000, strict=True, minimum=1, maximum=65535
            ),
            base_url=env(env_prefix, "BASE_URL"),
            bearer_token=env(env_prefix, "BEARER_TOKEN"),
            oidc_config_url=env(env_prefix, "OIDC_CONFIG_URL"),
            oidc_client_id=env(env_prefix, "OIDC_CLIENT_ID"),
            oidc_client_secret=env(env_prefix, "OIDC_CLIENT_SECRET"),
            oidc_audience=env(env_prefix, "OIDC_AUDIENCE"),
            oidc_required_scopes=scopes,
            oidc_jwt_signing_key=env(env_prefix, "OIDC_JWT_SIGNING_KEY"),
            oidc_verify_access_token=verify_access_token,
            kv_store_url=env(env_prefix, "KV_STORE_URL"),
            event_store_url=env(env_prefix, "EVENT_STORE_URL"),
            app_domain=env(env_prefix, "APP_DOMAIN"),
            auth_mode=env(env_prefix, "AUTH_MODE"),
            bearer_tokens_file=bearer_tokens_file,
            bearer_default_subject=bearer_default_subject,
        )


# The env-var suffixes ``ServerConfig.from_env`` reads (the part after a
# project's ``{PREFIX}_``). Kept in lockstep with ``from_env`` by
# ``test_config.py::TestServerConfigEnvSuffixes``, which AST-scans ``from_env``
# for ``env``/``env_int``/``env_float`` calls whose suffix is a string literal
# and fails if such a read is added/removed/renamed without updating this set.
# Keep every ``from_env`` read in the ``env(prefix, "LITERAL")`` form: a suffix
# built from a variable or passed by keyword would not be seen by the scan.
_SERVER_CONFIG_ENV_SUFFIXES: frozenset[str] = frozenset(
    {
        "TRANSPORT",
        "HOST",
        "PORT",
        "BASE_URL",
        "BEARER_TOKEN",
        "BEARER_TOKENS_FILE",
        "BEARER_DEFAULT_SUBJECT",
        "OIDC_CONFIG_URL",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_AUDIENCE",
        "OIDC_REQUIRED_SCOPES",
        "OIDC_JWT_SIGNING_KEY",
        "OIDC_VERIFY_ACCESS_TOKEN",
        "KV_STORE_URL",
        "EVENT_STORE_URL",
        "APP_DOMAIN",
        "AUTH_MODE",
    }
)


def server_config_env_suffixes() -> frozenset[str]:
    """Return the env-var suffixes :meth:`ServerConfig.from_env` reads.

    Each suffix is the part after a project's ``{PREFIX}_`` (e.g. ``BASE_URL``
    for ``MYAPP_BASE_URL``), so a consumer can check whether a given
    ``{PREFIX}_{SUFFIX}`` variable is part of the upstream ``ServerConfig``
    surface — for instance to detect drift between a config-wizard spec and the
    real config surface.

    Excludes native ``FASTMCP_*`` variables and consumer-specific
    (``ProjectConfig``) variables: it is purely the ``ServerConfig.from_env``
    surface.
    """
    return _SERVER_CONFIG_ENV_SUFFIXES


def domain_env_suffixes(config_cls: type) -> frozenset[str]:
    """Return the ``{PREFIX}_``-stripped env suffixes a domain config reads.

    AST-scans ``config_cls.from_env`` for ``env``/``env_int``/``env_float``
    calls whose suffix is a string literal, then recurses into the config's
    dataclass fields whose resolved type is itself a dataclass exposing a
    ``from_env`` classmethod — so a config that delegates env reads to
    sub-config *sections* (each with its own ``from_env``) reports its full
    surface. The composed :class:`ServerConfig` field is skipped (its surface
    is :func:`server_config_env_suffixes`). A flat config with no sub-config
    fields scans only its own ``from_env``.

    Recursion is depth-first with a visited-set, so nested sections and
    reference cycles terminate. Only literal-string suffixes are seen — a
    suffix built from a variable or passed by keyword is invisible; keep reads
    in the ``env(prefix, "LITERAL")`` form. Sub-config types must be resolvable
    via :func:`typing.get_type_hints` (importable in the config module's
    namespace), which matters under ``from __future__ import annotations``.

    Args:
        config_cls: The domain config dataclass (its ``from_env`` classmethod is
            the scan root).

    Returns:
        The frozenset of literal env suffixes read by ``config_cls.from_env``
        and its sub-config sections, excluding the :class:`ServerConfig` field.
    """
    read_funcs = {"env", "env_int", "env_float"}
    found: set[str] = set()
    visited: set[type] = set()

    def _literals_in(cls: type) -> None:
        src = textwrap.dedent(inspect.getsource(cls.from_env))  # type: ignore[attr-defined]
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

    def _visit(cls: type) -> None:
        if cls in visited or cls is ServerConfig or not dataclasses.is_dataclass(cls):
            return
        visited.add(cls)
        if hasattr(cls, "from_env"):
            _literals_in(cls)
        try:
            hints = typing.get_type_hints(cls)
        except Exception:  # noqa: BLE001 — unresolved hint: scan top level only
            hints = {}
        for f in dataclasses.fields(cls):
            ftype = hints.get(f.name, f.type)
            # When ``from __future__ import annotations`` is active and the type
            # is locally defined (e.g. inside a test method), ``get_type_hints``
            # may fail and ``f.type`` is a plain string.  Fall back to
            # ``f.default_factory`` when it is a type, so that the common
            # ``field(default_factory=SomeSubConfig)`` pattern is still visited.
            candidates: tuple[object, ...]
            factory: object = f.default_factory
            if not isinstance(ftype, type) and factory is not dataclasses.MISSING:
                candidates = (factory, *typing.get_args(ftype))
            else:
                candidates = (ftype, *typing.get_args(ftype))
            for candidate in candidates:
                if (
                    isinstance(candidate, type)
                    and dataclasses.is_dataclass(candidate)
                    and candidate is not ServerConfig
                    and hasattr(candidate, "from_env")
                ):
                    _visit(candidate)

    _visit(config_cls)
    return frozenset(found)
