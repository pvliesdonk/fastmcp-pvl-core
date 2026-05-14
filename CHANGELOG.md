# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [3.0.0] - UNRELEASED

### Removed

- **`register_file_exchange` no longer accepts the following kwargs.**
  Every one was either an override of a pvl-core shape decision or
  operator config that already had an env-var counterpart:
  - `artifact_store=` — pvl-core builds the store from
    `{PREFIX}_BASE_URL` + `{PREFIX}_FILE_EXCHANGE_TTL`. Tests
    inject via the private `_set_artifact_store_for_test` seam.
  - `transport=` — resolved from `{PREFIX}_TRANSPORT` (fallback
    `FASTMCP_TRANSPORT`, default `"stdio"`).
  - `download_tool_name=` and `fetch_tool_name=` — pvl-core's tool
    names (`create_download_link`, `fetch_file`) are the shared
    shape; downstream collisions resolve by downstream renaming
    the local tool.
  - `legacy_capability_shape=` — transitional shim from the v0.4
    amendments window (#76 / #77); the spec is back to v0.2.5 and
    the nested v0.4-style capability shape is the only shape
    advertised.

### Migration

Downstream consumers tracked per-repo:

- pvliesdonk/markdown-vault-mcp#492 — dep-pin bump only (this repo
  uses `register_file_exchange_upload`, not `register_file_exchange`).
- pvliesdonk/scholar-mcp#196 — drop `transport=` kwarg; ensure
  `SCHOLAR_MCP_TRANSPORT` env var is set by the CLI.
- pvliesdonk/image-generation-mcp#227 — drop `transport=` from
  `src/` and tests.
- pvliesdonk/reqeng-mcp#17 — drop `transport="auto"` (default; no
  behaviour change after migration).
- pvliesdonk/fastmcp-server-template#133 (child of #131) — drop
  `transport="auto"` from `server.py.jinja`.

### Notes

`register_file_exchange_upload` is intentionally untouched in this
release; #74 redoes it wholesale against the #71 spec evolution.
Its kwarg surface is audited at that point.

The framing principle that drives this change is documented
authoritatively in `README.md` `## Design principles` and `CLAUDE.md`
`## The framing principle`. See pvliesdonk/fastmcp-pvl-core#73 and
pvliesdonk/fastmcp-pvl-core#72 for context.

## Unreleased

### Added
- **MCP File Exchange (spec v0.2.5) end-to-end implementation.** Single
  `register_file_exchange(...)` call wires the spec-compliant
  `create_download_link` and `fetch_file` MCP tools, the
  `experimental.file_exchange` capability declaration, the artifact
  HTTP route, and the exchange-volume runtime. Producer-side
  `FileExchangeHandle.publish` accepts `bytes` / `pathlib.Path` /
  lazy callable. Everything env-gated via
  `{PREFIX}_FILE_EXCHANGE_ENABLED` (default true on HTTP transports,
  false on stdio); the `exchange` transfer method activates only when
  the deployer sets the unprefixed `MCP_EXCHANGE_DIR`.
- New public symbols: `FileRef`, `FileRefPreview`, `ExchangeURI`,
  `ExchangeURIError`, `FileExchangeCapability`,
  `register_file_exchange_capability`, `FileExchange`,
  `FileExchangeConfigError`, `ExchangeGroupMismatch`,
  `FileExchangeHandle`, `register_file_exchange`, `FetchContext`,
  `FetchResult`, `ConsumerSink`, `FILE_EXCHANGE_SPEC_VERSION`.
- `ArtifactStore` extensions (pure-additive): per-token TTL on
  `add()`, `base_url` / `route_path` on `__init__`,
  `build_url(token)`, `put_ephemeral(...)` convenience, `has_base_url`
  property, module-level `set_artifact_store` / `get_artifact_store`
  singleton accessor.
- Spec doc at `docs/specs/file-exchange.md` (v0.2.5 verbatim plus
  proposed v0.4.0 amendments).
- **`register_file_exchange_upload` — symmetric inbound mirror of
  `register_file_exchange`.** Mints one-time `POST` URLs via a
  registered `create_upload_link` tool; receiver callable handles
  domain-specific commit. Buffered (`receiver=`) or streaming
  (`stream_receiver=`) variants. Optional `pre_link_validator=` runs
  at link creation so invalid `target_id`s surface as in-band tool
  errors rather than after a wasted upload round-trip. Env-gated via
  `{PREFIX}_UPLOAD_ENABLED` / `{PREFIX}_UPLOAD_MAX_BYTES` /
  `{PREFIX}_UPLOAD_TTL`. Closes #64.
- New public symbols: `UploadRecord`, `UploadStore`, `UploadHandle`,
  `register_file_exchange_upload`, `get_upload_store`,
  `set_upload_store`.

### Changed
- **`httpx` is now a hard dependency** (was previously optional under
  the `remote-auth` extra). The `fetch_file` MCP tool's HTTP branch
  needs it. The `remote-auth` extra is retained for backwards
  compatibility but is now a no-op — `pip install fastmcp-pvl-core`
  pulls in `httpx` regardless. Downstream projects relying on
  `httpx` *not* being installed will now get it.
- `fastmcp` dependency floor moved from `>=3.0,<4` to `>=3.2.4,<4`
  (the file-exchange capability advertisement uses fastmcp's
  `Middleware.on_initialize` hook, available since 3.2.4).
- File Exchange capability `version` advertised by
  `register_file_exchange` bumps from `"0.2"` to `"0.4"` to reflect
  the v0.4.0 amendments draft (now including Amendments 10 and 11
  for direction tagging and inbound HTTP). The `transfer_methods.http`
  block now nests by direction (`download` and `upload` sub-keys)
  rather than the flat shape — `register_file_exchange` and
  `register_file_exchange_upload` cooperate via a shared capability
  builder so a server hosting both directions advertises a single
  merged capability.
- Internal: `ArtifactStore` and `TokenRecord` now live in
  `fastmcp_pvl_core._token_store` (alongside `UploadStore`,
  `UploadRecord`, and the new `_BaseTokenStore[T]` generic).
  `_artifacts.py` is a deprecation shim re-exporting the same names;
  slated for removal one minor version after this release.
