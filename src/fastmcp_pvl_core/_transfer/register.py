"""Wire the ``/transfer`` feature onto a FastMCP server (ADR 0001 §3/§5 / §11 #5).

Two entry points share one route-mount and one token store:

- :func:`register_transfer_routes` — **path 1**: builds the shared
  :class:`TransferStore`, mounts the ``/transfer/{token}`` route (the #217
  handler), and registers the two generic link tools ``create_download_link`` /
  ``create_upload_link``. The common case — a generic pair identical across
  downstreams.
- :func:`build_transfer_links` — **path 2**: mounts the route and returns a
  :class:`TransferLinks` minter, registering **no tools**, for a downstream whose
  transfer tool the generic pair cannot express (a different name, a
  domain-accurate description, domain-specific parameters). It builds its own
  tool on the returned minter. ``register_transfer_routes`` *is*
  ``build_transfer_links`` plus the two tools, so a server calls one of them, not
  both; it returns the same :class:`TransferLinks` so a server can also run mixed
  mode (the generic pair plus its own extra tools).

pvl-core owns every **shape** decision on both paths — the route path and its
method set, the token store and its namespace, the status codes, the TTL clamp,
the ``base_url``-required guard, and (for path 1's tools) the tool names and
metadata (annotations, icons, tags). Downstream supplies the ``sink`` domain hook
on both paths, plus — on path 1 — the ``validate`` hook and optional
``download_note`` / ``upload_note`` strings *appended* to the generic tool
descriptions. There are **no override kwargs** for any shape element (ADR §7 /
§10 item 2): a note adds domain context, it never replaces pvl-core's description
or changes a tool name, route, or status code. A downstream needing a different
tool *shape* uses path 2 rather than overriding path 1.

A server must not reach into :mod:`._transfer.store` / :mod:`._transfer.routes` to
rebuild the capability-link machinery by hand: :func:`build_transfer_links`
exposes exactly that machinery as a supported seam, so path 2 needs no private
imports. The token store, route, and generic-tool shape stay pvl-core's (ADR §10
item 2). The standalone ingest primitives :func:`fetch_url` and
:func:`decode_base64_capped` remain available for a server whose ingest is not a
capability link at all.

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


class TransferLinks:
    """Mints capability links over a mounted ``/transfer`` route and shared store.

    Returned by :func:`build_transfer_links` (path 2) and
    :func:`register_transfer_routes` (path 1 / mixed). A downstream building its
    own transfer tool calls :meth:`mint_download` / :meth:`mint_upload` with an
    already-validated ``sink_handle`` — the opaque routing string the sink
    interprets, not a caller-facing ref. There is **no** ``validate`` hook here:
    in path 2 the downstream's own tool is the validation site.

    Obtain an instance from :func:`build_transfer_links` or
    :func:`register_transfer_routes`; it is not constructed directly downstream.
    """

    def __init__(
        self,
        store: TransferStore,
        *,
        base_url: str,
        transfer_config: TransferConfig,
    ) -> None:
        self._store = store
        self._base = base_url  # trailing slash already stripped by the factory
        self._transfer_config = transfer_config

    def _clamp_ttl(self, ttl_s: float | None) -> float:
        """Resolve the link TTL: the default when omitted, else clamped to the max."""
        if ttl_s is None:
            return self._transfer_config.ttl_default_s
        return min(ttl_s, self._transfer_config.ttl_max_s)

    async def _mint(
        self, sink_handle: str, kind: TransferKind, ttl_s: float | None
    ) -> dict[str, Any]:
        ttl = self._clamp_ttl(ttl_s)
        token = await self._store.mint(
            kind=kind, sink_handle=sink_handle, caps={}, ttl_seconds=ttl
        )
        return {"url": f"{self._base}/transfer/{token}", "expires_in_s": ttl}

    async def mint_download(
        self, sink_handle: str, ttl_s: float | None = None
    ) -> dict[str, Any]:
        """Mint a download link for an already-validated *sink_handle*.

        *sink_handle* is the opaque routing string the sink interprets (the same
        value path 1's ``validate`` hook returns). *ttl_s* is the requested
        lifetime in seconds — omitted uses the configured default, over the
        configured maximum is clamped to it, non-positive is rejected. Returns
        ``{"url", "expires_in_s"}``.
        """
        return await self._mint(sink_handle, "download", ttl_s)

    async def mint_upload(
        self, sink_handle: str, ttl_s: float | None = None
    ) -> dict[str, Any]:
        """Mint an upload link for an already-validated *sink_handle*.

        Same contract as :meth:`mint_download`, for the ``upload`` kind.
        """
        return await self._mint(sink_handle, "upload", ttl_s)


def build_transfer_links(
    mcp: FastMCP,
    config: ServerConfig,
    transfer_config: TransferConfig,
    *,
    sink: TransferSink,
) -> TransferLinks:
    """Mount the ``/transfer`` route and return a link minter, registering no tools.

    The **path-2** seam: a downstream whose transfer tool the generic pair cannot
    express — a different name, a domain-accurate description, domain-specific
    parameters — builds its own tool on the returned :class:`TransferLinks`
    instead of importing pvl-core internals. :func:`register_transfer_routes`
    (path 1) *is* this function plus the two generic tools, so a server calls one
    of them, not both.

    Args:
        mcp: The FastMCP server to mount the ``/transfer/{token}`` route on.
        config: The server's :class:`ServerConfig` — supplies ``base_url``
            (required, to build link URLs) and ``kv_store_url`` (the token store
            backend).
        transfer_config: The transfer env section (TTL default/max, grace, lease,
            upload cap).
        sink: Domain hook — where bytes are read from / written to.

    Returns:
        A :class:`TransferLinks` minter over the mounted route and shared store.

    Raises:
        ConfigurationError: If ``config.base_url`` is unset or blank — a transfer
            link cannot be minted without a public base URL, so this fails at
            build time rather than deferring to the first mint.
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
    return TransferLinks(store, base_url=base, transfer_config=transfer_config)


def register_transfer_routes(
    mcp: FastMCP,
    config: ServerConfig,
    transfer_config: TransferConfig,
    *,
    sink: TransferSink,
    validate: TransferValidator,
    download_note: str | None = None,
    upload_note: str | None = None,
) -> TransferLinks:
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
        download_note: Domain hook (optional) — a domain-specific sentence
            appended to ``create_download_link``'s description. pvl-core's
            generic description always comes first and is never replaced; this
            only adds domain context (e.g. what a ``ref`` is for this server).
            Omitted or blank leaves the generic description unchanged.
        upload_note: Domain hook (optional) — the same for
            ``create_upload_link``. Usually the more valuable of the two: an
            upload ``ref`` is *authored* by the caller, so stating the
            destination rules here is what a calling model most lacks.

    Returns:
        The :class:`TransferLinks` minter backing the two generic tools. Ignore
        it for path 1; keep it to also register extra domain tools on the same
        route and store (mixed mode).

    Raises:
        ConfigurationError: If ``config.base_url`` is unset or blank — a
            transfer link cannot be minted without a public base URL, so this
            fails at registration rather than deferring to the first tool call.
    """
    links = build_transfer_links(mcp, config, transfer_config, sink=sink)

    # The tools are registered by an explicit ``mcp.tool(...)(fn)`` call rather
    # than ``@mcp.tool`` decoration so ``description=`` can be composed from each
    # function's own docstring: a nested closure cannot reference its own
    # ``__doc__`` in its decorator expression. The docstring therefore stays the
    # single source of the generic description; a downstream note is appended.
    # Each closure validates the caller ``ref`` to an opaque handle, then defers
    # to the shared minter — so path 1 and path 2 mint through one code path.
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
        handle = await validate(ref, "download")
        return await links.mint_download(handle, ttl_s)

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
        handle = await validate(ref, "upload")
        return await links.mint_upload(handle, ttl_s)

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
    return links
