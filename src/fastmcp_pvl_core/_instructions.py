"""Composable MCP server instructions.

Instructions carry what no single tool description can carry: identity, a
documentation pointer, the capability map, cross-tool workflows, enforced
instance facts, and operator context. Core features and domain code each
add snippets to the one builder per server; :func:`finalize_instructions`
prunes snippets whose tools the operator hid, serialises by priority, applies
the env contract, and sets ``FastMCP.instructions``.

Design: ``docs/superpowers/specs/2026-08-25-instructions-builder-design.md``.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._env import env
from ._errors import ConfigurationError
from ._visibility import exposed_tool_names

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ._config import ServerConfig

logger = logging.getLogger(__name__)

#: Named anchors on the priority scale. Priority is the mechanism; a
#: contributor that wants "just after the capability map" writes
#: ``CAPABILITIES + 10``.
IDENTITY = 0
DOCS = 100
CAPABILITIES = 200
WORKFLOWS = 300
INSTANCE = 400
OPERATOR = 500


@dataclass(frozen=True)
class _Snippet:
    text: str
    priority: int
    tools: frozenset[str]
    seq: int


class InstructionsBuilder:
    """Ordered, tool-aware collection of instruction snippets for one server.

    Obtain it with :func:`instructions_for`; do not construct one per call
    site. Every ``add`` is a plain string with a priority and the tool names
    it references. Rendering happens once, in :func:`finalize_instructions`.
    """

    def __init__(self) -> None:
        self._snippets: list[_Snippet] = []
        self._frozen = False
        self._result: str | None = None

    def add(self, text: str, *, priority: int, tools: Iterable[str] = ()) -> None:
        """Add one snippet.

        Args:
            text: Model-facing prose. Surrounding whitespace is stripped.
            priority: Sort key; ties keep insertion order. Use the anchors
                (``IDENTITY`` … ``OPERATOR``) or an offset from one.
            tools: Tool names the snippet references. If any is hidden or
                absent at finalize, the whole snippet is dropped.

        Raises:
            ConfigurationError: *text* is empty or whitespace.
            RuntimeError: The builder was already finalized.
        """
        if self._frozen:
            raise RuntimeError(
                "instructions already finalized; "
                "add snippets before finalize_instructions"
            )
        cleaned = text.strip()
        if not cleaned:
            raise ConfigurationError("instruction snippet text is empty")
        self._snippets.append(
            _Snippet(cleaned, priority, frozenset(tools), len(self._snippets))
        )

    def identity(self, text: str) -> None:
        """Add the one-line identity (``priority=IDENTITY``).

        Exactly one is required.
        """
        self.add(text, priority=IDENTITY)

    def documentation(self, url: str) -> None:
        """Add the documentation pointer in pvl-core's fixed shape.

        Priority: ``DOCS``.
        """
        cleaned = url.strip()
        if not cleaned:
            raise ConfigurationError("documentation url is empty")
        self.add(f"Full documentation for this server: {cleaned}", priority=DOCS)

    def _render(self, exposed: frozenset[str]) -> str:
        """Prune against *exposed* tool names and serialise.

        No env, no identity check.
        """
        kept: list[_Snippet] = []
        for s in self._snippets:
            missing = s.tools - exposed
            if missing:
                logger.debug(
                    (
                        "instructions: dropping snippet at priority %d; "
                        "tool(s) not exposed: %s"
                    ),
                    s.priority,
                    ", ".join(sorted(missing)),
                )
                continue
            kept.append(s)
        kept.sort(key=lambda s: (s.priority, s.seq))
        return "\n\n".join(s.text for s in kept)


_builders: weakref.WeakKeyDictionary[FastMCP, InstructionsBuilder] = (
    weakref.WeakKeyDictionary()
)


def instructions_for(mcp: FastMCP) -> InstructionsBuilder:
    """Return the builder for *mcp*, creating it on first use.

    One builder per server instance; ``register_*`` helpers and domain code
    reach it through the ``mcp`` they already hold, so no helper grows a kwarg.
    """
    builder = _builders.get(mcp)
    if builder is None:
        builder = InstructionsBuilder()
        _builders[mcp] = builder
    return builder


def finalize_instructions(
    mcp: FastMCP, config: ServerConfig, *, env_prefix: str
) -> str:
    """Render the server's instructions once, apply the env contract, and set them.

    Call after :func:`apply_tool_visibility`, and after every ``register_*``
    helper and domain contribution. Order of operations:

    1. exactly one identity snippet must remain (checked over the
       unpruned snippets: identity has no ``tools``, so pruning cannot
       remove it, and checking first keeps the builder unmutated on
       failure)
    2. exposed tools = :func:`exposed_tool_names` (registered ∧ operator
       rule) — computed before any further mutation, since it is the other
       source of :class:`~fastmcp_pvl_core._errors.ConfigurationError`
       (both visibility lists set) and must also leave the builder
       unmutated on failure
    3. ``{P}_INSTRUCTIONS_EXTRA`` appended at ``OPERATOR``; legacy
       ``{P}_INSTRUCTIONS`` replaces the whole text with one ``WARNING``
    4. drop every snippet whose ``tools`` are not all exposed (``DEBUG`` per drop)
    5. serialise by ``(priority, insertion)``, blank-line separated
    6. set ``mcp.instructions``, cache, freeze the builder

    A second call returns the cached string without re-reading env or
    logging. Per-subject auth visibility is out of scope (see the spec).

    Args:
        mcp: The server whose builder to finalize.
        config: Universal server config; only ``tools_allow`` / ``tools_deny``
            are read.
        env_prefix: Env-var prefix, with or without a trailing underscore.

    Returns:
        The final instructions string, also set on ``mcp.instructions``.

    Raises:
        ConfigurationError: No identity, more than one identity, or both
            visibility lists set.
        RuntimeError: FastMCP's component enumeration changed shape.
    """
    builder = instructions_for(mcp)
    if builder._result is not None:
        return builder._result

    prefix = env_prefix.rstrip("_")
    legacy = env(prefix, "INSTRUCTIONS") or ""
    extra = env(prefix, "INSTRUCTIONS_EXTRA") or ""

    if legacy:
        logger.warning(
            "%s_INSTRUCTIONS replaces all generated guidance and is deprecated; "
            "use %s_INSTRUCTIONS_EXTRA to add context.%s",
            prefix,
            prefix,
            f" {prefix}_INSTRUCTIONS_EXTRA is set and was ignored." if extra else "",
        )
        text = legacy
    else:
        identities = [s for s in builder._snippets if s.priority == IDENTITY]
        if len(identities) != 1:
            raise ConfigurationError(
                "instructions need exactly one identity snippet, found "
                f"{len(identities)}; call instructions_for(mcp).identity(...) once"
            )
        exposed = exposed_tool_names(mcp, config)
        if extra:
            builder.add(extra, priority=OPERATOR)
        text = builder._render(exposed)

    mcp.instructions = text
    builder._result = text
    builder._frozen = True
    return text
