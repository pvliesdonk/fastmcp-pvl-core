"""§13 error-envelope CallToolResult builder.

Single helper :func:`build_file_exchange_error` that returns a fully
formed ``CallToolResult`` with ``isError=True``, a human-readable
``TextContent`` block, and the spec-mandated ``_meta`` key
``"nl.liesdonk.file-exchange/error"`` carrying the structured
``{code, [transport], [detail]}`` payload.

The caller's tool function returns the resulting ``CallToolResult``
verbatim — fastmcp's ``tools/call`` handler passes it through with the
``isError`` flag and ``_meta`` intact.
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

    - ``text`` (caller-supplied): used verbatim, transport suffix NOT
      appended (caller already framed the message as they want).
    - Otherwise look up ``_DEFAULT_TEXT`` by code, append
      ``(transport: X)`` when ``transport`` is set.
    - Unknown code (not in ``KNOWN_CODES``): generic
      ``"File transfer failed: <code>"``.

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
            ``_meta[..., "transport"]`` and (if ``text`` is None)
            appends ``" (transport: X)"`` to the default text.
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
