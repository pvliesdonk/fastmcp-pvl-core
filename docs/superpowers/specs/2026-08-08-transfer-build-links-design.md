# `build_transfer_links` — the path-2 link-minting seam (issue #249)

## Problem

ADR 0001 shipped `register_transfer_routes`: it mounts the `/transfer/{token}`
route and registers the generic `create_download_link` / `create_upload_link`
tool pair. That serves servers happy with the generic pair ("path 1"). It does
not serve a second legitimate case: a downstream whose transfer tool the generic
pair cannot express — a different name, a domain-accurate description, or
domain-specific parameters ("path 2").

Today everything path 2 needs (`TransferStore`, `make_transfer_handler`) is under
`_transfer/` and unexported, so a downstream can only reach it by importing
`fastmcp_pvl_core._transfer.*` privates — which `fastmcp-server-template`#309
already did. This design adds the supported seam that removes that need.

## Ownership (why this is not an override kwarg)

ADR §3 assigns *tool names* to pvl-core — that governs **core's own tools**. A
downstream domain tool that happens to mint a capability link is *downstream's*
tool; core has no standing over its name or description because core does not
know the domain. So the two paths are intentional and distinct:

- **Path 1** — core's generic pair. Easy, predictable, identical everywhere.
- **Path 2** — downstream's own tools on core's link mechanics, when path 1 is
  insufficient or its generic description would mislead.

Path 2 is not an override of path 1's shape; it is a lower-level seam. Core still
owns the route, the token store, the TTL clamp, the status codes, and the link
URL shape — path 2 changes none of them. It only omits the two generic tools and
the `validate` hook, because in path 2 the downstream's own tool is the
validation site.

## Public API

```python
def build_transfer_links(
    mcp: FastMCP,
    config: ServerConfig,
    transfer_config: TransferConfig,
    *,
    sink: TransferSink,
) -> TransferLinks: ...


class TransferLinks:
    async def mint_download(
        self, sink_handle: str, ttl_s: float | None = None
    ) -> dict[str, Any]: ...

    async def mint_upload(
        self, sink_handle: str, ttl_s: float | None = None
    ) -> dict[str, Any]: ...
```

- `build_transfer_links` mounts the `/transfer/{token}` route, builds the shared
  `TransferStore`, and returns a `TransferLinks` minter. It registers **no
  tools**.
- `TransferLinks.mint_download` / `mint_upload` take a **pre-validated
  `sink_handle`**, not a caller `ref`. There is no `validate` hook: in path 2 the
  downstream's own tool validates before it calls the minter. Each clamps the
  TTL, mints a token, and returns `{"url", "expires_in_s"}` — the same payload
  path 1's tools return.
- A `TransferLinks` is obtained only from `build_transfer_links` (or
  `register_transfer_routes`); it is not constructed directly by downstream.

## Path 1 re-expressed on path 2

`register_transfer_routes` becomes `build_transfer_links` plus two tools:

```python
def register_transfer_routes(...) -> TransferLinks:
    links = build_transfer_links(mcp, config, transfer_config, sink=sink)
    # register create_download_link / create_upload_link; each closure does
    #   handle = await validate(ref, kind)
    #   return await links.mint_download(handle, ttl_s)   # or mint_upload
    return links
```

There is exactly **one** route-mount and **one** store-construction code path
(inside `build_transfer_links`), so the two paths cannot drift. The return type
changes `None` → `TransferLinks` (a non-breaking widening) so mixed mode works:
a server can take the generic pair *and* register extra tools on the returned
minter.

### The three supported call shapes

| Scenario | Call | Result |
|---|---|---|
| Path 1 only | `register_transfer_routes(...)` | Route + generic pair; ignore the return |
| Mixed | `links = register_transfer_routes(...)` | Route + generic pair; register extra tools on `links` |
| Pure path 2 | `links = build_transfer_links(...)` | Route only, no tools |

Calling *both* entry points on one server is not supported —
`register_transfer_routes` *is* `build_transfer_links` plus tools, so calling
both would mount the route twice.

## Surfacing

- New public module `fastmcp_pvl_core/transfer.py` re-exports the transfer
  feature's surface with an `__all__`: `build_transfer_links`, `TransferLinks`,
  `register_transfer_routes`, `TransferConfig`, `TransferSink`,
  `TransferReadResult`, `TransferKind`, `TransferValidator`. Importing
  `from fastmcp_pvl_core.transfer import build_transfer_links` reads as an
  explicit "I am building my own transfer tool."
- `build_transfer_links` and `TransferLinks` are also re-exported top-level from
  `fastmcp_pvl_core`, alongside the existing transfer names.
- `_transfer/` stays internal with relative imports (foldability): a fold-in
  stays a directory rename.

## Placement

`TransferLinks` and `build_transfer_links` live in `_transfer/register.py`
alongside `register_transfer_routes`. That module already imports the store,
route handler, sink, and config that `build_transfer_links` needs, and its module
docstring documents both entry points together — keeping the two halves of the
"path 1 = build + tools" story in one file.

## Still internal (unchanged)

`TransferStore`, `TransferToken`, `make_transfer_handler`, and the six
`Token*Error` types stay unexported. Path 2 needs none of them: `TransferLinks`
is the entire seam.

## Also in this PR — `register.py` module docstring

`register.py`'s module docstring on `main` still calls `register_transfer_routes`
"the one public entry point", says a server "must NOT … reach into
`._transfer.store` / `._transfer.routes` … full stop", and claims "the only two
things exported for standalone reuse are `fetch_url` and `decode_base64_capped`".
The private-import prohibition stands, but the implication that path 2 is
therefore impossible does not. Rewrite the docstring to describe both entry
points and point a server needing a different tool shape at
`build_transfer_links`.

## Tests

- **Pure path 2**: `build_transfer_links` registers no tools; a link minted via
  `mint_download` redeems end-to-end over ASGI (minter → store → route → handler
  → sink).
- **Mixed mode**: a path-1-minted link and a path-2-minted link both redeem
  against the same store/route.
- **Route mounted exactly once** by `build_transfer_links`.
- **`base_url` guard** fires from `build_transfer_links` (unset/blank).
- **TTL clamp on minter calls**: omitted → default; over max → clamped; in range
  → honoured; non-positive → rejected (`ValueError` from `store.mint`).
- **Path 1's existing contract tests pass unchanged.**
```
