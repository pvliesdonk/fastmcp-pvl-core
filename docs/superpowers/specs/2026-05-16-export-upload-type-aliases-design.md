# Design: export the upload-direction type aliases (issue #67)

**Status**: approved (brainstorm 2026-05-16)
**Issue**: [pvliesdonk/fastmcp-pvl-core#67](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/67)

## Problem

Three type aliases used by receiver-author code are not exported from the
`fastmcp_pvl_core` package root, so a downstream author writing typed code
must import them from private submodules:

- `BufferedReceiver` — defined in `_file_exchange_runtime.py`.
- `StreamReceiver` — defined in `_file_exchange_runtime.py`.
- `PreLinkValidator` — defined in `file_exchange.py`.

By contrast the download-direction aliases (`ConsumerSink`,
`FetchContext`, `FetchResult`) and the validator-adjacent
`PreLinkValidator` are already routed through `file_exchange.py` and, for
the download trio, re-exported from the package root. The receiver trio is
the inconsistent gap. This subsumes the closed duplicate #99, which
covered only `PreLinkValidator`.

## The design

Three additive changes. No behaviour changes, no change to the aliases'
definitions.

### 1. `src/fastmcp_pvl_core/file_exchange.py`

Add `"BufferedReceiver"` and `"StreamReceiver"` to the module's `__all__`
list, alphabetically. Both names are already imported at the top of
`file_exchange.py` for use in type annotations
(`register_file_exchange_upload`'s `receiver` / `stream_receiver`
parameters), so this is a pure `__all__` addition — no new import.

This makes `file_exchange.py` the complete public facade for every
file-exchange type, consistent with how `ConsumerSink`, `FetchContext`,
`FetchResult`, and `PreLinkValidator` already route through it.
`PreLinkValidator` is already in `file_exchange.py`'s `__all__` and needs
no change here.

### 2. `src/fastmcp_pvl_core/__init__.py`

Extend the existing `from fastmcp_pvl_core.file_exchange import (...)`
block with `BufferedReceiver`, `PreLinkValidator`, `StreamReceiver`,
alphabetically placed alongside the existing names (`ByteSourceResolver`,
`ConsumerSink`, …). Add the same three names to the package-level
`__all__` list, alphabetically.

All three are imported from `fastmcp_pvl_core.file_exchange` — the single
public facade — rather than `__init__.py` reaching into
`_file_exchange_runtime.py` directly.

### 3. `CHANGELOG.md`

Under `## [3.0.0] - UNRELEASED`, add an `### Added` subsection, placed
before the existing `### Removed` subsection (Keep-a-Changelog orders
Added → Changed → Deprecated → Removed → Fixed → Security). The entry
notes that `BufferedReceiver`, `StreamReceiver`, and `PreLinkValidator`
are now importable from the `fastmcp_pvl_core` package root, so
receiver-author code no longer needs to import them from private
submodules.

## Testing

A minimal regression test asserting the three names are importable from
`fastmcp_pvl_core` and present in `fastmcp_pvl_core.__all__`. The
implementation plan will check for an existing export / `__all__`
consistency test and extend it rather than add a new file if one exists;
otherwise it adds a small focused test.

## Out of scope

- Any change to the aliases' definitions or to `_file_exchange_runtime.py`.
- The Protocol-vs-`Callable` reconsideration for these seams — tracked
  separately in #60.
- Any change to the download-direction exports, which are already correct.

## Acceptance (from #67)

- [ ] `BufferedReceiver`, `StreamReceiver`, `PreLinkValidator` are
  importable from `fastmcp_pvl_core` and listed in its `__all__`.
- [ ] `BufferedReceiver` and `StreamReceiver` are listed in
  `file_exchange.py`'s `__all__`.
- [ ] `CHANGELOG.md` records the addition under `## [3.0.0]`.
- [ ] The full suite, `ruff`, and `mypy` stay clean.
