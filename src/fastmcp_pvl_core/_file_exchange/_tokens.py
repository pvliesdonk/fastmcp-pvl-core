"""Capability-token minter and token store for the file-exchange data plane.

A :class:`CapabilityTokenStore` mints high-entropy URL-safe tokens (§12) and
runs their mint/lookup/consume/revoke lifecycle on the unified
``build_kv_store`` factory (#122) — no new storage abstraction. Expiry is
delegated to the KV layer's TTL; single-use is enforced by an atomic
``delete``. The per-token ``metadata`` is opaque (the store never interprets
it); the download (#145) and upload (#146) data planes own the metadata shape,
the routes, and the full-URL assembly. See
``docs/superpowers/specs/2026-05-23-file-exchange-144-capability-token-store-design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastmcp_pvl_core._errors import ConfigurationError

# Token byte length: 32 bytes = 256 bits, well above §12's ≥128-bit floor.
_TOKEN_BYTES = 32

# Collection name within the (already namespace-prefixed) KV store.
_COLLECTION = "tokens"


@dataclass(frozen=True)
class MintedToken:
    """Result of :meth:`CapabilityTokenStore.mint`.

    ``expires_at`` reflects the *clamped* TTL (a caller cannot otherwise
    observe the ceiling clamp), so a route can build a descriptor's
    ``expiresAt`` directly from it. tz-aware UTC, matching the wire models'
    ``AwareDatetime`` fields.
    """

    token: str
    expires_at: datetime


@dataclass(frozen=True)
class TokenRecord:
    """A looked-up token's stored state. ``metadata`` is opaque to the store."""

    metadata: dict[str, Any]
    single_use: bool


def capability_url(base_url: str, path: str, token: str) -> str:
    """Join ``base_url`` + ``path`` + ``token`` into a §12 capability URL.

    ``path`` is the route path supplied by the caller (#145/#146, which own
    their routes). Enforces §12's ``https`` requirement; raises
    :class:`ConfigurationError` (operator misconfiguration) when ``base_url``
    is unset or not ``https``.
    """
    if not base_url:
        raise ConfigurationError(
            "base_url is required to build a capability URL; set "
            "<PREFIX>_BASE_URL to the server's public https origin."
        )
    if not base_url.startswith("https://"):
        raise ConfigurationError(
            "capability URLs must use https (§12); base_url must start with 'https://'."
        )
    segments = [base_url.rstrip("/")]
    trimmed = path.strip("/")
    if trimmed:
        segments.append(trimmed)
    segments.append(token)
    return "/".join(segments)
