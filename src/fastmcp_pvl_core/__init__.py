"""Shared FastMCP infrastructure.

Imported by MCP server projects that want auth mode dispatch,
middleware wiring, logging setup, config helpers, and server
factory building blocks without duplicating them per repo.
"""

from fastmcp_pvl_core._artifacts import (
    ArtifactStore,
    TokenRecord,
    get_artifact_store,
    set_artifact_store,
)
from fastmcp_pvl_core._auth import (
    AuthMode,
    build_auth,
    build_bearer_auth,
    build_oidc_proxy_auth,
    build_remote_auth,
    resolve_auth_mode,
)
from fastmcp_pvl_core._cli import make_serve_parser, normalise_http_path
from fastmcp_pvl_core._config import ServerConfig, Transport
from fastmcp_pvl_core._env import env, parse_bool, parse_list, parse_scopes
from fastmcp_pvl_core._factory import (
    build_event_store,
    build_instructions,
    compute_app_domain,
)
from fastmcp_pvl_core._icons import IconSpec, make_icon, register_tool_icons
from fastmcp_pvl_core._logging import SecretMaskFilter, configure_logging_from_env
from fastmcp_pvl_core._middleware import wire_middleware_stack
from fastmcp_pvl_core._server_info import (
    UpstreamProvider,
    UpstreamResult,
    register_server_info_tool,
)

__version__ = "1.0.0"  # PSR overrides at build time

__all__ = [
    "ArtifactStore",
    "AuthMode",
    "IconSpec",
    "SecretMaskFilter",
    "ServerConfig",
    "TokenRecord",
    "Transport",
    "UpstreamProvider",
    "UpstreamResult",
    "build_auth",
    "build_bearer_auth",
    "build_event_store",
    "build_instructions",
    "build_oidc_proxy_auth",
    "build_remote_auth",
    "compute_app_domain",
    "configure_logging_from_env",
    "env",
    "get_artifact_store",
    "make_icon",
    "make_serve_parser",
    "normalise_http_path",
    "parse_bool",
    "parse_list",
    "parse_scopes",
    "register_server_info_tool",
    "register_tool_icons",
    "resolve_auth_mode",
    "set_artifact_store",
    "wire_middleware_stack",
]
