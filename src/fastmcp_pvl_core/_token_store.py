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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from starlette.responses import Response

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request

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


# ---------------------------------------------------------------------------
# Artifact direction (downloads)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenRecord:
    """A one-time downloadable artifact.

    Attributes:
        content: The raw bytes to serve.
        filename: Suggested filename for the ``Content-Disposition`` header.
        mime_type: MIME type served in the ``Content-Type`` header.
        expires_at: Unix timestamp after which the record is considered expired.
    """

    content: bytes
    filename: str
    mime_type: str
    expires_at: float


def _sanitize_filename(filename: str) -> str:
    """Strip characters that would break a ``Content-Disposition`` header.

    The HTTP header grammar forbids raw CR/LF and treats double quote and
    backslash as structural characters. We remove CR/LF entirely (prevents
    header injection) and replace quote and backslash with ``_`` so the
    header remains parseable. Non-ASCII code points are not percent-encoded
    here — callers that need them should pass an already-sanitised filename.

    Args:
        filename: Raw filename supplied by the caller.

    Returns:
        A filename safe to embed inside a quoted-string ``filename=`` param.
    """
    cleaned = filename.replace("\r", "").replace("\n", "")
    cleaned = cleaned.replace('"', "_").replace("\\", "_")
    return cleaned or "download"


class ArtifactStore(_BaseTokenStore[TokenRecord]):
    """In-memory one-time artifact store with TTL expiry.

    Tokens are UUID4 hex strings (cryptographically unguessable). Each
    :meth:`add` / :meth:`pop` call triggers a lazy sweep of expired
    entries, so the store never needs a background task.

    Note:
        The store is process-local — tokens do not survive a restart
        and are not shared between workers.
    """

    def __init__(
        self,
        ttl_seconds: float = 3600.0,
        *,
        base_url: str | None = None,
        route_path: str = "/artifacts/{token}",
    ) -> None:
        """Create a new store.

        Args:
            ttl_seconds: Default lifetime of each token in seconds
                (default 1 hour). Per-call overrides on :meth:`add` and
                :meth:`put_ephemeral` take precedence.
            base_url: Public base URL (scheme + host, optionally a path
                prefix) used by :meth:`build_url` and
                :meth:`put_ephemeral` to assemble download URLs. ``None``
                means URL construction is unavailable; callers using only
                :meth:`add` / :meth:`pop` don't need it.
            route_path: URL template the artifact route is mounted at.
                MUST contain ``{token}``. Pass the same value as ``path``
                to :meth:`register_route` so the URL constructed here
                matches the route actually mounted on the server.
        """
        super().__init__()
        if "{token}" not in route_path:
            raise ValueError(
                f"route_path must contain '{{token}}' placeholder; got {route_path!r}"
            )
        self._ttl = float(ttl_seconds)
        self._base_url = base_url
        self._route_path = route_path

    def add(
        self,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
        ttl_seconds: float | None = None,
    ) -> str:
        """Stash ``content`` and return an opaque one-time token.

        Args:
            content: Raw bytes to serve on retrieval.
            filename: Filename advertised via ``Content-Disposition``.
            mime_type: MIME type advertised via ``Content-Type``.
            ttl_seconds: Per-token lifetime override. ``None`` (default)
                uses the store's default TTL set at construction.

        Returns:
            A hex UUID4 token string.
        """
        self._purge_expired()
        token = self._mint_token()
        ttl = self._ttl if ttl_seconds is None else float(ttl_seconds)
        self._records[token] = TokenRecord(
            content=content,
            filename=filename,
            mime_type=mime_type,
            expires_at=time.time() + ttl,
        )
        logger.debug(
            "artifact_add token_prefix=%s size=%d mime=%s ttl=%.1fs",
            token[:8],
            len(content),
            mime_type,
            ttl,
        )
        return token

    def pop(self, token: str) -> TokenRecord | None:
        """Consume ``token``, returning its record or ``None``.

        The token is always removed from the store, even if it has
        already expired. The return value is ``None`` when the token is
        unknown or when it has passed its ``expires_at`` timestamp.

        Args:
            token: The hex UUID token string returned by :meth:`add`.

        Returns:
            The :class:`TokenRecord`, or ``None`` if unknown/expired.
        """
        return self._atomic_consume(token)

    @property
    def has_base_url(self) -> bool:
        """``True`` iff :meth:`build_url` / :meth:`put_ephemeral` will work.

        Cheaper than catching the ``RuntimeError`` from
        :meth:`build_url`, and lets external callers branch without
        reaching into the private ``_base_url`` attribute.
        """
        return self._base_url is not None

    def build_url(self, token: str) -> str:
        """Return the public URL for ``token``.

        Args:
            token: Opaque token previously returned by :meth:`add` or
                :meth:`put_ephemeral`.

        Returns:
            The full public URL constructed from the store's
            ``base_url`` and ``route_path``.

        Raises:
            RuntimeError: If the store was constructed without
                ``base_url``.
        """
        if self._base_url is None:
            raise RuntimeError(
                "ArtifactStore.base_url is required for URL construction"
            )
        # Normalise the join so callers that pass a route_path without a
        # leading slash (e.g. "artifacts/{token}") still get a well-formed
        # URL — concatenation alone would silently yield "host/artifacts...".
        base = self._base_url.rstrip("/")
        path = "/" + self._route_path.lstrip("/")
        return f"{base}{path}".replace("{token}", token)

    def put_ephemeral(
        self,
        content: bytes,
        *,
        content_type: str,
        filename: str,
        ttl_seconds: float | None = None,
    ) -> str:
        """Stash ``content`` and return a one-time download URL.

        Convenience wrapper around :meth:`add` + :meth:`build_url` for
        the common "give me a URL that serves these bytes once" case.

        Args:
            content: Raw bytes to serve on retrieval.
            content_type: MIME type advertised via ``Content-Type``.
            filename: Filename advertised via ``Content-Disposition``.
            ttl_seconds: Per-token lifetime override. ``None`` (default)
                uses the store's default TTL.

        Returns:
            The full public URL pointing at the stashed content.

        Raises:
            RuntimeError: If the store was constructed without
                ``base_url``.
        """
        token = self.add(
            content,
            filename=filename,
            mime_type=content_type,
            ttl_seconds=ttl_seconds,
        )
        return self.build_url(token)

    @staticmethod
    def register_route(
        mcp: FastMCP,
        store: ArtifactStore,
        *,
        path: str = "/artifacts/{token}",
    ) -> None:
        """Mount ``GET {path}`` on a FastMCP HTTP app to serve artifacts.

        Unknown / expired / already-consumed tokens return HTTP 404. A
        hit returns the stored bytes with the stored ``Content-Type`` and
        an ``attachment`` ``Content-Disposition`` header.

        The route is registered via :meth:`FastMCP.custom_route`, which
        places it outside FastMCP's auth middleware — by design, since
        the token itself is the capability.

        Args:
            mcp: FastMCP server instance (HTTP/SSE transport).
            store: Shared :class:`ArtifactStore` holding tokens.
            path: URL template. Must contain ``{token}``. Defaults to
                ``/artifacts/{token}``.
        """

        @mcp.custom_route(path, methods=["GET"])
        async def _artifact_handler(request: Request) -> Response:
            token = request.path_params.get("token", "")
            record = store.pop(token)
            if record is None:
                logger.debug(
                    "artifact_handler_miss token_prefix=%s",
                    (token or "")[:8],
                )
                return Response(content="Not Found", status_code=404)

            safe_filename = _sanitize_filename(record.filename)
            logger.info(
                "artifact_handler_serve token_prefix=%s size=%d mime=%s",
                token[:8],
                len(record.content),
                record.mime_type,
            )
            return Response(
                content=record.content,
                media_type=record.mime_type,
                headers={
                    "Content-Disposition": (f'attachment; filename="{safe_filename}"'),
                },
            )


# ---------------------------------------------------------------------------
# Artifact singleton accessor
# ---------------------------------------------------------------------------
#
# The HTTP route handler registered via ``mcp.custom_route`` runs outside
# any DI/lifespan context, and tool bodies need to share the same
# ``ArtifactStore`` instance with it. A module-level singleton is the
# simplest way to bridge them.

_artifact_store: ArtifactStore | None = None


def set_artifact_store(store: ArtifactStore | None) -> None:
    """Install ``store`` as the module-level singleton.

    Pass ``None`` to clear (e.g. in tests that need a fresh slate).

    Args:
        store: The :class:`ArtifactStore` to install, or ``None``.
    """
    global _artifact_store
    _artifact_store = store


def get_artifact_store() -> ArtifactStore:
    """Return the module-level :class:`ArtifactStore` singleton.

    Returns:
        The currently-installed store.

    Raises:
        RuntimeError: If no store has been installed via
            :func:`set_artifact_store` — typically because the server's
            HTTP wiring did not run (e.g. stdio transport).
    """
    if _artifact_store is None:
        raise RuntimeError(
            "ArtifactStore singleton is not set — call set_artifact_store(...) "
            "during server startup (HTTP/SSE transports only)"
        )
    return _artifact_store
