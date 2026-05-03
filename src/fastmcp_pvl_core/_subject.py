# src/fastmcp_pvl_core/_subject.py
"""Uniform subject extraction across all auth modes.

Downstream code that wants to know "who is making this request?" should
import :func:`get_subject` from the package root and call it without
caring about which auth mode is active.

The per-mode complexity lives in the builders (see :mod:`_auth`); this
module is a thin extractor.
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_access_token

# Module-level pointer to the resolved auth mode. ``build_auth`` calls
# ``set_current_auth_mode`` exactly once at server startup; ``get_subject``
# reads it to decide whether the absence of an access token means
# "stdio/no-auth" (returns "local") or "auth configured but no valid
# token" (returns None).
_current_auth_mode: str | None = None


def set_current_auth_mode(mode: str | None) -> None:
    """Record the auth mode resolved at server startup.

    Called by :func:`fastmcp_pvl_core.build_auth`. Tests that bypass
    ``build_auth`` may call this directly.
    """
    global _current_auth_mode
    _current_auth_mode = mode


def get_subject(_ctx_or_request: object | None = None) -> str | None:
    """Return the subject of the current request, or ``None``.

    Resolution order:

    1. If FastMCP's :func:`get_access_token` returns a token, return
       ``token.claims["sub"]`` if present, else ``token.client_id``.
       The builders are responsible for ensuring ``client_id`` carries
       the right value per mode (mapped subject for ``bearer-mapped``,
       ``bearer_default_subject`` for ``bearer-single``).
    2. If there is no access token and ``set_current_auth_mode`` was
       called with ``"none"``, return the literal ``"local"``.
    3. Otherwise return ``None`` and let the caller decide whether to
       fall back or error.

    The optional ``_ctx_or_request`` argument is reserved for future use
    (an explicit request/context object); v1 ignores it and reads from
    FastMCP's ambient context plumbing.
    """
    access_token = get_access_token()
    if access_token is None:
        return "local" if _current_auth_mode == "none" else None
    claims = getattr(access_token, "claims", None) or {}
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if isinstance(sub, str) and sub:
        return sub
    client_id = getattr(access_token, "client_id", None)
    if isinstance(client_id, str) and client_id:
        return client_id
    return None
