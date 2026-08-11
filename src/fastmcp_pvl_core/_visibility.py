"""Operator-facing tool-visibility filtering.

Applies the ``{PREFIX}_TOOLS_ALLOW`` / ``{PREFIX}_TOOLS_DENY`` operator
contract carried by :class:`~fastmcp_pvl_core._config.ServerConfig` to a
FastMCP server, so an instance only exposes the tools its operator wants
— every hidden tool disappears from ``tools/list`` *and* is rejected on
``tools/call``.

The env vars are operator-side configuration; the semantics below are
shape decisions pvl-core owns (downstream servers wire the helper in and
accept them):

- ``{PREFIX}_TOOLS_ALLOW`` — comma-separated explicit tool names; when
  set, the instance exposes *only* these tools.
- ``{PREFIX}_TOOLS_DENY`` — comma-separated explicit tool names; the
  listed tools are hidden.
- Setting both is a :class:`~fastmcp_pvl_core._errors.ConfigurationError`
  (an allowlist already expresses every exclusion).
- Names matching no registered tool are inert: they are neither an error
  nor a warning, so one operator config survives downstream releases that
  add or remove tools.
- Only tools are affected; resources, resource templates, and prompts
  stay untouched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ._errors import ConfigurationError

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ._config import ServerConfig

logger = logging.getLogger(__name__)


def apply_tool_visibility(mcp: FastMCP, config: ServerConfig) -> None:
    """Apply the operator's tool allow-/denylist to *mcp*.

    Call once while building the server, *after* any visibility calls the
    server makes itself: fastmcp resolves visibility transforms in call
    order with later calls overriding earlier ones, so applying the
    operator's lists last is what makes operator configuration win.
    Registration order does not matter — the transforms are evaluated at
    listing/call time and cover tools registered (or mounted) afterwards.

    Both parameters are wiring, not hooks: *config* carries the
    operator-side ``tools_allow`` / ``tools_deny`` values (env-driven via
    :meth:`ServerConfig.from_env`), and there is deliberately no kwarg to
    alter the semantics.

    Args:
        mcp: The server instance to filter.
        config: Universal server config; only ``tools_allow`` and
            ``tools_deny`` are read. With neither set this is a no-op.

    Raises:
        ConfigurationError: If ``tools_allow`` and ``tools_deny`` are both
            non-empty. (:meth:`ServerConfig.from_env` already rejects this
            naming the offending env vars; this guard covers directly
            constructed configs.)
    """
    allow = config.tools_allow
    deny = config.tools_deny
    if allow and deny:
        raise ConfigurationError(
            "tools_allow and tools_deny are both set; set at most one — "
            "an allowlist already expresses every exclusion."
        )
    if allow:
        mcp.disable(components={"tool"})
        mcp.enable(names=set(allow), components={"tool"})
        logger.info("Tool allowlist active: exposing only %s", ", ".join(sorted(allow)))
    elif deny:
        mcp.disable(names=set(deny), components={"tool"})
        logger.info("Tool denylist active: hiding %s", ", ".join(sorted(deny)))
