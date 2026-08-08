# Transfer path 2 + description augmentation — design

- **Date:** 2026-08-08
- **Status:** Approved (design; implementation split across two issues)
- **Relates to:** ADR 0001 (`docs/adr/0001-transfer-lift.md`), PR #245 (tool
  metadata, released 4.7.0), PR #247 (docstring correction),
  `fastmcp-server-template`#308/#309, `markdown-vault-mcp`#979

---

## 1. Problem

ADR 0001 delivered `register_transfer_routes` — a complete, opinionated
capability-link feature. It works well for the case it was built for: a server
that wants generic upload/download links and implements two domain hooks.

It does **not** account for a second, legitimate case. A downstream may need a
transfer tool that pvl-core's generic pair cannot express: a different name, a
domain-accurate description, domain-specific parameters. Two things went wrong
around that gap:

1. **Path 2 was undocumented and unsupported.** Everything needed to build a
   custom link tool (`TransferStore`, `make_transfer_handler`) lives under
   `_transfer/` and is not exported. `_transfer/__init__.py` states the store,
   handler, and route mechanics stay internal.
2. **A false claim propagated.** PR #245's `register.py` module docstring told
   implementers to build on "the exported primitives (`TransferStore`,
   `fetch_url`, `decode_base64_capped`)". `TransferStore` was never exported.
   `fastmcp-server-template`#309 took this at face value and wrote a wiring
   example importing `fastmcp_pvl_core._transfer.routes` and `._transfer.store`
   directly.

A third, smaller gap surfaced alongside: core's generic tool descriptions carry
no domain knowledge, and for **upload** that is materially unhelpful (§4).

### The ownership question, resolved

ADR §3 assigns tool names to pvl-core. That governs **core's own tools**. A
downstream domain tool that happens to mint a capability link is *downstream's
tool* — core has no standing over its name or description, because core does
not know the domain. The two paths are intentional and complementary:

- **Path 1** — core's ready-made generic pair. Easy, predictable, identical on
  every server. Downstream implements only the two hooks.
- **Path 2** — downstream's own domain tools, built on core's link mechanics,
  for when path 1 is insufficient or its description would mislead.

pvl-core owns path 1's shape completely. Path 2 is downstream's, and core's job
is to expose enough for it to be built *without trespass*.

---

## 2. Scope

**In scope**

- **Part A** — a public link-minting surface enabling path 2.
- **Part B** — append-only description augmentation for path 1's tools.

**Out of scope (deferred, tracked)**

- **Scenario 2 of path 2** — a downstream driving the token state machine
  itself (`claim` → serve → `complete`/`release`) behind a custom route or
  streaming response. This needs `TransferToken` and the six `Token*Error`
  types public, and hands downstream the correctness burden of fencing,
  release-on-failure, and grace-settle that ADR §6 argues core should own.
  Filed as a known-but-unsupported shape so the boundary is explicit rather
  than silent.
- Any change to `TransferSink` / `TransferValidator`.
- The sink-error status-code gap (image-gen references it as #233).

---

## 3. Part A — public link-minting surface

### 3.1 What path 2 actually needs

With the route mounted once and core's handler serving it, downstream **never**
calls `claim` / `release` / `complete` — the handler does. Path 2 needs exactly
one capability: *mint a token and get a URL back*.

That rules out exporting `TransferStore` directly. `mint` knows nothing about
URLs, so downstream would hardcode `/transfer/{token}` and re-implement (or
skip) the TTL clamp — leaking two shape decisions precisely because the store
is the wrong altitude.

### 3.2 The seam

Two new public names.

```python
def build_transfer_links(
    mcp: FastMCP,
    config: ServerConfig,
    transfer_config: TransferConfig,
    *,
    sink: TransferSink,
) -> TransferLinks:
    """Mount the /transfer route and return its link minter. Registers no tools."""
```

```python
class TransferLinks:
    async def mint_download(
        self, sink_handle: str, ttl_s: float | None = None
    ) -> dict[str, Any]: ...

    async def mint_upload(
        self, sink_handle: str, ttl_s: float | None = None
    ) -> dict[str, Any]: ...
```

Each returns `{"url": ..., "expires_in_s": ...}` — the same payload path 1's
tools return today.

**`sink_handle`, not `ref`, and no `validate` hook.** In path 2 downstream's own
tool *is* the validation site: it resolves its domain ref to an opaque handle
before minting, in whatever way its domain requires. `validate` exists in path 1
only because core's generic tool must delegate that step to someone.

**What stays core's:** route path and method set, status codes, token entropy,
TTL clamp, `base_url` guard, store construction and namespace, the state
machine. `TransferLinks` exposes none of them.

### 3.3 Path 1 is re-expressed on path 2

`register_transfer_routes` becomes a thin wrapper: it calls
`build_transfer_links`, then registers its two tools, each applying `validate`
and delegating to the minter. One route-mount and one store-construction code
path, shared by both paths, so they cannot drift.

Its return type changes from `None` to `TransferLinks` — non-breaking, and it is
what makes mixed mode work.

### 3.4 The three scenarios

| Scenario | Call | Result |
|---|---|---|
| Path 1 only | `register_transfer_routes(...)` | Route + generic pair. Ignore the return. |
| Mixed | `links = register_transfer_routes(...)` | Route + generic pair; register extra domain tools on `links`. |
| Pure path 2 | `links = build_transfer_links(...)` | Route only, no tools. Register your own on `links`. |

One route, one store, one minter in every scenario. The route is mounted by
whichever entry point is called, and calling both is not a supported
combination — `register_transfer_routes` *is* `build_transfer_links` plus tools.

### 3.5 Surfacing

A public `fastmcp_pvl_core/transfer.py` module re-exports `build_transfer_links`
and `TransferLinks` from `_transfer`. So:

```python
from fastmcp_pvl_core.transfer import build_transfer_links
```

reads as an explicit "I am building my own tools". `_transfer/` stays internal
with relative imports, so a fold-in remains a directory rename (CLAUDE.md
foldability rule). Both names are also re-exported from the top-level
`__init__` alongside the existing transfer surface, so path 1 users need learn
nothing new.

`TransferStore`, `TransferToken`, `make_transfer_handler`, and the six
`Token*Error` types remain internal.

---

## 4. Part B — description augmentation

### 4.1 Why upload needs it more than download

Download refs are **referenced**; upload refs are **authored**.

A model calling `create_download_link` passes something it has already seen — a
path from a search result, an `image://` id from a listing. Core's generic
description plus the server's own instructions are usually enough, and the
validator rejects a bad ref at mint time in one round trip.

`create_upload_link` is different in kind: the model must *invent* a
destination, and nothing it has seen states the folder conventions, the
extension allowlist, or the note-vs-attachment distinction. Generic text there
is not merely terse — it leaves the model to discover the rules by being
rejected.

Both kwargs are provided anyway: download has genuine edge cases worth a
sentence, an asymmetric API invites "why only one?", and an omitted note costs
nothing.

### 4.2 The hook

```python
register_transfer_routes(
    mcp, config.server, config.transfer,
    sink=..., validate=...,
    download_note: str | None = None,
    upload_note: str | None = None,
)
```

Append-only. Core's generic description is always present and identical on every
server; the note is appended after a blank line. A `None`, empty, or
whitespace-only value is treated as absent and the description is byte-identical
to today's. There is no mechanism to replace core's text — that remains a shape
decision.

### 4.3 Why this is a hook, not a forbidden override

CLAUDE.md forbids "pvl-core has a default but downstream can override". That
rule targets **contested defaults** — where core picks a behaviour a downstream
dislikes, and the escape hatch is a kwarg. Its purpose is to stop repeated
"our server would prefer a different X" requests from eroding the shared shape.

A domain note has no contested default. There is no sentence core could pick
that downstream would want to override, because *core has no candidate sentence
at all* — it does not know the domain. Absent is not a default core chose; it is
the honest state of core's knowledge. There is nothing to disagree with and
nothing to escape.

`build_transfer_links` takes no note kwargs: path 2 downstream writes its own
tool docstrings.

### 4.4 Implementation

`@mcp.tool(description=...)` overrides the function docstring (verified against
the installed fastmcp). Core composes `inspect.cleandoc(<base>) + "\n\n" + note`
and passes the result explicitly, keeping the docstring as the single source of
the base text rather than duplicating it in a constant.

---

## 5. Testing

**Part A**

- Pure path 2: `build_transfer_links` registers no tools; a minted link redeems
  end-to-end over ASGI (minter → route → handler → sink).
- Mixed mode: `register_transfer_routes` returns a `TransferLinks`; a
  path-1-minted link and a path-2-minted link both redeem against the same
  store and route.
- The `/transfer/{token}` route is mounted exactly once.
- `base_url` guard fires from `build_transfer_links` at call time.
- TTL clamp applies to minter calls: omitted → default; over max → clamped;
  in range → honoured; non-positive → rejected.
- Path 1's existing contract tests continue to pass unchanged (the wrapper
  must not alter observable behaviour).

**Part B**

- A note is appended to the correct tool, after core's base text.
- Core's base text is intact and unmodified in the augmented description.
- Both notes omitted → descriptions byte-identical to the pre-change output.
- Empty / whitespace-only note treated as absent.

---

## 6. Sequencing

Two issues, two PRs, one issue-cycle each (CLAUDE.md).

1. **Part B first** — smaller, independent of Part A, and it unblocks
   `markdown-vault-mcp`'s upload-description problem immediately.
2. **Part A second** — the larger surface change.
3. **Scenario-2 deferral issue** — filed to document the known-but-unsupported
   shape.
4. **PR #247 amended** — reduced to the ADR §5 correction note (the factual
   record that `TransferStore` was never exported). Its prescriptive
   "must not reach into `._transfer.store`" sentence is dropped: Part A makes
   it obsolete by providing the supported alternative.

Downstream follow-ups, out of scope here: `fastmcp-server-template`#309's
Path 2 example rewritten onto `build_transfer_links`;
`markdown-vault-mcp`#979 unblocked for both parts.
