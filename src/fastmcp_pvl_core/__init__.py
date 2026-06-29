"""Shared FastMCP infrastructure.

Imported by MCP server projects that want auth mode dispatch,
middleware wiring, logging setup, config helpers, and server
factory building blocks without duplicating them per repo.
"""

from ._apps import app_tool_address, app_tool_meta, client_supports_apps
from ._auth import (
    AuthMode,
    build_auth,
    build_bearer_auth,
    build_oidc_proxy_auth,
    build_remote_auth,
    resolve_auth_mode,
)
from ._authorization import (
    any_check,
    load_acl,
    make_acl_check,
    make_claims_check,
    parse_claim_grants,
)
from ._cli import make_serve_parser, normalise_http_path
from ._config import (
    ServerConfig,
    Transport,
    domain_env_suffixes,
    server_config_env_suffixes,
)
from ._debug import maybe_start_debugpy
from ._env import (
    env,
    env_float,
    env_int,
    parse_bool,
    parse_list,
    parse_scopes,
)
from ._errors import ConfigurationError
from ._factory import (
    build_event_store,
    build_instructions,
    compute_app_domain,
)
from ._icons import IconSpec, make_icon, register_tool_icons
from ._kv_store import build_kv_store
from ._logging import SecretMaskFilter, configure_logging_from_env
from ._middleware import wire_middleware_stack
from ._server_info import (
    UpstreamProvider,
    UpstreamResult,
    register_server_info_tool,
)
from ._subject import get_claims, get_subject

__version__ = "4.3.0"  # PSR overrides at build time

__all__ = [
    "AuthMode",
    "ConfigurationError",
    "IconSpec",
    "SecretMaskFilter",
    "ServerConfig",
    "Transport",
    "UpstreamProvider",
    "UpstreamResult",
    "any_check",
    "app_tool_address",
    "app_tool_meta",
    "build_auth",
    "build_bearer_auth",
    "build_event_store",
    "build_instructions",
    "build_kv_store",
    "build_oidc_proxy_auth",
    "build_remote_auth",
    "client_supports_apps",
    "compute_app_domain",
    "configure_logging_from_env",
    "domain_env_suffixes",
    "env",
    "env_float",
    "env_int",
    "get_claims",
    "get_subject",
    "load_acl",
    "make_acl_check",
    "make_claims_check",
    "make_icon",
    "make_serve_parser",
    "maybe_start_debugpy",
    "normalise_http_path",
    "parse_bool",
    "parse_claim_grants",
    "parse_list",
    "parse_scopes",
    "register_server_info_tool",
    "register_tool_icons",
    "resolve_auth_mode",
    "server_config_env_suffixes",
    "wire_middleware_stack",
]
