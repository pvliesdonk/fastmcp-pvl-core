"""Auth mode resolution and builders.

Inspect :class:`ServerConfig` to determine which auth flavor is
configured, then dispatch to the right FastMCP auth provider.
Five modes: ``none``, ``bearer``, ``remote``, ``oidc-proxy``, ``multi``.
"""

from __future__ import annotations

import logging
from typing import Literal, cast

from fastmcp_pvl_core._config import ServerConfig

logger = logging.getLogger(__name__)

AuthMode = Literal["none", "bearer", "remote", "oidc-proxy", "multi"]

# The override only accepts the two OIDC modes that can apply to the same
# underlying configuration.  Bearer / multi / none are unambiguous from
# field presence, so allowing them as overrides only introduces silent
# failure modes (e.g. ``AUTH_MODE=bearer`` with no ``BEARER_TOKEN``
# would start the server unauthenticated).
_VALID_MODES: frozenset[Literal["remote", "oidc-proxy"]] = frozenset(
    {"remote", "oidc-proxy"}
)


def resolve_auth_mode(config: ServerConfig) -> AuthMode:
    """Decide which auth flavor to use based on configured fields.

    Precedence:

    - If ``config.auth_mode`` is set, it overrides auto-detection.  The
      override only accepts ``remote`` or ``oidc-proxy`` — these are the
      two modes that can apply to the same underlying OIDC
      configuration (all four OIDC vars set is ambiguous between them,
      depending on operator intent).  Other values (``bearer``,
      ``multi``, ``none``, and any unknown string) are ignored with a
      warning, and auto-detection is used.  The comparison is case- and
      whitespace-insensitive.
    - ``multi``: both a bearer token and an OIDC flavor are configured.
    - ``bearer``: only ``bearer_token`` set.
    - ``oidc-proxy``: all four OIDC client-credential vars set
      (``base_url``, ``oidc_config_url``, ``oidc_client_id``,
      ``oidc_client_secret``).
    - ``remote``: only ``base_url`` + ``oidc_config_url`` set.
    - ``none``: nothing configured.

    Args:
        config: Populated server configuration.

    Returns:
        One of the five :data:`AuthMode` literals.
    """
    explicit = (config.auth_mode or "").strip().lower()
    if explicit:
        if explicit in _VALID_MODES:
            logger.info("auth_mode=%s (explicit via AUTH_MODE)", explicit)
            return cast(AuthMode, explicit)
        logger.warning(
            "auth_mode_unknown value=%r — ignoring, falling back to auto-detection",
            explicit,
        )

    has_bearer = bool(config.bearer_token)
    has_oidc_proxy = all(
        (
            config.base_url,
            config.oidc_config_url,
            config.oidc_client_id,
            config.oidc_client_secret,
        )
    )
    has_remote = bool(config.base_url and config.oidc_config_url) and not has_oidc_proxy

    oidc_mode: AuthMode | None
    if has_oidc_proxy:
        oidc_mode = "oidc-proxy"
    elif has_remote:
        oidc_mode = "remote"
    else:
        oidc_mode = None

    if has_bearer and oidc_mode is not None:
        return "multi"
    if has_bearer:
        return "bearer"
    if oidc_mode is not None:
        return oidc_mode
    return "none"
