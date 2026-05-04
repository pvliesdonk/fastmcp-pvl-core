"""Uniform subject extraction across all auth modes.

Downstream code that wants to know "who is making this request?" should
import :func:`get_subject` from the package root and call it without
caring about which auth mode is active.

The per-mode complexity lives in the builders (see :mod:`_auth`); this
module is a thin extractor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.server.dependencies import get_access_token

if TYPE_CHECKING:
    # Imported only for typing; runtime import would create a cycle
    # (``_auth`` imports ``set_current_auth_mode`` from this module).
    from fastmcp_pvl_core._auth import AuthMode

# Process-global pointer to the auth mode resolved at server startup.
# ``build_auth`` calls ``set_current_auth_mode``; the most recent value
# is in effect. ``get_subject`` reads it to decide whether the absence
# of an access token means "stdio/no-auth" (returns "local") or "auth
# configured but no valid token" (returns None).
#
# This is a process-global rather than a contextvar by design — auth
# mode is resolved once at startup and is invariant across requests.
_current_auth_mode: AuthMode | None = None


def set_current_auth_mode(mode: AuthMode | None) -> None:
    """Record the auth mode resolved at server startup.

    Called by :func:`fastmcp_pvl_core.build_auth`. Tests that exercise
    :func:`get_subject` without going through ``build_auth`` may call
    this directly. Passing ``None`` resets the pointer (useful between
    tests).
    """
    global _current_auth_mode
    _current_auth_mode = mode


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
        return "local" if _current_auth_mode == "none" else None
    raw_claims = getattr(access_token, "claims", None)
    claims = raw_claims if isinstance(raw_claims, dict) else {}
    sub = claims.get("sub")
    if isinstance(sub, str) and sub:
        return sub
    client_id = getattr(access_token, "client_id", None)
    if isinstance(client_id, str) and client_id:
        return client_id
    return None
