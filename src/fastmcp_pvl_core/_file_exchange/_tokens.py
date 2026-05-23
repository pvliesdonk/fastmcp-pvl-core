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

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fastmcp_pvl_core._errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from key_value.aio.protocols.key_value import AsyncKeyValue

    from fastmcp_pvl_core._config import ServerConfig

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


class CapabilityTokenStore:
    """Mint/lookup/consume/revoke high-entropy capability tokens.

    Wraps an ``AsyncKeyValue`` (from :func:`build_capability_token_store`'s
    ``build_kv_store`` call) plus the operator TTL ceiling. Expiry rides on
    the KV layer's TTL; single-use rides on an atomic ``delete``.
    """

    def __init__(self, store: AsyncKeyValue, *, ttl_ceiling: float) -> None:
        if ttl_ceiling <= 0:
            raise ValueError("ttl_ceiling must be positive")
        self._store = store
        self._ttl_ceiling = ttl_ceiling

    async def mint(
        self,
        metadata: Mapping[str, Any],
        *,
        ttl: float,
        single_use: bool = True,
    ) -> MintedToken:
        """Mint a token, store ``metadata`` under it with a clamped TTL.

        ``ttl`` is clamped to the operator ceiling (§10.2 "shortest value").
        ``metadata`` must be a JSON-serialisable mapping (it is persisted via
        the KV backend). Returns a :class:`MintedToken` whose ``expires_at``
        reflects the clamped TTL.
        """
        effective_ttl = min(ttl, self._ttl_ceiling)
        if effective_ttl <= 0:
            raise ValueError("ttl must be positive")
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        await self._store.put(
            token,
            {"metadata": dict(metadata), "single_use": single_use},
            collection=_COLLECTION,
            ttl=effective_ttl,
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=effective_ttl)
        return MintedToken(token=token, expires_at=expires_at)

    async def lookup(self, token: str) -> TokenRecord | None:
        """Return the token's record, or ``None`` if absent/expired.

        Does not mutate. ``None`` covers an unknown token, an expired one
        (KV TTL), and a consumed single-use one (deleted by ``consume``).
        """
        record = await self._store.get(token, collection=_COLLECTION)
        if record is None:
            return None
        return TokenRecord(metadata=record["metadata"], single_use=record["single_use"])

    async def consume(self, token: str) -> bool:
        """Enforce single-use after a successful transfer.

        For a single-use token, atomically invalidate it and return whether
        this call won the race (the ``delete`` bool — at-most-once, §10.3).
        For a multi-use token (``download`` ``singleUse: false``), this is a
        no-op returning ``True`` (the token stays valid until its TTL).
        Returns ``False`` for an absent/expired/already-consumed token.

        Call this only on transfer completion — opening a connection alone
        MUST NOT invalidate the descriptor (§10.2).
        """
        record = await self._store.get(token, collection=_COLLECTION)
        if record is None:
            return False
        if record["single_use"]:
            return await self._store.delete(token, collection=_COLLECTION)
        return True

    async def revoke(self, token: str) -> None:
        """Unconditionally invalidate a token (§15 early invalidation).

        Idempotent — revoking an absent/expired token is a no-op.
        """
        await self._store.delete(token, collection=_COLLECTION)


def build_capability_token_store(config: ServerConfig) -> CapabilityTokenStore:
    """Build a :class:`CapabilityTokenStore` from operator config.

    Resolves the backend via :func:`~fastmcp_pvl_core.build_kv_store` under
    ``namespace="file-exchange-tokens"`` (sharing the operator's chosen KV
    backend with an isolated keyspace) and threads the TTL ceiling from
    ``config.file_exchange_token_ttl``. Mirrors ``build_event_store``.
    """
    from fastmcp_pvl_core._kv_store import build_kv_store

    store = build_kv_store(config, namespace="file-exchange-tokens")
    return CapabilityTokenStore(store, ttl_ceiling=config.file_exchange_token_ttl)


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
    if not token:
        raise ValueError("token must not be empty")
    segments = [base_url.rstrip("/")]
    trimmed = path.strip("/")
    if trimmed:
        segments.append(trimmed)
    segments.append(token)
    return "/".join(segments)
