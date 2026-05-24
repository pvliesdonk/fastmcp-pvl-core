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

import logging
from typing import TYPE_CHECKING, Literal

from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import IntakeTicket, UploadSink

if TYPE_CHECKING:
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints

logger = logging.getLogger(__name__)

# pvl-core's upload route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision (mirrors DOWNLOAD_PREFIX).
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
