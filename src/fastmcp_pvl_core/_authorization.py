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

import sys
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from contextvars import ContextVar
from pathlib import Path
from typing import TypeAlias

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - fallback for Python 3.10
    # ``import-not-found`` covers CI rows where ``tomli`` is excluded by
    # the marker (3.11+); ``unused-ignore`` covers local 3.10 envs where
    # ``tomli`` is installed and the ignore would otherwise be flagged.
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]

from fastmcp_pvl_core._errors import ConfigurationError

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


# ---------------------------------------------------------------------------
# ACL TOML loader
# ---------------------------------------------------------------------------


def load_acl(path: Path) -> dict[str, frozenset[str]]:
    """Load an ACL TOML file into a ``{subject: frozenset[scope]}`` dict.

    The path is normalised with :meth:`Path.expanduser` first.  This is
    the single expansion site for both env-loaded paths (which keep a
    leading ``~`` literal) and direct-construction paths.

    Schema:

    .. code-block:: toml

        [subjects]
        "user:alice@example.com" = ["read", "write"]
        "user:admin@example.com" = ["*"]

    The ``*`` scope is interpreted by :func:`make_acl_authorizer` as
    "any required scope passes".  No subject-side wildcard.

    Args:
        path: Path to the ACL TOML file.

    Returns:
        A ``dict`` mapping each subject to a ``frozenset`` of granted
        scope strings.

    Raises:
        ConfigurationError: file missing, unreadable, malformed,
            schema-invalid, or containing an empty / whitespace /
            ``"*"`` subject key.
    """
    path = path.expanduser()
    if not path.is_file():
        raise ConfigurationError(
            f"ACL file not found or not a regular file: {path}"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(
            f"ACL file at {path} could not be read: {exc}"
        ) from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            f"ACL file at {path} could not be parsed: {exc}"
        ) from exc

    subjects = data.get("subjects")
    if not isinstance(subjects, dict):
        raise ConfigurationError(
            f"ACL file at {path} must define a [subjects] table"
        )

    result: dict[str, frozenset[str]] = {}
    for subject, scopes in subjects.items():
        if not subject.strip():
            raise ConfigurationError(
                f"ACL file at {path}: subject key is empty or whitespace-only"
            )
        if subject == "*":
            raise ConfigurationError(
                f'ACL file at {path}: "*" as a subject key is not allowed '
                "(global subject wildcards collapse the model)"
            )
        if not isinstance(scopes, list):
            raise ConfigurationError(
                f"ACL file at {path}: subject {subject!r} value must be an "
                f"array of scope strings; got {type(scopes).__name__}"
            )
        cleaned: set[str] = set()
        for scope in scopes:
            if not isinstance(scope, str):
                raise ConfigurationError(
                    f"ACL file at {path}: subject {subject!r}: scope must "
                    f"be a string; got {type(scope).__name__}"
                )
            if not scope.strip():
                raise ConfigurationError(
                    f"ACL file at {path}: subject {subject!r}: scope is "
                    "empty or whitespace-only"
                )
            cleaned.add(scope)
        result[subject] = frozenset(cleaned)
    return result


# ---------------------------------------------------------------------------
# ACL → Authorizer bridge
# ---------------------------------------------------------------------------


def make_acl_authorizer(acl: Mapping[str, AbstractSet[str]]) -> Authorizer:
    """Bridge a ``{subject: scopes}`` mapping to an :data:`Authorizer`.

    Allow rules:

    - ``subject is None`` → deny.
    - Subject not in ``acl`` → deny.
    - ``"*"`` in the subject's grants → allow any required scope.
    - Otherwise → allow iff ``required_scope`` is in the grants.

    The mapping is captured by reference, not copied.  A downstream that
    mutates the dict in place sees the change reflected by the closure
    (intentional; the recommended pattern is rebuild + reassign, but
    reference-capture lets advanced consumers wire reload semantics
    without changing this signature).

    Args:
        acl: Mapping from subject string to a set of granted scope strings.

    Returns:
        An :data:`Authorizer` callable.
    """

    def authorize(subject: str | None, required_scope: str) -> bool:
        if subject is None:
            return False
        granted = acl.get(subject)
        if granted is None:
            return False
        return "*" in granted or required_scope in granted

    return authorize
