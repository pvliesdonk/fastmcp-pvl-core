"""The ``upload`` transport data plane (#146).

This module accrues the upload-transport helpers task by task:

- ``upload_receiver_mint`` — receiver role: mint a single-use capability
  token and emit an :class:`IntakeTicket` carrying one
  :class:`UploadSink` (this commit).
- The serving route, the sender, and the RFC 9530 / 7231 helpers land
  in subsequent commits per the implementation plan.

See ``docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md``
for the behavioural contract and ``…2026-05-27-file-exchange-146-failure-modes.md``
for the enumerated failure modes each test in this module exercises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
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
