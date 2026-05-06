"""Authorization primitives: middleware, annotation convention, ACL loader.

Downstream MCP servers that need to enforce per-subject access control
on their tools, resources, or prompts can opt in by:

1. Annotating components with ``meta={"required_scope": "<scope>"}``.
2. Building an :data:`Authorizer` (typically via :func:`load_acl` +
   :func:`make_acl_authorizer`).
3. Installing :class:`AuthorizationMiddleware` after
   :func:`fastmcp_pvl_core.wire_middleware_stack`.

See ``docs/specs/authorization-submodule.md`` for the design rationale.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import TypeAlias

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

Authorizer: TypeAlias = Callable[[str | None, str], bool]
"""Decision callable: ``(subject, required_scope) -> bool``.

Returns ``True`` to allow, ``False`` to deny.  ``None`` subject means
"no caller identity available" — typical authorizers deny that case.

This is a :class:`TypeAlias`, not a ``Protocol``.  The Protocol upgrade
across this and other Callable seams in the package is tracked in
issue #60.
"""


# ---------------------------------------------------------------------------
# AuthzDenied exception
# ---------------------------------------------------------------------------

class AuthzDenied(Exception):  # noqa: N818
    """Raised by :func:`check_authorization` when the authorizer denies.

    The :class:`AuthorizationMiddleware` catches this around
    ``call_next`` and re-raises as the per-operation MCP error
    (:class:`fastmcp.exceptions.ToolError` for a tool body,
    :class:`~fastmcp.exceptions.ResourceError` for a resource handler,
    :class:`~fastmcp.exceptions.PromptError` for a prompt handler).

    If the middleware is *not* installed, this propagates as a plain
    :class:`Exception` and surfaces as a generic MCP error.
    """

    subject: str | None
    required_scope: str

    def __init__(self, *, subject: str | None, required_scope: str) -> None:
        super().__init__(
            f"authorization denied: subject={subject!r} "
            f"required_scope={required_scope!r}"
        )
        self.subject = subject
        self.required_scope = required_scope


# ---------------------------------------------------------------------------
# Ambient authorizer (ContextVar plumbing)
# ---------------------------------------------------------------------------

_current_authorizer: ContextVar[Authorizer | None] = ContextVar(
    "fastmcp_pvl_core_current_authorizer",
    default=None,
)
"""Per-context pointer to the active authorizer.

Set by :class:`AuthorizationMiddleware.__init__`; read by
:func:`check_authorization` when its ``authorizer=`` kwarg is omitted.
Same pattern as ``_current_auth_mode`` in :mod:`_subject`; same
composition caveat (last writer wins; operators wishing to compose
multiple :class:`AuthorizationMiddleware` instances on distinct
contexts must wrap each install in
``contextvars.copy_context().run(...)``).
"""


def set_current_authorizer(authorizer: Authorizer | None) -> None:
    """Record the active authorizer for the current context.

    Called by :class:`AuthorizationMiddleware.__init__`.  Tests that
    exercise :func:`check_authorization` without going through the
    middleware may call this directly.  Passing ``None`` resets the
    pointer (useful between tests).
    """
    _current_authorizer.set(authorizer)
