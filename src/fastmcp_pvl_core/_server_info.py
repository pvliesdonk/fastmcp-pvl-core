"""Reusable ``get_server_info`` tool factory.

Lets domain MCP servers register a single tool that reports both the
wrapper's own version (``server_version``) and, optionally, the upstream
service's version.  This is the one-call sanity check operators want when
verifying that a fresh image is actually serving traffic — see issue #17
for the motivating story (a ``docker pull`` without rebuild leaving an
old image up).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

UpstreamResult = str | dict[str, Any] | None
"""What an ``upstream_version`` callable may return.

- ``str`` — bare version string; wrapped as ``{"version": <str>}``.
- ``dict`` — full upstream block; used as-is.
- ``None`` — wrapped as ``{"version": None}``.
"""

UpstreamProvider = Callable[[], UpstreamResult | Awaitable[UpstreamResult]]
"""Sync or async zero-arg callable returning an :data:`UpstreamResult`."""


def register_server_info_tool(
    mcp: FastMCP,
    *,
    server_version: str,
    server_name: str,
    upstream_version: UpstreamProvider | None = None,
    upstream_label: str = "upstream",
    tool_name: str = "get_server_info",
    description: str | None = None,
) -> None:
    """Register a ``get_server_info`` tool on a FastMCP instance.

    The registered tool is read-only and returns a structured payload::

        {
          "server_name": "<server_name>",
          "server_version": "<server_version>",
          "core_version": "<fastmcp_pvl_core.__version__>",
          "<upstream_label>": {"version": "..."}   # only when upstream_version set
        }

    If the upstream lookup raises, the upstream block becomes
    ``{"error": "<message>"}`` so the tool still returns the wrapper info
    instead of failing the whole call.

    Args:
        mcp: The :class:`FastMCP` instance to register on.
        server_version: The wrapper's own version (typically the domain
            project's ``__version__``).
        server_name: The wrapper's package/distribution name (e.g.
            ``"paperless-mcp"``).
        upstream_version: Optional zero-arg callable (sync or async)
            returning either a bare version string, a full dict block, or
            ``None``.  Wrapped automatically.
        upstream_label: Key under which the upstream block appears in the
            response.  Defaults to ``"upstream"``.  Ignored when
            ``upstream_version`` is ``None``.
        tool_name: Tool name to register.  Defaults to ``"get_server_info"``.
        description: Override for the tool description.  Defaults to a
            built-in description that mentions the wrapper name.
    """
    # Imported lazily so callers that never use this helper don't pay
    # for the mcp.types import.
    from mcp.types import ToolAnnotations

    from fastmcp_pvl_core import __version__ as core_version

    default_description = (
        f"Report wrapper and upstream version info for {server_name}. "
        "Returns server_name, server_version, core_version "
        "(fastmcp-pvl-core), and (when configured) an upstream version "
        "block.  Useful for verifying a deployment matches the expected "
        "build."
    )

    async def get_server_info() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "server_name": server_name,
            "server_version": server_version,
            "core_version": core_version,
        }
        if upstream_version is None:
            return payload

        try:
            result: Any = upstream_version()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 — surface as structured error
            logger.warning(
                "get_server_info upstream lookup failed: %s", exc, exc_info=True
            )
            payload[upstream_label] = {"error": str(exc)}
            return payload

        if isinstance(result, dict):
            payload[upstream_label] = result
        elif result is None:
            payload[upstream_label] = {"version": None}
        else:
            payload[upstream_label] = {"version": str(result)}
        return payload

    mcp.tool(
        name=tool_name,
        description=description or default_description,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(get_server_info)
