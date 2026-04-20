"""One-time artifact download support.

Downstream tools stash bytes and return an HTTP URL pointing at
``GET /artifacts/{token}``. The first retrieval consumes the token and
removes it from the store — subsequent retrievals 404. Tokens have a
TTL; expired entries are purged lazily on every access.

Domain-specific wrapping (e.g. loading bytes on demand from some
external source) should live in the downstream project; this module
provides only the generic token store and HTTP route.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.responses import Response

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request

logger = logging.getLogger(__name__)


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


class ArtifactStore:
    """In-memory one-time artifact store with TTL expiry.

    Tokens are UUID4 hex strings (cryptographically unguessable). Each
    :meth:`add` / :meth:`pop` call triggers a lazy sweep of expired
    entries, so the store never needs a background task.

    Note:
        The store is process-local — tokens do not survive a restart
        and are not shared between workers.
    """

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        """Create a new store.

        Args:
            ttl_seconds: Lifetime of each token in seconds (default 1 hour).
        """
        self._records: dict[str, TokenRecord] = {}
        self._ttl = float(ttl_seconds)

    def add(self, content: bytes, *, filename: str, mime_type: str) -> str:
        """Stash ``content`` and return an opaque one-time token.

        Args:
            content: Raw bytes to serve on retrieval.
            filename: Filename advertised via ``Content-Disposition``.
            mime_type: MIME type advertised via ``Content-Type``.

        Returns:
            A hex UUID4 token string.
        """
        self._purge_expired()
        token = uuid.uuid4().hex
        self._records[token] = TokenRecord(
            content=content,
            filename=filename,
            mime_type=mime_type,
            expires_at=time.time() + self._ttl,
        )
        logger.debug(
            "artifact_add token_prefix=%s size=%d mime=%s ttl=%.1fs",
            token[:8],
            len(content),
            mime_type,
            self._ttl,
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
        self._purge_expired()
        record = self._records.pop(token, None)
        if record is None:
            return None
        if time.time() > record.expires_at:
            logger.debug("artifact_pop_expired token_prefix=%s", token[:8])
            return None
        return record

    def _purge_expired(self) -> None:
        """Remove expired records (lazy cleanup).

        Called from :meth:`add` and :meth:`pop` so the store self-trims
        without needing a background task.
        """
        now = time.time()
        expired = [t for t, r in self._records.items() if now > r.expires_at]
        for t in expired:
            del self._records[t]
        if expired:
            logger.debug("artifact_purge count=%d", len(expired))

    @classmethod
    def register_route(
        cls,
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
