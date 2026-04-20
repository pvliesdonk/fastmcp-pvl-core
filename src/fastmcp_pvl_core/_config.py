"""Universal server configuration.

Downstream projects compose this into their own domain config dataclass
(they do not inherit). Core only owns fields that are universal to any
FastMCP server: transport, host, port, auth credentials, event store URL,
MCP Apps domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from fastmcp_pvl_core._env import env, parse_scopes

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

    event_store_url: str | None = None
    app_domain: str | None = None

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
            event_store_url=env(env_prefix, "EVENT_STORE_URL"),
            app_domain=env(env_prefix, "APP_DOMAIN"),
        )
