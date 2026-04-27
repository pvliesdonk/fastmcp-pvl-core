# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
