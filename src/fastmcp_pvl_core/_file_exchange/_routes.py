"""Cross-transport FastMCP route registration for the file-exchange HTTP transports.

``register_file_exchange_routes`` mounts the ``download`` GET route (when a
``source`` hook is given) and the ``upload`` PUT/POST route (when a ``sink`` hook
is given). Each transport's registrar lives in its own leaf module
(``_download``/``_upload``); this module is the single public entry point #148
threads ``token_store``/``source``/``sink``/``config`` into.
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
    """Mount the file-exchange HTTP routes on ``mcp``.

    Mounts the ``download`` GET route iff ``source`` is given and the ``upload``
    PUT/POST route iff ``sink`` is given. Mounting the upload route requires
    ``config`` (the operator size cap bounds untrusted request bodies). All of
    ``token_store``/``source``/``sink``/``config`` are threaded by #148.
    """
    if source is not None:
        register_download_route(mcp, token_store=token_store, source=source)
    if sink is not None:
        if config is None:
            raise ValueError(
                "register_file_exchange_routes: mounting the upload route "
                "requires `config` for the operator size cap"
            )
        register_upload_route(mcp, token_store=token_store, sink=sink, config=config)
