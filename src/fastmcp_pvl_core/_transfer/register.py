"""Wire the ``/transfer`` feature onto a FastMCP server (ADR 0001 §3/§5 / §11 #5).

:func:`register_transfer_routes` is the one public entry point: it builds a
single shared :class:`TransferStore`, mounts the ``/transfer/{token}`` route
(the #217 handler), and registers the two link tools ``create_download_link`` /
``create_upload_link``. pvl-core owns every **shape** decision here — the tool
names, the route path and its method set, the status codes, the TTL clamp, the
``base_url``-required guard, **and the tool metadata** (annotations, icons,
tags). The only hooks are ``sink`` (where bytes land) and ``validate`` (what
bytes are acceptable); there are **no override kwargs** for any shape element
(ADR §7 / §10 item 2).

The two tools carry generic, universal metadata so every downstream server
presents them identically. A server that needs domain-specific titles, icons, or
descriptions should build its own tools on the exported primitives
(:class:`TransferStore`, :func:`fetch_url`, :func:`decode_base64_capped`)
rather than mutating the registered tools post-hoc.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from mcp.types import Icon, ToolAnnotations

from .._config import ServerConfig
from .._errors import ConfigurationError
from .config import TransferConfig
from .routes import make_transfer_handler
from .sink import TransferKind, TransferSink, TransferValidator
from .store import TransferStore

# Lucide icons (MIT) embedded as data URIs so the tools carry universal icons
# with no file-system or network dependency — foldable and offline-capable.
_DOWNLOAD_ICON = Icon(
    src=(
        "data:image/svg+xml;base64,"
        "PHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyIgdmlld0JveD0iMCAwIDI0IDI0Ij48cGF0aCBmaWxsPSJub25lIiBzdHJva2U9ImN1cnJlbnRDb2xvciIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIiBzdHJva2Utd2lkdGg9IjIiIGQ9Ik0xMiAxNVYzbTkgMTJ2NGEyIDIgMCAwIDEtMiAySDVhMiAyIDAgMCAxLTItMnYtNCIvPjxwYXRoIGQ9Im03IDEwbDUgNWw1LTUiLz48L3N2Zz4="
    ),
    mimeType="image/svg+xml",
)
_UPLOAD_ICON = Icon(
    src=(
        "data:image/svg+xml;base64,"
        "PHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyIgdmlld0JveD0iMCAwIDI0IDI0Ij48cGF0aCBmaWxsPSJub25lIiBzdHJva2U9ImN1cnJlbnRDb2xvciIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIiBzdHJva2Utd2lkdGg9IjIiIGQ9Ik0xMiAzdjEybTUtN2wtNS01bC01IDVtMTQgN3Y0YTIgMiAwIDAgMS0yIDJINWEyIDIgMCAwIDEtMi0ydi00Ii8+PC9zdmc+"
    ),
    mimeType="image/svg+xml",
)

_ROUTE_PATH = "/transfer/{token}"

# Route the methods a client realistically sends a request *body* with, so each
# reaches the handler's own 405 + ``Connection: close`` dispatch (see
# ``make_transfer_handler``'s Note) — an undrained body on a keep-alive socket
# desyncs the next request. GET/POST/PUT are served; DELETE/PATCH fall to the
# handler's closing 405. ``custom_route`` requires ``methods=``, so this is a
# superset rather than "omit it". OPTIONS is deliberately NOT routed here: it must
# stay with Starlette's router so a CORS preflight is answered normally, not
# turned into a 405 (a preflight carries no body). A method not listed (a
# non-preflight OPTIONS with a body, TRACE — which forbids a body per RFC 7231
# §4.3.8 — or an exotic verb) falls to the router's 405 without the close header;
# that residual is accepted. HEAD is auto-added by Starlette for GET.
_ROUTE_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")


def register_transfer_routes(
    mcp: FastMCP,
    config: ServerConfig,
    transfer_config: TransferConfig,
    *,
    sink: TransferSink,
    validate: TransferValidator,
) -> None:
    """Register the ``/transfer`` route and the two link tools on *mcp*.

    Args:
        mcp: The FastMCP server to register the route and tools on.
        config: The server's :class:`ServerConfig` — supplies ``base_url``
            (required, to build link URLs) and ``kv_store_url`` (the token
            store backend).
        transfer_config: The transfer env section (TTL default/max, grace,
            lease, upload cap).
        sink: Domain hook — where bytes are read from / written to.
        validate: Domain hook — maps a caller ref + kind to a validated opaque
            ``sink_handle`` (raises to reject); invoked at link creation.

    Raises:
        ConfigurationError: If ``config.base_url`` is unset or blank — a
            transfer link cannot be minted without a public base URL, so this
            fails at registration rather than deferring to the first tool call.
    """
    if not config.base_url:
        raise ConfigurationError(
            "base_url is required to mint transfer links; set <PREFIX>_BASE_URL"
        )
    base = config.base_url.rstrip("/")
    store = TransferStore.from_config(
        config,
        lease_seconds=transfer_config.lease_s,
        grace_seconds=transfer_config.grace_ttl_s,
    )
    handler = make_transfer_handler(
        store, sink, max_upload_bytes=transfer_config.max_upload_bytes
    )
    mcp.custom_route(_ROUTE_PATH, methods=list(_ROUTE_METHODS))(handler)

    def _clamp_ttl(ttl_s: float | None) -> float:
        """Resolve the link TTL: the default when omitted, else clamped to the max."""
        if ttl_s is None:
            return transfer_config.ttl_default_s
        return min(ttl_s, transfer_config.ttl_max_s)

    async def _mint_link(
        ref: str, kind: TransferKind, ttl_s: float | None
    ) -> dict[str, Any]:
        handle = await validate(ref, kind)
        ttl = _clamp_ttl(ttl_s)
        token = await store.mint(
            kind=kind, sink_handle=handle, caps={}, ttl_seconds=ttl
        )
        return {"url": f"{base}/transfer/{token}", "expires_in_s": ttl}

    @mcp.tool(
        name="create_download_link",
        annotations=ToolAnnotations(
            title="Create Download Link",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
        ),
        icons=[_DOWNLOAD_ICON],
    )
    async def create_download_link(
        ref: str, ttl_s: float | None = None
    ) -> dict[str, Any]:
        """Mint a capability link that serves the bytes for *ref* once.

        *ref* is a domain reference the ``validate`` hook resolves to an opaque
        download handle (raising to reject). *ttl_s* is the requested lifetime in
        seconds — omitted uses the configured default, a value over the configured
        maximum is clamped to it, and a non-positive value is rejected. Returns
        ``{"url", "expires_in_s"}``.
        """
        return await _mint_link(ref, "download", ttl_s)

    @mcp.tool(
        name="create_upload_link",
        annotations=ToolAnnotations(
            title="Create Upload Link",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
        ),
        icons=[_UPLOAD_ICON],
        tags={"write"},
    )
    async def create_upload_link(
        ref: str, ttl_s: float | None = None
    ) -> dict[str, Any]:
        """Mint a capability link that accepts one upload for *ref*.

        *ref* is a domain reference the ``validate`` hook resolves to an opaque
        upload handle (raising to reject). *ttl_s* is the requested lifetime in
        seconds — omitted uses the configured default, a value over the configured
        maximum is clamped to it, and a non-positive value is rejected. Returns
        ``{"url", "expires_in_s"}``.
        """
        return await _mint_link(ref, "upload", ttl_s)
