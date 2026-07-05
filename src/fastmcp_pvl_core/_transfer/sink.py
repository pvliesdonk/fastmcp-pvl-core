"""The transfer subsystem's domain seam (ADR 0001 §3 / §11 #4).

Downstream servers implement exactly two things: *where* bytes land
(:class:`TransferSink`) and *what* bytes are acceptable
(:data:`TransferValidator`). Everything else — the ``/transfer/{token}`` route,
the link tools, the token store, size caps, TTL, redaction — is pvl-core's
shape.

The stored token carries an opaque ``sink_handle`` string that **only the sink
interprets** (e.g. a vault-relative path, an ``image://<id>`` URI). pvl-core
stores it, echoes it in tool payloads, and hands it back to
:meth:`TransferSink.read` / :meth:`TransferSink.write` — but never parses it, so
no domain branch can leak into core.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, NamedTuple, Protocol

TransferKind = Literal["download", "upload"]
"""The two link kinds; matched by the store on claim and passed to a validator."""


class TransferReadResult(NamedTuple):
    """What :meth:`TransferSink.read` returns — the bytes plus how to serve them.

    A ``NamedTuple`` (not a bare 3-tuple) so ``media_type`` and ``filename`` —
    both ``str`` — cannot be silently swapped at the construction site; it still
    unpacks positionally for callers that prefer that.
    """

    body: bytes
    media_type: str
    filename: str


class TransferSink(Protocol):
    """Where transferred bytes come from and go to — a downstream domain hook.

    ``handle`` is the opaque ``sink_handle`` the link was minted with; pvl-core
    never interprets it. Both methods are **byte-oriented and bounded by the
    size caps** (ADR §3): the whole body is materialised in memory and capped,
    not streamed. A constant-memory streaming variant is a deferred, opt-in
    future extension (ADR §12), not part of this contract.
    """

    async def read(self, handle: str) -> TransferReadResult:
        """Return the body, media type, and filename for a download *handle*."""
        ...

    async def write(self, handle: str, body: bytes) -> Mapping[str, Any]:
        """Commit an uploaded *body* to *handle*; return the tool result payload."""
        ...


TransferValidator = Callable[[str, TransferKind], Awaitable[str]]
"""Map a caller-facing ref + kind to a validated, opaque ``sink_handle``.

Raises to reject (bad extension, missing file, wrong kind, …). It is invoked at
**link creation** by the link tools — not by the route handler — because
content validation is *"what bytes are acceptable,"* a domain question pvl-core
cannot answer for a downstream. ``kind`` lets a validator apply different rules
to upload vs. download (e.g. an existence check on download, an extension
allowlist on upload). The returned string is stored verbatim as the token's
``sink_handle``.
"""
