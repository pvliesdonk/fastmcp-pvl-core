# Capability Builder v0.3.0 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the file-exchange capability builder to emit the v0.3.0 `source`/`sink` `transfer_methods` shape, realign `SPEC_VERSION` to `"0.3"`, and delete the `legacy_capability_shape` override kwarg.

**Architecture:** `_FileExchangeCapabilityBuilder` accumulates per-role tool contributions and `build()`s a `FileExchangeCapability`. The rework replaces the single `_download_tool` field (which collapsed producer and consumer into one slot) with separate `source`/`sink` tracking, emits `http` and `http_upload` as separate top-level method keys, and fixes the `register_file_exchange` call site so a produce-and-consume server advertises both `http` roles. The builder API change spans the builder class and both `register_*` call sites, so it lands as one atomic task.

**Tech Stack:** Python 3.10–3.13, `uv`, `pytest`, `ruff`, `mypy`. Repo `/mnt/code/fastmcp-pvl-core`, branch `impl/capability-builder-v030-issue-86`.

**Design doc:** `docs/superpowers/specs/2026-05-15-capability-builder-v030-migration-design.md` (issue #86).

---

## File Structure

- `src/fastmcp_pvl_core/_file_exchange_protocol.py` — `SPEC_VERSION` constant; `_FileExchangeCapabilityBuilder` class. The builder gains the `source`/`sink` API and shape; loses the legacy branch.
- `src/fastmcp_pvl_core/file_exchange.py` — `_get_or_create_builder` (drops `legacy_capability_shape`); `register_file_exchange` capability call site (the `if/elif` → `if/if` dual-role fix); `register_file_exchange_upload` (drops the `legacy_capability_shape` kwarg, switches to the new builder method).
- `tests/test_file_exchange_capability_merge.py` — full rewrite to the v0.3.0 shape and new builder API.

No new files. No production behaviour changes beyond capability emission and the dual-role advertisement fix.

---

## Task 1: Migrate the capability builder, call sites, and version

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange_protocol.py:36` (SPEC_VERSION) and `:449-550` (`_FileExchangeCapabilityBuilder`)
- Modify: `src/fastmcp_pvl_core/file_exchange.py:136-214` (`_get_or_create_builder`), `:761-788` (`register_file_exchange` call site), `:1539-1789` (`register_file_exchange_upload`)
- Test: `tests/test_file_exchange_capability_merge.py` (full rewrite)

This is one atomic API change — the builder's method names change, so the builder class and both call sites must move together to keep `mypy`/tests green. Steps 3–7 leave the tree temporarily inconsistent; the gate is run at Step 8 and the commit at Step 9.

- [ ] **Step 1: Rewrite the capability-merge test file**

Replace the entire contents of `tests/test_file_exchange_capability_merge.py` with:

```python
"""Tests for capability-merge across the http / http_upload registrars.

Capability declarations use the v0.3.0 ``source``/``sink`` role-keyed
``transfer_methods`` shape (spec §"Transfer Methods").
"""

from __future__ import annotations

from fastmcp import FastMCP

from fastmcp_pvl_core._file_exchange_protocol import (
    _FileExchangeCapabilityBuilder,
)


def test_builder_http_source_only_emits_source_role() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_source(tool_name="create_download_link")
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["version"] == "0.3"
    assert d["transfer_methods"]["http"] == {
        "source": {"tool": "create_download_link"},
    }


def test_builder_http_sink_only_emits_sink_role() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_sink(tool_name="fetch_file")
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["transfer_methods"]["http"] == {"sink": {"tool": "fetch_file"}}


def test_builder_http_both_roles_emits_source_and_sink() -> None:
    """Dual-role guard: a producer-and-consumer server advertises both roles."""
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_source(tool_name="create_download_link")
    b.set_http_sink(tool_name="fetch_file")
    cap = b.build()
    assert cap is not None
    http = cap.to_capability_dict()["transfer_methods"]["http"]
    assert http == {
        "source": {"tool": "create_download_link"},
        "sink": {"tool": "fetch_file"},
    }


def test_builder_http_upload_sink_emits_under_own_method_key() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_upload_sink(
        tool_name="create_upload_link",
        max_bytes=10_000_000,
        max_ttl_seconds=300,
    )
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert "http" not in d["transfer_methods"]
    assert d["transfer_methods"]["http_upload"] == {
        "sink": {
            "tool": "create_upload_link",
            "max_bytes": 10_000_000,
            "max_ttl_seconds": 300,
        },
    }


def test_builder_http_upload_sink_includes_explicit_accepts() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_upload_sink(
        tool_name="create_upload_link",
        max_bytes=1000,
        max_ttl_seconds=300,
        accepts=("text/markdown", "application/octet-stream"),
    )
    cap = b.build()
    assert cap is not None
    sink = cap.to_capability_dict()["transfer_methods"]["http_upload"]["sink"]
    assert sink["accepts"] == ["text/markdown", "application/octet-stream"]


def test_builder_http_and_http_upload_are_separate_top_level_keys() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_source(tool_name="create_download_link")
    b.set_http_upload_sink(
        tool_name="create_upload_link",
        max_bytes=10,
        max_ttl_seconds=60,
    )
    cap = b.build()
    assert cap is not None
    tm = cap.to_capability_dict()["transfer_methods"]
    assert tm["http"] == {"source": {"tool": "create_download_link"}}
    assert tm["http_upload"]["sink"]["tool"] == "create_upload_link"


def test_builder_with_no_method_returns_none() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    assert b.build() is None


def test_builder_set_exchange_false_drops_exchange_method() -> None:
    """``set_exchange(False)`` is the explicit no-op path (toggle off after on)."""
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_exchange(True)
    b.set_exchange(False)
    # No http/http_upload either, so the builder produces nothing.
    assert b.build() is None
    # With a download tool added, exchange must be absent even though
    # set_exchange was called once with True.
    b.set_http_source(tool_name="create_download_link")
    cap = b.build()
    assert cap is not None
    assert "exchange" not in cap.to_capability_dict()["transfer_methods"]


def test_emit_capability_returns_none_when_no_builder_registered() -> None:
    """``_emit_capability`` is idempotent on a FastMCP that has no builder."""
    from fastmcp_pvl_core.file_exchange import _BUILDER_ATTR, _emit_capability

    mcp = FastMCP(name="no-builder")
    assert getattr(mcp, _BUILDER_ATTR, None) is None
    assert _emit_capability(mcp) is None


def test_builder_is_attached_per_instance_not_module_level() -> None:
    """Capability builders live on the FastMCP instance, not in a global dict."""
    from fastmcp_pvl_core.file_exchange import _BUILDER_ATTR, _get_or_create_builder

    mcp_a = FastMCP(name="probe-a")
    _get_or_create_builder(mcp_a, namespace="ns-a")
    assert getattr(mcp_a, _BUILDER_ATTR, None) is not None
    assert mcp_a._pvl_file_exchange_builder.namespace == "ns-a"  # type: ignore[attr-defined]

    mcp_b = FastMCP(name="probe-b")
    assert getattr(mcp_b, _BUILDER_ATTR, None) is None
```

- [ ] **Step 2: Run the test file to verify it fails**

Run: `uv run pytest tests/test_file_exchange_capability_merge.py -q`
Expected: FAIL — `AttributeError: '_FileExchangeCapabilityBuilder' object has no attribute 'set_http_source'` (the new builder API does not exist yet).

- [ ] **Step 3: Bump `SPEC_VERSION`**

In `src/fastmcp_pvl_core/_file_exchange_protocol.py`, change line 36:

```python
SPEC_VERSION = "0.4"
```

to:

```python
SPEC_VERSION = "0.3"
```

- [ ] **Step 4: Replace the `_FileExchangeCapabilityBuilder` class**

In `src/fastmcp_pvl_core/_file_exchange_protocol.py`, replace the entire class (currently lines 449–550, from `@dataclass` / `class _FileExchangeCapabilityBuilder:` through the end of `_build_http_block`) with:

```python
@dataclass
class _FileExchangeCapabilityBuilder:
    """Accumulates per-role contributions into one capability dict.

    Both ``register_file_exchange`` (the ``http`` download method) and
    ``register_file_exchange_upload`` (the ``http_upload`` method) push
    their entries into a shared per-server instance keyed by the FastMCP
    instance; the capability dict is materialised by :meth:`build` once
    every registrar has run.

    Transfer methods are advertised in the v0.3.0 ``source``/``sink``
    role-keyed shape (spec §"Transfer Methods"): ``http`` and
    ``http_upload`` are separate top-level method keys, and each carries
    whichever of the ``source`` / ``sink`` roles the server fills.

    The builder is intentionally mutable (not frozen) — it accumulates
    state across multiple registrar calls. The capability it produces
    via :meth:`build` is the immutable :class:`FileExchangeCapability`.
    """

    namespace: str
    exchange_id: str | None = None
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    _exchange_present: bool = False
    _http_source_tool: str | None = None
    _http_sink_tool: str | None = None
    _http_upload_sink_tool: str | None = None
    _http_upload_max_bytes: int | None = None
    _http_upload_max_ttl_seconds: int | None = None
    _http_upload_accepts: tuple[str, ...] | None = None

    def set_exchange(self, present: bool = True) -> None:
        """Mark the ``exchange://`` shared-volume method as available."""
        self._exchange_present = present

    def set_http_source(self, *, tool_name: str) -> None:
        """Record the ``http`` producer (``source``) tool — mints download URLs."""
        self._http_source_tool = tool_name

    def set_http_sink(self, *, tool_name: str) -> None:
        """Record the ``http`` consumer (``sink``) tool — fetches from a URL."""
        self._http_sink_tool = tool_name

    def set_http_upload_sink(
        self,
        *,
        tool_name: str,
        max_bytes: int,
        max_ttl_seconds: int,
        accepts: tuple[str, ...] | None = None,
    ) -> None:
        """Record the ``http_upload`` receiver (``sink``) tool plus its
        admission metadata (the body-size and TTL ceilings and the
        accepted-``Content-Type`` filter)."""
        self._http_upload_sink_tool = tool_name
        self._http_upload_max_bytes = max_bytes
        self._http_upload_max_ttl_seconds = max_ttl_seconds
        self._http_upload_accepts = accepts

    def build(self) -> FileExchangeCapability | None:
        """Materialise the accumulated state into a FileExchangeCapability.

        Returns:
            ``None`` if no transfer method has been set (neither
            exchange nor an ``http`` role nor an ``http_upload`` role).
            Otherwise a frozen :class:`FileExchangeCapability` whose
            ``transfer_methods`` uses the v0.3.0 ``source``/``sink`` shape.
        """
        transfer_methods: dict[str, dict[str, Any]] = {}
        if self._exchange_present:
            transfer_methods["exchange"] = {}
        http_block = self._build_http_block()
        if http_block is not None:
            transfer_methods["http"] = http_block
        http_upload_block = self._build_http_upload_block()
        if http_upload_block is not None:
            transfer_methods["http_upload"] = http_upload_block
        if not transfer_methods:
            return None
        return FileExchangeCapability(
            namespace=self.namespace,
            exchange_id=self.exchange_id,
            produces=self.produces,
            consumes=self.consumes,
            transfer_methods=transfer_methods,
            version=SPEC_VERSION,
        )

    def _build_http_block(self) -> dict[str, Any] | None:
        """Build ``transfer_methods.http`` with ``source`` / ``sink`` roles.

        Returns ``None`` when the server fills neither ``http`` role.
        """
        block: dict[str, Any] = {}
        if self._http_source_tool is not None:
            block["source"] = {"tool": self._http_source_tool}
        if self._http_sink_tool is not None:
            block["sink"] = {"tool": self._http_sink_tool}
        return block or None

    def _build_http_upload_block(self) -> dict[str, Any] | None:
        """Build ``transfer_methods.http_upload`` with the receiver ``sink`` block.

        Returns ``None`` when no upload-receiver tool was registered.
        """
        if self._http_upload_sink_tool is None:
            return None
        sink: dict[str, Any] = {
            "tool": self._http_upload_sink_tool,
            "max_bytes": self._http_upload_max_bytes,
            "max_ttl_seconds": self._http_upload_max_ttl_seconds,
        }
        if self._http_upload_accepts is not None:
            sink["accepts"] = list(self._http_upload_accepts)
        return {"sink": sink}
```

- [ ] **Step 5: Drop `legacy_capability_shape` from `_get_or_create_builder`**

In `src/fastmcp_pvl_core/file_exchange.py`, the function `_get_or_create_builder` (lines 136–214) currently has a `legacy_capability_shape: bool = False` parameter, passes it to the builder constructor, and has a merge-mismatch warning block. Make three edits:

(5a) Delete the parameter — change the signature from:

```python
def _get_or_create_builder(
    mcp: FastMCP,
    *,
    namespace: str,
    exchange_id: str | None = None,
    produces: tuple[str, ...] = (),
    consumes: tuple[str, ...] = (),
    legacy_capability_shape: bool = False,
) -> _FileExchangeCapabilityBuilder:
```

to:

```python
def _get_or_create_builder(
    mcp: FastMCP,
    *,
    namespace: str,
    exchange_id: str | None = None,
    produces: tuple[str, ...] = (),
    consumes: tuple[str, ...] = (),
) -> _FileExchangeCapabilityBuilder:
```

(5b) Update the docstring paragraph about the merge contract — replace:

```
    Merge contract for the non-mergeable fields (``namespace``,
    ``legacy_capability_shape``): first-caller-wins. A subsequent
    caller passing a different value logs a WARNING and the original
    value is retained — ``namespace`` always, and
    ``legacy_capability_shape`` only when the second caller actively
    tries to enable it (the common ``False`` default does not warn).
    Likewise, ``exchange_id`` is set by the first caller that supplies
    a non-``None`` value and not overwritten thereafter.
```

with:

```
    Merge contract for the non-mergeable field ``namespace``:
    first-caller-wins. A subsequent caller passing a different
    namespace logs a WARNING and the original value is retained.
    Likewise, ``exchange_id`` is set by the first caller that supplies
    a non-``None`` value and not overwritten thereafter.
```

(5c) Remove the constructor argument — change:

```python
        builder = _FileExchangeCapabilityBuilder(
            namespace=namespace,
            exchange_id=exchange_id,
            produces=produces,
            consumes=consumes,
            legacy_capability_shape=legacy_capability_shape,
        )
```

to:

```python
        builder = _FileExchangeCapabilityBuilder(
            namespace=namespace,
            exchange_id=exchange_id,
            produces=produces,
            consumes=consumes,
        )
```

(5d) Delete the legacy mismatch-warning block entirely — remove these lines:

```python
        if (
            legacy_capability_shape != builder.legacy_capability_shape
            and legacy_capability_shape  # only warn if second caller TRIED to set True
        ):
            logger.warning(
                "_get_or_create_builder: legacy_capability_shape mismatch "
                "on FastMCP id=%d — first caller set %r, second tried "
                "%r; first-caller-wins.",
                id(mcp),
                builder.legacy_capability_shape,
                legacy_capability_shape,
            )
```

The `namespace` mismatch warning immediately above it and the `builder.exchange_id = ...` / produces / consumes merge lines below it stay unchanged.

- [ ] **Step 6: Fix the `register_file_exchange` capability call site (dual-role bug)**

In `src/fastmcp_pvl_core/file_exchange.py`, the capability block of `register_file_exchange` (around lines 761–788) currently uses an `if/elif` that hides the consumer tool on a produce-and-consume server. Replace:

```python
    # --- Capability declaration ---
    # Push contributions into the per-FastMCP builder so that an upload
    # registrar on the same ``mcp`` can merge into one capability dict.
    # The shape emitted is v0.4 (nested ``http.download`` / ``http.upload``);
    # legacy v0.2 flat shape is no longer wired here.
    capability: FileExchangeCapability | None = None
    if enabled:
        builder = _get_or_create_builder(
            mcp,
            namespace=namespace,
            exchange_id=exchange.exchange_id if exchange is not None else None,
            produces=tuple(produces) if produce else (),
            consumes=tuple(consumes) if consume else (),
        )
        if exchange is not None and (produce or consume):
            builder.set_exchange(True)
        if produce and store is not None and store.has_base_url:
            # Producer-side download: caller invokes ``create_download_link``
            # to mint a one-time URL.
            builder.set_download(tool_name=_DEFAULT_DOWNLOAD_TOOL)
        elif consume:
            # Consumer-side intake: the server's ``fetch_file`` tool pulls
            # bytes when given a URL. Re-uses the ``download`` slot in the
            # nested-http shape — both producer download-link minting and
            # consumer fetch are "download-direction" from the spec's
            # transfer-method perspective.
            builder.set_download(tool_name=_DEFAULT_FETCH_TOOL)
        capability = _emit_capability(mcp)
```

with:

```python
    # --- Capability declaration ---
    # Push contributions into the per-FastMCP builder so that an upload
    # registrar on the same ``mcp`` can merge into one capability dict.
    # The shape emitted is the v0.3.0 ``source``/``sink`` role-keyed form.
    capability: FileExchangeCapability | None = None
    if enabled:
        builder = _get_or_create_builder(
            mcp,
            namespace=namespace,
            exchange_id=exchange.exchange_id if exchange is not None else None,
            produces=tuple(produces) if produce else (),
            consumes=tuple(consumes) if consume else (),
        )
        if exchange is not None and (produce or consume):
            builder.set_exchange(True)
        # The two http roles are independent — a server that both produces
        # and consumes fills both. ``create_download_link`` is the producer
        # (``source``) tool; ``fetch_file`` is the consumer (``sink``) tool.
        if produce and store is not None and store.has_base_url:
            builder.set_http_source(tool_name=_DEFAULT_DOWNLOAD_TOOL)
        if consume:
            builder.set_http_sink(tool_name=_DEFAULT_FETCH_TOOL)
        capability = _emit_capability(mcp)
```

- [ ] **Step 7: Update `register_file_exchange_upload`**

In `src/fastmcp_pvl_core/file_exchange.py`, three edits to `register_file_exchange_upload`:

(7a) Remove the `legacy_capability_shape` parameter — delete this line from the signature (line 1554):

```python
    legacy_capability_shape: bool = False,
```

(7b) Remove the `legacy_capability_shape` entry from the docstring `Args` block. Delete exactly these five lines (around line 1633), leaving the `ttl_max:` entry above and the `Returns:` block below intact:

```
        legacy_capability_shape: Set to True during a migration window
            to advertise the v0.2 flat ``transfer_methods.http: {tool: ...}``
            shape instead of the v0.4 nested
            ``{download: ..., upload: ...}`` shape. The upload entry is
            dropped (with a logged warning) when legacy shape is selected.
```

(7c) Replace the builder wiring — change:

```python
    builder = _get_or_create_builder(
        mcp,
        namespace=namespace,
        legacy_capability_shape=legacy_capability_shape,
    )
    builder.set_upload(
        tool_name=upload_tool_name,
        max_bytes=int(max_bytes_default),
        max_ttl_seconds=int(ttl_max),
        accepts=accepts,
    )
```

to:

```python
    builder = _get_or_create_builder(mcp, namespace=namespace)
    builder.set_http_upload_sink(
        tool_name=upload_tool_name,
        max_bytes=int(max_bytes_default),
        max_ttl_seconds=int(ttl_max),
        accepts=accepts,
    )
```

- [ ] **Step 8: Run the capability-merge tests to verify they pass**

Run: `uv run pytest tests/test_file_exchange_capability_merge.py -q`
Expected: PASS — all tests in the rewritten file green.

- [ ] **Step 9: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange_protocol.py src/fastmcp_pvl_core/file_exchange.py tests/test_file_exchange_capability_merge.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): emit v0.3.0 source/sink capability shape (refs #86)

The capability builder advertised the pre-#83 nested
http: {download, upload} shape and version "0.4". Migrate it to the
v0.3.0 spec shape: http and http_upload are separate top-level
transfer_methods keys, each carrying source/sink role sub-objects.

_FileExchangeCapabilityBuilder gains set_http_source / set_http_sink
/ set_http_upload_sink (replacing set_download / set_upload) and
tracks the two http roles separately. register_file_exchange's
if/elif is now two independent ifs, so a produce-and-consume server
advertises both http.source and http.sink rather than hiding the
consumer tool. SPEC_VERSION 0.4 -> 0.3. The legacy_capability_shape
override kwarg is removed.

Refs #86.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Verification sweep and quality gate

**Files:** potentially any test under `tests/` that asserts the removed `http: {download, upload}` shape or `version: "0.4"`.

- [ ] **Step 1: Sync dependencies and run the full test suite**

Run:

```bash
uv sync --all-extras
uv run pytest -q
```

Expected: most tests pass. Any failure should be a test that constructs `_FileExchangeCapabilityBuilder` with the old API (`set_download` / `set_upload` / `legacy_capability_shape`), or asserts the old `transfer_methods.http` shape (`download` / `upload` sub-keys), or asserts `version == "0.4"`, or calls `register_file_exchange_upload(..., legacy_capability_shape=...)`.

- [ ] **Step 2: Fix any test asserting the removed shape**

For each failing test, apply the mechanical transformation:

- `builder.set_download(tool_name=X)` where `X` is the producer tool → `builder.set_http_source(tool_name=X)`; where `X` is the consumer/fetch tool → `builder.set_http_sink(tool_name=X)`.
- `builder.set_upload(tool_name=X, max_bytes=..., max_ttl_seconds=..., accepts=...)` → `builder.set_http_upload_sink(tool_name=X, max_bytes=..., max_ttl_seconds=..., accepts=...)`.
- An assertion `transfer_methods["http"] == {"download": {"tool": X}}` → `{"source": {"tool": X}}`.
- An assertion `transfer_methods["http"] == {"upload": {...}}` → assert `transfer_methods["http_upload"] == {"sink": {...}}`.
- An assertion `transfer_methods["http"]` has keys `{"download", "upload"}` → split: `transfer_methods["http"]` has `{"source"}` and/or `{"sink"}`, and `transfer_methods["http_upload"]` has `{"sink"}`.
- `version == "0.4"` → `version == "0.3"`.
- Any `register_file_exchange_upload(..., legacy_capability_shape=...)` call → drop the kwarg.
- Any test that *only* exists to exercise `legacy_capability_shape` behaviour (the v0.2 flat shape) → delete it; the v0.2 flat shape is no longer emitted.

Re-run `uv run pytest -q` until green.

- [ ] **Step 3: Run the quality gate**

Run:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

All three must pass clean. Fix any finding (e.g. an unused import left after deleting the legacy branch, or a `_DEFAULT_FETCH_TOOL` / `_DEFAULT_DOWNLOAD_TOOL` now-unused import — though both are still used by the Step 6 call site).

- [ ] **Step 4: Commit**

If Steps 2–3 changed any file:

```bash
git add -A
git commit -m "$(cat <<'EOF'
test(file-exchange): update tests for v0.3.0 capability shape (refs #86)

Sweep the suite for assertions on the removed http: {download, upload}
shape, the "0.4" version, and the legacy_capability_shape kwarg;
rewrite them against the v0.3.0 source/sink shape.

Refs #86.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If Steps 2–3 changed nothing (the full suite was already green after Task 1 and the gate was clean), skip the commit — there is nothing to record.

---

## Summary

Two tasks. Task 1 is the atomic builder + call-site + version migration with a fully rewritten capability-merge test file. Task 2 sweeps the rest of the suite and runs the quality gate.

## What changes

- `_FileExchangeCapabilityBuilder` — new `source`/`sink` API and v0.3.0 shape; `http` and `http_upload` separate keys.
- `register_file_exchange` — `if/elif` → `if/if`: produce-and-consume servers advertise both `http` roles.
- `SPEC_VERSION` — `"0.4"` → `"0.3"`.
- `legacy_capability_shape` — removed from the builder, `_get_or_create_builder`, and `register_file_exchange_upload`.

## What does NOT change

- The `create_upload_link` tool contract, the POST upload route, the download-serving route — out of scope (→ #74, #87).
- `register_file_exchange` / `register_file_exchange_upload` public behaviour apart from capability emission and the removed kwarg.
- Token store, receiver callbacks, transport resolution.

## Local review

`preflight-circus` (five core lenses + `pr-review-toolkit:code-reviewer`) runs on the cumulative diff; clean at the ≥80 confidence bar before opening the draft PR.

## Test plan

- [ ] `uv run pytest` green on the full suite.
- [ ] `uv run ruff format --check .` / `ruff check .` / `mypy src` clean.
- [ ] Capability output: `http`/`http_upload` use `source`/`sink`; `exchange` stays `{}`; `version` is `"0.3"`.
- [ ] A produce-and-consume server advertises both `http.source` and `http.sink` (the dual-role guard test).
- [ ] `grep -rn 'legacy_capability_shape\|set_download\|set_upload\b' src/` returns nothing.
- [ ] CI green.
- [ ] Bot review clean.

## Out of scope

- The `create_upload_link` tool contract and POST route status codes — #74.
- The sender-side `upload` tool — #85.
- The `http`-direction tool/route conformance audit — #87.

## Acceptance (from #86 / the design doc)

- [ ] `transfer_methods` uses the v0.3.0 `source`/`sink` shape for `http` and `http_upload`; `exchange` stays `{}`.
- [ ] A produce-and-consume server advertises both `http.source` and `http.sink`.
- [ ] `SPEC_VERSION` and the capability `version` field are `"0.3"`.
- [ ] `legacy_capability_shape` is gone from the codebase.
- [ ] Capability tests assert the v0.3.0 shape, including the dual-role guard.
