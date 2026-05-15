# Dual-Role e2e Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add end-to-end regression coverage for the produce-and-consume http dual-role capability path (issue #88) and for the default `accepts` wildcard reaching the wire.

**Architecture:** Two synchronous test functions plus one autouse env-isolation fixture, all added to `tests/test_file_exchange_capability_merge.py`. These are the first public-call-site tests in that file (its existing tests drive `_FileExchangeCapabilityBuilder` directly). No production code changes — #86 already shipped the dual-role fix; this plan only adds the missing regression tests.

**Tech Stack:** Python, pytest, `pytest.MonkeyPatch` for env, `fastmcp.FastMCP`.

---

## Notes for the implementer

- **This is regression coverage for already-correct code.** Both tests are expected to **PASS** on first run — the production behavior is already right (the #86 fix shipped). There is no red-then-green TDD cycle. To *prove* Test 1 genuinely guards the regression, Task 1 includes an **optional, local-only** verification step that temporarily reverts the #86 fix; that revert is never committed.
- All env is set via `monkeypatch.setenv`; the autouse `_clean_env` fixture (Task 1) clears every file-exchange env var so the two tests are hermetic regardless of order.
- The `consumer_sink` and `receiver` callables are **never invoked** — the capability is built at registration time. They only need the correct type and a non-`None` identity.

---

## Task 1: Env-isolation fixture + http dual-role e2e test

**Files:**
- Modify: `tests/test_file_exchange_capability_merge.py` (imports block at lines 7-13; append fixture + test at end of file)

- [ ] **Step 1: Extend the imports block**

The file currently starts (lines 1-13):

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
```

Replace that import section (everything from `from __future__` through the
`_FileExchangeCapabilityBuilder` import) with:

```python
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    FetchContext,
    FetchResult,
    register_file_exchange,
    register_file_exchange_upload,
)
from fastmcp_pvl_core._file_exchange_protocol import (
    _FileExchangeCapabilityBuilder,
)
```

- [ ] **Step 2: Append the autouse env-isolation fixture**

Add at the end of `tests/test_file_exchange_capability_merge.py`:

```python
# ---------------------------------------------------------------------------
# Public-call-site e2e tests (register_file_exchange* → capability shape)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test runs with no file-exchange env vars set.

    The builder-unit tests above do not read env; the public-call-site
    tests below do. Clearing keeps both kinds hermetic and order-independent.
    """
    for var in (
        "MCP_EXCHANGE_DIR",
        "MCP_EXCHANGE_ID",
        "MCP_EXCHANGE_NAMESPACE",
        "FASTMCP_TRANSPORT",
        "TEST_FE_TRANSPORT",
        "TEST_FE_BASE_URL",
        "TEST_FE_FILE_EXCHANGE_ENABLED",
        "TEST_FE_FILE_EXCHANGE_PRODUCE",
        "TEST_FE_FILE_EXCHANGE_CONSUME",
        "TEST_UP_TRANSPORT",
        "TEST_UP_BASE_URL",
        "TEST_UP_UPLOAD_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
```

- [ ] **Step 3: Append the dual-role e2e test**

Add immediately after the fixture:

```python
def test_register_file_exchange_dual_role_advertises_http_source_and_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A produce-and-consume server advertises BOTH http roles.

    Regression guard for #86: ``register_file_exchange`` used
    ``if produce / elif consume``, which hid the consumer (``sink``) tool
    on a server that did both. #86 changed it to two independent ``if``s.
    #86's own guard is at the builder-unit level
    (``test_builder_http_both_roles_emits_source_and_sink``); this is the
    integration-level twin, exercising the ``register_file_exchange``
    public call site where the fix actually lives.
    """
    monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
    monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")

    async def _sink(data: bytes, ctx: FetchContext) -> FetchResult:
        raise AssertionError("consumer_sink must not be invoked by this test")

    mcp = FastMCP(name="dual-role-probe")
    handle = register_file_exchange(
        mcp,
        namespace="image-mcp",
        env_prefix="TEST_FE",
        produces=("image/png",),
        consumer_sink=_sink,
    )

    assert handle.capability is not None
    http = handle.capability.to_capability_dict()["transfer_methods"]["http"]
    assert http == {
        "source": {"tool": "create_download_link"},
        "sink": {"tool": "fetch_file"},
    }
```

- [ ] **Step 4: Run the new test — expect PASS**

Run: `uv run pytest tests/test_file_exchange_capability_merge.py::test_register_file_exchange_dual_role_advertises_http_source_and_sink -v`
Expected: PASS (the #86 fix is already in `main`).

- [ ] **Step 5: Optional local-only regression-guard verification — DO NOT COMMIT**

To confirm the test genuinely catches the #86 bug, temporarily reintroduce it.
In `src/fastmcp_pvl_core/file_exchange.py`, find the two independent role
assignments inside `register_file_exchange` (search for
`builder.set_http_source` and `builder.set_http_sink` — they sit under
separate `if produce ...:` / `if consume:` statements). Temporarily change
the `if consume:` to `elif consume:` so it chains onto the producer `if`.

Run: `uv run pytest tests/test_file_exchange_capability_merge.py::test_register_file_exchange_dual_role_advertises_http_source_and_sink -v`
Expected: FAIL — the `sink` role is missing, so the dict assertion fails.

Then **revert** the `elif` back to `if`:

Run: `git checkout -- src/fastmcp_pvl_core/file_exchange.py`

Re-run the test to confirm it PASSES again. This step proves the guard works
and leaves no production change behind. If you skip this step, that is
acceptable — but do not commit any production-file modification.

- [ ] **Step 6: Commit**

```bash
git add tests/test_file_exchange_capability_merge.py
git commit -m "test(file-exchange): e2e dual-role http capability guard (refs #88)"
```

---

## Task 2: Default `accepts` wildcard reaches the wire

**Files:**
- Modify: `tests/test_file_exchange_capability_merge.py` (append one test at end of file)

- [ ] **Step 1: Append the default-accepts test**

Add at the end of `tests/test_file_exchange_capability_merge.py`, after the
dual-role test from Task 1:

```python
def test_register_file_exchange_upload_default_accepts_wildcard_on_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default ("*/*",) accepts reaches the http_upload.sink wire shape.

    Complements ``test_builder_http_upload_sink_includes_explicit_accepts``,
    which covers an *explicit* accepts value at the builder-unit level. This
    confirms the default propagates through the ``register_file_exchange_upload``
    public helper unchanged.
    """
    from fastmcp_pvl_core.file_exchange import _BUILDER_ATTR

    monkeypatch.setenv("TEST_UP_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UP_BASE_URL", "http://test.example")

    mcp = FastMCP(name="upload-accepts-probe")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UP",
        receiver=lambda record, body: {"ok": True},
    )

    builder = getattr(mcp, _BUILDER_ATTR)
    cap = builder.build()
    assert cap is not None
    sink = cap.to_capability_dict()["transfer_methods"]["http_upload"]["sink"]
    assert sink["accepts"] == ["*/*"]
```

The local `from ... import _BUILDER_ATTR` mirrors the existing
`test_builder_is_attached_per_instance_not_module_level` in this same file.
`register_file_exchange_upload` returns an `UploadHandle` (which carries no
`capability`), so the capability is read off the per-`mcp` builder — the
helper pushes its `set_http_upload_sink` contribution there and the `accepts`
argument (default `("*/*",)`) is passed verbatim.

- [ ] **Step 2: Run the new test — expect PASS**

Run: `uv run pytest tests/test_file_exchange_capability_merge.py::test_register_file_exchange_upload_default_accepts_wildcard_on_wire -v`
Expected: PASS.

- [ ] **Step 3: Run the whole test file**

Run: `uv run pytest tests/test_file_exchange_capability_merge.py -v`
Expected: every test PASSES — the pre-existing builder-unit tests plus the two
new public-call-site tests.

- [ ] **Step 4: Commit**

```bash
git add tests/test_file_exchange_capability_merge.py
git commit -m "test(file-exchange): e2e guard for default upload accepts wildcard (refs #88)"
```

---

## Task 3: Full quality gate

**Files:** none (verification only).

- [ ] **Step 1: Sync dependencies to match CI**

Run: `uv sync --all-extras`
Expected: completes without error.

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass (the prior baseline plus the 2 new tests).

- [ ] **Step 3: Formatting and lint**

Run: `uv run ruff format --check .`
Expected: all files already formatted.

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 4: Type check**

Run: `uv run mypy src`
Expected: `Success: no issues found` — unchanged from baseline, since no
`src/` file was modified.

- [ ] **Step 5: No production changes — verify**

Run: `git diff origin/main --stat`
Expected: only `tests/test_file_exchange_capability_merge.py` and the two
`docs/superpowers/` files (design + plan) appear. No `src/` file is listed.
If a `src/` file shows up, the Task 1 Step 5 revert was not undone — restore it
with `git checkout -- <file>` and re-run the gate.
