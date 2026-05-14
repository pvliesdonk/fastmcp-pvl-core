# file-exchange hook audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the framing principle from issue #73 to `register_file_exchange`'s kwarg surface: remove every override kwarg (`artifact_store`, `transport`, `download_tool_name`, `fetch_tool_name`, `legacy_capability_shape`), keep the five domain hooks, surface operator config through env vars only, and tighten the classification-test wording in `README.md`/`CLAUDE.md` to match the corrected framing the user established during brainstorm.

**Architecture:** Single-PR breaking change to the download helper only. Each removed kwarg is its own task with a TDD-style negative test ("kwarg gone → `TypeError`"). Existing tests that pass removed kwargs migrate to env-var setups (or to a new private `_set_artifact_store_for_test` seam). `register_file_exchange_upload` is deliberately untouched per #72's Notes — #74 redoes it wholesale. Version bumps to 3.0.0 via a `feat!:` or `refactor!:`-prefixed commit so `python-semantic-release` cuts the breaking release automatically.

**Tech Stack:** Python 3.10+; `pytest` + `pytest-asyncio`; `monkeypatch` for env-var fixtures; `uv` toolchain (sync/run); `ruff` (format + lint); `mypy` (strict); semantic-release.

**Spec:** [`docs/superpowers/specs/2026-05-14-file-exchange-hook-audit-design.md`](../specs/2026-05-14-file-exchange-hook-audit-design.md) (commit `3976f7d` on this branch).

---

## Task 0: Downstream survey + file migration issues

**Goal:** Establish ground truth for what breaks downstream so the pvl-core PR can link to migration trackers per the "all-at-once + downstream issues filed pre-merge" choice.

**Files:**
- Modify: `docs/superpowers/specs/2026-05-14-file-exchange-hook-audit-design.md` (append "Survey result" section)
- No code changes.

- [ ] **Step 1: Survey each named downstream consumer**

For each repo, run:

```bash
gh search code --repo pvliesdonk/markdown-vault-mcp 'register_file_exchange('
gh search code --repo pvliesdonk/scholar-mcp 'register_file_exchange('
gh search code --repo pvliesdonk/image-generation-mcp 'register_file_exchange('
gh search code --repo pvliesdonk/reqeng-mcp 'register_file_exchange('
gh search code --repo pvliesdonk/fastmcp-server-template 'register_file_exchange('
```

For each hit, fetch the file (`gh api /repos/<owner>/<repo>/contents/<path>` and base64-decode, or `gh browse` URL) and note:
- Whether the call passes any of the five removed kwargs (`artifact_store=`, `transport=`, `download_tool_name=`, `fetch_tool_name=`, `legacy_capability_shape=`).
- For `markdown-vault-mcp` specifically: confirm or rule out the documented `create_download_link(path)` tool collision.

- [ ] **Step 2: File a migration issue per affected consumer**

For each consumer that passes at least one removed kwarg OR has a known tool collision, file:

```bash
gh issue create --repo pvliesdonk/<consumer> \
    --title "migrate to fastmcp-pvl-core 3.0.0 (file-exchange kwarg removals)" \
    --body "$(cat <<'EOF'
## Why

`fastmcp-pvl-core` 3.0.0 removes the override kwargs from `register_file_exchange` per the framing principle in fastmcp-pvl-core#73 and the audit in fastmcp-pvl-core#72.  Operator-side knobs (transport, base URL, TTL) stay on environment variables; tool names are pvl-core's shape choice.

## What changes for this repo

(Survey-specific diff sketch. Example: \`artifact_store=...\` → drop; \`transport="http"\` → drop and ensure \`{PREFIX}_TRANSPORT=http\` or \`FASTMCP_TRANSPORT=http\` is set; \`download_tool_name="foo"\` → drop, this server's existing \`foo\` tool collides with pvl-core's \`create_download_link\` — rename the local tool.)

## Acceptance

- [ ] Code migrated (kwargs dropped, env vars set in deploy).
- [ ] \`fastmcp-pvl-core\` dep pin bumped to \`>=3.0.0\`.
- [ ] CI green on the migration PR.

## Related

- pvliesdonk/fastmcp-pvl-core#72 — upstream audit.
- pvliesdonk/fastmcp-pvl-core#80 — framing principle this enforces.
EOF
)"
```

For consumers that don't touch any removed kwarg, file a one-paragraph version asking only for the dep-pin bump.

- [ ] **Step 3: Append survey result to the design doc**

Open `docs/superpowers/specs/2026-05-14-file-exchange-hook-audit-design.md` and append a new section at the end:

```markdown
## Downstream survey result (2026-05-14)

| Consumer | Affected? | Migration issue |
|---|---|---|
| pvliesdonk/markdown-vault-mcp | Yes (transport=..., tool collision) | pvliesdonk/markdown-vault-mcp#NNN |
| pvliesdonk/scholar-mcp | <yes/no> | <link> |
| pvliesdonk/image-generation-mcp | <yes/no> | <link> |
| pvliesdonk/reqeng-mcp | <yes/no> | <link> |
| pvliesdonk/fastmcp-server-template | <yes/no> | <#131 if affected, else "no scaffold changes"> |
```

Fill in actual results from Steps 1–2.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-14-file-exchange-hook-audit-design.md
git commit -m "docs(spec): record downstream survey result for #72

Survey of register_file_exchange usage across the four named consumers
and the server template; per-affected-consumer migration issues filed
ahead of the pvl-core breaking change.  Result is appended to the
hook-audit design doc for permanent reference."
```

---

## Task 1: Add internal `_set_artifact_store_for_test` seam

**Goal:** Provide a private, test-only injection point so `artifact_store=` removal doesn't eliminate the test-fixture capability the kwarg currently offers.

**Files:**
- Modify: `src/fastmcp_pvl_core/file_exchange.py` (add module-level state + helper near the top of the file, used in `register_file_exchange`)
- Modify: `tests/conftest.py` (add autouse-`False` fixture)
- Test: `tests/test_file_exchange_facade.py` (new test for the seam)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_file_exchange_facade.py`:

```python
def test_internal_artifact_store_seam_injects_fake(monkeypatch):
    """``_set_artifact_store_for_test`` swaps in a fake store for
    the next register_file_exchange call.  Private API; tests only."""
    from fastmcp import FastMCP
    from fastmcp_pvl_core import register_file_exchange
    from fastmcp_pvl_core.file_exchange import _set_artifact_store_for_test
    from fastmcp_pvl_core._artifacts import ArtifactStore

    monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
    monkeypatch.setenv("TEST_FE_BASE_URL", "http://example.test")
    fake = ArtifactStore(ttl_seconds=10.0, base_url="http://fake.test")
    _set_artifact_store_for_test(fake)
    try:
        mcp = FastMCP(name="t")
        h = register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            produces=["application/pdf"],
        )
        assert h.artifact_store is fake
    finally:
        _set_artifact_store_for_test(None)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_file_exchange_facade.py::test_internal_artifact_store_seam_injects_fake -v
```

Expected: `ImportError` or `AttributeError` for `_set_artifact_store_for_test`.

- [ ] **Step 3: Implement the seam**

In `src/fastmcp_pvl_core/file_exchange.py`, find the module-level constants block near the top (look for `_DEFAULT_TTL_SECONDS`, `_DEFAULT_DOWNLOAD_TOOL`, etc., around lines 30–80). Just below the constants, add:

```python
# Private test seam: when set, ``register_file_exchange`` uses this store
# instead of building one from env vars. NOT public API — leading
# underscore is the signal. Reset to None between tests via the
# ``reset_artifact_store_test_seam`` fixture in tests/conftest.py.
_TEST_ARTIFACT_STORE: ArtifactStore | None = None


def _set_artifact_store_for_test(store: ArtifactStore | None) -> None:
    """Test-only seam for replacing the lazy-built artifact store.

    NOT public API. ``register_file_exchange``'s kwarg surface
    exposes only domain hooks; downstream production code has no
    domain-specific basis to inject a different store at runtime.
    Tests reach in here when they need fixture-level control.
    """
    global _TEST_ARTIFACT_STORE
    _TEST_ARTIFACT_STORE = store
```

In the same file, find the `# --- Artifact store ---` section inside `register_file_exchange` (around line 682):

```python
    # --- Artifact store ---
    base_url = env(env_prefix, "BASE_URL")
    store: ArtifactStore | None = artifact_store
    if enabled and produce and store is None:
```

Change the third line to consult the test seam first:

```python
    # --- Artifact store ---
    base_url = env(env_prefix, "BASE_URL")
    # Test seam takes precedence if set; otherwise fall through to the
    # legacy public kwarg path. The kwarg goes away in Task 3 and this
    # collapses to a single source.
    store: ArtifactStore | None = _TEST_ARTIFACT_STORE if _TEST_ARTIFACT_STORE is not None else artifact_store
    if enabled and produce and store is None:
```

- [ ] **Step 4: Add the conftest fixture**

In `tests/conftest.py`, append (or add a new section if the file doesn't already have one):

```python
import pytest

@pytest.fixture
def reset_artifact_store_test_seam():
    """Reset the file-exchange artifact-store test seam on teardown.

    Tests that call ``_set_artifact_store_for_test`` opt in by
    requesting this fixture; it makes sure leakage between tests
    doesn't poison unrelated tests.
    """
    yield
    from fastmcp_pvl_core.file_exchange import _set_artifact_store_for_test
    _set_artifact_store_for_test(None)
```

- [ ] **Step 5: Update the new test to use the fixture**

Replace the `try ... finally` in the test from Step 1 with the fixture:

```python
def test_internal_artifact_store_seam_injects_fake(
    monkeypatch, reset_artifact_store_test_seam
):
    """``_set_artifact_store_for_test`` swaps in a fake store for
    the next register_file_exchange call.  Private API; tests only."""
    from fastmcp import FastMCP
    from fastmcp_pvl_core import register_file_exchange
    from fastmcp_pvl_core.file_exchange import _set_artifact_store_for_test
    from fastmcp_pvl_core._artifacts import ArtifactStore

    monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
    monkeypatch.setenv("TEST_FE_BASE_URL", "http://example.test")
    fake = ArtifactStore(ttl_seconds=10.0, base_url="http://fake.test")
    _set_artifact_store_for_test(fake)
    mcp = FastMCP(name="t")
    h = register_file_exchange(
        mcp, namespace="x", env_prefix="TEST_FE", produces=["application/pdf"]
    )
    assert h.artifact_store is fake
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
uv run pytest tests/test_file_exchange_facade.py::test_internal_artifact_store_seam_injects_fake -v
```

Expected: PASS.

- [ ] **Step 7: Run the full file-exchange suite to verify no regression**

```bash
uv run pytest tests/test_file_exchange_facade.py tests/test_file_exchange_coverage.py tests/test_file_exchange_capability_merge.py -v
```

Expected: all PASS (existing tests still work since the seam falls through to the kwarg when `_TEST_ARTIFACT_STORE` is `None`).

- [ ] **Step 8: Commit**

```bash
git add src/fastmcp_pvl_core/file_exchange.py tests/conftest.py tests/test_file_exchange_facade.py
git commit -m "feat(file-exchange): add private _set_artifact_store_for_test seam

Module-level injection point for tests that previously passed
artifact_store=<fake> to register_file_exchange.  Not public API;
not exported from __init__.py.  Used by upcoming kwarg-removal
tasks (Task 3) to migrate existing test usages.

Refs #72."
```

---

## Task 2: Migrate `artifact_store=None` test usages

**Goal:** The four existing `artifact_store=None` call sites all pass the default-equivalent value. Drop the kwarg — no behaviour change.

**Files:**
- Modify: `tests/test_file_exchange_coverage.py` (lines 627, 648, 758, 791 currently; line numbers may shift after Task 1)

- [ ] **Step 1: Verify current usages**

```bash
grep -n 'artifact_store=' tests/test_file_exchange_coverage.py
```

Expected output: four lines, all `artifact_store=None,`.

- [ ] **Step 2: Drop each occurrence**

For each line returned by the grep, delete the `        artifact_store=None,` line. The simplest mechanical edit is one `Edit` call per line, or one `sed`:

```bash
sed -i '/^[[:space:]]*artifact_store=None,$/d' tests/test_file_exchange_coverage.py
```

Verify no `artifact_store=None` remains:

```bash
grep -n 'artifact_store=' tests/test_file_exchange_coverage.py
```

Expected: no output.

- [ ] **Step 3: Run the affected tests to confirm no regression**

```bash
uv run pytest tests/test_file_exchange_coverage.py -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_file_exchange_coverage.py
git commit -m "test(file-exchange): drop redundant artifact_store=None kwarg

All four call sites passed the default-equivalent value; dropping
is mechanical and preserves test intent.  Prepares for Task 3
(remove the kwarg entirely from register_file_exchange).

Refs #72."
```

---

## Task 3: Remove `artifact_store` kwarg

**Goal:** Strip the public kwarg from `register_file_exchange`. The Task 1 seam keeps test injection working.

**Files:**
- Modify: `src/fastmcp_pvl_core/file_exchange.py` (signature line ~608, body lines ~682–684, possibly docstring lines ~643–646)
- Test: `tests/test_file_exchange_facade.py` (new negative test)

- [ ] **Step 1: Write the failing negative test**

Append to `tests/test_file_exchange_facade.py`:

```python
def test_register_file_exchange_rejects_artifact_store_kwarg():
    """artifact_store is no longer accepted — pvl-core builds the
    store from env vars; test injection goes through the private
    _set_artifact_store_for_test seam."""
    from fastmcp import FastMCP
    from fastmcp_pvl_core import register_file_exchange
    import pytest

    mcp = FastMCP(name="t")
    with pytest.raises(TypeError, match="artifact_store"):
        register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            artifact_store=object(),  # type: ignore[call-arg]
        )
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_file_exchange_facade.py::test_register_file_exchange_rejects_artifact_store_kwarg -v
```

Expected: FAIL (the kwarg currently exists; the call does NOT raise).

- [ ] **Step 3: Remove the kwarg from the signature**

In `src/fastmcp_pvl_core/file_exchange.py`, edit the signature (around line 600):

```python
def register_file_exchange(
    mcp: FastMCP,
    *,
    namespace: str,
    env_prefix: str,
    produces: Sequence[str] = (),
    consumes: Sequence[str] = (),
    consumer_sink: ConsumerSink | None = None,
    artifact_store: ArtifactStore | None = None,
    transport: Literal["http", "stdio", "auto"] = "auto",
    download_tool_name: str = _DEFAULT_DOWNLOAD_TOOL,
    fetch_tool_name: str = _DEFAULT_FETCH_TOOL,
    legacy_capability_shape: bool = False,
) -> FileExchangeHandle:
```

Drop the `artifact_store: ArtifactStore | None = None,` line. After the edit:

```python
def register_file_exchange(
    mcp: FastMCP,
    *,
    namespace: str,
    env_prefix: str,
    produces: Sequence[str] = (),
    consumes: Sequence[str] = (),
    consumer_sink: ConsumerSink | None = None,
    transport: Literal["http", "stdio", "auto"] = "auto",
    download_tool_name: str = _DEFAULT_DOWNLOAD_TOOL,
    fetch_tool_name: str = _DEFAULT_FETCH_TOOL,
    legacy_capability_shape: bool = False,
) -> FileExchangeHandle:
```

- [ ] **Step 4: Simplify the body's store-resolution**

In `register_file_exchange`'s body, find the `# --- Artifact store ---` section (around line 682). The Task 1 edit currently reads:

```python
    # --- Artifact store ---
    base_url = env(env_prefix, "BASE_URL")
    # Test seam takes precedence if set; otherwise fall through to the
    # legacy public kwarg path. The kwarg goes away in Task 3 and this
    # collapses to a single source.
    store: ArtifactStore | None = _TEST_ARTIFACT_STORE if _TEST_ARTIFACT_STORE is not None else artifact_store
    if enabled and produce and store is None:
```

Replace with:

```python
    # --- Artifact store ---
    base_url = env(env_prefix, "BASE_URL")
    # Lazy build from env vars; tests override via _set_artifact_store_for_test.
    store: ArtifactStore | None = _TEST_ARTIFACT_STORE
    if enabled and produce and store is None:
```

- [ ] **Step 5: Remove `artifact_store` from the docstring Args block**

Find the docstring Args section (around line 631–657). Delete these lines:

```
        artifact_store: Optional pre-built store. When ``None`` and
            HTTP is enabled, the facade builds one with ``base_url``
            from ``{PREFIX}_BASE_URL`` and TTL from
            ``{PREFIX}_FILE_EXCHANGE_TTL``.
```

(Full docstring rework is Task 8 — minimal removal here, only the lines that talk about the now-gone kwarg.)

- [ ] **Step 6: Run the negative test to verify it now passes**

```bash
uv run pytest tests/test_file_exchange_facade.py::test_register_file_exchange_rejects_artifact_store_kwarg -v
```

Expected: PASS.

- [ ] **Step 7: Run the full file-exchange suite to verify no regression**

```bash
uv run pytest tests/test_file_exchange_facade.py tests/test_file_exchange_coverage.py tests/test_file_exchange_capability_merge.py -v
```

Expected: all PASS, including the Task 1 seam test (it uses `_set_artifact_store_for_test`, not the removed kwarg).

- [ ] **Step 8: Commit (BREAKING)**

```bash
git add src/fastmcp_pvl_core/file_exchange.py tests/test_file_exchange_facade.py
git commit -m "refactor(file-exchange)!: remove artifact_store kwarg from register_file_exchange

BREAKING CHANGE: register_file_exchange no longer accepts
artifact_store=.  pvl-core builds the store from {PREFIX}_BASE_URL
and {PREFIX}_FILE_EXCHANGE_TTL; downstream has no domain-specific
basis to inject a different store at runtime per the framing
principle in #73.

Tests retain injection capability via the private
_set_artifact_store_for_test seam added in the prior commit
(see file_exchange.py).

Refs #72.  Downstream migration tracked per consumer in the issues
filed in Task 0."
```

---

## Task 4: Migrate `transport=` test usages to env vars

**Goal:** The ~23 test call sites that pass `transport="http"` or `transport="stdio"` migrate to setting `{PREFIX}_TRANSPORT` (or `FASTMCP_TRANSPORT`) via `monkeypatch.setenv`.

**Files:**
- Modify: `tests/test_file_exchange_facade.py` (17 call sites)
- Modify: `tests/test_file_exchange_coverage.py` (6 call sites)

- [ ] **Step 1: List every affected line**

```bash
grep -n 'transport=' tests/test_file_exchange_facade.py tests/test_file_exchange_coverage.py | grep -v ASGITransport
```

Expected: ~23 lines total. (The `ASGITransport` lines are httpx, not pvl-core, and stay.)

- [ ] **Step 2: Migrate each call site**

For each test function that calls `register_file_exchange(..., transport="http"|"stdio", ...)`:

a) At the start of the test (before the `register_file_exchange` call), add a `monkeypatch.setenv` for the right env var. The test fixture must already accept `monkeypatch` as a parameter — most do; if a specific test doesn't, add it.

b) Drop the `transport=...` kwarg from the `register_file_exchange` call.

**Pattern A — explicit http:**

Before:
```python
def test_foo(monkeypatch):
    mcp = FastMCP(name="t")
    monkeypatch.setenv("TEST_FE_BASE_URL", "http://example.test")
    h = register_file_exchange(
        mcp, namespace="x", env_prefix="TEST_FE", transport="http"
    )
```

After:
```python
def test_foo(monkeypatch):
    mcp = FastMCP(name="t")
    monkeypatch.setenv("TEST_FE_BASE_URL", "http://example.test")
    monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
    h = register_file_exchange(
        mcp, namespace="x", env_prefix="TEST_FE"
    )
```

**Pattern B — explicit stdio:**

Before:
```python
    h = register_file_exchange(
        mcp, namespace="test-mcp", env_prefix="TEST_FE", transport="stdio"
    )
```

After:
```python
    monkeypatch.setenv("TEST_FE_TRANSPORT", "stdio")
    h = register_file_exchange(
        mcp, namespace="test-mcp", env_prefix="TEST_FE"
    )
```

If the test name implies `stdio` is the absence-of-config default (no env set, no `FASTMCP_TRANSPORT`), prefer the explicit `setenv("...", "stdio")` for clarity; the `_resolve_transport` helper defaults to stdio when both are unset, but tests should not lean on the default to assert stdio specifically.

- [ ] **Step 3: Run the affected suites to confirm green**

```bash
uv run pytest tests/test_file_exchange_facade.py tests/test_file_exchange_coverage.py -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_file_exchange_facade.py tests/test_file_exchange_coverage.py
git commit -m "test(file-exchange): migrate transport= test kwarg to monkeypatch env

23 call sites updated to use monkeypatch.setenv('{PREFIX}_TRANSPORT', ...)
instead of the transport= kwarg.  Prepares for Task 5 (remove the
kwarg from register_file_exchange).  No behaviour change in the
helper; tests still cover both http and stdio paths.

Refs #72."
```

---

## Task 5: Remove `transport` kwarg

**Files:**
- Modify: `src/fastmcp_pvl_core/file_exchange.py` (signature, body, `_resolve_transport` helper)
- Test: `tests/test_file_exchange_facade.py` (new negative test)

- [ ] **Step 1: Write the failing negative test**

Append to `tests/test_file_exchange_facade.py`:

```python
def test_register_file_exchange_rejects_transport_kwarg():
    """transport is no longer accepted — pvl-core resolves transport
    from {PREFIX}_TRANSPORT (fallback FASTMCP_TRANSPORT)."""
    from fastmcp import FastMCP
    from fastmcp_pvl_core import register_file_exchange
    import pytest

    mcp = FastMCP(name="t")
    with pytest.raises(TypeError, match="transport"):
        register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            transport="http",  # type: ignore[call-arg]
        )
```

- [ ] **Step 2: Run it to verify failure**

```bash
uv run pytest tests/test_file_exchange_facade.py::test_register_file_exchange_rejects_transport_kwarg -v
```

Expected: FAIL.

- [ ] **Step 3: Remove the kwarg from the signature**

In `src/fastmcp_pvl_core/file_exchange.py`, delete the line:

```python
    transport: Literal["http", "stdio", "auto"] = "auto",
```

from `register_file_exchange`'s signature.

- [ ] **Step 4: Simplify `_resolve_transport`**

Find `_resolve_transport` (around line 770):

```python
def _resolve_transport(
    env_prefix: str, override: Literal["http", "stdio", "auto"]
) -> Literal["http", "stdio"]:
    if override != "auto":
        return override
    raw = (
        env(env_prefix, "TRANSPORT") or env("FASTMCP", "TRANSPORT") or "stdio"
    ).lower()
    if raw in ("http", "sse", "streamable-http"):
        return "http"
    return "stdio"
```

Replace with the override-free form:

```python
def _resolve_transport(env_prefix: str) -> Literal["http", "stdio"]:
    """Resolve transport from {PREFIX}_TRANSPORT, falling back to
    FASTMCP_TRANSPORT, defaulting to stdio."""
    raw = (
        env(env_prefix, "TRANSPORT") or env("FASTMCP", "TRANSPORT") or "stdio"
    ).lower()
    if raw in ("http", "sse", "streamable-http"):
        return "http"
    return "stdio"
```

- [ ] **Step 5: Update the call site inside `register_file_exchange`**

Find (around line 663):

```python
    resolved_transport = _resolve_transport(env_prefix, transport)
```

Replace with:

```python
    resolved_transport = _resolve_transport(env_prefix)
```

- [ ] **Step 6: Remove `transport` from the docstring Args block**

Find and delete from the docstring:

```
        transport: ``"auto"`` (default) infers from
            ``{PREFIX}_TRANSPORT`` / ``FASTMCP_TRANSPORT``; ``"http"``
            and ``"stdio"`` force the choice.
```

(Full docstring rework is Task 8.)

- [ ] **Step 7: Run the negative test + full file-exchange suite**

```bash
uv run pytest tests/test_file_exchange_facade.py tests/test_file_exchange_coverage.py tests/test_file_exchange_capability_merge.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit (BREAKING)**

```bash
git add src/fastmcp_pvl_core/file_exchange.py tests/test_file_exchange_facade.py
git commit -m "refactor(file-exchange)!: remove transport kwarg from register_file_exchange

BREAKING CHANGE: register_file_exchange no longer accepts transport=.
pvl-core resolves transport from {PREFIX}_TRANSPORT (fallback
FASTMCP_TRANSPORT, default 'stdio') unconditionally.  Downstream
has no domain-specific basis to disagree with the env-var resolution.

Refs #72."
```

---

## Task 6: Remove `download_tool_name` + `fetch_tool_name` kwargs

**Files:**
- Modify: `src/fastmcp_pvl_core/file_exchange.py` (signature, two body call sites, FileExchangeHandle assignment)
- Test: `tests/test_file_exchange_facade.py` (two negative tests)

- [ ] **Step 1: Write the failing negative tests**

Append to `tests/test_file_exchange_facade.py`:

```python
def test_register_file_exchange_rejects_download_tool_name_kwarg():
    """Tool name is pvl-core's shape decision; not overridable."""
    from fastmcp import FastMCP
    from fastmcp_pvl_core import register_file_exchange
    import pytest

    mcp = FastMCP(name="t")
    with pytest.raises(TypeError, match="download_tool_name"):
        register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            download_tool_name="not_allowed",  # type: ignore[call-arg]
        )


def test_register_file_exchange_rejects_fetch_tool_name_kwarg():
    """Tool name is pvl-core's shape decision; not overridable."""
    from fastmcp import FastMCP
    from fastmcp_pvl_core import register_file_exchange
    import pytest

    mcp = FastMCP(name="t")
    with pytest.raises(TypeError, match="fetch_tool_name"):
        register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            fetch_tool_name="not_allowed",  # type: ignore[call-arg]
        )
```

- [ ] **Step 2: Run both to verify failure**

```bash
uv run pytest tests/test_file_exchange_facade.py -k "rejects_download_tool_name or rejects_fetch_tool_name" -v
```

Expected: FAIL × 2.

- [ ] **Step 3: Remove the kwargs from the signature**

In the `register_file_exchange` signature, delete:

```python
    download_tool_name: str = _DEFAULT_DOWNLOAD_TOOL,
    fetch_tool_name: str = _DEFAULT_FETCH_TOOL,
```

- [ ] **Step 4: Replace `download_tool_name` references in the body**

Find (around line 733):

```python
            builder.set_download(tool_name=download_tool_name)
```

Replace with:

```python
            builder.set_download(tool_name=_DEFAULT_DOWNLOAD_TOOL)
```

Find (around line 740):

```python
            builder.set_download(tool_name=fetch_tool_name)
```

Replace with:

```python
            builder.set_download(tool_name=_DEFAULT_FETCH_TOOL)
```

- [ ] **Step 5: Update FileExchangeHandle construction**

Find (around line 751):

```python
    handle = FileExchangeHandle(
        namespace=namespace,
        enabled=enabled,
        produce=produce,
        consume=consume,
        artifact_store=store,
        exchange=exchange,
        capability=capability,
        download_tool_name=download_tool_name,
        fetch_tool_name=fetch_tool_name,
        ttl_seconds=ttl_seconds,
    )
```

Replace the two `*_tool_name` lines:

```python
    handle = FileExchangeHandle(
        namespace=namespace,
        enabled=enabled,
        produce=produce,
        consume=consume,
        artifact_store=store,
        exchange=exchange,
        capability=capability,
        download_tool_name=_DEFAULT_DOWNLOAD_TOOL,
        fetch_tool_name=_DEFAULT_FETCH_TOOL,
        ttl_seconds=ttl_seconds,
    )
```

- [ ] **Step 6: Remove from the docstring Args block**

Find and delete:

```
        download_tool_name: Override the default ``create_download_link``
            tool name.
        fetch_tool_name: Override the default ``fetch_file`` tool name.
```

- [ ] **Step 7: Run the negative tests + full file-exchange suite**

```bash
uv run pytest tests/test_file_exchange_facade.py tests/test_file_exchange_coverage.py tests/test_file_exchange_capability_merge.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit (BREAKING)**

```bash
git add src/fastmcp_pvl_core/file_exchange.py tests/test_file_exchange_facade.py
git commit -m "refactor(file-exchange)!: remove download_tool_name and fetch_tool_name kwargs

BREAKING CHANGE: register_file_exchange no longer accepts
download_tool_name= or fetch_tool_name=.  pvl-core owns the tool
names ('create_download_link', 'fetch_file') as part of its shared
shape; downstream collisions resolve by downstream renaming the
local tool, not by pvl-core relaxing the shape.

See pvliesdonk/markdown-vault-mcp's migration issue for the
canonical example (pre-existing create_download_link(path) tool
must be renamed when adopting pvl-core 3.0.0).

Refs #72."
```

---

## Task 7: Remove `legacy_capability_shape` kwarg

**Goal:** Delete the compatibility shim added during the v0.4-amendments window. The four tests exercising legacy-shape behaviour are deleted along with the feature.

**Files:**
- Delete: tests in `tests/test_file_exchange_capability_merge.py` that pass `legacy_capability_shape=True`
- Modify: `src/fastmcp_pvl_core/file_exchange.py` (signature, `_get_or_create_builder` call site, the builder's legacy-shape code path)
- Modify: `tests/test_file_exchange_facade.py` (new negative test)

- [ ] **Step 1: Identify the affected tests**

```bash
grep -n 'legacy_capability_shape' tests/test_file_exchange_capability_merge.py
```

Expected: 5 hits (4 in test bodies, 1 in a docstring around line 171). Note the test function names — each test that calls `legacy_capability_shape=True` will be deleted.

- [ ] **Step 2: Delete legacy-shape tests**

Open `tests/test_file_exchange_capability_merge.py`. For each function whose body contains `legacy_capability_shape=True` (per Step 1's grep), delete the entire function (from its `def test_...` line through the closing line of its body).

Tests likely include something like:
- The function containing line 16
- The function containing line 77
- The function containing line 112
- The function containing line 189 (the docstring at 171 names this one)

Also delete the file-level imports that become unused after the deletions (e.g. if a test imported `FileExchangeCapability` only for legacy-shape assertions). Run `ruff check` after the deletions to catch unused-import noise.

- [ ] **Step 3: Write the failing negative test**

Append to `tests/test_file_exchange_facade.py`:

```python
def test_register_file_exchange_rejects_legacy_capability_shape_kwarg():
    """legacy_capability_shape was a v0.4-amendments-window shim;
    removed in 3.0.0."""
    from fastmcp import FastMCP
    from fastmcp_pvl_core import register_file_exchange
    import pytest

    mcp = FastMCP(name="t")
    with pytest.raises(TypeError, match="legacy_capability_shape"):
        register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            legacy_capability_shape=True,  # type: ignore[call-arg]
        )
```

- [ ] **Step 4: Run the negative test to verify it fails**

```bash
uv run pytest tests/test_file_exchange_facade.py::test_register_file_exchange_rejects_legacy_capability_shape_kwarg -v
```

Expected: FAIL.

- [ ] **Step 5: Remove the kwarg from the signature**

In `register_file_exchange`, delete:

```python
    legacy_capability_shape: bool = False,
```

- [ ] **Step 6: Remove the legacy-shape pass-through inside the body**

Find (around line 720):

```python
        builder = _get_or_create_builder(
            mcp,
            namespace=namespace,
            exchange_id=exchange.exchange_id if exchange is not None else None,
            produces=tuple(produces) if produce else (),
            consumes=tuple(consumes) if consume else (),
            legacy_capability_shape=legacy_capability_shape,
        )
```

Delete the `legacy_capability_shape=legacy_capability_shape,` line:

```python
        builder = _get_or_create_builder(
            mcp,
            namespace=namespace,
            exchange_id=exchange.exchange_id if exchange is not None else None,
            produces=tuple(produces) if produce else (),
            consumes=tuple(consumes) if consume else (),
        )
```

- [ ] **Step 7: Find and remove the builder-side legacy-shape code path**

```bash
grep -n 'legacy_capability_shape' src/fastmcp_pvl_core/file_exchange.py src/fastmcp_pvl_core/_file_exchange_protocol.py src/fastmcp_pvl_core/_file_exchange_runtime.py
```

For each surviving reference (outside the just-edited `register_file_exchange` body), follow the trail:
- `_get_or_create_builder` likely accepts a `legacy_capability_shape: bool = False` kwarg that flips a flag on a builder dataclass.
- The builder's emit-capability method probably has an `if self.legacy_capability_shape:` branch that emits the flat v0.2 `transfer_methods.http: {tool: ...}` shape.

Remove:
- The kwarg from `_get_or_create_builder`'s signature.
- The flag from the builder dataclass.
- The `if`-branch in the emit method; keep only the v0.4-style nested shape (which is now the only shape).
- Any helper functions exclusively serving the legacy path.

Also check `src/fastmcp_pvl_core/file_exchange.py` (the upload helper, `register_file_exchange_upload`, at line 1485) — it has its own `legacy_capability_shape` kwarg. **Do NOT touch the upload helper in this PR.** Its kwarg surface is #74's job. The flag on the shared builder dataclass stays alive as long as `register_file_exchange_upload` still references it.

This means: in Step 7's builder cleanup, only remove the legacy-shape path if the upload helper's kwarg is *also* removed (it isn't, in this PR). The pragmatic path is:

- Keep the legacy-shape flag on the builder.
- Keep the emit-method branch.
- Stop passing `legacy_capability_shape` from `register_file_exchange` to the builder (Step 6 above).
- The upload helper still passes its own `legacy_capability_shape` kwarg to the builder, which still works.
- When #74 lands, it removes the upload helper's kwarg and the builder-side cleanup happens then.

Re-scope this step to: only remove the line in Step 6. Don't touch the builder or the upload helper.

- [ ] **Step 8: Remove from the docstring Args block**

Find and delete from the `register_file_exchange` docstring:

```
        legacy_capability_shape: Set to True during a migration window
            to advertise the v0.2 flat ``transfer_methods.http: {tool: ...}``
            shape instead of the v0.4 nested
            ``{download: ..., upload: ...}`` shape. The upload entry is
            dropped (with a logged warning) when legacy shape is selected.
```

- [ ] **Step 9: Run the negative test + full file-exchange suite + ruff**

```bash
uv run pytest tests/test_file_exchange_facade.py tests/test_file_exchange_coverage.py tests/test_file_exchange_capability_merge.py -v
uv run ruff check src tests
```

Expected: all PASS; no new lint errors. (Test count in `test_file_exchange_capability_merge.py` drops by 4 from the deletions.)

- [ ] **Step 10: Commit (BREAKING)**

```bash
git add src/fastmcp_pvl_core/file_exchange.py tests/test_file_exchange_facade.py tests/test_file_exchange_capability_merge.py
git commit -m "refactor(file-exchange)!: remove legacy_capability_shape from register_file_exchange

BREAKING CHANGE: register_file_exchange no longer accepts
legacy_capability_shape=.  This was a transitional shim from the
v0.4-amendments window of file-exchange's history; v0.4 was reverted
in #77 and the spec is now v0.2.5.  No deprecation window per the
'no opt-out' framing.

The shared builder still carries the flag because the upload helper
(register_file_exchange_upload) also accepts it; that helper is
out of scope for this PR (see #74).  Builder-side cleanup happens
when #74 lands.

Tests in test_file_exchange_capability_merge.py that exercised the
legacy shape are removed; the surviving tests cover the v0.4-style
nested shape which is now the only shape advertised by this helper.

Refs #72."
```

---

## Task 8: Update `register_file_exchange` docstring

**Goal:** Final docstring carrying the worked-example annotation: design note paragraph, `**Domain hook**` tags on each of the five remaining kwargs, and an "Environment" subsection enumerating the operator vars.

**Files:**
- Modify: `src/fastmcp_pvl_core/file_exchange.py` (docstring of `register_file_exchange`)

- [ ] **Step 1: Read the current docstring**

After Tasks 3–7 the docstring should be missing the `artifact_store`, `transport`, `download_tool_name`, `fetch_tool_name`, and `legacy_capability_shape` entries. Confirm:

```bash
sed -n '600,665p' src/fastmcp_pvl_core/file_exchange.py
```

- [ ] **Step 2: Rewrite the docstring**

Replace the entire docstring of `register_file_exchange` (currently from the line after the signature to the start of the body — `resolved_transport = ...`) with:

```python
    """Wire MCP File Exchange (v0.2.5) onto ``mcp``.

    The kwarg surface is intentionally minimal — five domain hooks,
    no operator-config kwargs, no override seams. Operator config
    goes to environment variables (see "Environment" below).
    Implementation choices pvl-core makes (tool names, transport
    resolution, capability shape) are not overridable; downstream
    collisions resolve by downstream migration. See ``CLAUDE.md``
    "framing principle" for the rationale.

    Performs four pieces of wiring, each gated by env vars:

    1. Builds (or adopts) an :class:`ArtifactStore`, mounts its
       ``/artifacts/{token}`` route, and installs the module-level
       singleton.
    2. Resolves the :class:`FileExchange` runtime from
       ``MCP_EXCHANGE_DIR`` (deployer-controlled, unprefixed).
    3. Advertises ``experimental.file_exchange`` on the MCP
       ``initialize`` response (spec §"Capability declaration").
    4. Registers ``create_download_link`` (spec §"Transfer Methods /
       http") and ``fetch_file`` (spec §"Transfer Negotiation") MCP
       tools as appropriate for the resolved producer / consumer /
       transport state.

    Args:
        mcp: The :class:`fastmcp.FastMCP` server instance.
        namespace: **Domain hook.** This server's logical name. Used
            as both the ``FileRef.origin_server`` and the exchange
            namespace.
        env_prefix: **Domain hook.** Per-server env-var prefix (e.g.
            ``"IMAGE_GENERATION_MCP"``).
        produces: **Domain hook.** MIME types this server emits as
            file references — advertised in the capability declaration.
        consumes: **Domain hook.** MIME types this server can ingest
            via ``fetch_file``.
        consumer_sink: **Domain hook.** Required to register
            ``fetch_file``. Receives the resolved bytes and a
            :class:`FetchContext`; returns a :class:`FetchResult`.
            When ``None``, the consumer side is not advertised in the
            capability declaration and ``fetch_file`` is not registered.

    Environment:
        Operator-controlled configuration. ``{PREFIX}`` matches the
        ``env_prefix`` argument.

        - ``{PREFIX}_TRANSPORT`` (fallback ``FASTMCP_TRANSPORT``, default
          ``"stdio"``): selects transport. ``"http"`` / ``"sse"`` /
          ``"streamable-http"`` enable HTTP-side wiring.
        - ``{PREFIX}_BASE_URL``: required for the HTTP-side
          ``create_download_link`` tool to produce reachable URLs;
          unset means the producer side is silently skipped.
        - ``{PREFIX}_FILE_EXCHANGE_TTL`` (default 3600 seconds): TTL
          for issued download URLs and for published file records.
        - ``{PREFIX}_FILE_EXCHANGE_PRODUCE`` (default ``"true"``):
          operator opt-out of producer side independent of transport.
        - ``{PREFIX}_FILE_EXCHANGE_CONSUME`` (default ``"true"``):
          operator opt-out of consumer side independent of transport.
        - ``MCP_EXCHANGE_DIR`` (unprefixed, deployer-controlled): the
          shared volume for the ``exchange`` transfer method;
          unset means the exchange-volume side is skipped.

    Returns:
        A :class:`FileExchangeHandle`. Stash it where your producer-side
        tools can reach it.
    """
```

- [ ] **Step 3: Run the suite to confirm no behaviour change**

```bash
uv run pytest tests/test_file_exchange_facade.py tests/test_file_exchange_coverage.py tests/test_file_exchange_capability_merge.py -v
uv run ruff check src
uv run mypy src
```

Expected: all green. (Docstring is descriptive; behaviour-neutral.)

- [ ] **Step 4: Commit**

```bash
git add src/fastmcp_pvl_core/file_exchange.py
git commit -m "docs(file-exchange): annotate register_file_exchange kwargs as domain hooks

Reflects the audit landed across Tasks 3-7 in this PR.  All five
remaining kwargs now carry the **Domain hook** tag in the docstring.
A new design-note paragraph at the top of the docstring states the
minimal-surface principle; a new Environment subsection enumerates
the operator-config env vars that replaced the removed kwargs.

Worked example for the classification test landed in #73 and tightened
in the upcoming Task 9 (README.md / CLAUDE.md).

Refs #72."
```

---

## Task 9: Tighten classification test in `README.md`

**Files:**
- Modify: `README.md` `## Design principles` → `### Hooks expose domain-specific behaviour only` subsection.

- [ ] **Step 1: Locate the section**

```bash
grep -n '### Hooks expose domain-specific behaviour only' README.md
```

- [ ] **Step 2: Replace the classification test paragraph + bullets**

Find this block in `README.md` (under the `### Hooks expose domain-specific behaviour only` heading):

```markdown
### Hooks expose domain-specific behaviour only

A hook like *"where in my storage model do these bytes go?"* is
appropriate — pvl-core cannot know the answer for a particular
downstream. A hook like *"what should this tool be called?"* or
*"what HTTP status code should an oversize body return?"* is not —
those are shape decisions pvl-core owns, and downstream accepts them.

Classification test for a proposed new keyword argument on a
`register_*` helper, `Build*` factory, or middleware constructor:

- The caller is supplying a value pvl-core could not reasonably know
  on its own (a callback to its own storage, a per-instance label
  visible only to the deployer) — **domain hook**, accept.
- The caller is asking to override a decision pvl-core has already
  made or should make (rename a tool, change a parameter shape,
  swap a status code) — **reject**. If downstream genuinely needs
  different behaviour, pvl-core changes shape and *all* downstreams
  follow.
- The caller is supplying a deployer-side value (TTL ceiling, max
  body size, listening port, debug flag) — **operator
  configuration**: expose via environment variable, not kwarg.

If a proposed kwarg mixes categories — a legitimate hook bundled
with an override of shape — split it: keep the hook, drop the
override. PRs that grow override kwargs disguised as hooks are
rejected.
```

Replace with:

```markdown
### Hooks expose domain-specific behaviour only

A hook like *"where in my storage model do these bytes go?"* is
appropriate — pvl-core cannot know the answer for a particular
downstream. A hook like *"what should this tool be called?"* or
*"what HTTP status code should an oversize body return?"* is not —
those are shape decisions pvl-core owns, and downstream accepts them.

The test for any proposed kwarg on a `register_*` helper, `Build*`
factory, or middleware constructor: **would pvl-core be wrong to
make this decision itself?** If pvl-core could pick a sensible value
and downstream has no domain-specific basis to disagree, pvl-core
picks it — no kwarg. If pvl-core *literally cannot* answer because
the answer is about the downstream's domain, the kwarg exists and is
not optional unless the entire feature is opt-in. There is no third
bucket of "pvl-core has a default but downstream can override."

Operator-side configuration (TTL ceilings, max body sizes, listening
ports, debug flags) is a separate axis — environment variables, not
kwargs. The kwarg surface is purely domain hooks.

If a proposed kwarg mixes the two — a legitimate hook bundled with an
override of shape — split it: keep the hook, drop the override. PRs
that grow override kwargs disguised as hooks are rejected. The
worked example is `register_file_exchange` in
`src/fastmcp_pvl_core/file_exchange.py`: five kwargs, all domain
hooks, every operator value on an env var.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): tighten classification test wording (refs #72)

Sharpens the test landed in #73 to match the corrected framing the
brainstorm-on-#72 surfaced: 'would pvl-core be wrong to make this
decision itself?' is strictly stronger than 'could pvl-core know
this on its own?', and rules out the implicit 'pvl-core has a
default but downstream can override' bucket that the prior wording
left room for.

References register_file_exchange as the worked example of the
sharpened test in action.

Refs #72."
```

---

## Task 10: Tighten classification test in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md` `## The framing principle` → `### Hooks expose domain-specific behaviour only`

- [ ] **Step 1: Locate the section**

```bash
grep -n '### Hooks expose domain-specific behaviour only' CLAUDE.md
```

- [ ] **Step 2: Replace the classification test block**

Find this block in `CLAUDE.md`:

```markdown
### Hooks expose domain-specific behaviour only

A hook like *"where in my storage model do these bytes go?"* is
appropriate — pvl-core cannot know the answer for a particular
downstream. A hook like *"what should this tool be called?"* or
*"what HTTP status code should an oversize body return?"* is not —
those are shape decisions pvl-core owns and downstream must accept.

**Classification test** for a proposed new keyword argument on a
`register_*` helper, `Build*` factory, or middleware constructor:

- Is the caller supplying a value pvl-core could not reasonably know
  on its own? → **domain hook** — accept the kwarg.
- Is the caller asking to override a decision pvl-core has already
  made (or should make)? → **reject**. Keep the decision in pvl-core;
  if downstream genuinely needs different behaviour, pvl-core changes
  shape and *all* downstreams follow.
- Is the caller supplying a deployer-side value (TTL ceiling, max body
  size, listening port, debug flag)? → **operator configuration**
  — expose via environment variable, not kwarg.

If a proposed kwarg mixes categories — a legitimate hook bundled
with an override of shape — split it: keep the hook component, drop
the override component. Reviewers reject PRs that grow override
kwargs disguised as hooks.
```

Replace with:

```markdown
### Hooks expose domain-specific behaviour only

A hook like *"where in my storage model do these bytes go?"* is
appropriate — pvl-core cannot know the answer for a particular
downstream. A hook like *"what should this tool be called?"* or
*"what HTTP status code should an oversize body return?"* is not —
those are shape decisions pvl-core owns and downstream accepts them.

**The classification test for a proposed new keyword argument** on a
`register_*` helper, `Build*` factory, or middleware constructor:
**would pvl-core be wrong to make this decision itself?**

- pvl-core *could* pick a sensible value and downstream has no
  domain-specific basis to disagree → **pvl-core picks it; no kwarg.**
  If downstream genuinely needs different behaviour, pvl-core changes
  shape and *all* downstreams follow.
- pvl-core *literally cannot* answer because the answer is about the
  downstream's domain → **domain hook**, accept the kwarg. The kwarg
  is not optional unless the entire feature is opt-in. There is no
  third bucket of "pvl-core has a default but downstream can override."

Operator-side configuration (TTL ceilings, max body sizes, listening
ports, debug flags) is a separate axis — environment variables, not
kwargs at all. The kwarg surface stays purely domain hooks.

If a proposed kwarg mixes the two — a legitimate hook bundled with an
override of shape — split it: keep the hook component, drop the
override component. Reviewers reject PRs that grow override kwargs
disguised as hooks. `register_file_exchange` in
`src/fastmcp_pvl_core/file_exchange.py` is the worked example of the
sharpened test applied end-to-end: five kwargs, all domain hooks,
every operator value on an env var.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): tighten classification test wording (refs #72)

Parallel update to README.md's tightening landed in the prior commit.
Same content, contributor-facing voice retained.  Worked-example
pointer to register_file_exchange added.

Refs #72."
```

---

## Task 11: `CHANGELOG.md` entry for 3.0.0

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Inspect the current top of the file**

```bash
head -30 CHANGELOG.md
```

Note the format (whether `python-semantic-release` already wrote a `## [2.1.0]` or `## v2.1.0` section). The 3.0.0 entry follows the same format.

- [ ] **Step 2: Prepend a 3.0.0 section**

Insert at the top (after the title line, if any) a block like:

```markdown
## [3.0.0] - UNRELEASED

### BREAKING CHANGES

- `register_file_exchange` no longer accepts the following kwargs;
  every one was either an override of a pvl-core shape decision or
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

- pvliesdonk/markdown-vault-mcp — `<link>`
- pvliesdonk/scholar-mcp — `<link>` (or "no kwarg changes; bump dep pin only")
- pvliesdonk/image-generation-mcp — `<link>`
- pvliesdonk/reqeng-mcp — `<link>`
- pvliesdonk/fastmcp-server-template — `<link>` (or "scaffold unaffected")

### Notes

`register_file_exchange_upload` is intentionally untouched in this
release; #74 redoes it wholesale against the #71 spec evolution.
Its kwarg surface is audited at that point.

The framing principle that drives this change is documented
authoritatively in `README.md` `## Design principles` and `CLAUDE.md`
`## The framing principle`. See pvliesdonk/fastmcp-pvl-core#73 and
pvliesdonk/fastmcp-pvl-core#72 for context.
```

Fill in the actual issue links from Task 0's survey.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): add 3.0.0 BREAKING CHANGES entry

Closes #72.  Enumerates the five removed kwargs, the env-var
replacements / private test seam, the per-downstream migration
trackers, and the explicit deferral of register_file_exchange_upload
to #74.

This is the last logical commit in the PR; subsequent activity is
local review + bot review.

Closes #72.
Refs #75 (umbrella), #73 (framing principle), #80 (framing PR)."
```

The `Closes #72` footer in the trailing commit is what marks the issue resolved on merge. The body's `Closes #72` is also included for redundancy when GitHub parses commit-body trailers.

---

## Task 12: Local review circus

**Goal:** Per the project's PR workflow (see `~/.claude/CLAUDE.md` → "Pre-flight checklist for opening any PR"), dispatch the two-subagent local review circus on the cumulative diff before opening the PR.

**Files:** none (review-only step).

- [ ] **Step 1: Refresh the branch state**

```bash
git fetch origin main
git log --oneline origin/main..HEAD
```

Confirm the commit list (~10 commits — one per task, plus the design-doc commit `3976f7d` from before this plan).

- [ ] **Step 2: Dispatch primary reviewer**

Use the Agent tool with `subagent_type: "pr-review-toolkit:code-reviewer"`. Prompt includes:

- Goal of the PR (issue #72, hook-audit per #73's framing).
- Spec file path: `docs/superpowers/specs/2026-05-14-file-exchange-hook-audit-design.md`.
- Cumulative diff via `git diff origin/main...HEAD`.
- Explicit instruction to read full files (`file_exchange.py`, `README.md`, `CLAUDE.md`, the affected test files) as well as the diff — not diff-only review.
- Note that `register_file_exchange_upload` is intentionally untouched in this PR; the upload helper's `legacy_capability_shape` and operator-config kwargs are #74's audit fodder.

- [ ] **Step 3: Dispatch second-opinion reviewer**

Use the Agent tool with `subagent_type: "feature-dev:code-reviewer"`. Different prompt focus:

- Apply the sharpened classification test (as tightened in Tasks 9–10) to *each* remaining kwarg on `register_file_exchange`. Verify each survives the test.
- Verify the worked-example annotation in the docstring accurately reflects the post-audit state.
- Check the downstream survey (Task 0 output appended to the spec doc) for completeness.

- [ ] **Step 4: Address findings**

For any blocker or should-fix finding from either reviewer:

- Either fix in code and re-dispatch the relevant reviewer, or
- Defend in a written PR comment if the finding is wrong (per the `receiving-code-review` skill).

Iterate until both reviewers return clean.

- [ ] **Step 5: Open PR as draft**

```bash
gh pr create --draft \
    --title "refactor(file-exchange)!: audit register_file_exchange kwargs (closes #72)" \
    --body "$(cat <<'EOF'
Closes #72. Refs #75 (umbrella), #73 (framing principle PR #80), #74 (deferred upload-helper audit).

## Summary

Applies the framing principle from #73 to register_file_exchange.  Five kwargs remain (all domain hooks); five were removed (four overrides, one operator-config-as-kwarg).  Operator config now lives purely on environment variables.  Tests inject artifact stores via a private `_set_artifact_store_for_test` seam.

See `docs/superpowers/specs/2026-05-14-file-exchange-hook-audit-design.md` for the full design and per-kwarg disposition table.

## What's removed

- `artifact_store=` — pvl-core builds from env vars; tests use private seam.
- `transport=` — env-var-only resolution.
- `download_tool_name=` + `fetch_tool_name=` — pvl-core owns the names.
- `legacy_capability_shape=` — transitional shim from the v0.4-amendments window; spec is back to v0.2.5.

## What's added

- Five `**Domain hook**` annotations on the surviving kwargs (worked example for #73's classification test).
- Tightened classification-test wording in `README.md` and `CLAUDE.md`.
- Private `_set_artifact_store_for_test` test seam.
- `CHANGELOG.md` 3.0.0 entry.

## Downstream

Per-consumer migration issues filed before this PR opened:

| Consumer | Issue |
|---|---|
| pvliesdonk/markdown-vault-mcp | <link> |
| pvliesdonk/scholar-mcp | <link or "dep-pin bump only"> |
| pvliesdonk/image-generation-mcp | <link> |
| pvliesdonk/reqeng-mcp | <link> |
| pvliesdonk/fastmcp-server-template | <link or "scaffold unaffected"> |

`register_file_exchange_upload` is intentionally untouched (per #72's own Notes; redo is #74's job after #71 lands a spec release).

## Local review

Two-subagent local review circus passed before opening:

- `pr-review-toolkit:code-reviewer` — clean.
- `feature-dev:code-reviewer` (codebase-grounded second opinion, applied the sharpened classification test to each surviving kwarg) — clean.

## Test plan

- [x] All four removed kwargs trigger TypeError when passed (negative tests).
- [x] No remaining lowercase `must` / unformatted `http` strings in modified docs.
- [x] `uv run pytest` green.
- [x] `uv run ruff check` clean.
- [x] `uv run mypy src` clean.
- [ ] CI green (verify after push).
- [ ] Bot review clean (verify after push).
EOF
)"
```

- [ ] **Step 6: Verify CI green, then flip ready**

The harness watches CI completion. Once green AND both bot bodies say LGTM (not just check status — per `~/.claude/CLAUDE.md` "Reading bot review verdicts"), flip:

```bash
gh pr ready <PR-number>
```

Gemini auto-runs on flip-to-ready per the repo's `.gemini/config.yaml`. Address any findings per the iteration cap (one bot round after open).

---

## Out of scope

- `register_file_exchange_upload` kwarg cleanup → #74 (depends on #71's spec release).
- `markdown-vault-mcp` migration PR → tracked per-repo in the migration issue filed in Task 0.
- Builder-side `legacy_capability_shape` cleanup → blocked on #74 (still used by the upload helper).
- Template scaffold update → folds into template #131 if survey indicates need.

## Acceptance (from issue #72)

- [x] Each kwarg classified per the three-way split (design doc table).
- [x] Reclassifications applied: domain hooks documented (Task 8); overrides pulled into pvl-core (Tasks 3, 5, 6, 7); operator config remains on env vars (no new env vars; existing ones are sole source after kwarg removals).
- [x] Principle written up authoritatively (CLAUDE.md / README.md tightening in Tasks 9–10; worked example in Task 8).
- [x] Template impact addressed (Task 0 survey, child issue under #131 if needed).
