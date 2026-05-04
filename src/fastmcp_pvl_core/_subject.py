"""Uniform subject extraction across all auth modes.

Downstream code that wants to know "who is making this request?" should
import :func:`get_subject` from the package root and call it without
caring about which auth mode is active.

The per-mode complexity lives in the builders (see :mod:`_auth`); this
module is a thin extractor.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

from fastmcp.server.dependencies import get_access_token

if TYPE_CHECKING:
    # Imported only for typing; runtime import would create a cycle
    # (``_auth`` imports ``set_current_auth_mode`` from this module).
    from fastmcp_pvl_core._auth import AuthMode

# Per-context pointer to the auth mode resolved at server startup.
# ``build_auth`` calls ``set_current_auth_mode`` once after resolving
# the mode; ``get_subject`` reads it to decide whether the absence of
# an access token means "stdio/no-auth" (returns "local") or "auth
# configured but no valid token" (returns None).
#
# Implemented as a :class:`ContextVar` rather than a module global so
# that the suite-wide autouse fixture in ``tests/conftest.py``
# (``_reset_auth_mode``, which calls ``set_current_auth_mode(None)``
# before/after every test) and explicitly-isolated harnesses
# (``contextvars.copy_context().run(...)``) can reset / scope the
# value cleanly.  Note: pytest does not auto-reset ``ContextVar``
# values between tests on its own; the autouse fixture is what makes
# isolation work.  In the natural single-context production pattern
# (two ``build_auth`` calls in the same ``main()`` body) last-writer
# still wins — caller code wishing to compose multiple ``FastMCP``
# instances with distinct auth modes must wrap each ``build_auth`` in
# its own ``copy_context().run(...)``.  In standard server-startup-
# then-serve deployments, asyncio's task context inherits the
# startup value uniformly across requests.
_current_auth_mode: ContextVar[AuthMode | None] = ContextVar(
    "fastmcp_pvl_core_current_auth_mode",
    default=None,
)


def set_current_auth_mode(mode: AuthMode | None) -> None:
    """Record the auth mode resolved at server startup.

    Called by :func:`fastmcp_pvl_core.build_auth`. Tests that exercise
    :func:`get_subject` without going through ``build_auth`` may call
    this directly. Passing ``None`` resets the pointer (useful between
    tests).
    """
    _current_auth_mode.set(mode)


def get_subject() -> str | None:
    """Return the subject of the current request, or ``None``.

    Resolution order:

    1. If FastMCP's :func:`get_access_token` returns a token, prefer
       ``token.claims["sub"]`` (OIDC's standard subject claim); fall
       back to ``token.client_id`` when ``sub`` is absent or non-string.
       The builders normalise ``client_id`` per bearer mode (mapped
       subject for ``bearer-mapped``, ``bearer_default_subject`` for
       ``bearer-single``).
    2. If there is no access token and ``set_current_auth_mode`` was
       called with ``"none"``, return the literal ``"local"``.
    3. Otherwise return ``None`` and let the caller decide whether to
       fall back or error.
    """
    access_token = get_access_token()
    if access_token is None:
        return "local" if _current_auth_mode.get() == "none" else None
    raw_claims = getattr(access_token, "claims", None)
    claims = raw_claims if isinstance(raw_claims, dict) else {}
    sub = claims.get("sub")
    if isinstance(sub, str) and sub:
        return sub
    client_id = getattr(access_token, "client_id", None)
    if isinstance(client_id, str) and client_id:
        return client_id
    return None
