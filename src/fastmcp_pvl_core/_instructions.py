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

from ._errors import ConfigurationError

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
) -> str:  # pragma: no cover
    raise NotImplementedError
