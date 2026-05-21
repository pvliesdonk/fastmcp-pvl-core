"""§13 error-envelope CallToolResult builder.

Single helper :func:`build_file_exchange_error` that returns a fully
formed ``CallToolResult`` with ``isError=True``, a human-readable
``TextContent`` block, and the spec-mandated ``_meta`` key
``"nl.liesdonk.file-exchange/error"`` carrying the structured
``{code, [transport], [detail]}`` payload.

This builds the §13 error in its ``CallToolResult`` *Python* shape.
**It is not a tool return value you can hand back from a plain
``@mcp.tool`` function and expect on the wire.** fastmcp 3.3.1's
``tools/call`` handler only special-cases ``fastmcp.tools.ToolResult``
(which carries no ``isError`` field); a tool that returns an
``mcp.types.CallToolResult`` has it serialised into a text block, so
the wire response ends up ``isError: false`` with the envelope buried
in content. Conversely ``raise ToolError(...)`` sets wire
``isError: true`` but drops ``_meta``. Neither path natively carries
both halves of the §13 contract.

Emitting this envelope as a true wire-level tool-execution error
therefore requires a fastmcp middleware that intercepts a role
helper's failure and maps it onto the response (setting wire
``isError`` and wire ``_meta`` together). That middleware ships with
the ``register_file_exchange_*`` helpers in #148. Until then this
builder is consumed by code that already controls the wire response
(middleware, custom request handlers) and by tests that assert the
Python-object shape.
"""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, TextContent

from fastmcp_pvl_core._file_exchange._codes import (
    KNOWN_CODES,
    TransferErrorCode,
)

_NAMESPACE_KEY = "nl.liesdonk.file-exchange/error"

# Default human-readable text per spec-defined code. Keyed by the enum
# member (which is also the str value via the mixin).
_DEFAULT_TEXT: dict[TransferErrorCode, str] = {
    TransferErrorCode.NO_SUPPORTED_TRANSPORT: (
        "No supported transport found in transfer reference."
    ),
    TransferErrorCode.DESCRIPTOR_EXPIRED: (
        "Selected transfer descriptor expired before transfer completed."
    ),
    TransferErrorCode.NOT_ACCESSIBLE: "Transfer location is not accessible.",
    TransferErrorCode.DIGEST_MISMATCH: (
        "Transferred bytes did not match the expected digest."
    ),
    TransferErrorCode.SIZE_MISMATCH: (
        "Transferred byte count did not match the expected size."
    ),
    TransferErrorCode.TOO_LARGE: "Artifact exceeded the declared size limit.",
    TransferErrorCode.MIME_TYPE_REJECTED: (
        "Artifact's media type was not in the receiver's accepted list."
    ),
    TransferErrorCode.UNSUPPORTED_REQUIREMENT: (
        "Transfer reference requires a feature this party does not implement."
    ),
    TransferErrorCode.TRANSFER_FAILED: "File transfer failed.",
}


def _render_text(code_str: str, transport: str | None, text: str | None) -> str:
    """Pick the text block content.

    Exactly three cases, checked in order:

    1. ``text`` (caller-supplied): used verbatim. No transport suffix
       (the caller already framed the message as they want).
    2. Known code (in ``KNOWN_CODES``), ``text`` is None: the
       ``_DEFAULT_TEXT`` entry, with ``" (transport: X)"`` appended
       when ``transport`` is set.
    3. Unknown code (not in ``KNOWN_CODES``), ``text`` is None: the
       generic ``"File transfer failed: <code>"``. The transport
       suffix is NOT appended here even when ``transport`` is set —
       the generic fallback is intentionally terse, and ``transport``
       still lands in ``_meta`` regardless.

    ``detail`` is never rendered into the text — log-leak guard. The
    structured ``detail`` field is for machine consumption via
    ``_meta``; operators who want it in the text pass ``text=``
    explicitly.
    """
    if text is not None:
        return text
    if code_str in KNOWN_CODES:
        default = _DEFAULT_TEXT[TransferErrorCode(code_str)]
    else:
        return f"File transfer failed: {code_str}"
    if transport is not None:
        return f"{default} (transport: {transport})"
    return default


def build_file_exchange_error(
    code: str | TransferErrorCode,
    *,
    transport: str | None = None,
    detail: str | None = None,
    text: str | None = None,
) -> CallToolResult:
    """Build the §13 tool-execution-error CallToolResult.

    Args:
        code: A spec-defined error code (any
            :class:`TransferErrorCode` member) or, for future-spec
            codes, a raw string. The literal string lands in
            ``_meta[..., "code"]``.
        transport: Optional transport name (``"filesystem"``,
            ``"download"``, ``"upload"``, etc.). When set, populates
            ``_meta[..., "transport"]``. Additionally, when ``text``
            is None *and* ``code`` is a known code, appends
            ``" (transport: X)"`` to the default text; the suffix is
            not appended to the generic unknown-code fallback.
        detail: Optional structured detail string for machine
            consumers (e.g. ``"expected sha-256:9f..., got
            sha-256:1b..."``). Populates ``_meta[..., "detail"]``.
            **Never** appears in the human-readable text block — pass
            ``text=`` explicitly if you want it there.
        text: Optional caller-supplied text block content. Overrides
            the default text and suppresses the transport suffix.

    Returns:
        A ``CallToolResult`` with ``isError=True``, one
        ``TextContent`` block, and ``_meta`` carrying the namespaced
        envelope. Optional fields (``transport``, ``detail``) are
        OMITTED from ``_meta`` when None — no JSON nulls.
    """
    code_str = code.value if isinstance(code, TransferErrorCode) else code

    envelope: dict[str, Any] = {"code": code_str}
    if transport is not None:
        envelope["transport"] = transport
    if detail is not None:
        envelope["detail"] = detail

    rendered = _render_text(code_str, transport, text)
    result = CallToolResult(
        content=[TextContent(type="text", text=rendered)],
        isError=True,
    )
    # ``CallToolResult.meta`` has JSON alias ``_meta``; the constructor
    # doesn't accept ``meta=`` by name because the model lacks
    # ``populate_by_name=True``. Set it after construction.
    result.meta = {_NAMESPACE_KEY: envelope}
    return result
