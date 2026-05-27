"""The ``upload`` transport data plane (#146).

Three free helpers — ``upload_receiver_mint``, ``upload_sender_consume``,
and ``register_upload_route`` — that mirror ``_download.py``'s pull plane.
The route accepts an HTTPS ``PUT``/``POST`` capability URL, streams the
request body to a transient temp file with size + digest verification,
and on a clean verify deposits the bytes into the receiver's
``ArtifactSink`` (#142). The sender stages the offered bytes (hook stream
→ temp + hash) and ``PUT``s them through the #147 SSRF guard.

See ``docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md``
for the contract and ``…2026-05-27-file-exchange-146-failure-modes.md`` for
the enumerated failure modes each test in this module exercises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import IntakeTicket, UploadSink

if TYPE_CHECKING:
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints

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
