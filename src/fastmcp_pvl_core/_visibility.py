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
- Individual names matching no registered tool are inert: neither an
  error nor a warning, so one operator config survives downstream
  releases that add or remove tools. The degenerate case is different:
  an allowlist that leaves *zero* tools exposed logs a ``WARNING`` (the
  server still boots), because a fully mistyped or fully stale
  ``TOOLS_ALLOW`` would otherwise present as a silent total tool outage.
- Only tools are affected; resources, resource templates, and prompts
  stay untouched.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ._errors import ConfigurationError
from ._icons import _index_tools_by_name

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ._config import ServerConfig

logger = logging.getLogger(__name__)


def _reject_both_lists(config: ServerConfig) -> None:
    """Raise if *config* sets both ``tools_allow`` and ``tools_deny``.

    Shared by :func:`apply_tool_visibility` and :func:`exposed_tool_names` so
    the message lives in one place.
    """
    if config.tools_allow and config.tools_deny:
        raise ConfigurationError(
            "tools_allow and tools_deny are both set; set at most one — "
            "an allowlist already expresses every exclusion."
        )


def apply_tool_visibility(mcp: FastMCP, config: ServerConfig) -> None:
    """Apply the operator's tool allow-/denylist to *mcp*.

    Call once while building the server, as the *last* step after tools
    are registered and after any visibility calls the server makes
    itself: fastmcp resolves visibility transforms in call order with
    later calls overriding earlier ones, so applying the operator's
    lists last is what makes operator configuration win — and the
    zero-tools-exposed diagnostic below is only meaningful once the
    tools it judges are registered. The filtering itself is
    order-independent: transforms are evaluated at listing/call time and
    cover tools registered (or mounted) afterwards.

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
    _reject_both_lists(config)
    allow = config.tools_allow
    deny = config.tools_deny
    if allow:
        mcp.disable(components={"tool"})
        mcp.enable(names=set(allow), components={"tool"})
        logger.info("Tool allowlist active: exposing only %s", ", ".join(sorted(allow)))
        _warn_if_no_tools_exposed(mcp, allow)
    elif deny:
        mcp.disable(names=set(deny), components={"tool"})
        logger.info("Tool denylist active: hiding %s", ", ".join(sorted(deny)))


def _warn_if_no_tools_exposed(mcp: FastMCP, allow: tuple[str, ...]) -> None:
    """Warn when the applied allowlist leaves zero tools exposed.

    A partially matching allowlist stays silent by design, but a fully
    mistyped (or fully stale) ``TOOLS_ALLOW`` turns the instance into a
    silent total tool outage that an operator can only detect from a
    connected client seeing no tools. Warning — not raising — keeps the
    forward-compat tolerance: a deployment must not be taken down by a
    name that stops matching after a downstream upgrade.

    The check needs the (async) listing path, so it runs in a private
    event loop; inside an already-running loop it is skipped rather than
    guessed at. Best-effort by construction: a failure here must never
    take down startup.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return
    try:
        visible = asyncio.run(mcp.list_tools())
    except Exception:
        logger.debug("Tool allowlist zero-match check skipped", exc_info=True)
        return
    if not visible:
        logger.warning(
            "Tool allowlist matched no registered tool — this instance "
            "exposes ZERO tools. Check the configured names (%s) for typos "
            "or names gone stale after an upgrade.",
            ", ".join(sorted(allow)),
        )


def exposed_tool_names(mcp: FastMCP, config: ServerConfig) -> frozenset[str]:
    """Tool names *mcp* exposes under the operator allow-/denylist in *config*.

    Evaluates the same rule :func:`apply_tool_visibility` installs, without
    asking FastMCP: it stores ``disable``/``enable`` as async transforms with
    no synchronous query, and ``make_server`` is synchronous. Registered names
    come from the same enumeration ``register_tool_icons`` uses.

    This models only the operator allow-/denylist rule over the tools
    registered when it is called; it does not model server-side
    ``mcp.disable()``/``mcp.enable()`` calls, whose transforms FastMCP
    resolves at listing time.

    Raises:
        ConfigurationError: ``tools_allow`` and ``tools_deny`` are both set.
        RuntimeError: FastMCP's component enumeration changed shape.
    """
    _reject_both_lists(config)
    allow = config.tools_allow
    deny = config.tools_deny
    registered = frozenset(_index_tools_by_name(mcp))
    if allow:
        return registered & frozenset(allow)
    if deny:
        return registered - frozenset(deny)
    return registered
