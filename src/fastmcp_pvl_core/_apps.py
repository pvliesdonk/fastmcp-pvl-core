"""MCP Apps helpers — wraps private fastmcp addressing and capability APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    from fastmcp.apps.config import UI_EXTENSION_ID
    from fastmcp.server.providers.addressing import hash_tool, hashed_backend_name
except ImportError as exc:
    raise ImportError(
        "fastmcp.apps.config and fastmcp.server.providers.addressing are required "
        "(fastmcp >= 3.3.1). Pin fastmcp accordingly in pyproject.toml."
    ) from exc

if TYPE_CHECKING:
    from fastmcp.server.context import Context


def app_tool_meta(app_name: str, tool_name: str) -> dict[str, object]:
    """Build the meta= dict for an app-only backend tool registration.

    Returns the dict that @mcp.tool(meta=...) expects for a tool whose
    visibility=["app"] makes it callable from the app UI but hidden from
    the LLM's tool list. Pass the result directly to meta=:

        @mcp.tool(
            meta=app_tool_meta("vault", "vault_list"),
            app=AppConfig(resource_uri=..., visibility=["app"]),
        )
        async def vault_list(...): ...
    """
    return {"fastmcp": {"app": app_name, "_tool_hash": hash_tool(app_name, tool_name)}}


def app_tool_address(app_name: str, tool_name: str) -> str:
    """Return the hashed callable name for use in SPA HTML rewrites.

    The returned string is the name the MCP Apps JS SDK uses when calling
    back to the server. Use it to rewrite static SPA HTML literals at
    import time so they survive server-composition renames:

        html = re.sub(
            r"vault___(vault_[a-z_]+)",
            lambda m: app_tool_address("vault", m.group(1)),
            html,
        )
    """
    return hashed_backend_name(app_name, tool_name)


def client_supports_apps(ctx: Context) -> bool:
    """Return True if the connected client advertises the MCP Apps extension.

    Thin wrapper around ctx.client_supports_extension(UI_EXTENSION_ID) so
    downstream does not import UI_EXTENSION_ID from fastmcp internals.
    Returns False when called outside a request context.
    """
    return bool(ctx.client_supports_extension(UI_EXTENSION_ID))
