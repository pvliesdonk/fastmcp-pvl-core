# File-Exchange #148 — top-level `register_file_exchange_*` helpers + Tasks integration + adoption docs

> **Status:** Design record for issue #148 (10/10 of EPIC #138). The
> implementation in the same PR (or stack of PRs) is the source of truth;
> this captures the shape agreed before implementation. This is **not** a
> wire spec — the spec lives upstream in `pvliesdonk/mcp-file-exchange-ext`.

## Goal

Ship the **opinionated tool-registration layer** on top of the data-plane
primitives that landed in #145 (download) and #146 (upload, slices 1–5).
Downstream servers want a one-call-per-role surface that registers an
MCP tool, wires the cross-cutting infrastructure (token store, routes,
Tasks capability declarations), and gets out of the way for the
domain-specific lookup logic that only the downstream can write.

## Scope (from #138's decomposition + #148's scope statement)

#148 owns the four `register_file_exchange_*` helpers, a single
`register_file_exchange` setup call, the §14 Tasks-metadata integration,
and the documentation set (`docs/file-exchange.md` + downstream adoption
guide + README section + CHANGELOG). It does **not** introduce new
data-plane primitives, new transports, or any change to the vendored
schema.

## The asymmetry that drives the design

The four roles in the file-exchange protocol have different shapes:

- **Provider** and **receiver** *respond to* a tool call — their response
  carries a `TransferHandle` / `IntakeTicket`. The tool's **arguments**
  are domain-specific (`report_id`, `case_number`, whatever), so
  pvl-core cannot generate the tool body for them. The downstream's
  lookup / authorization / validation must live inside the tool body.

- **Fetcher** and **sender** *make* a tool call against a peer and then
  consume the returned Handle / Ticket. Their input *is* the spec-defined
  Handle / Ticket wire dict, so pvl-core *can* generate the entire tool
  body — nothing is domain-specific except which `key` the sender uses
  to address its local source.

One shape doesn't fit all four. The design uses a hybrid:

- Provider and receiver: **decorator on a downstream-owned tool body.**
- Fetcher and sender: **fully-generated tool registration.**

This keeps each role at the right level of opinionatedness for its shape.

## Architecture

```
register_file_exchange(mcp, *, config, base_url, source=None, sink=None)
  ↓ (builds token_store, mounts routes, declares server-level Tasks capability)
FileExchangeContext(token_store, base_url, config, source, sink)
  ↓ (consumed by each per-tool helper)
register_file_exchange_provider(mcp, name, fxctx)          # decorator
register_file_exchange_receiver(mcp, name, fxctx)          # decorator
register_file_exchange_fetcher(mcp, name, fxctx)           # registers a generated tool
register_file_exchange_sender(mcp, name, fxctx)            # registers a generated tool
```

## `register_file_exchange` — setup call

```python
fxctx = register_file_exchange(
    mcp,
    config=config,
    base_url="https://my-server.example",
    source=my_source,    # required if any provider/sender helper will be used
    sink=my_sink,        # required if any receiver/fetcher helper will be used
)
```

### What it does

1. Builds the `CapabilityTokenStore` via the existing
   `build_capability_token_store(config)`. No new storage abstraction.
2. Calls `register_file_exchange_routes(mcp, token_store=..., source=source,
   sink=sink, config=config)` (the route mount we already shipped in
   slice 4 / PR #170). The existing precondition gate (`source`-or-`sink`,
   `sink`-needs-`config`) applies unchanged.
3. Declares the server-level MCP capability `tasks.requests.tools.call`
   so peers know this server accepts `tools/call` as a task submission.
4. Returns a small immutable `FileExchangeContext` dataclass
   `(token_store, base_url, config, source, sink)`. Per-tool helpers
   consume it; downstream just holds it in scope for subsequent
   registrations.

### Validation

The `source`-required-if-provider-or-sender-tool and
`sink`-required-if-receiver-or-fetcher-tool constraints can't be fully
checked at setup time (the per-tool helpers come later), so the
per-tool helpers raise `ValueError` at registration time if their
required hook is missing from the context. The setup call validates
only the cross-cutting precondition shared with
`register_file_exchange_routes`.

## `register_file_exchange_provider` — decorator

```python
@register_file_exchange_provider(mcp, "get_report", fxctx)
async def get_report(report_id: str) -> tuple[ArtifactMetadata, str]:
    meta = await lookup_meta(report_id)
    return meta, report_id
```

### Contract

- **Wraps** the user function. The wrapped tool's parameters are the
  inner function's parameters; nothing changes about the tool's
  signature from the MCP client's point of view.
- **Inner-function return shape**: `tuple[ArtifactMetadata, str]` —
  `(metadata, key)`. The `key` is the downstream's opaque server-side
  identifier that gets passed back to `source.open_artifact(key)` when
  the peer fetches.
- **After the inner function returns**, the wrapper calls
  `download_provider_mint(metadata, key, token_store=fxctx.token_store,
  base_url=fxctx.base_url, ttl=fxctx.config.file_exchange_token_ttl,
  single_use=True)` and returns the resulting `TransferHandle`.
- **Tool registration**: calls `mcp.tool(name=tool_name,
  annotations={"taskSupport": "optional"})` on the wrapped function.
  (`taskSupport` placement on FastMCP tools is implementation detail
  for the plan; the design names the responsibility.)
- **`fxctx.source is None`** raises `ValueError` at decoration time —
  the misconfiguration of registering a provider tool without a source
  hook fails loudly, not silently at first peer call.

### Return type of the registered tool

`TransferHandle` (Pydantic). FastMCP's normal tool-arg/output handling
serialises this to the wire dict shape for the MCP client.

### Knob source

`ttl` comes from `config.file_exchange_token_ttl`. A future caller that
needs per-tool TTL gets a kwarg added then; the design doesn't
pre-empt it.

## `register_file_exchange_receiver` — decorator

Symmetric to provider but mints an `IntakeTicket`.

```python
@register_file_exchange_receiver(mcp, "accept_report", fxctx)
async def accept_report(case_id: str) -> tuple[str, ArtifactConstraints | None]:
    return f"case-{case_id}-attachment", await lookup_constraints(case_id)
```

### Contract

- Same outer-wrapping pattern as provider; preserves the inner
  function's domain signature.
- **Inner-function return shape**: `tuple[str, ArtifactConstraints |
  None]` — `(artifact_id, expected)`. The two-element tuple even when
  `expected` is `None` matches the provider's shape (discoverability
  win; consistent for adoption).
- **After the inner function returns**, the wrapper calls
  `upload_receiver_mint(artifact_id,
  token_store=fxctx.token_store, base_url=fxctx.base_url,
  ttl=fxctx.config.file_exchange_token_ttl, expected=expected)` and
  returns the resulting `IntakeTicket`.
- **Tool registration**: same `taskSupport="optional"` injection as
  provider.
- **`fxctx.sink is None`** raises `ValueError` at decoration time.

### Return type of the registered tool

`IntakeTicket` (Pydantic).

## `register_file_exchange_fetcher` — generated tool

Not a decorator. The fetcher tool's input is the spec-defined
`TransferHandle`; there's nothing domain-specific for downstream to
write.

```python
register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)
```

### What the helper generates

```python
@mcp.tool(name="consume_transfer", annotations={"taskSupport": "optional"})
async def _consume_transfer(handle: TransferHandle) -> None:
    descriptor = select_source(handle)
    if descriptor is None:
        raise FileExchangeTransferError(
            TransferErrorCode.NO_USABLE_DESCRIPTOR,
            transport=None,
            detail="no usable source in handle",
        )
    if descriptor.transport == "filesystem":
        await filesystem_fetcher_consume(handle, descriptor, fxctx.sink,
                                         config=fxctx.config)
    elif descriptor.transport == "download":
        await download_fetcher_consume(handle, descriptor, fxctx.sink,
                                       config=fxctx.config)
    else:
        # UnknownTransportDescriptor reached selection — selection should have
        # filtered it; defensive guard for forward-compat wire payloads.
        raise FileExchangeTransferError(
            TransferErrorCode.NO_USABLE_DESCRIPTOR,
            transport=descriptor.transport,
            detail=f"unsupported transport {descriptor.transport!r}",
        )
```

### Notes

- Pydantic validates the incoming `handle` via FastMCP's normal tool-arg
  handling. The tool signature uses `TransferHandle`, not a generic
  dict — peers get schema discovery for free.
- `select_source` already filters out `UnknownTransportDescriptor` per
  #140's selection algorithm; the `else` branch is a defensive guard
  for future transports.
- Returns `None`. The deposit into `fxctx.sink` is the side effect; the
  wire response is just an acknowledgement.
- `fxctx.sink is None` raises `ValueError` at registration time.

## `register_file_exchange_sender` — generated tool

Symmetric to fetcher. Input is the spec-defined `IntakeTicket`; body is
generated.

```python
register_file_exchange_sender(mcp, "send_to_receiver", fxctx)
```

### What the helper generates

```python
@mcp.tool(name="send_to_receiver", annotations={"taskSupport": "optional"})
async def _send_to_receiver(ticket: IntakeTicket, key: str) -> None:
    descriptor = select_sink(ticket)
    if descriptor is None:
        raise FileExchangeTransferError(
            TransferErrorCode.NO_USABLE_DESCRIPTOR,
            transport=None,
            detail="no usable sink in ticket",
        )
    if descriptor.transport == "filesystem":
        await filesystem_sender_consume(descriptor, fxctx.source, key,
                                        config=fxctx.config)
    elif descriptor.transport == "upload":
        await upload_sender_consume(descriptor, fxctx.source, key,
                                    config=fxctx.config)
    else:
        raise FileExchangeTransferError(
            TransferErrorCode.NO_USABLE_DESCRIPTOR,
            transport=descriptor.transport,
            detail=f"unsupported transport {descriptor.transport!r}",
        )
```

### The second arg `key`

Unlike the fetcher (which deposits whatever bytes the peer chose to
ship), the sender has to choose what to send. That choice is
domain-specific. Making `key` the second tool argument keeps the design
simple: the caller (typically a downstream tool body that received the
ticket from a peer) passes the ticket plus the local key.

A decorator variant for senders who want richer key-derivation from
domain inputs is **deferred** under YAGNI. The simple form composes —
downstream can write their own thin wrapper if they need it.

`fxctx.source is None` raises `ValueError` at registration time.
Returns `None`.

## Tasks integration (§14)

Two distinct declarations:

**Per-tool**: every tool registered by any of the four role helpers gets
`taskSupport="optional"` injected on the FastMCP tool registration.
`"optional"` matches §14's recommendation for tools that *might* be
long-running (file transfers can be). Implementation detail for the
plan: the exact kwarg name on `mcp.tool` (likely `annotations`).

**Server-level**: `register_file_exchange` declares the
`tasks.requests.tools.call` capability on the MCP server immediately
after the route mount. Tells peers this server accepts `tools/call` as a
task submission.

**No new task-execution logic in pvl-core.** Issue #148's "Tasks
composition" doesn't mean writing a scheduler — FastMCP's existing
Tasks infrastructure handles execution. The role helpers only flip the
metadata.

## Documentation

Four artifacts, each focused on what its audience needs.

### README section (`README.md` — `## File-exchange extension`)

~15-line section: what the extension is, one-liner per role, one
minimal `register_file_exchange_provider` snippet, links to
`docs/file-exchange.md` and the upstream spec at
`pvliesdonk/mcp-file-exchange-ext`. The README is the front door, not
the manual.

### `docs/file-exchange.md` — pvl-core's implementation notes

The pvl-core-specific story. **Not** a wire spec (the wire spec lives
upstream and is vendored as `_schema/file-exchange.json`). Sections:

- Architecture sketch (the four roles, who calls whom, where the routes
  live, the token-store layer).
- Setup walkthrough (`register_file_exchange` + the four helpers, in
  the order a downstream would call them).
- Operator-knob reference (the `ServerConfig.file_exchange_*` fields and
  what they bound).
- Error model (`FileExchangeTransferError` + the §13 code envelope).
- Pointers to the failure-mode matrix + restructure spec under
  `docs/superpowers/specs/`.
- Explicit non-scope: this is implementation; the wire spec is
  authoritative upstream.

### Downstream adoption guide — `docs/file-exchange-adoption.md`

One minimal *worked example* per role, in the order a downstream would
adopt them. Each example is ~20–30 lines of code with running prose:

1. Provider — a server that offers reports.
2. Fetcher — a server that imports reports from a peer.
3. Receiver — a server that accepts uploads.
4. Sender — a server that sends to a peer.

Each example uses the simplest plausible `ArtifactSource` /
`ArtifactSink` (in-memory or single-file) so the example doesn't drag
in a storage backend.

### `CHANGELOG.md` entry

Standard unreleased-section entry naming the four new helpers + the
setup call + the new docs.

## Public surface added

Re-exported via `file_exchange.py` and the subpackage `__init__.py`
(both `__all__`s updated, alphabetical):

- `register_file_exchange` — setup call.
- `register_file_exchange_provider` — provider decorator.
- `register_file_exchange_receiver` — receiver decorator.
- `register_file_exchange_fetcher` — fetcher tool-registration helper.
- `register_file_exchange_sender` — sender tool-registration helper.
- `FileExchangeContext` — opaque dataclass returned by the setup call.

`register_file_exchange_routes` stays exported (slice 4); the setup
call composes it rather than replacing it.

## Testing strategy

- Unit tests for each helper covering: (a) the happy path produces the
  expected `TransferHandle` / `IntakeTicket` / sink-deposit / source-read;
  (b) the `fxctx.source`-or-`fxctx.sink`-missing path raises
  `ValueError` at registration time, not at first peer call;
  (c) the `taskSupport` metadata reaches the MCP tool descriptor;
  (d) the server-level `tasks.requests.tools.call` capability is set
  after `register_file_exchange`.
- End-to-end tests for the worked examples in the adoption guide.
  Reuses the e2e patterns from slice 5's `test_upload_e2e.py` and the
  download e2e suite.
- No new matrix needed — #148 is composition over the data-plane
  primitives, not new failure-mode surface.

## Error handling

- All five `register_*` calls raise `ValueError` at *registration*
  time for missing hook / config dependencies. Failures at *call*
  time (token-store unreachable, hook raised, etc.) propagate the
  existing `FileExchangeTransferError` codes the data-plane primitives
  already produce; the role helpers do not re-wrap or swallow.
- The fetcher/sender's generated `else: raise FileExchangeTransferError`
  for an unknown transport is a defensive guard — `select_source` /
  `select_sink` already filter `UnknownTransportDescriptor`.

## #148 ↔ #149 boundary

#148 ships pvl-core's helpers + docs. **#149** (separate issue) tracks
downstream rollout — adopting the helpers in the
`pvliesdonk/{markdown-vault-mcp,scholar-mcp,image-generation-mcp,
reqeng-mcp,fastmcp-server-template}` consumers. #149 is explicitly out
of scope here.

## References

- EPIC #138 (adopt mcp-file-exchange-ext v0.1); this is 10/10. Depends
  on every prior child issue: #139 wire format, #140 selection,
  #141 paths, #142 hooks, #143 filesystem, #144 token store,
  #145 download data plane, #146 upload data plane, #147 SSRF guard.
- `docs/superpowers/specs/2026-05-27-file-exchange-146-failure-modes.md`
  — failure-mode matrix that drove the upload restructure; informs
  the testing strategy.
- `docs/superpowers/specs/2026-05-28-content-digest-restructure.md` —
  records the root-cause-driven `_content_digest.py` extraction during
  slice 3.
- Wire spec (`mcp-file-exchange-ext`, pinned commit `5f50a4e…`):
  §11 (carrying references through `tools/call`), §14 (Tasks
  integration), §16 (conformance). This document is **not** a wire
  spec; it documents pvl-core's implementation of the helpers.
- `CLAUDE.md` — shape decisions live in pvl-core; hooks
  (`ArtifactSource`/`ArtifactSink`) stay mechanism-agnostic;
  implementation choices belong in `docs/file-exchange.md`, not in
  `docs/specs/`.
