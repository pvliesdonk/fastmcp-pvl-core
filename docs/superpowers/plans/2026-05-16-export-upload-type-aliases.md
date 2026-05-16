# Export Upload-Direction Type Aliases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export `BufferedReceiver`, `StreamReceiver`, and `PreLinkValidator` from the `fastmcp_pvl_core` package root (issue #67).

**Architecture:** Additive export-hygiene change. `file_exchange.py` becomes the complete public facade for file-exchange types (`BufferedReceiver`/`StreamReceiver` join its `__all__`); `__init__.py` re-exports all three from that facade. A new test guards the export. No behaviour changes, no change to the aliases' definitions.

**Tech Stack:** Python, pytest.

---

## Notes for the implementer

- Genuine red-then-green TDD: the three names are *not* currently exported, so the new test fails with `ImportError` before the export changes and passes after.
- The aliases already exist — `BufferedReceiver`/`StreamReceiver` in `_file_exchange_runtime.py` (and are already imported into `file_exchange.py` for annotations), `PreLinkValidator` in `file_exchange.py`. This plan only changes `__all__` lists, the `__init__.py` import block, the CHANGELOG, and adds one test.

---

## Task 1: Export the type aliases (TDD)

**Files:**
- Create: `tests/test_package_exports.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py` (the `__all__` list)
- Modify: `src/fastmcp_pvl_core/__init__.py` (the `from fastmcp_pvl_core.file_exchange import (...)` block and the `__all__` list)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write the failing test**

Create `tests/test_package_exports.py`:

```python
"""The fastmcp_pvl_core package root exports its documented public API."""

from __future__ import annotations


def test_upload_direction_type_aliases_are_exported() -> None:
    """#67: receiver-author type aliases are importable from the package root.

    ``BufferedReceiver`` / ``StreamReceiver`` (receiver callbacks) and
    ``PreLinkValidator`` (the ``create_upload_link`` validation hook) are
    part of the upload-direction public API. Before #67 they were only
    importable from private submodules, unlike the download-direction
    aliases ``ConsumerSink`` / ``FetchContext`` / ``FetchResult``.
    """
    import fastmcp_pvl_core
    from fastmcp_pvl_core import file_exchange
    from fastmcp_pvl_core import (
        BufferedReceiver,
        PreLinkValidator,
        StreamReceiver,
    )

    # Listed in the package-root __all__.
    for name in ("BufferedReceiver", "StreamReceiver", "PreLinkValidator"):
        assert name in fastmcp_pvl_core.__all__, (
            f"{name} missing from fastmcp_pvl_core.__all__"
        )

    # Listed in the file_exchange facade's __all__.
    for name in ("BufferedReceiver", "StreamReceiver", "PreLinkValidator"):
        assert name in file_exchange.__all__, (
            f"{name} missing from file_exchange.__all__"
        )

    # The package-root names are the same objects as the facade's.
    assert BufferedReceiver is file_exchange.BufferedReceiver
    assert StreamReceiver is file_exchange.StreamReceiver
    assert PreLinkValidator is file_exchange.PreLinkValidator
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_package_exports.py -v`
Expected: FAIL — `ImportError: cannot import name 'BufferedReceiver' from 'fastmcp_pvl_core'` (the names are not yet exported).

- [ ] **Step 3: Add `BufferedReceiver` / `StreamReceiver` to `file_exchange.py`'s `__all__`**

In `src/fastmcp_pvl_core/file_exchange.py`, the `__all__` list currently reads:

```python
__all__ = [
    "ByteSourceResolver",
    "ConsumerSink",
    "FetchContext",
    "FetchResult",
    "FileExchangeHandle",
    "PreLinkValidator",
    "ResolvedSource",
    "UploadHandle",
    "UploadSenderHandle",
    "register_file_exchange",
    "register_file_exchange_upload",
    "register_file_exchange_upload_sender",
]
```

Replace it with (adds `"BufferedReceiver"` and `"StreamReceiver"`, alphabetically):

```python
__all__ = [
    "BufferedReceiver",
    "ByteSourceResolver",
    "ConsumerSink",
    "FetchContext",
    "FetchResult",
    "FileExchangeHandle",
    "PreLinkValidator",
    "ResolvedSource",
    "StreamReceiver",
    "UploadHandle",
    "UploadSenderHandle",
    "register_file_exchange",
    "register_file_exchange_upload",
    "register_file_exchange_upload_sender",
]
```

`BufferedReceiver` and `StreamReceiver` are already imported near the top of `file_exchange.py` (from `_file_exchange_runtime`) for use in `register_file_exchange_upload`'s type annotations — no new import is needed, only the `__all__` entries.

- [ ] **Step 4: Add the three aliases to `__init__.py`**

In `src/fastmcp_pvl_core/__init__.py`, the import block currently reads:

```python
from fastmcp_pvl_core.file_exchange import (
    ByteSourceResolver,
    ConsumerSink,
    FetchContext,
    FetchResult,
    FileExchangeHandle,
    ResolvedSource,
    UploadHandle,
    UploadSenderHandle,
    register_file_exchange,
    register_file_exchange_upload,
    register_file_exchange_upload_sender,
)
```

Replace it with (adds `BufferedReceiver`, `PreLinkValidator`, `StreamReceiver`):

```python
from fastmcp_pvl_core.file_exchange import (
    BufferedReceiver,
    ByteSourceResolver,
    ConsumerSink,
    FetchContext,
    FetchResult,
    FileExchangeHandle,
    PreLinkValidator,
    ResolvedSource,
    StreamReceiver,
    UploadHandle,
    UploadSenderHandle,
    register_file_exchange,
    register_file_exchange_upload,
    register_file_exchange_upload_sender,
)
```

Then add the three names to the package `__all__` list, alphabetically. The list currently contains, in order, `... "AuthzDenied", "ByteSourceResolver", ...`; `... "IconSpec", "ResolvedSource", ...`; `... "ServerConfig", "TokenRecord", ...`. Insert:

- `"BufferedReceiver",` between `"AuthzDenied",` and `"ByteSourceResolver",`
- `"PreLinkValidator",` between `"IconSpec",` and `"ResolvedSource",`
- `"StreamReceiver",` between `"ServerConfig",` and `"TokenRecord",`

So those three regions become:

```python
    "AuthzDenied",
    "BufferedReceiver",
    "ByteSourceResolver",
```

```python
    "IconSpec",
    "PreLinkValidator",
    "ResolvedSource",
```

```python
    "ServerConfig",
    "StreamReceiver",
    "TokenRecord",
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_package_exports.py -v`
Expected: PASS.

- [ ] **Step 6: Update `CHANGELOG.md`**

In `CHANGELOG.md`, the `[3.0.0]` section currently begins:

```markdown
## [3.0.0] - UNRELEASED

### Removed
```

Insert an `### Added` subsection before `### Removed` (Keep-a-Changelog
orders Added → Changed → Deprecated → Removed → Fixed → Security):

```markdown
## [3.0.0] - UNRELEASED

### Added

- **Upload-direction type aliases are now exported from the package
  root.** `BufferedReceiver`, `StreamReceiver`, and `PreLinkValidator`
  are importable directly from `fastmcp_pvl_core`; receiver-author code
  no longer needs to import them from private submodules. This brings
  the upload-direction aliases in line with the already-exported
  download-direction aliases (`ConsumerSink`, `FetchContext`,
  `FetchResult`). (#67)

### Removed
```

- [ ] **Step 7: Commit**

```bash
git add tests/test_package_exports.py src/fastmcp_pvl_core/file_exchange.py src/fastmcp_pvl_core/__init__.py CHANGELOG.md
git commit -m "feat(file-exchange): export upload-direction type aliases from package root (refs #67)"
```

---

## Task 2: Full quality gate

**Files:** none (verification only).

- [ ] **Step 1: Sync dependencies to match CI**

Run: `uv sync --all-extras`
Expected: completes without error.

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass (the prior baseline plus the 1 new test).

- [ ] **Step 3: Formatting and lint**

Run: `uv run ruff format --check .`
Expected: all files already formatted.

Run: `uv run ruff check .`
Expected: `All checks passed!` — in particular no `F401` unused-import or `RUF022` unsorted-`__all__` complaint.

- [ ] **Step 4: Type check**

Run: `uv run mypy src`
Expected: `Success: no issues found`.
