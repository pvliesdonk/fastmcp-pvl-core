"""Universal server configuration.

Downstream projects compose this into their own domain config dataclass
(they do not inherit). Core only owns fields that are universal to any
FastMCP server: transport, host, port, auth credentials, event store URL,
background-task backend URL, MCP Apps domain.
"""

from __future__ import annotations

import ast
import dataclasses
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ._env import _resolve_key, env, env_int, parse_bool, parse_list, parse_scopes
from ._errors import ConfigurationError

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
                "Public base URL of the deployed server, for example "
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
                "OIDC discovery document URL, for example "
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
            "help": (
                "Scopes a caller must present, space- or comma-separated. "
                "Defaults to ``openid`` in oidc-proxy mode."
            ),
            "tags": ("auth", "oidc"),
            "wizard": {"group": "Auth", "when": "oidc"},
        },
    )
    oidc_advertised_scopes: tuple[str, ...] = field(
        default_factory=tuple,
        metadata={
            "help": (
                "Scopes advertised to MCP clients in protected-resource "
                "metadata, space- or comma-separated. Overrides the default "
                "``openid offline_access``; ``oidc_required_scopes`` is always "
                "added on top. Set this when the registered client is not "
                "permitted ``offline_access``, or to have clients request "
                "extra claim scopes (such as ``groups``) without also requiring "
                "them in every token."
            ),
            "tags": ("auth", "oidc"),
            "wizard": {"group": "Auth", "when": "oidc"},
        },
    )
    oidc_jwt_signing_key: str | None = field(
        default=None,
        metadata={
            "help": (
                "Signing key for issued tokens; used in oidc-proxy mode only. "
                "When unset, the key is derived deterministically from "
                "``oidc_client_secret``, so tokens survive a restart. Rotating "
                "that secret then invalidates every issued token. Set "
                "this explicitly to decouple token validity from secret "
                "rotation. Generate with ``openssl rand -hex 32``."
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
                "their matching extra. When unset, defaults to "
                "``file:///data/state`` (the volume family Docker images "
                "mount), or to ``memory://`` (with a warning) on a host "
                "where that directory is not usable."
            ),
            "tags": ("persistence", "readme"),
            "wizard": {"group": "Persistence", "when": "server"},
        },
    )
    event_store_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "Legacy state-backend override, used by "
                "``build_event_store`` and ``build_kv_store`` only when "
                "``kv_store_url`` is unset. It then backs every namespace, "
                "not just HTTP resumability. Prefer ``kv_store_url`` for new "
                "deployments."
            ),
            "tags": ("persistence",),
            "wizard": {"group": "Persistence", "when": "server"},
        },
    )
    tasks_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "Background-task (Docket) backend URL: ``memory://`` is "
                "in-process and lost on restart; ``redis://`` is durable and "
                "multi-process. When unset, a ``redis://`` ``kv_store_url`` "
                "is reused for tasks too; otherwise fastmcp's ``memory://`` "
                "default applies. Only meaningful when task-enabled tools "
                "exist. Applied via ``configure_task_backend``."
            ),
            "tags": ("persistence", "tasks"),
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

    tools_allow: tuple[str, ...] = field(
        default_factory=tuple,
        metadata={
            "help": (
                "Comma-separated explicit tool names this instance exposes; "
                "every other tool is hidden from listings and cannot be "
                "invoked. Names matching no registered tool are inert. "
                "Mutually exclusive with ``tools_deny``. Takes effect through "
                "``apply_tool_visibility``."
            ),
            "tags": ("server",),
            "wizard": "inferred",
        },
    )
    tools_deny: tuple[str, ...] = field(
        default_factory=tuple,
        metadata={
            "help": (
                "Comma-separated explicit tool names hidden from this "
                "instance (absent from listings, cannot be invoked). Names "
                "matching no registered tool are inert. Mutually exclusive "
                "with ``tools_allow``. Takes effect through "
                "``apply_tool_visibility``."
            ),
            "tags": ("server",),
            "wizard": "inferred",
        },
    )

    auth_mode: str | None = field(
        default=None,
        metadata={
            "help": (
                "Explicit auth-mode override, accepting ``remote`` or "
                "``oidc-proxy`` (case- and whitespace-insensitive). When "
                "unset the mode is auto-detected from which auth variables "
                "are set; the override exists because having all four OIDC "
                "variables set is ambiguous between those two modes. Other "
                "values are ignored with a warning."
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
                non-integer or out-of-``1..65535`` value; if
                ``{env_prefix}_TOOLS_ALLOW`` and ``{env_prefix}_TOOLS_DENY``
                are both set; or if either is set but parses to zero tool
                names (e.g. a lone ``,``).
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

        advertised_raw = env(env_prefix, "OIDC_ADVERTISED_SCOPES")
        advertised_scopes = tuple(parse_scopes(advertised_raw) or ())

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

        # "Set but parses to zero names" (e.g. a lone ",") is rejected rather
        # than treated as unset: for TOOLS_ALLOW the silent reading would
        # expose every tool — the exact opposite of the lockdown the operator
        # was expressing. TOOLS_DENY gets the same guard for symmetry.
        tools_allow_raw = env(env_prefix, "TOOLS_ALLOW")
        tools_allow = tuple(parse_list(tools_allow_raw)) if tools_allow_raw else ()
        if tools_allow_raw and not tools_allow:
            raise ConfigurationError(
                f"{_resolve_key(env_prefix, 'TOOLS_ALLOW')} is set but "
                "contains no tool names; unset it to expose all tools."
            )
        tools_deny_raw = env(env_prefix, "TOOLS_DENY")
        tools_deny = tuple(parse_list(tools_deny_raw)) if tools_deny_raw else ()
        if tools_deny_raw and not tools_deny:
            raise ConfigurationError(
                f"{_resolve_key(env_prefix, 'TOOLS_DENY')} is set but "
                "contains no tool names; unset it to hide no tools."
            )
        if tools_allow and tools_deny:
            raise ConfigurationError(
                f"{_resolve_key(env_prefix, 'TOOLS_ALLOW')} and "
                f"{_resolve_key(env_prefix, 'TOOLS_DENY')} are both set; "
                "set at most one — an allowlist already expresses every "
                "exclusion."
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
            oidc_advertised_scopes=advertised_scopes,
            oidc_jwt_signing_key=env(env_prefix, "OIDC_JWT_SIGNING_KEY"),
            oidc_verify_access_token=verify_access_token,
            kv_store_url=env(env_prefix, "KV_STORE_URL"),
            event_store_url=env(env_prefix, "EVENT_STORE_URL"),
            tasks_url=env(env_prefix, "TASKS_URL"),
            app_domain=env(env_prefix, "APP_DOMAIN"),
            tools_allow=tools_allow,
            tools_deny=tools_deny,
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
        "OIDC_ADVERTISED_SCOPES",
        "OIDC_JWT_SIGNING_KEY",
        "OIDC_VERIFY_ACCESS_TOKEN",
        "KV_STORE_URL",
        "EVENT_STORE_URL",
        "TASKS_URL",
        "APP_DOMAIN",
        "AUTH_MODE",
        "TOOLS_ALLOW",
        "TOOLS_DENY",
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

    Mostly semantic — core says *what* a field is about, not which file
    documents it. One exception: ``readme`` marks a field prominent
    enough for a landing-page summary table, which is a prominence
    signal rather than a topic. A field may carry several tags, and
    appearing in more than one destination is intentional.
    """

    inferred: bool
    """True when no wizard control is offered for this field.

    A wizard-presentation concern only. The variable remains
    operator-settable and MUST still appear in env references and
    ``.env.example`` — this flag is not a signal that the value cannot
    be set.
    """

    wizard: Mapping[str, object]
    """Presentation hints for a config wizard.

    Recognised keys:

    - ``group`` — name of the wizard's collapsed "Advanced" section.
      Absence means the field is a primary question.
    - ``when`` — the context in which the question applies: ``"server"``
      for HTTP deployments, or ``"oidc"`` / ``"bearer"`` for the
      matching auth selection (both of which also imply an HTTP
      deployment).
    - ``secret`` — ``True`` when the value must never appear in a
      shareable link.
    - ``control`` — ``"emit"`` when a routing select emits this value
      and the field gets no question of its own.

    Empty when :attr:`inferred` is True.
    """


def _config_field_from(f: dataclasses.Field[Any]) -> ConfigField:
    """Build one :class:`ConfigField` record from a ``ServerConfig`` field.

    Raises:
        ValueError: If ``metadata["wizard"]`` is neither a mapping of hints
            nor the recognised ``"inferred"`` shorthand — e.g. a typo like
            ``"infered"``, or an accidental list. Falling through silently
            would otherwise crash later on ``raw_wizard.items()``, since
            only a mapping has ``.items()``.
    """
    if f.default is not dataclasses.MISSING:
        default: object = f.default
    elif f.default_factory is not dataclasses.MISSING:
        default = f.default_factory()
    else:
        # A field with neither a default nor a default_factory — a required
        # var. No ``ServerConfig`` field hits this (they all have defaults), but
        # ``domain_env_surface`` reaches it for a domain sub-config's required
        # field; ``_domain_env_var_from`` then reports ``required=True``.
        default = None

    tags = tuple(str(tag) for tag in f.metadata.get("tags", ()))

    # ``metadata={"wizard": "inferred"}`` is the shorthand for a field with
    # no control; anything else is a mapping of presentation hints.
    raw_wizard = f.metadata.get("wizard", {})
    inferred = raw_wizard == "inferred"
    wizard: dict[str, object] = {}
    if not inferred and raw_wizard:
        if not isinstance(raw_wizard, Mapping):
            raise ValueError(
                f"ServerConfig.{f.name}: metadata['wizard'] must be a "
                f"mapping of hints or the string 'inferred'; got "
                f"{raw_wizard!r}."
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

    Covers the same 21 variables as :func:`server_config_env_suffixes`, adding
    each field's type, default, help text, tags, and wizard hints.
    """
    return tuple(_config_field_from(f) for f in dataclasses.fields(ServerConfig))


_ENV_READ_FUNCS = frozenset({"env", "env_int", "env_float"})


def _literal_env_reads(node: ast.AST) -> list[tuple[str, int, int]]:
    """Return ``(suffix, lineno, col)`` for each literal env read under *node*.

    Walks *node* for unqualified ``env``/``env_int``/``env_float`` calls whose
    suffix argument is a string literal. Shared by :func:`domain_env_suffixes`
    and :func:`domain_env_surface` so both scans recognise exactly the same
    reads; a renamed import, an attribute-form call, or a variable/keyword-form
    suffix is invisible to either. Position is included so a consumer that wants
    deterministic ordering can sort by it; the suffix-only caller ignores it.
    """
    out: list[tuple[str, int, int]] = []
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in _ENV_READ_FUNCS
            and len(n.args) >= 2
            and isinstance(n.args[1], ast.Constant)
            and isinstance(n.args[1].value, str)
        ):
            out.append((n.args[1].value, n.lineno, n.col_offset))
    return out


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
    import inspect
    import textwrap

    # ``is_dataclass`` is true for instances too; require the class itself so a
    # mistakenly-passed instance fails loudly rather than silently scanning.
    if not isinstance(config_cls, type) or not dataclasses.is_dataclass(config_cls):
        raise TypeError(
            f"domain_env_suffixes: expected a dataclass type, got {config_cls!r}"
        )

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
        for suffix, _lineno, _col in _literal_env_reads(ast.parse(src)):
            found.add(suffix)

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


@dataclass(frozen=True)
class DomainEnvVar:
    """One env var a domain config reads, with provenance and field metadata.

    Carries a suffix a domain config (or a composed sub-config) reads, the class
    that read it, and — when resolvable — the metadata of the field it populates.
    This is the per-record counterpart to :func:`domain_env_suffixes`'s bare
    ``frozenset[str]``. The frozenset flattens every recursed suffix into one
    set and discards which class read it, so a consumer cannot attach per-field
    metadata to a suffix a composed sub-config contributed. Each record here
    keeps that provenance (:attr:`source`) and links the var to its declaring
    field, so a downstream generator can document a composed sub-config's vars
    with the same help / tags / wizard hints and required-ness as a top-level
    field — the gap recorded in the motivating issue — without flattening the
    config.
    """

    suffix: str
    """Env suffix as read — the part after ``{PREFIX}_``, e.g.
    ``TRANSFER_TTL_DEFAULT_S``. For a composed *section* this carries the
    section's own prefix, so it is generally **not** ``name.upper()``."""

    source: str
    """``__qualname__`` of the (sub-)config class whose ``from_env`` reads this
    var — e.g. ``TransferConfig``. This is the provenance the flat frozenset
    discards; a suffix read by two different classes yields one record per
    class."""

    name: str | None
    """Dataclass field the read populates — e.g. ``ttl_default_s`` — or ``None``
    when the read is not tied to a single constructor field (a throwaway read,
    or a value assembled from several reads). When ``None`` the metadata fields
    below carry neutral placeholders and must not be treated as authoritative;
    the var still appears so the surface never loses a suffix the frozenset had.
    """

    type_name: str | None
    """The field's annotation as written, e.g. ``float``. ``None`` when
    :attr:`name` is ``None``."""

    default: object
    """The field's declared default (a ``default_factory`` field reports the
    built value). ``None`` when :attr:`name` is ``None``."""

    help: str
    """The field's ``metadata["help"]``. Empty when undocumented or unresolved."""

    tags: tuple[str, ...]
    """The field's ``metadata["tags"]``. Empty when untagged or unresolved."""

    inferred: bool
    """True when the field carries the ``"inferred"`` wizard shorthand (no
    control offered). ``False`` when :attr:`name` is ``None``."""

    wizard: Mapping[str, object]
    """The field's wizard presentation hints. Empty for inferred or unresolved
    vars."""

    required: bool
    """True when the field has no default, so an operator must set the var.
    ``False`` when :attr:`name` is ``None`` — required-ness is a field property
    and is unknown for a read that maps to no field."""


def _domain_env_var_from(
    source: type, suffix: str, f: dataclasses.Field[Any] | None
) -> DomainEnvVar:
    """Build one :class:`DomainEnvVar`.

    When ``f`` is ``None`` the read could not be tied to a constructor field, so
    the metadata fields carry neutral placeholders (the var is still emitted so
    the surface is a strict superset of :func:`domain_env_suffixes`). Otherwise
    the field's metadata is extracted via :func:`_config_field_from` — the same
    reader ``server_config_surface`` uses, so help/tags/wizard parsing (and its
    validation) live in one place.
    """
    if f is None:
        return DomainEnvVar(
            suffix=suffix,
            source=source.__qualname__,
            name=None,
            type_name=None,
            default=None,
            help="",
            tags=(),
            inferred=False,
            wizard={},
            required=False,
        )
    cf = _config_field_from(f)
    required = (
        f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    )
    return DomainEnvVar(
        suffix=suffix,
        source=source.__qualname__,
        name=cf.name,
        type_name=cf.type_name,
        default=cf.default,
        help=cf.help,
        tags=cf.tags,
        inferred=cf.inferred,
        wizard=cf.wizard,
        required=required,
    )


def domain_env_surface(config_cls: type) -> tuple[DomainEnvVar, ...]:
    """Return :class:`DomainEnvVar` records for the env vars a domain config reads.

    The metadata-carrying counterpart of :func:`domain_env_suffixes`: it walks
    the same scan — literal ``env``/``env_int``/``env_float`` reads in
    ``config_cls.from_env`` plus every composed sub-config's ``from_env``,
    excluding the :class:`ServerConfig` field — but returns one record per var
    instead of a flat ``frozenset[str]``. Each record keeps the sub-config it
    came from (:attr:`DomainEnvVar.source`) and, when the read populates a
    constructor field, that field's metadata and required-ness, so a composed
    sub-config's vars can be documented like a top-level field's without
    flattening the config.

    **Suffix→field resolution** happens in two tiers. First, each ``cls(...)`` /
    ``{ClassName}(...)`` construction in ``from_env`` is inspected: a keyword
    argument whose value expression contains exactly one literal env read links
    that field to that suffix. This tier is what resolves a *section* field,
    whose suffix carries the section's own prefix (a ``ttl_default_s`` field read
    as ``TRANSFER_TTL_DEFAULT_S``), so ``name.upper()`` alone cannot identify it.

    Second, a read the first tier does not tie to a keyword — one consumed via
    an intermediate local (``x = parse_bool(env(prefix, "X")); cls(x=x)``), or a
    value assembled from several reads — falls back to the field whose
    ``name.upper()`` equals the suffix, since a field's env var is
    ``{PREFIX}_{NAME.upper()}`` by convention. This restores the metadata the
    pre-4.6 field-name resolution attached and covers the common shape where a
    value needs parsing or a fallback before construction.

    A suffix that neither tier resolves — a throwaway ``_ = env(...)``, or a
    *section* field read via a local (its prefixed suffix is not ``name.upper()``
    of any field, and the keyword did not carry the literal) — still yields a
    record, with :attr:`DomainEnvVar.name` ``None`` and neutral metadata, so no
    suffix the frozenset carried is dropped. Keep a section field's read inline
    in its constructor keyword (``field=env(prefix, "LITERAL")``) so its metadata
    is attached; a top-level field resolves either way.

    Records are ordered deterministically: depth-first over the config tree
    (a class's own reads before its sub-configs'), and within a class by the
    source position of each read — so a consumer that renders this tuple
    produces byte-stable output, as :func:`server_config_surface` does.
    ``{v.suffix for v in domain_env_surface(cls)}`` equals
    ``domain_env_suffixes(cls)``.

    Args:
        config_cls: The domain config dataclass; its ``from_env`` classmethod
            is the scan root.

    Returns:
        A tuple of :class:`DomainEnvVar` records, one per ``(source, suffix)``.

    Raises:
        TypeError: If ``config_cls`` is not a dataclass, or a sub-config's
            ``from_env`` is not a readable Python function.
        OSError: If a sub-config's ``from_env`` source cannot be read.
        NameError: If a field annotation cannot be resolved at
            :func:`typing.get_type_hints` time (the config or a sub-config is
            not defined at module scope, or has a broken forward reference).
        ValueError: If a resolved field's ``metadata["wizard"]`` is malformed
            (see :func:`_config_field_from`).
    """
    import inspect
    import textwrap

    if not isinstance(config_cls, type) or not dataclasses.is_dataclass(config_cls):
        raise TypeError(
            f"domain_env_surface: expected a dataclass type, got {config_cls!r}"
        )

    records: list[DomainEnvVar] = []
    visited: set[type] = set()
    seen: set[tuple[str, str]] = set()

    def _field_by_suffix(tree: ast.AST, cls: type) -> dict[str, str]:
        """Map a literal suffix to the ``cls(...)`` keyword it is read into.

        Only a keyword whose value expression contains exactly one literal env
        read is mapped; zero or several is ambiguous and left unmapped. If the
        same suffix appears in two keywords (unusual — a section's suffixes are
        distinct), the first in source order wins and the later field goes
        unmapped, matching the frozenset's de-duplication of that suffix.
        """
        ctor_names = {"cls", cls.__name__}
        mapping: dict[str, str] = {}
        for n in ast.walk(tree):
            if not (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in ctor_names
            ):
                continue
            for kw in n.keywords:
                if kw.arg is None:  # ``**kwargs`` splat — no field name
                    continue
                literals = {lit for lit, _, _ in _literal_env_reads(kw.value)}
                if len(literals) == 1:
                    mapping.setdefault(next(iter(literals)), kw.arg)
        return mapping

    def _scan(cls: type) -> None:
        try:
            src = textwrap.dedent(inspect.getsource(cls.from_env))  # type: ignore[attr-defined]
        except (OSError, TypeError) as exc:  # source unreadable / not a function
            raise type(exc)(
                f"domain_env_surface: cannot read source for "
                f"{cls.__qualname__}.from_env: {exc}"
            ) from exc
        tree = ast.parse(src)
        field_of = _field_by_suffix(tree, cls)
        fields_by_name = {f.name: f for f in dataclasses.fields(cls)}
        # Field-name fallback: a field's env var is ``{PREFIX}_{NAME.upper()}``
        # by convention, so a read this class does not tie to a constructor
        # keyword (consumed via a local, or assembled from several reads) still
        # resolves to the field whose ``name.upper()`` equals the suffix.
        fields_by_suffix = {f.name.upper(): f for f in dataclasses.fields(cls)}
        ordered: list[str] = []
        local_seen: set[str] = set()
        for suffix, _lineno, _col in sorted(
            _literal_env_reads(tree), key=lambda t: (t[1], t[2])
        ):
            if suffix not in local_seen:
                local_seen.add(suffix)
                ordered.append(suffix)
        for suffix in ordered:
            key = (cls.__qualname__, suffix)
            if key in seen:
                continue
            seen.add(key)
            fname = field_of.get(suffix)
            if fname is not None:
                f = fields_by_name.get(fname)
            else:
                f = fields_by_suffix.get(suffix)
            records.append(_domain_env_var_from(cls, suffix, f))

    def _visit(cls: type) -> None:
        if cls in visited or cls is ServerConfig or not dataclasses.is_dataclass(cls):
            return
        visited.add(cls)
        if hasattr(cls, "from_env"):
            _scan(cls)
        try:
            hints = typing.get_type_hints(cls)
        except NameError as exc:
            raise NameError(
                f"domain_env_surface: cannot resolve type hints for "
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
    return tuple(records)
