"""Universal server configuration.

Downstream projects compose this into their own domain config dataclass
(they do not inherit). Core only owns fields that are universal to any
FastMCP server: transport, host, port, auth credentials, event store URL,
MCP Apps domain.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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

    transport: Transport = field(
        default="stdio",
        metadata={
            "help": (
                "Transport the server speaks: ``stdio`` for local Claude "
                "Desktop/Code, ``http`` or ``sse`` for a network server."
            ),
            "tags": ("server",),
            # Emitted by a routing select in the wizard, not a free-text field.
            "wizard": {"control": "emit"},
        },
    )
    host: str = field(
        default="127.0.0.1",
        metadata={
            "help": "Interface the HTTP server binds to.",
            "tags": ("server",),
            "wizard": {"group": "Server", "when": "server"},
        },
    )
    port: int = field(
        default=8000,
        metadata={
            "help": "TCP port for the HTTP server.",
            "tags": ("server",),
            "wizard": {"group": "Server", "when": "server"},
        },
    )
    base_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "Public base URL of the deployed server, e.g. "
                "``https://mcp.example.com``. Required for OIDC. Also the "
                "fallback source of the MCP Apps domain when ``app_domain`` "
                "is unset."
            ),
            "tags": ("server", "oidc", "apps"),
            "wizard": {"when": "server"},
        },
    )

    bearer_token: str | None = field(
        default=None,
        metadata={
            "help": (
                "Single shared bearer token; enables bearer auth unless "
                "``bearer_tokens_file`` is set, which takes precedence."
            ),
            "tags": ("auth", "bearer"),
            "wizard": {"when": "bearer", "secret": True},
        },
    )

    oidc_config_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "OIDC discovery document URL, e.g. "
                "``https://auth.example.com/.well-known/openid-configuration``."
            ),
            "tags": ("auth", "oidc"),
            "wizard": {"when": "oidc"},
        },
    )
    oidc_client_id: str | None = field(
        default=None,
        metadata={
            "help": "OIDC client identifier registered with the provider.",
            "tags": ("auth", "oidc"),
            "wizard": {"when": "oidc"},
        },
    )
    oidc_client_secret: str | None = field(
        default=None,
        metadata={
            "help": "OIDC client secret registered with the provider.",
            "tags": ("auth", "oidc"),
            "wizard": {"when": "oidc", "secret": True},
        },
    )
    oidc_audience: str | None = field(
        default=None,
        metadata={
            "help": (
                "Expected ``aud`` claim; tokens issued for another audience "
                "are rejected."
            ),
            "tags": ("auth", "oidc"),
            "wizard": {"group": "Auth", "when": "oidc"},
        },
    )
    oidc_required_scopes: tuple[str, ...] = field(
        default_factory=tuple,
        metadata={
            "help": "Scopes a caller must present, space- or comma-separated.",
            "tags": ("auth", "oidc"),
            "wizard": {"group": "Auth", "when": "oidc"},
        },
    )
    oidc_jwt_signing_key: str | None = field(
        default=None,
        metadata={
            "help": (
                "Signing key for issued JWTs. Strongly recommended on "
                "Linux/Docker — without it the generated fallback is "
                "ephemeral and invalidates every token on restart. Generate "
                "with ``openssl rand -hex 32``."
            ),
            "tags": ("auth", "oidc"),
            "wizard": {"when": "oidc", "secret": True},
        },
    )
    oidc_verify_access_token: bool = field(
        default=False,
        metadata={
            "help": "Validate the access token instead of the id token.",
            "tags": ("auth", "oidc"),
            "wizard": {"group": "Auth", "when": "oidc"},
        },
    )

    kv_store_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "Persistent-state backend URL shared by every pvl-core "
                "subsystem that needs state. ``memory://`` is in-process and "
                "lost on restart; ``file:///path`` persists on one server; "
                "``redis://``, ``dynamodb://`` and ``mongodb://`` each need "
                "their matching extra."
            ),
            "tags": ("persistence", "readme"),
            "wizard": {"group": "Persistence", "when": "server"},
        },
    )
    event_store_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "Legacy state-backend override, honoured by "
                "``build_event_store`` and ``build_kv_store`` only when "
                "``kv_store_url`` is unset — it then backs every namespace, "
                "not just HTTP resumability. Prefer ``kv_store_url`` for new "
                "deployments."
            ),
            "tags": ("persistence",),
            "wizard": {"group": "Persistence", "when": "server"},
        },
    )
    app_domain: str | None = field(
        default=None,
        metadata={
            "help": (
                "MCP Apps iframe domain, used for CSP sandboxing. Overrides "
                "the host derived from ``base_url``."
            ),
            "tags": ("apps",),
            "wizard": {"group": "MCP Apps", "when": "server"},
        },
    )

    auth_mode: str | None = field(
        default=None,
        metadata={
            "help": (
                "Resolved authentication mode. Inferred from which auth "
                "variables are set, so no dedicated control is offered for it."
            ),
            "tags": ("auth",),
            "wizard": "inferred",
        },
    )

    bearer_tokens_file: Path | None = field(
        default=None,
        metadata={
            "help": (
                "Path to a TOML file mapping bearer tokens to subjects; "
                "overrides the single-token ``bearer_token`` mode."
            ),
            "tags": ("auth", "bearer"),
            "wizard": {"group": "Auth", "when": "bearer"},
        },
    )
    bearer_default_subject: str = field(
        default=DEFAULT_BEARER_SUBJECT,
        metadata={
            "help": (
                "Subject assigned to the single-token bearer mode; ignored "
                "when ``bearer_tokens_file`` is set, since mapped mode carries "
                "per-token subjects."
            ),
            "tags": ("auth", "bearer"),
            "wizard": {"group": "Auth", "when": "bearer"},
        },
    )

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


@dataclass(frozen=True)
class ConfigField:
    """One env-configurable :class:`ServerConfig` field and its metadata.

    ``help`` deliberately does **not** restate a scalar default: ``default``
    carries it structurally, and a documentation generator renders both. The
    one exception is ``transport``, where naming the accepted literal values is
    the documentation.
    """

    suffix: str
    """Env suffix, i.e. the part after ``{PREFIX}_`` — e.g. ``BASE_URL``."""

    name: str
    """Python field name — e.g. ``base_url``."""

    type_name: str
    """The annotation as written, e.g. ``str | None``."""

    default: object
    """The declared default. ``default_factory`` fields report the built value."""

    help: str
    """One-or-two-sentence description. Empty when undocumented."""

    tags: tuple[str, ...]
    """Semantic tags used to route this field into documentation sections.

    Layout-agnostic by design: core says *what* a field is about, never which
    file it belongs in. A field may carry several tags, and appearing in more
    than one destination is intentional.
    """

    inferred: bool
    """True when the value is derived rather than set directly, so no control
    should be offered for it."""

    wizard: Mapping[str, object]
    """Presentation hints for a config wizard — e.g. ``group``, ``secret``,
    ``when``. Empty for inferred fields."""


def _config_field_from(f: dataclasses.Field[Any]) -> ConfigField:
    """Build one :class:`ConfigField` record from a ``ServerConfig`` field.

    Raises:
        ValueError: If ``metadata["wizard"]`` is a string other than the
            recognised ``"inferred"`` shorthand — e.g. a typo like
            ``"infered"``. Falling through silently would otherwise crash
            later on ``raw_wizard.items()`` since a plain string has no
            ``.items()``.
    """
    if f.default is not dataclasses.MISSING:
        default: object = f.default
    elif f.default_factory is not dataclasses.MISSING:
        default = f.default_factory()
    else:  # pragma: no cover — every current field has a default
        default = None

    tags = tuple(str(tag) for tag in f.metadata.get("tags", ()))

    # ``metadata={"wizard": "inferred"}`` is the shorthand for a field with
    # no control; anything else is a mapping of presentation hints.
    raw_wizard = f.metadata.get("wizard", {})
    inferred = raw_wizard == "inferred"
    wizard: dict[str, object] = {}
    if not inferred and raw_wizard:
        if isinstance(raw_wizard, str):
            raise ValueError(
                f"ServerConfig.{f.name}: metadata['wizard'] is the string "
                f"{raw_wizard!r}; the only accepted string form is "
                f"'inferred'. Use a mapping of hints for anything else."
            )
        wizard = {str(k): v for k, v in raw_wizard.items()}

    return ConfigField(
        suffix=f.name.upper(),
        name=f.name,
        type_name=f.type if isinstance(f.type, str) else str(f.type),
        default=default,
        help=str(f.metadata.get("help", "")),
        tags=tags,
        inferred=inferred,
        wizard=wizard,
    )


def server_config_surface() -> tuple[ConfigField, ...]:
    """Return every :class:`ServerConfig` env field, in declaration order.

    Declaration order is part of the contract: a consumer that renders this
    tuple produces byte-identical output on every run. Prefer this over
    :func:`server_config_env_suffixes`, which returns a ``frozenset`` whose
    iteration order varies between processes under hash randomisation.

    Covers the same 18 variables as :func:`server_config_env_suffixes`, adding
    each field's type, default, help text, tags, and wizard hints.
    """
    return tuple(_config_field_from(f) for f in dataclasses.fields(ServerConfig))


def domain_env_suffixes(config_cls: type) -> frozenset[str]:
    """Return the ``{PREFIX}_``-stripped env suffixes a domain config reads.

    AST-scans ``config_cls.from_env`` for ``env``/``env_int``/``env_float``
    calls whose suffix is a string literal, and recurses into the config's
    dataclass fields whose resolved type — or a type argument of it, e.g. the
    inner ``Section`` of ``Optional[Section]`` or the element type of
    ``list[Section]`` — is a dataclass exposing a ``from_env`` classmethod. So
    a config that delegates env reads to sub-config *sections* reports its full
    surface. The composed :class:`ServerConfig` field is excluded (its surface
    is :func:`server_config_env_suffixes`); a flat config with no sub-config
    fields scans only its own ``from_env``. Recursion is depth-first with a
    visited-set, so reference cycles and a type shared by two fields terminate
    and de-duplicate.

    Field types are resolved with :func:`typing.get_type_hints`, so the config
    and its sub-configs must be defined at module scope (the normal case); a
    resolution failure propagates rather than yielding a silently-incomplete
    set in a drift gate. Only unqualified ``env(prefix, "LITERAL")`` reads are
    seen — a renamed import (``env as _e``), an attribute-form call
    (``mod.env(...)``), or a variable/keyword-form suffix is invisible to the
    scan; keep reads in the literal ``env(prefix, "LITERAL")`` form.

    Only one level of :func:`typing.get_args` is expanded — ``list[Section]``
    (element type) and ``Section | None`` (direct union member) are traversed,
    but a nested container generic such as ``list[Optional[Section]]`` is not.

    Args:
        config_cls: The domain config dataclass; its ``from_env`` classmethod
            is the scan root.

    Returns:
        The frozenset of literal env suffixes read by ``config_cls.from_env``
        and its sub-config sections, excluding the :class:`ServerConfig` field.

    Raises:
        TypeError: If ``config_cls`` is not a dataclass.
        OSError: If a sub-config's ``from_env`` source cannot be read
            (compiled, frozen, or dynamically-defined class).
        NameError: If a field annotation cannot be resolved at
            :func:`typing.get_type_hints` time — the config or a sub-config
            is not defined at module scope, or contains a broken forward
            reference.
    """
    import ast
    import inspect
    import textwrap

    # ``is_dataclass`` is true for instances too; require the class itself so a
    # mistakenly-passed instance fails loudly rather than silently scanning.
    if not isinstance(config_cls, type) or not dataclasses.is_dataclass(config_cls):
        raise TypeError(
            f"domain_env_suffixes: expected a dataclass type, got {config_cls!r}"
        )

    read_funcs = {"env", "env_int", "env_float"}
    found: set[str] = set()
    visited: set[type] = set()

    def _literals_in(cls: type) -> None:
        try:
            src = textwrap.dedent(inspect.getsource(cls.from_env))  # type: ignore[attr-defined]
        except (OSError, TypeError) as exc:  # source unreadable / not a Python function
            # Re-raise preserving the original type (OSError vs TypeError) with
            # class context, so a type error isn't masqueraded as I/O.
            raise type(exc)(
                f"domain_env_suffixes: cannot read source for "
                f"{cls.__qualname__}.from_env: {exc}"
            ) from exc
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
        except NameError as exc:
            raise NameError(
                f"domain_env_suffixes: cannot resolve type hints for "
                f"{cls.__qualname__} — annotations must be importable at module "
                f"scope: {exc}"
            ) from exc
        for f in dataclasses.fields(cls):
            resolved = hints.get(f.name, f.type)
            for candidate in (resolved, *typing.get_args(resolved)):
                if (
                    isinstance(candidate, type)
                    and dataclasses.is_dataclass(candidate)
                    and candidate is not ServerConfig
                    and hasattr(candidate, "from_env")
                ):
                    _visit(candidate)

    _visit(config_cls)
    return frozenset(found)
