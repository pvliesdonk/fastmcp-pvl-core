"""Cross-transport route registrar (#146).

``register_file_exchange_routes`` is the single public mount point for the
file-exchange data planes. It validates all preconditions before mounting
any route (matrix row A7), then delegates to per-transport registrars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp_pvl_core._file_exchange._download import register_download_route
from fastmcp_pvl_core._file_exchange._upload import register_upload_route

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore


def register_file_exchange_routes(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    source: ArtifactSource | None = None,
    sink: ArtifactSink | None = None,
    config: ServerConfig | None = None,
) -> None:
    """Mount the file-exchange data-plane routes on ``mcp``.

    Mounts the ``download`` GET route iff ``source`` is given, and the
    ``upload`` PUT/POST route iff ``sink`` is given — supporting
    download-only, upload-only, or both. Mounting the upload route requires
    ``config`` (the operator body-size cap is load-bearing for §15 untrusted
    bytes). All **preconditions** are validated before any route is mounted
    (matrix row A7) — ``ValueError`` only fires from input validation, never
    after a mount has started. **Note:** A7 does not promise rollback if a
    per-transport mount itself raises (e.g. a framework-level route
    collision after the download route has been mounted): callers should
    treat any exception out of this function as ``mcp`` being in an
    undefined partial-mount state and not reuse it.

    Kwargs (per CLAUDE.md classification):

    - ``token_store`` (**shape**): the capability-token store. pvl-core owns
      the token-store contract; downstream constructs but does not subclass.
    - ``source`` (**hook**): downstream's :class:`ArtifactSource` for the
      download transport. Omitting it skips the download-route mount.
    - ``sink`` (**hook**): downstream's :class:`ArtifactSink` for the
      upload transport. Omitting it skips the upload-route mount.
    - ``config`` (**config**): operator-side :class:`ServerConfig` carrying
      ``file_exchange_max_artifact_size`` (the body-size cap). Required
      whenever ``sink`` is provided.
    """
    if source is None and sink is None:
        raise ValueError(
            "register_file_exchange_routes: at least one of source/sink must be given"
        )
    if sink is not None and config is None:
        raise ValueError(
            "register_file_exchange_routes: sink requires config for "
            "file_exchange_max_artifact_size"
        )
    if source is not None:
        register_download_route(mcp, token_store=token_store, source=source)
    if sink is not None:
        # ``config`` is guaranteed non-None by the precondition gate above;
        # the assert is structural type-narrowing for mypy, not defensive
        # validation (the spec contract is the load-bearing guarantee).
        assert config is not None
        register_upload_route(mcp, token_store=token_store, sink=sink, config=config)
