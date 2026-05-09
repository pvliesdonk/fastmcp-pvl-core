"""Generic token-lifecycle helpers.

This module hosts the shared one-time-token machinery used by both the
artifact-download direction (``ArtifactStore``) and the upload direction
(``UploadStore``). The generic :class:`_BaseTokenStore` provides UUID4
minting, lazy expiry sweep, and the atomic consume-and-remove primitive
both directions need.

The concrete stores layer their direction-specific data on top:

- ``ArtifactStore`` keeps the existing ``add(content, ...)`` / ``pop(token)``
  shape — it stores the bytes inline, since downloads serve them on
  demand from a single record.
- ``UploadStore`` reserves a slot at link-creation time and consumes it
  when the POST arrives — bytes do not live in the record.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Generic, Protocol, TypeVar

logger = logging.getLogger(__name__)


class _HasExpiresAt(Protocol):
    """Structural type for any record the base store can hold."""

    @property
    def expires_at(self) -> float: ...


T = TypeVar("T", bound=_HasExpiresAt)


class _BaseTokenStore(Generic[T]):
    """In-memory keyed store with TTL and atomic one-time consume.

    Tokens are UUID4 hex strings (cryptographically unguessable). Each
    public access path triggers a lazy expiry sweep.

    Subclasses provide the public mutation API (``add``/``pop`` for
    artifacts; ``reserve``/``consume`` for uploads). The base only owns
    token minting, expiry tracking, and the consume-and-remove
    primitive.
    """

    def __init__(self) -> None:
        self._records: dict[str, T] = {}

    def _mint_token(self) -> str:
        """Return a fresh UUID4 hex token."""
        return uuid.uuid4().hex

    def _atomic_consume(self, token: str) -> T | None:
        """Pop ``token`` if present and unexpired, else ``None``.

        The token is always removed from the store (even when expired)
        so a subsequent caller cannot retry with the same token.
        """
        self._purge_expired()
        record = self._records.pop(token, None)
        if record is None:
            return None
        # Defense in depth: a record can tip past expires_at between
        # _purge_expired's internal now() and this post-pop check.
        if time.time() > record.expires_at:
            return None
        return record

    def _purge_expired(self) -> None:
        """Drop any record whose ``expires_at`` is in the past."""
        now = time.time()
        expired = [t for t, r in self._records.items() if now > r.expires_at]
        for t in expired:
            del self._records[t]
        if expired:
            logger.debug("token_store_purge count=%d", len(expired))
