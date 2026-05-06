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

import json
import logging
import sys
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeAlias

from fastmcp.exceptions import PromptError, ResourceError, ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - fallback for Python 3.10
    # ``import-not-found`` covers CI rows where ``tomli`` is excluded by
    # the marker (3.11+); ``unused-ignore`` covers local 3.10 envs where
    # ``tomli`` is installed and the ignore would otherwise be flagged.
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._subject import get_subject

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
        raise ConfigurationError(f"ACL file not found or not a regular file: {path}")
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
        raise ConfigurationError(f"ACL file at {path} must define a [subjects] table")

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
            cleaned.add(scope.strip())
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


# ---------------------------------------------------------------------------
# check_authorization (escape-hatch helper)
# ---------------------------------------------------------------------------


def check_authorization(
    required_scope: str,
    *,
    authorizer: Authorizer | None = None,
    subject: str | None = None,
) -> None:
    """Imperative authz check for use inside a tool / resource / prompt body.

    Resolution order:

    1. ``authorizer`` argument if given.
    2. The ambient :data:`_current_authorizer` (set by
       :class:`AuthorizationMiddleware.__init__`).
    3. Otherwise raise :class:`RuntimeError`.

    Subject resolution:

    - ``subject`` used as-is when a non-``None`` value is passed.
    - When omitted (or ``None``), :func:`fastmcp_pvl_core.get_subject`
      is called.  ``get_subject`` itself may return ``None`` if no auth
      context is available — that ``None`` is then forwarded to the
      authorizer, which typically denies it.

    Scope normalisation:

    The ``required_scope`` is stripped of surrounding whitespace before
    the authorizer is called.  Passing an empty or whitespace-only scope
    raises :class:`ValueError`.

    Args:
        required_scope: Scope string to require, e.g. ``"write"`` or
            ``"read:project-foo"``.
        authorizer: Override the ambient authorizer.  Useful when the
            middleware isn't installed but a code path still wants the
            check.
        subject: Override the ``get_subject()`` lookup.  Both omitting
            the argument and explicitly passing ``None`` trigger
            :func:`get_subject`; to force ``None`` to the authorizer (the
            "no auth context" test case), patch :func:`get_subject` directly
            or use :func:`make_acl_authorizer` (which returns ``False`` on
            ``None`` subject) and call it directly.

    Raises:
        AuthzDenied: when the authorizer returns ``False``.
        RuntimeError: when no authorizer is reachable (neither ambient
            nor explicit).
        ValueError: when ``required_scope`` is empty or whitespace-only.
    """
    required_scope = required_scope.strip()
    if not required_scope:
        raise ValueError("required_scope must be a non-empty string")

    if authorizer is None:
        authorizer = _current_authorizer.get()
        if authorizer is None:
            raise RuntimeError(
                "no authorizer in context; install AuthorizationMiddleware "
                "or pass authorizer= explicitly to check_authorization()"
            )

    resolved_subject = subject if subject is not None else get_subject()

    if not authorizer(resolved_subject, required_scope):
        raise AuthzDenied(subject=resolved_subject, required_scope=required_scope)


# ---------------------------------------------------------------------------
# AuthorizationMiddleware
# ---------------------------------------------------------------------------


logger = logging.getLogger(__name__)


class AuthorizationMiddleware(Middleware):
    """fastmcp middleware that enforces ``meta["required_scope"]`` on components.

    Tools, resources, and prompts opt in by setting
    ``meta={"required_scope": "<scope>"}`` at registration.  Components
    without the meta key are unrestricted.

    See ``docs/specs/authorization-submodule.md`` for the full design.
    """

    def __init__(
        self,
        *,
        authorizer: Authorizer,
        expose_subject_in_error: bool = False,
    ) -> None:
        """Construct the middleware and publish the authorizer ambient.

        Args:
            authorizer: Decision callable.  Saved on the instance and
                also written to the package-internal
                ``_current_authorizer`` :class:`ContextVar` so that
                :func:`check_authorization` calls inside tool bodies
                find it without an explicit ``authorizer=`` kwarg.
            expose_subject_in_error: When ``True``, the wire-side deny
                payload includes the ``subject`` key.  Defaults to
                ``False`` (multi-user disclosure risk).  The subject is
                always logged at WARNING regardless.
        """
        self._authorizer = authorizer
        self._expose_subject = expose_subject_in_error
        set_current_authorizer(authorizer)

    def _format_deny_payload(self, *, subject: str | None, required_scope: str) -> str:
        """Render the JSON-encoded deny payload for the wire.

        When ``expose_subject_in_error`` is ``True`` and the subject is
        ``None``, the payload includes ``"subject": null``.  Downstream code
        that consumes the payload should defend against this case.
        """
        body: dict[str, Any] = {
            "code": "authz_denied",
            "required_scope": required_scope,
        }
        if self._expose_subject:
            body["subject"] = subject
        return json.dumps(body, separators=(",", ":"))

    def _log_deny(
        self, *, kind: str, name: str, subject: str | None, required_scope: str
    ) -> None:
        """Log an authz denial at WARNING (subject always included in logs)."""
        logger.warning(
            "authz_denied kind=%s name=%s subject=%r required_scope=%r",
            kind,
            name,
            subject,
            required_scope,
        )

    def _enforce_static(
        self,
        *,
        kind: str,
        name: str,
        meta: Mapping[str, Any],
        error_cls: type[Exception],
    ) -> None:
        """Run the static ``meta["required_scope"]`` check.

        Raises ``error_cls`` (constructed with the JSON deny payload) on
        deny.  Does nothing when meta has no requirement.
        """
        required = meta.get("required_scope")
        if not isinstance(required, str):
            return
        required = required.strip()
        if not required:
            return
        subject = get_subject()
        if not self._authorizer(subject, required):
            self._log_deny(
                kind=kind, name=name, subject=subject, required_scope=required
            )
            raise error_cls(
                self._format_deny_payload(subject=subject, required_scope=required)
            )

    async def _call_with_authz_translation(
        self,
        *,
        kind: str,
        name: str,
        error_cls: type[Exception],
        call_next: Any,
        context: MiddlewareContext,
    ) -> Any:
        """Run ``call_next`` and translate AuthzDenied to ``error_cls``."""
        try:
            return await call_next(context)
        except AuthzDenied as exc:
            self._log_deny(
                kind=kind,
                name=name,
                subject=exc.subject,
                required_scope=exc.required_scope,
            )
            raise error_cls(
                self._format_deny_payload(
                    subject=exc.subject,
                    required_scope=exc.required_scope,
                )
            ) from None

    def _filter_components(self, components: list[Any]) -> list[Any]:
        """Drop components whose ``meta["required_scope"]`` denies the caller."""
        subject = get_subject()
        kept: list[Any] = []
        for component in components:
            meta = getattr(component, "meta", None) or {}
            required = meta.get("required_scope")
            if not isinstance(required, str):
                kept.append(component)
                continue
            required = required.strip()
            if not required:
                kept.append(component)
                continue
            if self._authorizer(subject, required):
                kept.append(component)
        return kept

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        """Enforce ``required_scope`` on tool calls."""
        if context.fastmcp_context is None:
            raise RuntimeError(
                "AuthorizationMiddleware.on_call_tool: fastmcp_context is None; "
                "ensure the middleware is installed on a FastMCP server"
            )
        # NOTE: lookup failure path skips the static meta check. See
        # docs/specs/authorization-submodule.md (AuthorizationMiddleware
        # section, "When the inner-component lookup ... raises").
        try:
            tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
        except Exception as exc:  # noqa: BLE001 — defensive, logged
            logger.warning(
                "tool lookup failed during authz check; falling through name=%s exc=%r",
                context.message.name,
                exc,
            )
            return await self._call_with_authz_translation(
                kind="tool",
                name=context.message.name,
                error_cls=ToolError,
                call_next=call_next,
                context=context,
            )
        self._enforce_static(
            kind="tool",
            name=context.message.name,
            meta=getattr(tool, "meta", None) or {},
            error_cls=ToolError,
        )
        return await self._call_with_authz_translation(
            kind="tool",
            name=context.message.name,
            error_cls=ToolError,
            call_next=call_next,
            context=context,
        )

    async def on_read_resource(self, context: MiddlewareContext, call_next: Any) -> Any:
        """Enforce ``required_scope`` on resource reads."""
        if context.fastmcp_context is None:
            raise RuntimeError(
                "AuthorizationMiddleware.on_read_resource: fastmcp_context is None; "
                "ensure the middleware is installed on a FastMCP server"
            )
        # NOTE: lookup failure path skips the static meta check. See
        # docs/specs/authorization-submodule.md (AuthorizationMiddleware
        # section, "When the inner-component lookup ... raises").
        try:
            resource = await context.fastmcp_context.fastmcp.get_resource(
                context.message.uri
            )
        except Exception as exc:  # noqa: BLE001 — defensive, logged
            logger.warning(
                "resource lookup failed during authz check; falling through "
                "uri=%s exc=%r",
                context.message.uri,
                exc,
            )
            return await self._call_with_authz_translation(
                kind="resource",
                name=str(context.message.uri),
                error_cls=ResourceError,
                call_next=call_next,
                context=context,
            )
        self._enforce_static(
            kind="resource",
            name=str(context.message.uri),
            meta=getattr(resource, "meta", None) or {},
            error_cls=ResourceError,
        )
        return await self._call_with_authz_translation(
            kind="resource",
            name=str(context.message.uri),
            error_cls=ResourceError,
            call_next=call_next,
            context=context,
        )

    async def on_get_prompt(self, context: MiddlewareContext, call_next: Any) -> Any:
        """Enforce ``required_scope`` on prompt retrievals."""
        if context.fastmcp_context is None:
            raise RuntimeError(
                "AuthorizationMiddleware.on_get_prompt: fastmcp_context is None; "
                "ensure the middleware is installed on a FastMCP server"
            )
        # NOTE: lookup failure path skips the static meta check. See
        # docs/specs/authorization-submodule.md (AuthorizationMiddleware
        # section, "When the inner-component lookup ... raises").
        try:
            prompt = await context.fastmcp_context.fastmcp.get_prompt(
                context.message.name
            )
        except Exception as exc:  # noqa: BLE001 — defensive, logged
            logger.warning(
                "prompt lookup failed during authz check; falling through "
                "name=%s exc=%r",
                context.message.name,
                exc,
            )
            return await self._call_with_authz_translation(
                kind="prompt",
                name=context.message.name,
                error_cls=PromptError,
                call_next=call_next,
                context=context,
            )
        self._enforce_static(
            kind="prompt",
            name=context.message.name,
            meta=getattr(prompt, "meta", None) or {},
            error_cls=PromptError,
        )
        return await self._call_with_authz_translation(
            kind="prompt",
            name=context.message.name,
            error_cls=PromptError,
            call_next=call_next,
            context=context,
        )

    async def on_list_tools(self, context: MiddlewareContext, call_next: Any) -> Any:
        """Filter tool listings by what the caller can call."""
        tools = await call_next(context)
        return self._filter_components(tools)

    async def on_list_resources(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Filter resource listings by what the caller can read."""
        resources = await call_next(context)
        return self._filter_components(resources)

    async def on_list_resource_templates(
        self, context: MiddlewareContext, call_next: Any
    ) -> Any:
        """Filter resource template listings by what the caller can read."""
        templates = await call_next(context)
        return self._filter_components(templates)

    async def on_list_prompts(self, context: MiddlewareContext, call_next: Any) -> Any:
        """Filter prompt listings by what the caller can retrieve."""
        prompts = await call_next(context)
        return self._filter_components(prompts)
