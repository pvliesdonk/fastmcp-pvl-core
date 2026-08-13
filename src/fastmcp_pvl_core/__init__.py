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
    ConfigField,
    DomainEnvVar,
    ServerConfig,
    Transport,
    domain_env_suffixes,
    domain_env_surface,
    server_config_env_suffixes,
    server_config_surface,
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
from ._jobs import (
    JOB_POLL_TOOL_NAME,
    JOB_RETRY_AFTER_S,
    JobHandle,
    JobLimitExceededError,
    JobNotFoundError,
    JobRecord,
    Jobs,
    JobsConfig,
    JobStatus,
    build_jobs,
    register_job_tools,
    register_long_running_tool,
)
from ._kv_store import build_kv_store
from ._logging import SecretMaskFilter, configure_logging_from_env
from ._middleware import wire_middleware_stack
from ._server_info import (
    UpstreamProvider,
    UpstreamResult,
    register_server_info_tool,
)
from ._subject import get_claims, get_subject
from ._tasks import configure_task_backend
from ._transfer import (
    FetchResult,
    TransferBadGatewayError,
    TransferConfig,
    TransferForbiddenError,
    TransferGatewayTimeoutError,
    TransferKind,
    TransferLinks,
    TransferNotFoundError,
    TransferRateLimitedError,
    TransferReadResult,
    TransferResourceGoneError,
    TransferSink,
    TransferSinkError,
    TransferUnavailableError,
    TransferValidator,
    build_transfer_links,
    decode_base64_capped,
    fetch_url,
    register_transfer_routes,
)
from ._visibility import apply_tool_visibility

__version__ = "4.11.0"  # PSR overrides at build time

__all__ = [
    "AuthMode",
    "ConfigField",
    "ConfigurationError",
    "DomainEnvVar",
    "FetchResult",
    "IconSpec",
    "JOB_POLL_TOOL_NAME",
    "JOB_RETRY_AFTER_S",
    "JobHandle",
    "JobLimitExceededError",
    "JobNotFoundError",
    "JobRecord",
    "JobStatus",
    "Jobs",
    "JobsConfig",
    "SecretMaskFilter",
    "ServerConfig",
    "Transport",
    "TransferBadGatewayError",
    "TransferConfig",
    "TransferForbiddenError",
    "TransferGatewayTimeoutError",
    "TransferKind",
    "TransferLinks",
    "TransferNotFoundError",
    "TransferRateLimitedError",
    "TransferReadResult",
    "TransferResourceGoneError",
    "TransferSink",
    "TransferSinkError",
    "TransferUnavailableError",
    "TransferValidator",
    "UpstreamProvider",
    "UpstreamResult",
    "any_check",
    "app_tool_address",
    "app_tool_meta",
    "apply_tool_visibility",
    "build_auth",
    "build_bearer_auth",
    "build_event_store",
    "build_instructions",
    "build_jobs",
    "build_kv_store",
    "build_oidc_proxy_auth",
    "build_remote_auth",
    "build_transfer_links",
    "client_supports_apps",
    "compute_app_domain",
    "configure_logging_from_env",
    "configure_task_backend",
    "decode_base64_capped",
    "domain_env_suffixes",
    "domain_env_surface",
    "env",
    "env_float",
    "env_int",
    "fetch_url",
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
    "register_job_tools",
    "register_long_running_tool",
    "register_server_info_tool",
    "register_tool_icons",
    "register_transfer_routes",
    "resolve_auth_mode",
    "server_config_env_suffixes",
    "server_config_surface",
    "wire_middleware_stack",
]
