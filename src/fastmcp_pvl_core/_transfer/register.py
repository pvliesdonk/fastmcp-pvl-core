"""Wire the ``/transfer`` feature onto a FastMCP server (ADR 0001 §3/§5 / §11 #5).

:func:`register_transfer_routes` is the one public entry point: it builds a
single shared :class:`TransferStore`, mounts the ``/transfer/{token}`` route
(the #217 handler), and registers the two link tools ``create_download_link`` /
``create_upload_link``. pvl-core owns every **shape** decision here — the tool
names, the route path and its method set, the status codes, the TTL clamp, the
``base_url``-required guard, **and the tool metadata** (annotations, icons,
tags). Downstream supplies the ``sink`` and ``validate`` hooks, plus optional
``download_note`` / ``upload_note`` strings that are *appended* to the generic
tool descriptions. There are **no override kwargs** for any shape element (ADR
§7 / §10 item 2): a note adds domain context, it never replaces pvl-core's
description or changes a tool name, route, or status code.

The two tools carry generic, universal metadata so every downstream server
presents them identically. A server that needs domain-specific titles, icons, or
descriptions must NOT mutate the registered tools post-hoc, nor reach into
:mod:`._transfer.store` / :mod:`._transfer.routes` to rebuild the capability-link
machinery under a different name — the token store, route, and link-tool shape
are pvl-core's, full stop (ADR §10 item 2). The only two things exported for
standalone reuse are :func:`fetch_url` and :func:`decode_base64_capped` — a
server with a genuinely different ingest shape (e.g. no capability link at all)
builds a tool on those, not on the transfer framework's internals.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

import base64
import inspect
from typing import Any

from fastmcp import FastMCP
from mcp.types import Icon, ToolAnnotations

from .._config import ServerConfig
from .._errors import ConfigurationError
from .config import TransferConfig
from .routes import make_transfer_handler
from .sink import TransferKind, TransferSink, TransferValidator
from .store import TransferStore

# Lucide icons (MIT) — raw SVG markup, base64-encoded once at import time into
# data URIs so the tools carry universal icons with no file-system or network
# dependency (foldable and offline-capable). The raw markup stays reviewable and
# diffable in source; the base64 blob is derived, not authored.
_DOWNLOAD_SVG = (
    '<svg width="24" height="24" xmlns="http://www.w3.org/2000/svg"'
    ' viewBox="0 0 24 24">'
    '<path fill="none" stroke="currentColor" stroke-linecap="round"'
    ' stroke-linejoin="round" stroke-width="2"'
    ' d="M12 15V3m9 12v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    '<path d="m7 10l5 5l5-5"/>'
    "</svg>"
)
_UPLOAD_SVG = (
    '<svg width="24" height="24" xmlns="http://www.w3.org/2000/svg"'
    ' viewBox="0 0 24 24">'
    '<path fill="none" stroke="currentColor" stroke-linecap="round"'
    ' stroke-linejoin="round" stroke-width="2"'
    ' d="M12 3v12m5-7l-5-5l-5 5m14 7v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    "</svg>"
)


def _icon(svg: str) -> Icon:
    """Return an :class:`Icon` with a base64 data URI for *svg*."""
    payload = base64.b64encode(svg.encode()).decode("ascii")
    return Icon(src=f"data:image/svg+xml;base64,{payload}", mimeType="image/svg+xml")


_DOWNLOAD_ICON = _icon(_DOWNLOAD_SVG)
_UPLOAD_ICON = _icon(_UPLOAD_SVG)


def _describe(fn: Any, note: str | None) -> str:
    """Compose a tool description from *fn*'s docstring plus an optional *note*.

    pvl-core's generic description (the function docstring) always comes first
    and is never altered; a downstream *note* is appended after a blank line.
    An absent note (``None`` or blank) yields the generic description unchanged.
    """
    base = inspect.cleandoc(fn.__doc__ or "")
    if note and note.strip():
        return f"{base}\n\n{note.strip()}"
    return base


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
    download_note: str | None = None,
    upload_note: str | None = None,
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
        download_note: Optional domain-specific sentence appended to
            ``create_download_link``'s description. pvl-core's generic
            description always comes first and is never replaced; this only adds
            domain context (e.g. what a ``ref`` is for this server). Omitted or
            blank leaves the generic description unchanged.
        upload_note: The same for ``create_upload_link``. Usually the more
            valuable of the two: an upload ``ref`` is *authored* by the caller,
            so stating the destination rules here is what a calling model most
            lacks.

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

    # The tools are registered by an explicit ``mcp.tool(...)(fn)`` call rather
    # than ``@mcp.tool`` decoration so ``description=`` can be composed from each
    # function's own docstring: a nested closure cannot reference its own
    # ``__doc__`` in its decorator expression. The docstring therefore stays the
    # single source of the generic description; a downstream note is appended.
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

    mcp.tool(
        name="create_download_link",
        description=_describe(create_download_link, download_note),
        annotations=ToolAnnotations(
            title="Create Download Link",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
        ),
        icons=[_DOWNLOAD_ICON],
    )(create_download_link)

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

    mcp.tool(
        name="create_upload_link",
        description=_describe(create_upload_link, upload_note),
        annotations=ToolAnnotations(
            title="Create Upload Link",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
        ),
        icons=[_UPLOAD_ICON],
        tags={"write"},
    )(create_upload_link)
