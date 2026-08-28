"""Composable MCP server instructions.

Instructions carry what no single tool description can carry: identity, a
documentation pointer, the capability map, cross-tool workflows, enforced
instance facts, and operator context. Core features and domain code each
add snippets to the one builder per server; :func:`finalize_instructions`
prunes snippets whose tools are absent or operator-hidden, serialises by
priority, applies the env contract, and sets ``FastMCP.instructions``.

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
from ._visibility import exposed_tool_names, registered_tool_names

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


def _drop_reason(missing: frozenset[str], registered: frozenset[str]) -> str:
    """Explain why the non-empty *missing* tool names are not exposed.

    The two cases call for different action and are not otherwise
    distinguishable from the log: a registered name was hidden by the
    operator's ``TOOLS_ALLOW`` / ``TOOLS_DENY``, while an unregistered one is
    either a tool this instance's configuration did not register — the case
    ``tools=`` exists for — or a rename or typo in the ``tools=`` declaration.
    """
    hidden = sorted(missing & registered)
    absent = sorted(missing - registered)
    parts = []
    if hidden:
        parts.append("hidden by the operator visibility rule: " + ", ".join(hidden))
    if absent:
        parts.append("not registered on this server: " + ", ".join(absent))
    return "; ".join(parts)


def _identity_error(count: int) -> str:
    """Message for a mis-filled ``IDENTITY`` slot.

    Naming the slot rather than :meth:`InstructionsBuilder.identity` matters
    for the over-filled case: a second snippet added with
    ``add(..., priority=IDENTITY)`` is what usually causes it, and pointing at
    ``identity()`` sends the reader to count calls they will find correct.
    """
    if count == 0:
        return (
            "instructions need a snippet at priority IDENTITY, found none; "
            "call instructions_for(mcp).identity(...) once"
        )
    return (
        f"instructions allow one snippet at priority IDENTITY, found {count}; "
        "identity() and add(priority=IDENTITY) both fill that slot, so check "
        "for both — prose that should follow the identity belongs at a later "
        "priority such as IDENTITY + 10"
    )


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
                ``IDENTITY`` is the one anchor with a cardinality: exactly
                one snippet may sit there, whether it arrived through
                :meth:`identity` or through ``add(..., priority=IDENTITY)``.
                For prose that should follow the identity, offset it —
                ``IDENTITY + 10``.
            tools: Tool names the snippet references. If any is absent or
                hidden by the ``TOOLS_ALLOW`` / ``TOOLS_DENY`` operator rule
                at finalize, the whole snippet is dropped. Server-side
                ``disable`` / ``enable`` transforms are not modelled.

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

        A convenience wrapper over :meth:`add`, not a distinct mechanism:
        the slot at ``IDENTITY`` holds exactly one snippet however it was
        filled, and finalize requires it to be filled.
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

    def _render(self, exposed: frozenset[str], registered: frozenset[str]) -> str:
        """Prune against *exposed* tool names and serialise.

        *registered* is every tool name on the server before the operator
        rule; it does not change which snippets are dropped, only how the
        ``DEBUG`` line explains a drop. No env, no identity check.
        """
        kept: list[_Snippet] = []
        for s in self._snippets:
            missing = s.tools - exposed
            if missing:
                logger.debug(
                    "instructions: dropping snippet at priority %d; %s",
                    s.priority,
                    _drop_reason(missing, registered),
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
    helper and domain contribution — ``finalize_instructions`` must run last,
    after every tool registration and after :func:`apply_tool_visibility`.
    Under that precondition the pruned set equals what a client lists under
    the operator rule. Server-side ``disable``/``enable`` transforms before
    finalize are not modelled, and tools registered after finalize are not
    seen. Per-subject auth visibility is separately out of scope.

    If ``{P}_INSTRUCTIONS`` is set (non-whitespace), the numbered steps below
    are skipped entirely and its value is used verbatim — no identity
    requirement, no pruning. The numbered list describes the non-legacy path.

    Order of operations:

    1. exactly one snippet must sit at priority ``IDENTITY``, whether
       :meth:`InstructionsBuilder.identity` or ``add(priority=IDENTITY)``
       put it there — such snippets carry no ``tools``, so pruning cannot
       remove them; the count is taken before pruning, which also keeps the
       builder unmutated on failure
    2. exposed tools = :func:`exposed_tool_names` (registered ∧ operator
       rule), plus :func:`registered_tool_names` for the drop diagnostics —
       both computed before any further mutation, since the first is the
       other source of :class:`~fastmcp_pvl_core._errors.ConfigurationError`
       (both visibility lists set) and either can raise ``RuntimeError`` on
       enumeration drift; a failure must leave the builder unmutated
    3. ``{P}_INSTRUCTIONS_EXTRA`` appended at ``OPERATOR``; legacy
       ``{P}_INSTRUCTIONS`` replaces the whole text with one ``WARNING``
    4. drop every snippet whose ``tools`` are not all exposed — one
       ``DEBUG`` per drop, naming the missing tools and saying of each
       whether it was operator-hidden or never registered
    5. serialise by ``(priority, insertion)``, blank-line separated
    6. set ``mcp.instructions``, cache, freeze the builder

    A second call returns the cached string without re-reading env or
    logging.

    Args:
        mcp: The server whose builder to finalize.
        config: Universal server config; only ``tools_allow`` / ``tools_deny``
            are read.
        env_prefix: Env-var prefix, with or without a trailing underscore.

    Returns:
        The final instructions string, also set on ``mcp.instructions``.

    Raises:
        ConfigurationError: No snippet at priority ``IDENTITY``, more than
            one, or both visibility lists set.
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
            raise ConfigurationError(_identity_error(len(identities)))
        # Both enumerations precede the add() below: either can raise, and
        # a failed finalize must leave the builder unmutated and unfrozen.
        exposed = exposed_tool_names(mcp, config)
        registered = registered_tool_names(mcp)
        if extra:
            builder.add(extra, priority=OPERATOR)
        text = builder._render(exposed, registered)

    mcp.instructions = text
    builder._result = text
    builder._frozen = True
    return text
