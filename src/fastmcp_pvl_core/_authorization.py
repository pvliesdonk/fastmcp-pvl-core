"""Authorization primitives: native auth checks, ACL loader, claims support.

Downstream MCP servers that need to enforce per-subject access control
on their tools, resources, or prompts can opt in by:

1. Annotating components with ``meta={"required_scope": "<scope>"}``.
2. Building a native ``AuthCheck`` via :func:`make_acl_check` (bearer/ACL
   mode), :func:`make_claims_check` (OIDC claim mode), or
   :func:`any_check` (OR-combinator for ``multi`` mode).
3. Installing FastMCP's ``AuthMiddleware(auth=<check>)`` in the server's
   middleware stack.
"""

from __future__ import annotations

import inspect
import json
import logging
import sys
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthCheck, AuthContext

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - fallback for Python 3.10
    # ``import-not-found`` covers CI rows where ``tomli`` is excluded by
    # the marker (3.11+); ``unused-ignore`` covers local 3.10 envs where
    # ``tomli`` is installed and the ignore would otherwise be flagged.
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]

from fastmcp_pvl_core._errors import ConfigurationError

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

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

    The ``*`` scope is interpreted by :func:`make_acl_check` as
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
# Native AuthCheck helpers (FastMCP 3.3+)
# ---------------------------------------------------------------------------


def _resolve_required(required: str | None, component: object) -> str | None:
    """Resolve the required scope: explicit arg, else component meta.

    Returns ``None`` when no scope is required (component is
    unrestricted). An invalid ``meta["required_scope"]`` (present but not
    a non-empty string) is logged and treated as unrestricted, matching
    the opt-in posture.
    """
    if required is not None:
        required = required.strip()
        return required or None
    meta = getattr(component, "meta", None) or {}
    value = meta.get("required_scope")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        logger.warning(
            "authz_meta_invalid required_scope=%r — expected non-empty "
            "string; treating as unrestricted",
            value,
        )
        return None
    return value.strip()


def _subject_of(token: object) -> str | None:
    """Resolve the caller subject from a token.

    Prefers the OIDC ``sub`` claim; falls back to ``client_id`` (the
    bearer-mode subject). Mirrors ``fastmcp_pvl_core.get_subject``'s
    resolution rule so the ACL keys identically across modes. (Kept local
    to avoid coupling to the ambient ``get_access_token`` path that
    ``get_subject`` uses.)
    """
    claims = getattr(token, "claims", None)
    if isinstance(claims, dict):
        sub = claims.get("sub")
        if isinstance(sub, str) and sub:
            return sub
    client_id = getattr(token, "client_id", None)
    if isinstance(client_id, str) and client_id:
        return client_id
    return None


def _extract_claim_values(claims: object, claim: str) -> set[str]:
    """Normalise a claim's value to a set of strings (lenient).

    Scalar string -> ``{value}`` (never whitespace-split). List/tuple/set
    -> its string elements only. Any other shape (absent, int, bool,
    None, dict, empty list) -> empty set. A request is never failed on an
    unexpected claim shape.
    """
    if not isinstance(claims, dict):
        return set()
    value = claims.get(claim)
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {v for v in value if isinstance(v, str)}
    return set()


def make_claims_check(
    claim: str,
    grants: Mapping[str, AbstractSet[str]] | None = None,
    required: str | None = None,
) -> AuthCheck:
    """Build a native ``AuthCheck`` enforcing a claim→scope grant.

    Reads ``claim`` from ``ctx.token.claims``. With ``grants=None`` the
    caller is granted exactly the string values in the claim (identity).
    With ``grants`` supplied, those values are mapped through the table
    and unioned. OIDC modes only — bearer tokens carry no usable claims.
    Required scope resolves from ``required=`` or
    ``ctx.component.meta["required_scope"]``.
    """
    claim = claim.strip()
    if not claim:
        raise ValueError("claim must be a non-empty string")

    def check(ctx: AuthContext) -> bool:
        token = ctx.token
        if token is None:
            return False
        scope = _resolve_required(required, ctx.component)
        if scope is None:
            return True
        values = _extract_claim_values(getattr(token, "claims", None), claim)
        if grants is None:
            granted: set[str] = set(values)
        else:
            granted = set()
            for value in values:
                mapped = grants.get(value)
                if mapped is not None:
                    granted |= set(mapped)
        return "*" in granted or scope in granted

    return check


def parse_claim_grants(raw: str) -> dict[str, frozenset[str]]:
    """Parse an inline-JSON claim-value→scopes map. Fail-fast.

    Schema: a JSON object mapping each claim value (e.g. a group name) to
    an array of scope strings. Empty object is permitted (deny-everyone).
    Mirrors ``load_acl``'s value validation. Raises
    :class:`ConfigurationError` on every malformed condition.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigurationError(
            f"claim grants could not be parsed as JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigurationError(
            "claim grants must be a JSON object mapping claim values to "
            f"scope arrays; got {type(data).__name__}"
        )
    result: dict[str, frozenset[str]] = {}
    for key, scopes in data.items():
        # JSON object keys are always strings.
        if not key.strip():
            raise ConfigurationError(
                "claim grants: claim-value key is empty or whitespace-only"
            )
        if key == "*":
            raise ConfigurationError(
                'claim grants: "*" as a claim-value key is not allowed '
                "(global key wildcards collapse the model)"
            )
        if not isinstance(scopes, list):
            raise ConfigurationError(
                f"claim grants: value for {key!r} must be an array of scope "
                f"strings; got {type(scopes).__name__}"
            )
        cleaned: set[str] = set()
        for scope in scopes:
            if not isinstance(scope, str):
                raise ConfigurationError(
                    f"claim grants: {key!r}: scope must be a string; got "
                    f"{type(scope).__name__}"
                )
            if not scope.strip():
                raise ConfigurationError(
                    f"claim grants: {key!r}: scope is empty or whitespace-only"
                )
            cleaned.add(scope.strip())
        result[key] = frozenset(cleaned)
    return result


def make_acl_check(
    acl: Mapping[str, AbstractSet[str]],
    required: str | None = None,
) -> AuthCheck:
    """Build a native ``AuthCheck`` enforcing a subject→scope ACL.

    The returned check reads the caller subject from ``ctx.token``
    (``sub`` claim, else ``client_id``) and the required scope from
    ``required=`` or ``ctx.component.meta["required_scope"]``. This is
    the only authz primitive usable in bearer modes, which carry no
    OIDC claims. ``acl`` is captured by reference.
    """

    def check(ctx: AuthContext) -> bool:
        token = ctx.token
        if token is None:
            return False
        scope = _resolve_required(required, ctx.component)
        if scope is None:
            return True
        subject = _subject_of(token)
        if subject is None:
            return False
        granted = acl.get(subject)
        if granted is None:
            return False
        return "*" in granted or scope in granted

    return check


def any_check(*checks: AuthCheck) -> AuthCheck:
    """Combine checks with OR (native ``AuthMiddleware`` uses AND).

    Returns an async check that passes if any sub-check passes,
    short-circuiting on the first ``True``. Sub-checks may be sync or
    async; coroutine results are awaited. Used for ``multi`` mode where a
    bearer caller satisfies the ACL check and an OIDC caller satisfies
    the claims check.
    """
    if not checks:
        raise ValueError("any_check requires at least one check")

    async def combined(ctx: AuthContext) -> bool:
        for check in checks:
            result = check(ctx)
            if inspect.isawaitable(result):
                result = await result
            if result:
                return True
        return False

    return combined
