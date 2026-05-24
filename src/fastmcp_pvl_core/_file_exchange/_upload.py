"""The ``upload`` transport data plane (#146).

Three role helpers (receiver mint, the PUT/POST serving route, sender) plus the
RFC 9530 / RFC 7231 HTTP helpers the route and sender need, free functions
mirroring ``_filesystem.py`` / ``_download.py``. The receiver mints a capability
URL backed by the #144 token store; the route streams the pushed body to a
transient temp file, verifies the declared ``Content-Digest`` and the ticket's
``expected`` constraints **before** handing the #142 ``ArtifactSink`` a real sync
fd, then consumes the single-use token only on a successful store; the sender
stages the artifact, computes a ``Content-Digest``, and pushes it through the
#147 ``guarded_stream``. See
``docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md``.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import TYPE_CHECKING, Literal

from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._staging import _HASHLIB_BY_LABEL
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import IntakeTicket, UploadSink

if TYPE_CHECKING:
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints

logger = logging.getLogger(__name__)

# pvl-core's upload route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision (mirrors DOWNLOAD_PREFIX).
UPLOAD_PREFIX = "/fx/u"

# Default digest algorithm for the receiver's recorded ArtifactMetadata.digest
# and the sender's Content-Digest (pvl-core's shape). Members of _HASHLIB_BY_LABEL
# are also accepted on an inbound Content-Digest.
_DEFAULT_DIGEST_LABEL = "sha-256"


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

    ``artifact_id`` is the server's opaque identifier for the artifact slot,
    stored in the token for the route to correlate the pushed bytes; ``expected``
    is the §7.4 constraint set the route enforces at ingest (stored opaquely too).
    ``base_url`` is the server's public https origin; ``ttl`` is clamped by the
    token store's ceiling. Minting only — no hook runs and no bytes move (the
    sink is threaded into the route, where the bytes actually arrive).
    """
    minted = await token_store.mint(
        {
            "artifact_id": artifact_id,
            "expected": expected.model_dump(mode="json") if expected else None,
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


def _format_content_digest(label: str, raw: bytes) -> str:
    """Format a raw digest as an RFC 9530 ``Content-Digest`` member: ``label=:b64:``.

    The Structured-Field byte-sequence form (base64 wrapped in colons) — distinct
    from the wire ``ArtifactMetadata.digest`` field's ``label:hex`` form.
    """
    return f"{label}=:{base64.b64encode(raw).decode('ascii')}:"


def _parse_content_digest(header: str) -> tuple[str, bytes] | None:
    """Parse the first supported RFC 9530 ``Content-Digest`` member.

    Returns ``(label, raw_digest_bytes)`` for the first member whose algorithm is
    in :data:`_HASHLIB_BY_LABEL` and whose value is a well-formed byte sequence,
    or ``None`` when the header has no supported, well-formed member. A present
    header that parses to ``None`` is unverifiable -> the route rejects it
    (digest-mismatch), never silently skips (§15).
    """
    for member in header.split(","):
        label, sep, value = member.strip().partition("=")
        if sep != "=":
            continue
        label = label.strip().lower()
        value = value.strip()
        if label not in _HASHLIB_BY_LABEL:
            continue
        if len(value) < 2 or value[0] != ":" or value[-1] != ":":
            continue
        try:
            raw = base64.b64decode(value[1:-1], validate=True)
        except (binascii.Error, ValueError):
            continue
        return label, raw
    return None


def _media_type_accepted(content_type: str | None, accept: list[str]) -> bool:
    """Match a request media type against RFC 7231 §3.1.1.1 media-ranges.

    ``type/subtype`` matches exactly; ``type/*`` matches any subtype of ``type``;
    ``*/*`` matches anything. Parameters (``; charset=...``) are ignored. A
    missing or malformed ``Content-Type`` matches nothing (the route rejects).
    """
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if "/" not in media:
        return False
    main, sub = media.split("/", 1)
    for entry in accept:
        candidate = entry.split(";", 1)[0].strip().lower()
        if candidate == "*/*":
            return True
        if "/" not in candidate:
            continue
        cand_main, cand_sub = candidate.split("/", 1)
        if cand_main == main and cand_sub in ("*", sub):
            return True
    return False
