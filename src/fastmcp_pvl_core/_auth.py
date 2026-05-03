"""Auth mode resolution and builders.

Inspect :class:`ServerConfig` to determine which auth flavor is
configured, then dispatch to the right FastMCP auth provider.
Six modes: ``none``, ``bearer-single``, ``bearer-mapped``, ``remote``,
``oidc-proxy``, ``multi``.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any, Literal, cast

from fastmcp_pvl_core._config import ServerConfig

if TYPE_CHECKING:
    from fastmcp.server.auth import (
        RemoteAuthProvider,
        StaticTokenVerifier,
    )
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

logger = logging.getLogger(__name__)

AuthMode = Literal[
    "none", "bearer-single", "bearer-mapped", "remote", "oidc-proxy", "multi"
]

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
    - ``multi``: any bearer flavor (single or mapped) and an OIDC
      flavor are both configured.
    - ``bearer-mapped``: ``bearer_tokens_file`` set (takes precedence
      over a single ``bearer_token`` if both are configured).
    - ``bearer-single``: only ``bearer_token`` set.
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

    has_mapped_bearer = config.bearer_tokens_file is not None
    has_single_bearer = bool(config.bearer_token) and not has_mapped_bearer
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

    has_any_bearer = has_mapped_bearer or has_single_bearer

    if has_any_bearer and oidc_mode is not None:
        return "multi"
    if has_mapped_bearer:
        return "bearer-mapped"
    if has_single_bearer:
        return "bearer-single"
    if oidc_mode is not None:
        return oidc_mode
    return "none"


def build_bearer_auth(config: ServerConfig) -> StaticTokenVerifier | None:
    """Build a :class:`StaticTokenVerifier` from ``config.bearer_token``.

    Returns a verifier that validates ``Authorization: Bearer <token>``
    headers against the configured static token.  The token is granted
    ``read`` and ``write`` scopes; operators scope down at the MCP layer
    (e.g. tag-based tool hiding) rather than by differentiating bearer
    scopes.

    Args:
        config: Populated server configuration.

    Returns:
        A configured :class:`StaticTokenVerifier`, or ``None`` when the
        bearer token is absent or blank.
    """
    token = (config.bearer_token or "").strip()
    if not token:
        logger.debug("bearer_auth_skipped reason=not_configured")
        return None
    logger.debug("bearer_auth_enabled token=<redacted>")
    from fastmcp.server.auth import StaticTokenVerifier

    return StaticTokenVerifier(
        tokens={token: {"client_id": "bearer", "scopes": ["read", "write"]}},
    )


def build_oidc_proxy_auth(config: ServerConfig) -> OIDCProxy | None:
    """Build an :class:`OIDCProxy` provider, or return ``None``.

    Requires all four of ``base_url``, ``oidc_config_url``,
    ``oidc_client_id``, and ``oidc_client_secret`` on *config*.  By
    default the proxy verifies the upstream ``id_token`` (a standard JWT
    per OIDC Core) rather than the ``access_token`` — this works with
    every OIDC provider, including those that issue opaque access tokens
    (e.g. Authelia).  Set ``config.oidc_verify_access_token=True`` to
    revert to access-token verification.

    ``required_scopes`` defaults to ``["openid"]`` when *config* does not
    configure any, matching OIDC Core semantics (``openid`` must be
    requested for an id_token to be issued).

    Args:
        config: Populated server configuration.

    Returns:
        A configured :class:`OIDCProxy`, or ``None`` when any of the
        four required fields is missing.
    """
    # Keep the secret out of the "missing" list so it never enters logs
    # (static-analysis taint tools flag this otherwise).
    required_public = {
        "BASE_URL": config.base_url,
        "OIDC_CONFIG_URL": config.oidc_config_url,
        "OIDC_CLIENT_ID": config.oidc_client_id,
    }
    has_secret = bool(config.oidc_client_secret)
    if not all(required_public.values()) or not has_secret:
        missing = [k for k, v in required_public.items() if not v]
        if not has_secret:
            missing.append("OIDC_CLIENT_SECRET")
        logger.debug("oidc_proxy_auth_skipped missing=%s", ",".join(missing))
        return None

    # Narrow types — all four are non-None after the guard above.
    base_url = cast(str, config.base_url)
    oidc_config_url = cast(str, config.oidc_config_url)
    oidc_client_id = cast(str, config.oidc_client_id)
    oidc_client_secret = cast(str, config.oidc_client_secret)

    required_scopes: list[str] = list(config.oidc_required_scopes) or ["openid"]

    verify_access_token = config.oidc_verify_access_token
    verify_id_token = not verify_access_token

    if verify_id_token and "openid" not in required_scopes:
        logger.warning(
            "oidc_proxy_auth_scope_warning "
            "verify_id_token=True missing_scope=openid — "
            "the id_token may be absent from the token response; "
            "add 'openid' to required_scopes or set "
            "oidc_verify_access_token=True"
        )

    if config.oidc_jwt_signing_key is None and sys.platform.startswith("linux"):
        logger.warning(
            "oidc_proxy_auth_ephemeral_signing_key "
            "oidc_jwt_signing_key=<unset> — tokens will be invalidated on "
            "every server restart; configure OIDC_JWT_SIGNING_KEY in "
            "production"
        )

    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    return OIDCProxy(
        config_url=oidc_config_url,
        client_id=oidc_client_id,
        client_secret=oidc_client_secret,
        base_url=base_url,
        audience=config.oidc_audience,
        required_scopes=required_scopes,
        jwt_signing_key=config.oidc_jwt_signing_key,
        verify_id_token=verify_id_token,
        require_authorization_consent=False,
    )


def build_remote_auth(config: ServerConfig) -> RemoteAuthProvider | None:
    """Build a :class:`RemoteAuthProvider` from OIDC discovery.

    Fetches the OIDC discovery document at startup to extract
    ``jwks_uri`` and ``issuer``, then constructs a ``JWTVerifier`` for
    local token validation via JWKS.  No client credentials are needed —
    tokens are validated locally.

    Requires ``base_url`` and ``oidc_config_url`` on *config*.  Returns
    ``None`` when either is missing, when ``httpx`` is not installed
    (the ``remote-auth`` extra), when the discovery request fails, or
    when the discovery document is missing ``jwks_uri``/``issuer``.

    Args:
        config: Populated server configuration.

    Returns:
        A configured :class:`RemoteAuthProvider`, or ``None`` when
        remote auth cannot be built.
    """
    if not config.base_url or not config.oidc_config_url:
        logger.debug("remote_auth_skipped reason=missing_base_url_or_config_url")
        return None

    try:
        import httpx
    except ImportError:
        logger.warning(
            "remote_auth_skipped reason=httpx_missing — "
            "install with `pip install fastmcp-pvl-core[remote-auth]`"
        )
        return None

    try:
        resp = httpx.get(config.oidc_config_url, timeout=10)
        resp.raise_for_status()
        discovery = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.exception(
            "remote_auth_discovery_failed config_url=%s",
            config.oidc_config_url,
        )
        return None

    jwks_uri = discovery.get("jwks_uri")
    issuer = discovery.get("issuer")
    if not jwks_uri or not issuer:
        logger.error(
            "remote_auth_discovery_incomplete jwks_uri=%s issuer=%s",
            jwks_uri,
            issuer,
        )
        return None

    required_scopes: list[str] | None = list(config.oidc_required_scopes) or None

    from fastmcp.server.auth import JWTVerifier, RemoteAuthProvider

    verifier = JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        audience=config.oidc_audience,
        required_scopes=required_scopes,
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[issuer],
        base_url=config.base_url,
    )


def build_auth(config: ServerConfig) -> Any:
    """Dispatch to the correct FastMCP auth provider for *config*.

    Resolves the auth mode via :func:`resolve_auth_mode` and composes the
    individual builders.  In ``multi`` mode, wraps an OIDC provider and a
    bearer verifier into a single :class:`~fastmcp.server.auth.MultiAuth`
    with ``required_scopes=[]``.

    Args:
        config: Populated server configuration.

    Returns:
        The appropriate auth provider:

        - ``None`` when no auth is configured.
        - A :class:`~fastmcp.server.auth.StaticTokenVerifier` in
          ``bearer-single`` or ``bearer-mapped`` mode.
        - An :class:`~fastmcp.server.auth.oidc_proxy.OIDCProxy` in
          ``oidc-proxy`` mode.
        - A :class:`~fastmcp.server.auth.RemoteAuthProvider` in
          ``remote`` mode.
        - A :class:`~fastmcp.server.auth.MultiAuth` in ``multi`` mode,
          always constructed with ``required_scopes=[]`` (load-bearing —
          see implementation comment).
    """
    mode = resolve_auth_mode(config)
    if mode == "none":
        return None
    if mode in ("bearer-single", "bearer-mapped"):
        return build_bearer_auth(config)
    if mode == "oidc-proxy":
        return build_oidc_proxy_auth(config)
    if mode == "remote":
        return build_remote_auth(config)

    # mode == "multi"
    oidc_auth: OIDCProxy | RemoteAuthProvider | None = build_oidc_proxy_auth(
        config
    ) or build_remote_auth(config)
    bearer_auth = build_bearer_auth(config)

    if oidc_auth is None or bearer_auth is None:
        # One of the two builders returned None despite resolve_auth_mode
        # reporting "multi" (e.g. remote-auth discovery failed, or httpx
        # missing).  Fall back to whichever one is available rather than
        # silently dropping auth entirely.
        logger.warning(
            "multi_auth_degraded oidc=%s bearer=%s — falling back to "
            "whichever auth provider succeeded",
            oidc_auth is not None,
            bearer_auth is not None,
        )
        return oidc_auth or bearer_auth

    from fastmcp.server.auth import MultiAuth

    # required_scopes=[] is load-bearing: without it, OIDC's ["openid"]
    # scope propagates to FastMCP's RequireAuthMiddleware and rejects
    # bearer tokens lacking "openid" with 403 insufficient_scope
    # (MV PR #249).
    #
    # OIDCProxy / RemoteAuthProvider (both OAuthProvider subclasses) MUST
    # go in server= — passing an OAuthProvider in verifiers= silently
    # drops its OAuth routes because get_routes/get_well_known_routes
    # only delegate to self.server.
    return MultiAuth(
        server=oidc_auth,
        verifiers=[bearer_auth],
        required_scopes=[],
    )
