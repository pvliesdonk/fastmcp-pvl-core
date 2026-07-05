"""KV-backed one-time capability-link token store (ADR 0001 §6 / §11 #3).

A ``TransferStore`` persists one-time link tokens over an
:class:`~key_value.aio.protocols.key_value.AsyncKeyValue` backend
(``build_kv_store(config, namespace="transfer")``) and runs the
``available → in_flight → consumed`` state machine on them:

- :meth:`TransferStore.mint` creates an ``available`` token whose lifetime is
  the KV entry TTL — there is no sweep loop; an expired token (and any
  abandoned reservation past its TTL) simply vanishes from the backend.
- :meth:`TransferStore.claim` marks a token ``in_flight`` under a short lease.
  A *crashed* handler's reservation auto-frees once the lease lapses, so the
  next claim reclaims it without an explicit release.
- :meth:`TransferStore.release` reverts ``in_flight → available`` on a transient
  failure. **The one-time link survives** — ADR §6.1 rejects delete-on-claim
  precisely because mid-serve failures are common; a failed attempt must not
  spend the link.
- :meth:`TransferStore.complete` burns the token (``consumed``) on success.

**Correctness boundary (ADR §6.3).** The KV facade exposes no compare-and-set,
so one in-process :class:`asyncio.Lock` serialises every read-modify-write.
Within the single ``serve --transport http`` process these servers run, that
gives exact one-time semantics *and* restart survival (the record lives in KV).
A future horizontally-scaled deployment sharing one backend would degrade to
best-effort single-use — acceptable by design, because the TTL, not strict
single-use, is the security-relevant bound.

The token carries an **opaque ``sink_handle``** (where bytes land) and opaque
``caps`` that the store never interprets — only ``kind`` is matched on claim.
``sink_handle`` and ``caps`` must be JSON-serialisable: they round-trip through
the KV backend. Intra-package imports stay relative so a fold-in is a directory
rename.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict, cast

from .._config import ServerConfig
from .._kv_store import build_kv_store

if TYPE_CHECKING:
    from key_value.aio.protocols.key_value import AsyncKeyValue

_TOKEN_COLLECTION = "tokens"
"""KV collection holding token records, under the ``transfer`` namespace."""

_TOKEN_BYTES = 32
"""Entropy of a minted token — an unguessable 32-byte URL-safe secret."""

_NAMESPACE = "transfer"
"""The KV namespace pvl-core pins for the transfer subsystem (a shape decision:
downstream does not choose the keyspace)."""

# The three token states as a Literal, so mypy checks every comparison and
# assignment — a typo like ``"in-flight"`` fails the type check rather than
# silently creating an unreachable state. The bare ``Final`` constants each
# narrow to their individual literal type and give a single source of truth;
# the stored value stays a plain string (JSON-serialisable).
TokenStatus = Literal["available", "in_flight", "consumed"]
_AVAILABLE: Final = "available"
_IN_FLIGHT: Final = "in_flight"
_CONSUMED: Final = "consumed"


class _TokenRecord(TypedDict):
    """The persisted shape of a token, round-tripped through the KV backend.

    A ``TypedDict`` (not a class) so it stays a plain JSON-serialisable dict at
    rest while mypy checks every field access in this module. ``sink_handle``
    and ``caps`` are opaque passthrough the store never interprets:
    ``sink_handle`` is typed ``object`` (not ``Any``), so a consumer of a claim
    must narrow it before use; ``caps`` is a ``Mapping[str, Any]`` the store
    never inspects.
    """

    status: TokenStatus
    kind: str
    sink_handle: object
    caps: Mapping[str, Any]
    lease_expires_at: float | None


class TransferTokenError(Exception):
    """Base class for the token-store's claim/complete failures."""


class TokenNotFoundError(TransferTokenError):
    """The token does not exist — never minted, or its TTL has lapsed."""


class TokenKindMismatchError(TransferTokenError):
    """The token exists but was minted for a different link kind."""


class TokenAlreadyConsumedError(TransferTokenError):
    """The token was already burned by a successful :meth:`TransferStore.complete`."""


class TokenInFlightError(TransferTokenError):
    """The token is claimed and its lease is still live — a concurrent holder."""


class TokenNotClaimedError(TransferTokenError):
    """:meth:`TransferStore.complete` was called on a token that was never claimed."""


@dataclass(frozen=True)
class TransferToken:
    """A successful claim: the opaque routing info the caller needs to proceed.

    ``sink_handle`` and ``caps`` are exactly what :meth:`TransferStore.mint`
    stored — the store never interprets them.
    """

    token: str
    kind: str
    sink_handle: object
    caps: Mapping[str, Any]


class TransferStore:
    """One-time capability-link token store backed by an ``AsyncKeyValue``.

    Args:
        kv: The backend store, typically from
            ``build_kv_store(config, namespace="transfer")``. Injected so the
            store is testable against an in-memory backend.
        lease_seconds: Reclaim window for an ``in_flight`` reservation — a
            crashed handler's token auto-frees this long after :meth:`claim`.
            Operator-tunable timing (wired from config by the route layer);
            must be positive.
        clock: Wall-clock source for lease timestamps. Defaults to
            :func:`time.time`; injected only for deterministic tests. Wall
            clock (not monotonic) so a lease persisted in KV stays meaningful
            across a process restart.
    """

    def __init__(
        self,
        kv: AsyncKeyValue,
        *,
        lease_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError(f"lease_seconds must be positive, got {lease_seconds}")
        self._kv = kv
        self._lease_seconds = float(lease_seconds)
        self._clock = clock
        self._lock = asyncio.Lock()

    @classmethod
    def from_config(
        cls,
        config: ServerConfig,
        *,
        lease_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> TransferStore:
        """Build a store from operator config, pinning ``namespace="transfer"``.

        The namespace is pvl-core's decision, not the caller's — every transfer
        deployment shares one keyspace regardless of downstream.
        """
        return cls(
            build_kv_store(config, namespace=_NAMESPACE),
            lease_seconds=lease_seconds,
            clock=clock,
        )

    async def mint(
        self,
        *,
        kind: str,
        sink_handle: object,
        caps: Mapping[str, Any],
        ttl_seconds: float,
    ) -> str:
        """Create an ``available`` token and return its unguessable id.

        Args:
            kind: Link kind matched on :meth:`claim` (e.g. ``"download"``).
            sink_handle: Opaque routing info the store never interprets.
            caps: Opaque caps the store never interprets.
            ttl_seconds: Token lifetime — the KV entry TTL, the security bound.

        ``sink_handle`` and ``caps`` must be JSON-serialisable so they round-trip
        through the KV backend. That is the caller's responsibility, enforced by
        the backend at persist time (a serialising backend raises on a bad
        value) — this method does not probe it.

        Raises:
            ValueError: On empty ``kind`` or non-positive ``ttl_seconds``.
        """
        if not kind:
            raise ValueError("kind must be a non-empty string")
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        # A fresh 32-byte secret cannot collide, so minting needs no lock.
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        record: _TokenRecord = {
            "status": _AVAILABLE,
            "kind": kind,
            "sink_handle": sink_handle,
            "caps": caps,
            "lease_expires_at": None,
        }
        await self._kv.put(
            token, record, collection=_TOKEN_COLLECTION, ttl=float(ttl_seconds)
        )
        return token

    async def claim(self, token: str, kind: str) -> TransferToken:
        """Reserve *token* for *kind*, marking it ``in_flight`` under a lease.

        Reclaims an ``in_flight`` token whose lease has lapsed (a crashed
        handler). Preserves the token's remaining TTL on the re-put — dropping
        it would make the one-time link immortal.

        Raises:
            TokenNotFoundError: The token is missing or its TTL has lapsed.
            TokenKindMismatchError: The token was minted for a different kind.
            TokenAlreadyConsumedError: The token was already burned.
            TokenInFlightError: The token is claimed and its lease is still live.
        """
        async with self._lock:
            record, remaining = await self._read(token)
            if record["kind"] != kind:
                raise TokenKindMismatchError(
                    f"token is for kind {record['kind']!r}, not {kind!r}"
                )
            status = record["status"]
            if status == _CONSUMED:
                raise TokenAlreadyConsumedError("token was already consumed")
            if status == _IN_FLIGHT:
                lease = record["lease_expires_at"]
                if lease is not None and self._clock() < lease:
                    raise TokenInFlightError("token is claimed and its lease is live")
                # Lease lapsed → the previous holder crashed; reclaim below.
            record["status"] = _IN_FLIGHT
            record["lease_expires_at"] = self._clock() + self._lease_seconds
            await self._kv.put(
                token, record, collection=_TOKEN_COLLECTION, ttl=remaining
            )
            return TransferToken(
                token=token,
                kind=record["kind"],
                sink_handle=record["sink_handle"],
                caps=record["caps"],
            )

    async def release(self, token: str) -> None:
        """Revert an ``in_flight`` reservation to ``available`` (release-on-failure).

        Idempotent and safe: a no-op if the token is missing, already
        ``available``, or ``consumed`` — a late release after a successful
        :meth:`complete` must never resurrect a burned link.
        """
        async with self._lock:
            try:
                record, remaining = await self._read(token)
            except TokenNotFoundError:
                return  # expired/gone — nothing to release
            if record["status"] != _IN_FLIGHT:
                return  # available or consumed — nothing to revert
            record["status"] = _AVAILABLE
            record["lease_expires_at"] = None
            await self._kv.put(
                token, record, collection=_TOKEN_COLLECTION, ttl=remaining
            )

    async def complete(self, token: str) -> None:
        """Burn *token* (``in_flight → consumed``) on a successful transfer.

        Idempotent on an already-``consumed`` token, and a no-op if the token
        expired mid-transfer (its TTL lapsed).

        Raises:
            TokenNotClaimedError: The token exists but was never claimed.
        """
        async with self._lock:
            try:
                record, remaining = await self._read(token)
            except TokenNotFoundError:
                return  # expired mid-transfer — nothing left to burn
            status = record["status"]
            if status == _CONSUMED:
                return  # idempotent re-complete
            if status != _IN_FLIGHT:
                raise TokenNotClaimedError("token was never claimed")
            record["status"] = _CONSUMED
            record["lease_expires_at"] = None
            await self._kv.put(
                token, record, collection=_TOKEN_COLLECTION, ttl=remaining
            )

    async def _read(self, token: str) -> tuple[_TokenRecord, float]:
        """Read a live token record and its remaining TTL, or raise.

        A single ``ttl()`` call returns both the value and its remaining
        lifetime. A token with no positive remaining TTL is treated as gone —
        this both handles expiry and fails closed on the anomalous no-TTL
        record we never mint, so the remaining value is always safe to re-apply
        on the caller's put.

        The value is cast to :class:`_TokenRecord`: the store is the only writer
        to this keyspace, so a live record is trusted to have the minted shape.
        """
        record, remaining = await self._kv.ttl(token, collection=_TOKEN_COLLECTION)
        if record is None or remaining is None or remaining <= 0:
            raise TokenNotFoundError("token not found or expired")
        return cast("_TokenRecord", record), remaining
