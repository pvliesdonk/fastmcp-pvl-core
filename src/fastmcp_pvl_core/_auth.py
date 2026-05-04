"""Auth mode resolution and builders.

Inspect :class:`ServerConfig` to determine which auth flavor is
configured, then dispatch to the right FastMCP auth provider.
Six modes: ``none``, ``bearer-single``, ``bearer-mapped``, ``remote``,
``oidc-proxy``, ``multi``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeGuard, cast

if sys.version_info >= (3, 11):
    from typing import assert_never

    import tomllib
else:  # pragma: no cover - fallback for Python 3.10
    # ``import-not-found`` covers CI rows where ``tomli`` is excluded by
    # the marker (3.11+); ``unused-ignore`` covers local 3.10 envs where
    # ``tomli`` is installed and the ignore would otherwise be flagged.
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]
    from typing_extensions import assert_never

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._subject import set_current_auth_mode

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
# failure modes (e.g. ``AUTH_MODE=bearer-single`` with no ``BEARER_TOKEN``
# would start the server unauthenticated).
_VALID_MODES: frozenset[Literal["remote", "oidc-proxy"]] = frozenset(
    {"remote", "oidc-proxy"}
)


def _is_valid_override(value: str) -> TypeGuard[Literal["remote", "oidc-proxy"]]:
    """Narrow ``value`` to the override-valid subset of :data:`AuthMode`."""
    return value in _VALID_MODES


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
        One of the six :data:`AuthMode` literals.
    """
    explicit = (config.auth_mode or "").strip().lower()
    if explicit:
        if _is_valid_override(explicit):
            logger.info("auth_mode=%s (explicit via AUTH_MODE)", explicit)
            return explicit
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


def _load_bearer_tokens(path: Path) -> dict[str, str]:
    """Parse a bearer-token TOML file into a {token: subject} dict.

    The path is normalised with :meth:`Path.expanduser` first.  This is
    the single expansion site for both env-loaded configs (``from_env``
    intentionally keeps a leading ``~`` literal) and direct-construction
    configs (where ``Path("~/tokens.toml")`` also keeps the tilde
    literal).  Both call sites converge on the same expanded path here.

    Raises:
        ConfigurationError: file missing, unparseable, schema-invalid, or
            containing empty/non-string values.
    """
    path = path.expanduser()
    if not path.is_file():
        raise ConfigurationError(
            f"bearer tokens file not found or not a regular file: {path}"
        )
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(
            f"bearer tokens file at {path} could not be read: {exc}"
        ) from exc
    if not raw:
        raise ConfigurationError(f"bearer tokens file is empty: {path}")
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            f"bearer tokens file at {path} could not be parsed: {exc}"
        ) from exc
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens:
        raise ConfigurationError(
            f"bearer tokens file at {path} must define a non-empty [tokens] table"
        )
    result: dict[str, str] = {}
    for token, subject in tokens.items():
        # TOML keys are always strings, so just check they're non-blank.
        if not token.strip():
            raise ConfigurationError(
                f"bearer tokens file at {path}: token key is empty or whitespace-only"
            )
        if isinstance(subject, dict):
            raise ConfigurationError(
                f"bearer tokens file at {path}: token entry is a "
                f"nested table — quote token strings as "
                f'\'"<token>" = "<subject>"\''
            )
        if not isinstance(subject, str):
            raise ConfigurationError(
                f"bearer tokens file at {path}: subject must be a "
                f"string, got {type(subject).__name__}"
            )
        if not subject.strip():
            raise ConfigurationError(f"bearer tokens file at {path}: subject is empty")
        result[token] = subject
    return result


def build_bearer_auth(config: ServerConfig) -> StaticTokenVerifier | None:
    """Build a :class:`StaticTokenVerifier` for either bearer flavor.

    Two modes:

    - **Mapped** (``bearer_tokens_file`` set): parse the TOML file and
      build one verifier entry per ``token → subject`` row. The subject
      is carried via the entry's ``client_id`` so request-time code can
      retrieve it via :func:`get_subject`.
    - **Single** (``bearer_token`` set, no file): one verifier entry
      whose ``client_id`` is ``config.bearer_default_subject``
      (defaults to ``"bearer-anon"``).

    If both ``bearer_tokens_file`` and ``bearer_token`` are configured,
    the file wins and a ``WARNING`` is logged.

    Args:
        config: Populated server configuration.

    Returns:
        A configured :class:`StaticTokenVerifier`, or ``None`` when
        neither flavor is configured.

    Raises:
        ConfigurationError: when ``bearer_tokens_file`` is set but the
            file is missing, unparseable, or schema-invalid.
    """
    from fastmcp.server.auth import StaticTokenVerifier

    tokens_file = config.bearer_tokens_file
    if tokens_file is not None:
        if config.bearer_token:
            logger.warning(
                "bearer_tokens_file_takes_precedence "
                "bearer_tokens_file=%s bearer_token=<redacted> — "
                "single-token value is ignored",
                tokens_file,
            )
        mapping = _load_bearer_tokens(tokens_file)
        return StaticTokenVerifier(
            tokens={
                token: {"client_id": subject, "scopes": ["read", "write"]}
                for token, subject in mapping.items()
            },
        )

    token = (config.bearer_token or "").strip()
    if not token:
        logger.debug("bearer_auth_skipped reason=not_configured")
        return None

    # ``ServerConfig.__post_init__`` normalises blank/whitespace-only
    # ``bearer_default_subject`` to the package default, so we can read
    # the field directly without a defensive consumer-side fallback.
    logger.debug("bearer_auth_enabled token=<redacted>")
    return StaticTokenVerifier(
        tokens={
            token: {
                "client_id": config.bearer_default_subject,
                "scopes": ["read", "write"],
            },
        },
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
    ``None`` only as a precondition signal when either is missing
    (caller should already have routed away from ``remote`` mode in
    that case).  Other failure modes — ``httpx`` not installed, the
    discovery request failing (network error or malformed JSON), the
    discovery document missing ``jwks_uri`` / ``issuer`` — raise
    :class:`ConfigurationError` rather than returning ``None``: a
    server that asked for OIDC and cannot get it must fail at startup,
    never silently degrade to "no auth at all" or "bearer break-glass
    only".

    Args:
        config: Populated server configuration.

    Returns:
        A configured :class:`RemoteAuthProvider`, or ``None`` when
        ``base_url`` / ``oidc_config_url`` are absent (precondition
        miss; not a failure mode).

    Raises:
        ConfigurationError: ``httpx`` missing, discovery failed
            (network error or malformed JSON), or the discovery
            document is incomplete.
    """
    if not config.base_url or not config.oidc_config_url:
        logger.debug("remote_auth_skipped reason=missing_base_url_or_config_url")
        return None

    try:
        import httpx
    except ImportError as exc:
        raise ConfigurationError(
            "remote auth requires the 'remote-auth' extra "
            "(install with `pip install fastmcp-pvl-core[remote-auth]`); "
            "refusing to start without auth"
        ) from exc

    try:
        resp = httpx.get(config.oidc_config_url, timeout=10)
        resp.raise_for_status()
        discovery = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Catches both network-layer failures (``httpx.HTTPError``) and
        # parse-layer failures (``ValueError`` from ``resp.json()``);
        # phrasing avoids "fetch" so the message stays accurate for the
        # parse case too.
        raise ConfigurationError(
            f"OIDC discovery failed at {config.oidc_config_url}: {exc}"
        ) from exc

    jwks_uri = discovery.get("jwks_uri")
    issuer = discovery.get("issuer")
    if not jwks_uri or not issuer:
        raise ConfigurationError(
            f"OIDC discovery document at {config.oidc_config_url} is "
            f"incomplete: jwks_uri={jwks_uri!r} issuer={issuer!r}"
        )

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
    # Record the resolved mode for ``get_subject``; must run before the
    # early ``return None`` in the ``mode == "none"`` branch below so
    # tools called in stdio/no-auth servers still get ``"local"``.
    set_current_auth_mode(mode)

    # ``match``-based dispatch with an explicit ``case _`` calling
    # ``assert_never`` makes adding a new :data:`AuthMode` literal a
    # mypy error rather than a silent fall-through.
    match mode:
        case "none":
            return None
        case "bearer-single" | "bearer-mapped":
            return build_bearer_auth(config)
        case "oidc-proxy":
            return build_oidc_proxy_auth(config)
        case "remote":
            return build_remote_auth(config)
        case "multi":
            # ``build_remote_auth`` raises ``ConfigurationError`` on
            # discovery / dependency failures, so the only way for either
            # ``oidc_auth`` or ``bearer_auth`` to be ``None`` here is a
            # precondition mismatch (e.g. ``build_oidc_proxy_auth`` finds
            # missing fields and ``build_remote_auth`` likewise returns
            # ``None`` from its precondition check).  In multi mode that
            # is itself a misconfiguration: ``resolve_auth_mode`` only
            # picks "multi" when both bearer and OIDC inputs are present
            # at startup.  Hard-fail rather than silent-degrade.
            oidc_auth: OIDCProxy | RemoteAuthProvider | None = build_oidc_proxy_auth(
                config
            ) or build_remote_auth(config)
            bearer_auth = build_bearer_auth(config)

            if oidc_auth is None:
                raise ConfigurationError(
                    "multi-mode auth requires both OIDC and bearer providers; "
                    "OIDC builder returned None — check OIDC configuration "
                    "(base_url, oidc_config_url, and the proxy fields if used). "
                    "Refusing to start without OIDC; would otherwise silently "
                    "degrade to bearer-only and break the operator's "
                    "real-identity contract."
                )
            if bearer_auth is None:
                raise ConfigurationError(
                    "multi-mode auth requires both OIDC and bearer providers; "
                    "bearer builder returned None — check bearer configuration "
                    "(bearer_token or bearer_tokens_file)."
                )

            from fastmcp.server.auth import MultiAuth

            # ``required_scopes=[]`` is load-bearing: without it, OIDC's
            # ``["openid"]`` scope propagates to FastMCP's
            # RequireAuthMiddleware and rejects bearer tokens lacking
            # ``openid`` with 403 insufficient_scope (MV PR #249).
            #
            # OIDCProxy / RemoteAuthProvider (both OAuthProvider
            # subclasses) MUST go in ``server=`` — passing an
            # ``OAuthProvider`` in ``verifiers=`` silently drops its OAuth
            # routes because ``get_routes`` / ``get_well_known_routes``
            # only delegate to ``self.server``.
            return MultiAuth(
                server=oidc_auth,
                verifiers=[bearer_auth],
                required_scopes=[],
            )
        case _:
            assert_never(mode)
