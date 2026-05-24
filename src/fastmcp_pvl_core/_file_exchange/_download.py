"""The ``download`` transport data plane (#145).

Three role helpers plus a serving route, free functions mirroring
``_filesystem.py``. The transport is **lazy**: the GET route calls the #142
``ArtifactSource`` hook on demand and streams the result — pvl-core holds no
copy of the artifact. The provider mints a capability URL backed by the #144
token store; the fetcher retrieves it through the #147 ``guarded_stream`` into a
transient temp file, verifies size+digest before handing the sink a real sync
fd, then deletes the temp. See
``docs/superpowers/specs/2026-05-24-file-exchange-145-download-data-plane-design.md``.

An ``ArtifactSource`` offered via ``download`` MUST yield stable bytes for the
token's lifetime: the route re-opens the hook on every GET and on each ``Range``
resume.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import DownloadSource, TransferHandle

if TYPE_CHECKING:
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

logger = logging.getLogger(__name__)

# pvl-core's download route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision.
DOWNLOAD_PREFIX = "/fx/d"


async def download_provider_mint(
    artifact: ArtifactMetadata,
    key: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    single_use: bool = True,
) -> TransferHandle:
    """Provider role (pull): mint a download token and emit a TransferHandle.

    ``artifact`` is the caller-supplied metadata for what is being offered
    (lazy serving means the source hook is untouched at mint; the route opens it
    at GET). ``key`` is the server's opaque artifact identifier, stored opaquely
    in the token for the route to read back. ``base_url`` is the server's public
    https origin; ``ttl`` is clamped by the token store's ceiling.
    """
    minted = await token_store.mint({"key": key}, ttl=ttl, single_use=single_use)
    url = capability_url(base_url, DOWNLOAD_PREFIX, minted.token)
    return TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=artifact,
        sources=[
            DownloadSource(
                transport="download",
                url=url,
                expiresAt=minted.expires_at,
                singleUse=single_use,
            )
        ],
    )
