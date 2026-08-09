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

A sink may additionally raise :class:`TransferSinkError` (or a named subclass) to
signal a specific 4xx/5xx status for one transfer instead of the default 500;
that status is the sink's domain judgment, bounded by pvl-core to an error status.

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


class TransferSinkError(Exception):
    """Raised by a :class:`TransferSink` to signal a specific HTTP error status.

    A sink's :meth:`~TransferSink.read` / :meth:`~TransferSink.write` normally
    lets an unexpected failure propagate, which the ``/transfer/{token}`` handler
    turns into an opaque **500** (after releasing the token). Raising this
    instead tells the handler to return a specific status — a domain judgment
    only the sink can make (the resource is gone, the backend is briefly down,
    …). pvl-core owns the invariant: ``status_code`` must be a client- or
    server-error status (**4xx/5xx**, 400-599); the handler still releases the
    token, and any non-``TransferSinkError`` failure still maps to 500.

    Prefer a named subclass (:class:`TransferResourceGoneError`, …) for a common
    case; use the base directly for a status without one (e.g.
    ``TransferSinkError(401)``).

    Args:
        status_code: The HTTP status to return, in 400-599.
        *args: An optional message, preserved on ``str(exc)``.

    Raises:
        ValueError: If ``status_code`` is not in 400-599 — a programming error
            surfaced at the raise site rather than silently mis-mapped.
    """

    status_code: int

    def __init__(self, status_code: int, *args: object) -> None:
        if not 400 <= status_code <= 599:
            raise ValueError(
                f"TransferSinkError status_code must be a 4xx/5xx HTTP error "
                f"status (400-599), got {status_code}"
            )
        super().__init__(*args)
        self.status_code = status_code

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        # Keep the exception picklable/copyable. Exception.__reduce__ replays
        # ``self.args`` (the message only) through ``type(self)(...)``; for the
        # base that would call ``TransferSinkError(<message>)`` and fail the
        # status_code check. Reconstruct with the right arity: the base needs
        # status_code first, a fixed-status subclass takes only the message args.
        if type(self) is TransferSinkError:
            return (self.__class__, (self.status_code, *self.args))
        return (self.__class__, self.args)


class TransferResourceGoneError(TransferSinkError):
    """The resource the link pointed at existed and is now gone (**410 Gone**).

    Distinct from the claim-time 404: the sink runs only after a successful
    claim, so the caller held a valid link — 410 says the resource vanished, not
    that the link is bad.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(410, *args)


class TransferNotFoundError(TransferSinkError):
    """The resource the handle names was never there (**404 Not Found**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(404, *args)


class TransferForbiddenError(TransferSinkError):
    """The handle resolved but access to the resource is denied (**403**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(403, *args)


class TransferRateLimitedError(TransferSinkError):
    """A backend the sink calls is rate-limiting the request (**429**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(429, *args)


class TransferUnavailableError(TransferSinkError):
    """The backend is temporarily unavailable; the caller may retry (**503**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(503, *args)


class TransferBadGatewayError(TransferSinkError):
    """An upstream dependency returned an invalid/failed response (**502**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(502, *args)


class TransferGatewayTimeoutError(TransferSinkError):
    """An upstream dependency the sink calls timed out (**504**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(504, *args)
