"""Cross-transport route registrar (#146).

``register_file_exchange_routes`` is the single public mount point for the
file-exchange data planes. It validates all preconditions before mounting
any route (matrix row A7), then delegates to per-transport registrars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

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
    bytes). All preconditions are validated **before** any route is mounted
    (matrix row A7).
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
        register_upload_route(
            mcp,
            token_store=token_store,
            sink=sink,
            config=cast("ServerConfig", config),
        )
