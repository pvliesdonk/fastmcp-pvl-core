"""The ``upload`` transport data plane (#146).

This module accrues the upload-transport helpers task by task:

- ``upload_receiver_mint`` — receiver role: mint a single-use capability
  token and emit an :class:`IntakeTicket` carrying one
  :class:`UploadSink`.
- ``_content_digest_parse`` / ``_content_digest_format`` / ``_media_range_matches``
  — RFC 9530 Content-Digest dictionary-entry parse + format, and an RFC 7231
  media-range matcher used by the route to enforce ``acceptMimeTypes``.
- The serving route and the sender land in subsequent commits per
  the implementation plan.

See ``docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md``
for the behavioural contract and ``…2026-05-27-file-exchange-146-failure-modes.md``
for the enumerated failure modes each test in this module exercises.
"""

from __future__ import annotations

import base64 as _b64
import binascii
from typing import TYPE_CHECKING, Literal

from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._staging import _HASHLIB_BY_LABEL
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import IntakeTicket, UploadSink

if TYPE_CHECKING:
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints

# pvl-core's upload route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision.
UPLOAD_PREFIX = "/fx/u"


async def upload_receiver_mint(
    artifact_id: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    expected: ArtifactConstraints | None = None,
    method: Literal["PUT", "POST"] = "PUT",
) -> IntakeTicket:
    """Receiver role (push): mint an upload token and emit an IntakeTicket.

    Mint-only — no hook is called, no bytes move. The token stores the
    ``artifact_id`` and the ``expected`` constraints so the route can
    correlate received bytes back to a wire id and enforce limits at
    deposit time. The token store treats the metadata as opaque (#144).
    """
    minted = await token_store.mint(
        {
            "artifact_id": artifact_id,
            "expected": expected.model_dump() if expected is not None else None,
        },
        ttl=ttl,
        single_use=True,
    )
    url = capability_url(base_url, UPLOAD_PREFIX, minted.token)
    return IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId=artifact_id,
        expected=expected,
        sinks=[
            UploadSink(
                transport="upload",
                url=url,
                method=method,
                expiresAt=minted.expires_at,
            )
        ],
    )


def _content_digest_parse(header: str) -> tuple[str, bytes] | None:
    """Parse an RFC 9530 ``Content-Digest`` structured-field dictionary entry.

    Returns ``(algo_label, raw_digest_bytes)`` on success, or ``None`` if no
    supported, well-formed entry is present. Unsupported algorithms within a
    dictionary are silently skipped (RFC 9530 §3: a recipient MUST ignore
    digest values associated with algorithms that it does not support); the
    function returns the first supported, well-formed entry it finds. ``None``
    is treated by the caller as a verification failure (``digest-mismatch``),
    never a silent skip of a header that did declare a known algorithm
    (matrix rows D4, D5; spec §10.3).
    """
    if not header:
        return None
    for entry in header.split(","):
        entry = entry.strip()
        if not entry:
            continue
        label, sep, rest = entry.partition("=")
        if sep != "=":
            continue
        label = label.strip().lower()
        if label not in _HASHLIB_BY_LABEL:
            continue
        rest = rest.strip()
        if len(rest) < 2 or not rest.startswith(":") or not rest.endswith(":"):
            continue
        b64 = rest[1:-1]
        if not b64:
            continue
        try:
            raw = _b64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        return label, raw
    return None


def _content_digest_format(label: str, raw: bytes) -> str:
    """Format ``(label, raw_digest_bytes)`` as ``algo=:base64:`` per RFC 9530."""
    return f"{label}=:{_b64.b64encode(raw).decode('ascii')}:"


def _media_range_matches(content_type: str, accept: list[str]) -> bool:
    """RFC 7231 §3.1.1.1 media-range match: parameters ignored, case-insensitive.

    ``type/*`` matches any subtype; ``*/*`` matches anything. An empty
    ``content_type`` or an empty ``accept`` list never matches
    (matrix rows F3, F4).
    """
    if not content_type or not accept:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    if "/" not in main:
        return False
    main_type, main_sub = main.split("/", 1)
    for entry in accept:
        if not entry:
            continue
        entry_main = entry.split(";", 1)[0].strip().lower()
        if "/" not in entry_main:
            continue
        e_type, e_sub = entry_main.split("/", 1)
        if (e_type == "*" or e_type == main_type) and (
            e_sub == "*" or e_sub == main_sub
        ):
            return True
    return False
