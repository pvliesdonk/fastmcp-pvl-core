"""Shared FastMCP infrastructure.

Imported by MCP server projects that want auth mode dispatch,
middleware wiring, logging setup, config helpers, and server
factory building blocks without duplicating them per repo.
"""

from fastmcp_pvl_core._auth import AuthMode, resolve_auth_mode
from fastmcp_pvl_core._config import ServerConfig, Transport
from fastmcp_pvl_core._env import env, parse_bool, parse_list, parse_scopes

__version__ = "0.0.0"  # PSR overrides at build time

__all__ = [
    "AuthMode",
    "ServerConfig",
    "Transport",
    "env",
    "parse_bool",
    "parse_list",
    "parse_scopes",
    "resolve_auth_mode",
]
