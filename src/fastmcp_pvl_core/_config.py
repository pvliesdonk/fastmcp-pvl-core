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
    bearer_default_subject: str = "bearer-anon"

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
        # ``Path(...)`` keeps a leading ``~`` literal here.  Expansion is
        # performed once, in :func:`fastmcp_pvl_core._auth._load_bearer_tokens`,
        # so both this env-driven path and a directly-constructed
        # ``ServerConfig(bearer_tokens_file=Path("~/tokens.toml"))`` resolve
        # the tilde at the same call site.
        bearer_tokens_file = Path(tokens_file_raw) if tokens_file_raw else None
        bearer_default_subject = env(
            env_prefix, "BEARER_DEFAULT_SUBJECT", "bearer-anon"
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
