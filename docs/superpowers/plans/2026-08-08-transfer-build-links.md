# `build_transfer_links` path-2 seam — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `build_transfer_links(...) -> TransferLinks`, a public seam that mounts the `/transfer/{token}` route and returns a link minter registering no tools, so a downstream can build its own transfer tools without importing pvl-core internals.

**Architecture:** `build_transfer_links` owns the sole route-mount and store-construction path and returns a `TransferLinks` minter. `register_transfer_routes` (path 1) becomes `build_transfer_links` plus the two generic tools and returns the same `TransferLinks` (so mixed mode works); its return type widens `None → TransferLinks`. A new public `fastmcp_pvl_core/transfer.py` re-exports the transfer surface; `build_transfer_links` and `TransferLinks` are also re-exported top-level. `_transfer/` stays internal with relative imports.

**Tech Stack:** Python 3.10+, FastMCP, Starlette, httpx (test ASGI), pytest (async).

## Global Constraints

- Intra-package imports stay **relative** (`from ._x import …`); no runtime self-name lookups (foldability, per `CLAUDE.md`).
- `TransferStore`, `TransferToken`, `make_transfer_handler`, and the six `Token*Error` types stay **unexported**.
- No override kwargs: `build_transfer_links` takes only the `sink` domain hook; it has **no** `validate` hook (path 2's own tool validates).
- Public API signatures (copy verbatim):
  - `def build_transfer_links(mcp: FastMCP, config: ServerConfig, transfer_config: TransferConfig, *, sink: TransferSink) -> TransferLinks`
  - `async def TransferLinks.mint_download(self, sink_handle: str, ttl_s: float | None = None) -> dict[str, Any]`
  - `async def TransferLinks.mint_upload(self, sink_handle: str, ttl_s: float | None = None) -> dict[str, Any]`
- Return payload of both mint methods: `{"url": f"{base}/transfer/{token}", "expires_in_s": ttl}` (identical to path 1's tools).
- Local checks before any push: `uv sync --all-extras && uv run pytest && uv run ruff format --check . && uv run ruff check . && uv run mypy src`.
- Verified FastMCP/Starlette facts: `await mcp.list_tools()` returns a `list` (empty `[]` when no tools). Custom routes appear in `mcp.http_app().routes` as `starlette.routing.Route` objects with `.path == "/transfer/{token}"`.

---

### Task 1: `TransferLinks` + `build_transfer_links`, refactor `register_transfer_routes`, top-level surfacing, docstring

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py` (add `TransferLinks`, `build_transfer_links`; refactor `register_transfer_routes`; rewrite module docstring)
- Modify: `src/fastmcp_pvl_core/_transfer/__init__.py` (export the two new names)
- Modify: `src/fastmcp_pvl_core/__init__.py` (re-export the two new names top-level)
- Test: `tests/test_transfer_register.py` (add path-2, mixed-mode, route-once tests; keep path-1 tests unchanged)

**Interfaces:**
- Consumes (existing, unchanged): `TransferStore.from_config(config, *, lease_seconds, grace_seconds)`, `TransferStore.mint(*, kind, sink_handle, caps, ttl_seconds) -> str`, `make_transfer_handler(store, sink, *, max_upload_bytes)`, module constants `_ROUTE_PATH = "/transfer/{token}"`, `_ROUTE_METHODS`, and the icon/annotation constants already in `register.py`.
- Produces:
  - `class TransferLinks` with `async mint_download(self, sink_handle: str, ttl_s: float | None = None) -> dict[str, Any]` and `async mint_upload(...)` of the same signature. Constructed only via the factory: `TransferLinks(store, *, base_url: str, transfer_config: TransferConfig)`.
  - `def build_transfer_links(mcp, config, transfer_config, *, sink: TransferSink) -> TransferLinks`.
  - `register_transfer_routes(...) -> TransferLinks` (return type widened from `None`).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_transfer_register.py`. First extend the top import block:

```python
from fastmcp_pvl_core import (
    ServerConfig,
    TransferConfig,
    TransferLinks,
    TransferReadResult,
    build_transfer_links,
    register_transfer_routes,
)
```

Add a `starlette` import near the other imports:

```python
from starlette.routing import Route
```

Add a path-2 builder helper and a route-count helper after the existing `_register` helper:

```python
def _build_links(
    *,
    base_url: str | None = "https://x.example.com",
    transfer_config: TransferConfig | None = None,
    sink: _RecordingSink | None = None,
) -> tuple[FastMCP, TransferLinks, _RecordingSink]:
    mcp = FastMCP("t")
    sink = sink or _RecordingSink()
    config = ServerConfig(base_url=base_url, kv_store_url="memory://")
    links = build_transfer_links(mcp, config, transfer_config or _tconfig(), sink=sink)
    return mcp, links, sink


def _transfer_route_count(mcp: FastMCP) -> int:
    """Count mounted ``/transfer/{token}`` routes in the assembled ASGI app."""
    app = mcp.http_app()
    return sum(
        1 for r in app.routes if isinstance(r, Route) and r.path == "/transfer/{token}"
    )
```

Then add the test classes:

```python
class TestBuildTransferLinksGuard:
    def test_unset_base_url_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="base_url"):
            _build_links(base_url=None)

    def test_blank_base_url_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="base_url"):
            _build_links(base_url="")


class TestBuildTransferLinksNoTools:
    async def test_registers_no_tools(self) -> None:
        mcp, _, _ = _build_links()
        assert await mcp.list_tools() == []

    async def test_route_mounted_once(self) -> None:
        mcp, _, _ = _build_links()
        assert _transfer_route_count(mcp) == 1


class TestTransferLinksMinting:
    async def test_mint_download_shape(self) -> None:
        _, links, _ = _build_links()
        res = await links.mint_download("handle:doc:download")
        assert res["url"].startswith("https://x.example.com/transfer/")
        assert res["expires_in_s"] == 100.0  # the configured default

    async def test_mint_upload_shape(self) -> None:
        _, links, _ = _build_links()
        res = await links.mint_upload("handle:dest:upload")
        assert res["url"].startswith("https://x.example.com/transfer/")
        assert res["expires_in_s"] == 100.0

    async def test_base_url_trailing_slash_stripped(self) -> None:
        _, links, _ = _build_links(base_url="https://x.example.com/")
        res = await links.mint_download("h")
        assert "/transfer/" in res["url"]
        assert "com//transfer" not in res["url"]


class TestTransferLinksTtlClamp:
    async def test_omitted_ttl_uses_default(self) -> None:
        _, links, _ = _build_links()
        res = await links.mint_download("h")
        assert res["expires_in_s"] == 100.0

    async def test_over_max_ttl_is_clamped(self) -> None:
        _, links, _ = _build_links()
        res = await links.mint_download("h", ttl_s=9999)
        assert res["expires_in_s"] == 200.0  # the max

    async def test_in_range_ttl_is_honoured(self) -> None:
        _, links, _ = _build_links()
        res = await links.mint_download("h", ttl_s=150)
        assert res["expires_in_s"] == 150.0

    async def test_non_positive_ttl_is_rejected(self) -> None:
        # The clamp only bounds the ceiling; store.mint rejects a dead link.
        _, links, _ = _build_links()
        with pytest.raises(ValueError):
            await links.mint_download("h", ttl_s=0)


class TestPurePath2EndToEnd:
    async def test_minted_link_redeems_over_http(self) -> None:
        mcp, links, sink = _build_links()
        res = await links.mint_download("handle:doc:download")
        async with _client(mcp) as client:
            resp = await client.get(_path_of(res["url"]))
        assert resp.status_code == 200
        assert resp.content == b"BODY"
        assert sink.read_handles == ["handle:doc:download"]


class TestMixedMode:
    async def test_register_returns_transfer_links(self) -> None:
        mcp = FastMCP("t")
        config = ServerConfig(base_url="https://x.example.com", kv_store_url="memory://")
        links = register_transfer_routes(
            mcp, config, _tconfig(), sink=_RecordingSink(), validate=_RecordingValidator()
        )
        assert isinstance(links, TransferLinks)

    async def test_path1_and_path2_links_redeem_same_store(self) -> None:
        mcp = FastMCP("t")
        sink = _RecordingSink()
        config = ServerConfig(base_url="https://x.example.com", kv_store_url="memory://")
        links = register_transfer_routes(
            mcp, config, _tconfig(), sink=sink, validate=_RecordingValidator()
        )
        p1 = await mcp.call_tool("create_download_link", {"ref": "doc1"})
        p2 = await links.mint_download("handle:doc2:download")
        async with _client(mcp) as client:
            r1 = await client.get(_path_of(p1.structured_content["url"]))
            r2 = await client.get(_path_of(p2["url"]))
        assert r1.status_code == 200 and r1.content == b"BODY"
        assert r2.status_code == 200 and r2.content == b"BODY"
        # Both links resolved against the one shared store/route/sink.
        assert sink.read_handles == ["handle:doc1:download", "handle:doc2:download"]

    async def test_register_mounts_route_once(self) -> None:
        mcp, _, _ = _register()
        assert _transfer_route_count(mcp) == 1
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_transfer_register.py -k "BuildTransferLinks or TransferLinksMinting or TransferLinksTtlClamp or PurePath2 or MixedMode" -v`
Expected: FAIL — `ImportError: cannot import name 'build_transfer_links'` (the top-level import does not resolve yet).

- [ ] **Step 3: Implement `TransferLinks` in `src/fastmcp_pvl_core/_transfer/register.py`**

Insert the class just below the `_ROUTE_METHODS` constant (before `register_transfer_routes`). `Any` and `TransferKind` are already imported in this module.

```python
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
```

- [ ] **Step 4: Implement `build_transfer_links` in the same file**

Insert immediately after `TransferLinks` (before `register_transfer_routes`):

```python
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
```

- [ ] **Step 5: Refactor `register_transfer_routes` to call `build_transfer_links`**

Replace the whole body of `register_transfer_routes` below its docstring. Delete the old `base_url` guard, `base = …`, `store = …`, `handler = …`, `mcp.custom_route(...)`, the `_clamp_ttl` closure, and the `_mint_link` closure. Change the signature return annotation to `-> TransferLinks`. The two tool closures now validate then delegate to the returned minter:

```python
    links = build_transfer_links(mcp, config, transfer_config, sink=sink)

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
```

Update the `register_transfer_routes` signature line `) -> None:` to `) -> TransferLinks:` and add a `Returns:` paragraph to its docstring:

```
    Returns:
        The :class:`TransferLinks` minter backing the two generic tools. Ignore
        it for path 1; keep it to also register extra domain tools on the same
        route and store (mixed mode).
```

- [ ] **Step 6: Rewrite the `register.py` module docstring**

Replace lines 1–27 (the module docstring) with:

```python
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
```

- [ ] **Step 7: Export the two names from `src/fastmcp_pvl_core/_transfer/__init__.py`**

Add to the `from .register import …` line so it reads:

```python
from .register import TransferLinks, build_transfer_links, register_transfer_routes
```

Add `"TransferLinks"` and `"build_transfer_links"` to `__all__` (keep it sorted), and update the module docstring's "public surface" sentence to mention the two entry points and the `TransferLinks` minter.

- [ ] **Step 8: Re-export top-level in `src/fastmcp_pvl_core/__init__.py`**

In the `from ._transfer import (…)` block add `TransferLinks` and `build_transfer_links` (keep the block sorted), and add both to the top-level `__all__` (keep sorted).

- [ ] **Step 9: Run the full transfer register suite**

Run: `uv run pytest tests/test_transfer_register.py -v`
Expected: PASS — the new path-2/mixed/route-once tests and **all pre-existing path-1 tests** (base_url guard, tool registration, domain notes, link minting, TTL clamp, validator rejection, end-to-end).

- [ ] **Step 10: Run lint + types over the change**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/register.py \
        src/fastmcp_pvl_core/_transfer/__init__.py \
        src/fastmcp_pvl_core/__init__.py \
        tests/test_transfer_register.py
git commit -m "feat(transfer): build_transfer_links path-2 minter seam (#249)"
```

---

### Task 2: Public `fastmcp_pvl_core/transfer.py` namespace module

**Files:**
- Create: `src/fastmcp_pvl_core/transfer.py`
- Test: `tests/test_transfer_public_surface.py`

**Interfaces:**
- Consumes: the `_transfer` package re-exports `TransferConfig`, `TransferKind`, `TransferLinks`, `TransferReadResult`, `TransferSink`, `TransferValidator`, `build_transfer_links`, `register_transfer_routes` (the last two added in Task 1).
- Produces: importable module `fastmcp_pvl_core.transfer` with `__all__` covering the eight names above.

- [ ] **Step 1: Write the failing test** — create `tests/test_transfer_public_surface.py`:

```python
"""The transfer feature's public import surface (issue #249).

``fastmcp_pvl_core.transfer`` is a cohesive re-export of the transfer feature,
not a second implementation; every name is the same object as the top-level
re-export, and the internal store/handler/token types stay unexported.
"""

from __future__ import annotations

import fastmcp_pvl_core
from fastmcp_pvl_core import transfer

_EXPECTED = {
    "TransferConfig",
    "TransferKind",
    "TransferLinks",
    "TransferReadResult",
    "TransferSink",
    "TransferValidator",
    "build_transfer_links",
    "register_transfer_routes",
}


def test_all_lists_the_full_surface() -> None:
    assert set(transfer.__all__) == _EXPECTED


def test_every_name_is_importable() -> None:
    for name in _EXPECTED:
        assert hasattr(transfer, name), name


def test_names_alias_top_level_not_reimplemented() -> None:
    for name in _EXPECTED:
        assert getattr(transfer, name) is getattr(fastmcp_pvl_core, name), name


def test_path2_names_reexported_top_level() -> None:
    assert "build_transfer_links" in fastmcp_pvl_core.__all__
    assert "TransferLinks" in fastmcp_pvl_core.__all__


def test_internals_not_reexported() -> None:
    for name in ("TransferStore", "TransferToken", "make_transfer_handler"):
        assert not hasattr(transfer, name), name
        assert not hasattr(fastmcp_pvl_core, name), name
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_transfer_public_surface.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fastmcp_pvl_core.transfer'`.

- [ ] **Step 3: Create `src/fastmcp_pvl_core/transfer.py`**

```python
"""Public namespace for the transfer capability-link feature (ADR 0001).

Importing from ``fastmcp_pvl_core.transfer`` gathers the whole transfer surface
in one place and reads as an explicit "I am wiring the transfer feature" — most
pointedly :func:`build_transfer_links`, the path-2 seam a downstream uses to
build its own transfer tool. Every name here is also available from the
top-level ``fastmcp_pvl_core`` package; this module is a cohesive re-export, not
a second implementation. The feature's mechanics stay in the internal
``_transfer`` package (relative imports, foldable).
"""

from __future__ import annotations

from ._transfer import (
    TransferConfig,
    TransferKind,
    TransferLinks,
    TransferReadResult,
    TransferSink,
    TransferValidator,
    build_transfer_links,
    register_transfer_routes,
)

__all__ = [
    "TransferConfig",
    "TransferKind",
    "TransferLinks",
    "TransferReadResult",
    "TransferSink",
    "TransferValidator",
    "build_transfer_links",
    "register_transfer_routes",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_transfer_public_surface.py -v`
Expected: PASS.

- [ ] **Step 5: Run lint + types**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/transfer.py tests/test_transfer_public_surface.py
git commit -m "feat(transfer): public fastmcp_pvl_core.transfer namespace module (#249)"
```

---

## Final verification (after both tasks)

- [ ] Run the whole suite matching CI's dependency state:

Run: `uv sync --all-extras && uv run pytest && uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] Grep to confirm internals stayed internal:

Run: `grep -rn "TransferStore\|TransferToken\|make_transfer_handler" src/fastmcp_pvl_core/transfer.py src/fastmcp_pvl_core/__init__.py`
Expected: no hits (those names appear only inside `_transfer/`).

- [ ] Run the `preflight-circus` skill over `origin/main..HEAD` before opening the PR.

## Self-review notes

- **Spec coverage:** `build_transfer_links` + `TransferLinks` (Task 1); path-1-on-path-2 refactor with one route/store path (Task 1, Steps 4–5); return-type widening (Task 1, Step 5); surfacing via `transfer.py` + top-level (Task 1 Steps 7–8, Task 2); internals stay unexported (Global Constraints + Task 2 `test_internals_not_reexported` + final grep); docstring rewrite (Task 1, Step 6); every listed test scenario mapped to a test.
- **Type consistency:** `mint_download` / `mint_upload` signatures and `dict[str, Any]` return are identical across plan, spec, and tests; `build_transfer_links` signature matches the spec verbatim; `TransferLinks(store, *, base_url, transfer_config)` construction matches its use in `build_transfer_links`.
- **No placeholders:** every code and test block is concrete; the two FastMCP/Starlette introspection facts the tests rely on were verified by running against the installed versions.
