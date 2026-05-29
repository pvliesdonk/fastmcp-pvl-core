# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [3.0.0] - UNRELEASED

### Added

- **Unified key-value storage factory** (`build_kv_store`). One
  `<PREFIX>_KV_STORE_URL` selects a backend for every pvl-core
  subsystem that needs persistent state — event store today, OAuth
  proxy client storage and the cleanroom file-exchange token store
  going forward. URL-scheme dispatch covers `memory://`,
  `file:///path`, `redis://...`, `dynamodb://<table>?region=...`,
  and `mongodb://...`; the redis / dynamodb / mongodb backends are
  optional extras (`fastmcp-pvl-core[redis]` etc.) with lazy
  imports so memory/file deployments do not pull in client
  libraries. Returned stores are namespaced with
  `PrefixCollectionsWrapper` so subsystems sharing a backend cannot
  collide on collection names. (#121)
- `ServerConfig.kv_store_url` field, loaded from
  `<PREFIX>_KV_STORE_URL`.
- File-exchange umbrella helpers (#148): `register_file_exchange` setup
  call plus `register_file_exchange_provider` / `_receiver` / `_fetcher`
  / `_sender` per-role helpers. Provider and receiver are decorators on
  downstream-owned tool bodies; fetcher and sender are fully-generated
  tool registrations. Every helper-registered tool carries the §14
  `taskSupport="optional"` annotation; the setup call declares the
  server-level `tasks.requests.tools.call` capability.
  `FileExchangeContext` carries an optional `volume_map: VolumeMap`
  so the filesystem transport is opt-in via the setup call.
- `docs/file-exchange.md` — pvl-core's implementation notes for the
  file-exchange extension.
- `docs/file-exchange-adoption.md` — one worked example per role.

### Changed

- `build_event_store` now delegates backend selection to
  `build_kv_store` (`namespace="events"`). The legacy
  `EVENT_STORE_URL` is honoured as a fallback when `KV_STORE_URL`
  is unset and logs a one-shot warning pointing operators at the
  new variable. The new default directory is `/data/state` (the
  previous per-subsystem default was `/data/state/events`); on-disk
  layout for file-backed event stores changes accordingly — set
  `EVENT_STORE_URL=file:///data/state/events` explicitly during the
  migration window to preserve the previous path, or accept the new
  layout (event entries TTL at 1h so functional impact is bounded).

### Removed

- **The file-exchange spec and implementation have been removed in
  full.** Gone: `docs/specs/file-exchange.md`; the `file_exchange`,
  `_file_exchange_protocol`, `_file_exchange_runtime`, `_token_store`,
  and `_artifacts` modules; and every symbol they exported from the
  package root — `register_file_exchange`,
  `register_file_exchange_upload`,
  `register_file_exchange_upload_sender`,
  `register_file_exchange_capability`, `FileExchange`,
  `FileExchangeHandle`, `FileExchangeCapability`,
  `FileExchangeConfigError`, `ExchangeGroupMismatch`, `ExchangeURI`,
  `ExchangeURIError`, `FileRef`, `FileRefPreview`,
  `FILE_EXCHANGE_SPEC_VERSION`, `ArtifactStore`, `UploadStore`,
  `TokenRecord`, `UploadRecord`, `UploadHandle`, `UploadSenderHandle`,
  `SourceHook`, `SinkHook`, `SinkContext`, `ResolvedSource`,
  `PreLinkValidator`, `get_artifact_store`, `set_artifact_store`,
  `get_upload_store`, `set_upload_store`. Repeated remediation passes
  could not clear the spec and implementation of LLM contamination;
  the protocol will be reintroduced through a cleanroom spec rewrite.
  (#116)

## [2.1.0] - 2026-05-10

> Note: this section accumulated entries across the 1.x → 2.x release
> window and was never split per-release at tag time (PSR auto-bumps
> the version tag but doesn't move CHANGELOG entries). Treat as the
> cumulative `2.x` history; new entries go into the unreleased
> section above.

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
