"""Universal server configuration.

Downstream projects compose this into their own domain config dataclass
(they do not inherit). Core only owns fields that are universal to any
FastMCP server: transport, host, port, auth credentials, event store URL,
MCP Apps domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastmcp_pvl_core._env import env, parse_bool, parse_scopes

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

    event_store_url: str | None = None
    app_domain: str | None = None

    auth_mode: str | None = None

    bearer_tokens_file: Path | None = None
    # Subject for the single-token bearer mode; ignored when
    # ``bearer_tokens_file`` is set (mapped mode uses per-token subjects).
    bearer_default_subject: str = DEFAULT_BEARER_SUBJECT

    def __post_init__(self) -> None:
        """Enforce the non-blank ``bearer_default_subject`` invariant.

        Blank, whitespace-only, or ``None`` values are rewritten to
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

        The ``or ""`` in the guard is belt-and-suspenders against a
        stray ``None`` reaching this method from a non-mypy-checked
        construction site (unpacked-config dict, dynamic test fixture,
        etc.).  The field is typed ``str`` and mypy is the primary
        enforcement; the runtime guard preserves the equivalent of the
        prior ``_auth.py`` fallback's robustness.
        """
        if not (self.bearer_default_subject or "").strip():
            # ``object.__setattr__`` bypasses the frozen-dataclass guard;
            # this is the documented escape hatch for ``__post_init__``
            # normalisation on a frozen dataclass.
            object.__setattr__(self, "bearer_default_subject", DEFAULT_BEARER_SUBJECT)

    @classmethod
    def from_env(cls, env_prefix: str) -> ServerConfig:
        """Load all fields from ``{env_prefix}_*`` environment variables.

        Unknown values for ``TRANSPORT`` silently fall back to ``"stdio"``
        rather than raising.  This matches the rest of the env-reading
        helpers, which prefer permissive defaults over hard failures.

        Args:
            env_prefix: Env var prefix, no trailing underscore needed.

        Returns:
            A populated :class:`ServerConfig` instance.
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
        port_str = env(env_prefix, "PORT", "8000")

        scopes_raw = env(env_prefix, "OIDC_REQUIRED_SCOPES")
        scopes = tuple(parse_scopes(scopes_raw) or ())

        verify_access_raw = env(env_prefix, "OIDC_VERIFY_ACCESS_TOKEN")
        verify_access_token = (
            parse_bool(verify_access_raw) if verify_access_raw is not None else False
        )

        tokens_file_raw = env(env_prefix, "BEARER_TOKENS_FILE")
        bearer_tokens_file = (
            Path(tokens_file_raw).expanduser() if tokens_file_raw else None
        )
        bearer_default_subject = env(
            env_prefix, "BEARER_DEFAULT_SUBJECT", DEFAULT_BEARER_SUBJECT
        )

        return cls(
            transport=transport,
            host=host,
            port=int(port_str),
            base_url=env(env_prefix, "BASE_URL"),
            bearer_token=env(env_prefix, "BEARER_TOKEN"),
            oidc_config_url=env(env_prefix, "OIDC_CONFIG_URL"),
            oidc_client_id=env(env_prefix, "OIDC_CLIENT_ID"),
            oidc_client_secret=env(env_prefix, "OIDC_CLIENT_SECRET"),
            oidc_audience=env(env_prefix, "OIDC_AUDIENCE"),
            oidc_required_scopes=scopes,
            oidc_jwt_signing_key=env(env_prefix, "OIDC_JWT_SIGNING_KEY"),
            oidc_verify_access_token=verify_access_token,
            event_store_url=env(env_prefix, "EVENT_STORE_URL"),
            app_domain=env(env_prefix, "APP_DOMAIN"),
            auth_mode=env(env_prefix, "AUTH_MODE"),
            bearer_tokens_file=bearer_tokens_file,
            bearer_default_subject=bearer_default_subject,
        )
