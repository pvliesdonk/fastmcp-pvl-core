# File-Exchange #148 — umbrella helpers + Tasks integration + adoption docs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the opinionated tool-registration layer (`register_file_exchange` setup + four per-role helpers) on top of the data-plane primitives already on `main`, plus README/docs/adoption-guide/CHANGELOG.

**Architecture:** One new private module `_helpers.py` carrying the five helpers + the `FileExchangeContext` dataclass. Provider/receiver are *decorators* wrapping downstream-owned tool bodies; fetcher/sender are *fully-generated* tool registrations. Every registered tool gets `taskSupport="optional"` metadata; the setup call declares the server-level `tasks.requests.tools.call` capability.

**Tech Stack:** Python 3.10–3.13, FastMCP (`mcp.tool(...)` + capability surface), `pydantic`, existing `_file_exchange` primitives (`download_provider_mint`, `upload_receiver_mint`, `download_fetcher_consume`, `upload_sender_consume`, `filesystem_*`, `register_file_exchange_routes`, `build_capability_token_store`, `select_source`, `select_sink`).

**Spec:** `docs/superpowers/specs/2026-05-28-file-exchange-148-umbrella-helpers-design.md` (commit `741440d`).

**Branch:** `feat/148-umbrella-helpers` off `main`. Single PR; the work is small enough not to need slice-decomposition.

**Discipline at every Task boundary:**

```bash
uv run pytest tests/_file_exchange tests/test_file_exchange_namespace.py -q
uv run --python 3.10 pytest tests/_file_exchange tests/test_file_exchange_namespace.py -q
uv run --python 3.13 pytest tests/_file_exchange tests/test_file_exchange_namespace.py -q
uv run ruff format --check .
uv run ruff check src tests
uv run mypy src
```

---

## File structure

- **`src/fastmcp_pvl_core/_file_exchange/_helpers.py`** (new) — `FileExchangeContext` dataclass + `register_file_exchange` + four per-role helpers. Target: ≤ 350 LOC.
- **`src/fastmcp_pvl_core/_file_exchange/__init__.py`** (modify) — re-export the new names.
- **`src/fastmcp_pvl_core/file_exchange.py`** (modify) — re-export at the public namespace.
- **`tests/_file_exchange/test_helpers.py`** (new) — unit tests per helper.
- **`tests/_file_exchange/test_helpers_e2e.py`** (new) — two-server provider→fetcher and receiver→sender end-to-end.
- **`tests/test_file_exchange_namespace.py`** (modify) — add the new public surface to the importability test.
- **`README.md`** (modify) — `## File-exchange extension` section.
- **`docs/file-exchange.md`** (new) — pvl-core implementation notes.
- **`docs/file-exchange-adoption.md`** (new) — four worked examples.
- **`CHANGELOG.md`** (modify) — unreleased entry.

---

## Task 0 — Verify FastMCP's task-capability surface

Before any code, empirically confirm two FastMCP behaviours so subsequent steps don't have to guess the right kwargs.

**Files:** none (verification only)

- [ ] **Step 0.1: Probe `mcp.tool` `annotations` shape.**

```bash
uv run python << 'EOF'
from fastmcp import FastMCP
mcp = FastMCP("probe")

async def f(report_id: str) -> str:
    return "ok"

# Pass a free-form annotations dict and inspect what lands on the tool.
t = mcp.tool(name="f", annotations={"taskSupport": "optional", "title": "F"})(f)
print("type:", type(t).__name__)
print("annotations attr:", getattr(t, "annotations", "NOT_PRESENT"))
print("meta attr:", getattr(t, "meta", "NOT_PRESENT"))
EOF
```

Record the output. **Decision rule:**
- If `annotations` carries `taskSupport` through to the registered tool, use `annotations={"taskSupport": "optional"}` in every helper.
- If `annotations` is restricted to known fields and silently drops unknowns, fall back to `meta={"taskSupport": "optional"}`.

- [ ] **Step 0.2: Probe server-level Tasks capability.**

```bash
uv run python << 'EOF'
from fastmcp import FastMCP
m1 = FastMCP("a")
m2 = FastMCP("b", tasks=True)
for name, m in (("default", m1), ("tasks=True", m2)):
    print(name, "_support_tasks_by_default:", getattr(m, "_support_tasks_by_default", "?"))
    print(name, "experimental_capabilities:", getattr(m, "experimental_capabilities", "?"))
EOF
```

Note which knob, if any, makes FastMCP declare the `tasks.requests.tools.call` capability **without** requiring the `fastmcp[tasks]`/`docket` extra. If FastMCP only declares the capability when `tasks=True` (which pulls in `docket`), the setup call must either:
- accept that the operator opts in to `docket` (set `tasks=True` on the `FastMCP` instance themselves before calling `register_file_exchange`, no pvl-core change required), or
- inject the capability manually by mutating the server's capability-set attribute.

Pick whichever path the empirical probe says is honest. Record the decision in the commit message for Task 1.

- [ ] **Step 0.3: Commit (no code, but the decisions inform Task 1).**

No commit — this is a verification step. The findings feed into the next tasks.

---

## Task 1 — `FileExchangeContext` + `register_file_exchange` setup call

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_helpers.py`
- Create: `tests/_file_exchange/test_helpers.py`

- [ ] **Step 1.1: Write the failing test.**

```python
# tests/_file_exchange/test_helpers.py
"""Tests for #148 umbrella helpers."""

from __future__ import annotations

from typing import BinaryIO

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _helpers
from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _Sink:
    async def store_artifact(
        self, artifact_id: str | None, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:  # pragma: no cover - unused in setup-only test
        raise AssertionError


class _Source:
    async def open_artifact(self, key: str):  # pragma: no cover - unused in setup-only test
        raise AssertionError


def _cfg() -> ServerConfig:
    return ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=1024,
    )


def test_register_file_exchange_returns_context_with_token_store_and_inputs():
    cfg = _cfg()
    mcp = FastMCP("t")
    source = _Source()
    sink = _Sink()
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=source,
        sink=sink,
    )
    assert isinstance(fxctx, _helpers.FileExchangeContext)
    assert isinstance(fxctx.token_store, CapabilityTokenStore)
    assert fxctx.base_url == "https://my.example"
    assert fxctx.config is cfg
    assert fxctx.source is source
    assert fxctx.sink is sink


def test_register_file_exchange_mounts_routes():
    cfg = _cfg()
    mcp = FastMCP("t")
    _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),
        sink=_Sink(),
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/d") for p in paths)
    assert any(p.startswith("/fx/u") for p in paths)


def test_register_file_exchange_source_only_mounts_download_only():
    cfg = _cfg()
    mcp = FastMCP("t")
    _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),
    )
    paths = {r.path for r in mcp.http_app().routes}
    assert any(p.startswith("/fx/d") for p in paths)
    assert not any(p.startswith("/fx/u") for p in paths)
```

- [ ] **Step 1.2: Run — expect ImportError.**

```bash
uv run pytest tests/_file_exchange/test_helpers.py -v
```

Expected: `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._helpers'`.

- [ ] **Step 1.3: Create `_helpers.py` with `FileExchangeContext` + `register_file_exchange`.**

```python
# src/fastmcp_pvl_core/_file_exchange/_helpers.py
"""Umbrella tool-registration helpers for the file-exchange extension (#148).

Provides one setup call (``register_file_exchange``) that wires the
cross-cutting infrastructure once, plus four per-role helpers
(``register_file_exchange_provider`` / ``_receiver`` / ``_fetcher``
/ ``_sender``). Provider and receiver are decorators on
downstream-owned tool bodies; fetcher and sender are fully-generated
tool registrations.

See ``docs/superpowers/specs/2026-05-28-file-exchange-148-umbrella-helpers-design.md``
for the architecture and the per-role contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastmcp_pvl_core._file_exchange._routes import register_file_exchange_routes
from fastmcp_pvl_core._file_exchange._tokens import (
    CapabilityTokenStore,
    build_capability_token_store,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource


@dataclass(frozen=True)
class FileExchangeContext:
    """Shared state produced by :func:`register_file_exchange` and consumed
    by the four per-role helpers.

    Frozen so a downstream that holds it cannot accidentally swap in a
    different token store or hook out from under in-flight registrations.
    """

    token_store: CapabilityTokenStore
    base_url: str
    config: ServerConfig
    source: ArtifactSource | None
    sink: ArtifactSink | None


def register_file_exchange(
    mcp: FastMCP,
    *,
    config: ServerConfig,
    base_url: str,
    source: ArtifactSource | None = None,
    sink: ArtifactSink | None = None,
) -> FileExchangeContext:
    """One-shot file-exchange setup: token store + routes + (later) Tasks
    capability declaration. Returns the context the per-tool helpers
    consume.

    Kwargs (per CLAUDE.md classification):

    - ``config`` (**config**): operator-side ``ServerConfig``.
    - ``base_url`` (**config**): origin URL the capability URLs encode.
    - ``source`` (**hook**): downstream's :class:`ArtifactSource` — required
      if any provider or sender helper will be registered later.
    - ``sink`` (**hook**): downstream's :class:`ArtifactSink` — required if
      any receiver or fetcher helper will be registered later.

    Mounting validation (``source``-or-``sink``, ``sink``-needs-``config``)
    is delegated to :func:`register_file_exchange_routes`; the per-tool
    helpers further raise ``ValueError`` at *their* registration time if
    they need a hook the context lacks.
    """
    token_store = build_capability_token_store(config)
    register_file_exchange_routes(
        mcp,
        token_store=token_store,
        source=source,
        sink=sink,
        config=config,
    )
    # Task 2 inserts the server-level Tasks capability declaration here.
    return FileExchangeContext(
        token_store=token_store,
        base_url=base_url,
        config=config,
        source=source,
        sink=sink,
    )
```

- [ ] **Step 1.4: Run — expect PASS.**

```bash
uv run pytest tests/_file_exchange/test_helpers.py -v
```

- [ ] **Step 1.5: Lint, type-check, commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_helpers.py \
        tests/_file_exchange/test_helpers.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): FileExchangeContext + register_file_exchange setup (#148)

Setup call that builds the CapabilityTokenStore, mounts the routes
via the existing register_file_exchange_routes, and returns the
FileExchangeContext the per-tool helpers will consume. Tasks-capability
declaration lands in the next commit per the Task 0 verification.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — Server-level Tasks capability declaration

Adds the `tasks.requests.tools.call` declaration inside `register_file_exchange` per Task 0's empirical finding.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_helpers.py`
- Modify: `tests/_file_exchange/test_helpers.py`

- [ ] **Step 2.1: Write the failing test.**

Append to `tests/_file_exchange/test_helpers.py`:

```python
def test_register_file_exchange_declares_tasks_capability():
    """The setup call advertises ``tasks.requests.tools.call`` so peers
    know the server accepts tools/call as a task submission (§14)."""
    cfg = _cfg()
    mcp = FastMCP("t")
    _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),
    )
    # The exact attribute/path is set by Task 0's empirical probe. Two
    # candidate predicates — pick the one that matches the FastMCP API
    # in scope:
    has_capability = (
        getattr(mcp, "_support_tasks_by_default", False) is True
        or "tasks.requests.tools.call"
        in getattr(mcp, "experimental_capabilities", {})
    )
    assert has_capability, (
        "register_file_exchange must declare tasks.requests.tools.call"
    )
```

- [ ] **Step 2.2: Run — expect failure.**

- [ ] **Step 2.3: Implement based on Task 0's finding.**

Inside `register_file_exchange`, after the `register_file_exchange_routes` call and before the `return`, add the capability declaration. Two shapes:

**Shape A — if FastMCP exposes `_support_tasks_by_default` as a settable attribute that drives the capability and does not require `docket`:**

```python
    # Declare the server-level Tasks capability so peers know this server
    # accepts tools/call as a task submission (§14).
    mcp._support_tasks_by_default = True
```

**Shape B — if the capability has to be added to `experimental_capabilities` directly:**

```python
    mcp.experimental_capabilities["tasks"] = {"requests": {"tools": {"call": True}}}
```

(The exact JSON path comes from §14's wire shape; cross-reference the upstream `mcp-file-exchange-ext` spec before locking the dict structure. If neither shape is correct after empirical probing, the verification step has produced enough information to write the third option directly.)

Cite the §14 reference in a `# §14:` inline comment so future readers can trace back.

- [ ] **Step 2.4: Run — expect PASS.**

- [ ] **Step 2.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_helpers.py \
        tests/_file_exchange/test_helpers.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): declare tasks.requests.tools.call capability (#148)

The setup call now advertises the server-level Tasks capability per
§14. Per-tool taskSupport metadata lands as each role helper is added.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — Provider decorator happy path

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_helpers.py`
- Modify: `tests/_file_exchange/test_helpers.py`

- [ ] **Step 3.1: Write the failing test.**

```python
async def test_provider_decorator_mints_transfer_handle():
    """The decorated tool returns a TransferHandle whose download
    descriptor's url is the minted capability URL; the source hook is
    NOT called at mint time."""
    import asyncio

    cfg = _cfg()
    mcp = FastMCP("t")
    source_calls: list[str] = []

    class _RecSource:
        async def open_artifact(self, key):  # pragma: no cover - mint only
            source_calls.append(key)
            raise AssertionError

    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        source=_RecSource(),
    )

    captured_args: dict = {}

    @_helpers.register_file_exchange_provider(mcp, "get_report", fxctx)
    async def get_report(report_id: str) -> tuple[ArtifactMetadata, str]:
        captured_args["report_id"] = report_id
        return ArtifactMetadata(size=11, mimeType="application/pdf"), report_id

    # Resolve the registered tool and invoke it.
    tool = await mcp.get_tool("get_report")
    handle = await tool.fn(report_id="rpt-1")
    from fastmcp_pvl_core._file_exchange._wire import TransferHandle

    assert isinstance(handle, TransferHandle)
    assert handle.artifact.size == 11
    assert handle.artifact.mimeType == "application/pdf"
    assert len(handle.sources) == 1
    download_url = handle.sources[0].url  # type: ignore[union-attr]
    assert download_url.startswith("https://route.test/fx/d/")
    assert source_calls == []
    # The user function received its domain arg.
    assert captured_args["report_id"] == "rpt-1"


def test_provider_decorator_without_source_raises_value_error():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        sink=_Sink(),  # sink only; no source
    )
    with pytest.raises(ValueError):

        @_helpers.register_file_exchange_provider(mcp, "get_report", fxctx)
        async def get_report(report_id: str) -> tuple[ArtifactMetadata, str]:
            return ArtifactMetadata(), report_id
```

(The first test calls `tool.fn(...)` directly; `mcp.get_tool` returns a `FunctionTool` whose `.fn` attribute is the wrapped callable. If FastMCP exposes a different invocation point, adapt — the test's intent is "run the decorated body once and assert it produces a TransferHandle".)

- [ ] **Step 3.2: Run — expect failure.**

- [ ] **Step 3.3: Implement.**

Append to `_helpers.py`:

```python
from fastmcp_pvl_core._file_exchange._download import download_provider_mint
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactMetadata,
    TransferHandle,
)


def register_file_exchange_provider(
    mcp: FastMCP,
    tool_name: str,
    fxctx: FileExchangeContext,
):
    """Decorator that turns a downstream-owned ``(metadata, key)``
    producer into a registered file-exchange provider tool.

    The decorated function's parameters become the MCP tool's parameters;
    the decorator mints a ``TransferHandle`` from the returned
    ``(metadata, key)`` and returns *that* as the tool's response.
    """
    if fxctx.source is None:
        raise ValueError(
            f"register_file_exchange_provider({tool_name!r}): fxctx has "
            "no source — set source= when calling register_file_exchange"
        )

    def _wrap(fn):
        # Wrapper preserves the inner signature (so the MCP tool schema
        # comes from ``fn`` directly) and mints the handle on the way out.
        from functools import wraps

        @wraps(fn)
        async def _wrapped(*args, **kwargs) -> TransferHandle:
            metadata, key = await fn(*args, **kwargs)
            return await download_provider_mint(
                metadata,
                key,
                token_store=fxctx.token_store,
                base_url=fxctx.base_url,
                ttl=fxctx.config.file_exchange_token_ttl,
                single_use=True,
            )

        # Task 11 adds the taskSupport annotation here.
        mcp.tool(name=tool_name)(_wrapped)
        return _wrapped

    return _wrap
```

- [ ] **Step 3.4: Run — expect PASS.**

- [ ] **Step 3.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_helpers.py \
        tests/_file_exchange/test_helpers.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): register_file_exchange_provider decorator (#148)

Decorator that turns a downstream (metadata, key) producer into a
registered MCP provider tool. The decorator preserves the user
function's signature (so the MCP tool schema comes from the inner fn)
and mints the TransferHandle on the way out. ValueError at decoration
time if fxctx.source is None — fail loudly at startup, not at first
peer call.

taskSupport metadata wiring lands in Task 11.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4 — Receiver decorator (symmetric to provider)

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_helpers.py`
- Modify: `tests/_file_exchange/test_helpers.py`

- [ ] **Step 4.1: Write the failing tests.**

```python
async def test_receiver_decorator_mints_intake_ticket():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
    )

    from fastmcp_pvl_core._file_exchange._wire import (
        ArtifactConstraints,
        IntakeTicket,
    )

    @_helpers.register_file_exchange_receiver(mcp, "accept_report", fxctx)
    async def accept_report(case_id: str) -> tuple[str, ArtifactConstraints | None]:
        return f"case-{case_id}-attachment", ArtifactConstraints(maxSize=1024)

    tool = await mcp.get_tool("accept_report")
    ticket = await tool.fn(case_id="42")
    assert isinstance(ticket, IntakeTicket)
    assert ticket.artifactId == "case-42-attachment"
    assert ticket.expected is not None
    assert ticket.expected.maxSize == 1024
    assert len(ticket.sinks) == 1
    assert ticket.sinks[0].url.startswith("https://route.test/fx/u/")  # type: ignore[union-attr]


async def test_receiver_decorator_no_expected_constraints_is_none():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
    )

    @_helpers.register_file_exchange_receiver(mcp, "accept_blob", fxctx)
    async def accept_blob(blob_id: str):
        return blob_id, None

    tool = await mcp.get_tool("accept_blob")
    ticket = await tool.fn(blob_id="b1")
    assert ticket.expected is None


def test_receiver_decorator_without_sink_raises_value_error():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),  # source only; no sink
    )
    with pytest.raises(ValueError):

        @_helpers.register_file_exchange_receiver(mcp, "accept_report", fxctx)
        async def accept_report(case_id: str):
            return f"c-{case_id}", None
```

- [ ] **Step 4.2: Run — expect failure.**

- [ ] **Step 4.3: Implement.**

Append to `_helpers.py`:

```python
from fastmcp_pvl_core._file_exchange._upload import upload_receiver_mint
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    IntakeTicket,
)


def register_file_exchange_receiver(
    mcp: FastMCP,
    tool_name: str,
    fxctx: FileExchangeContext,
):
    """Decorator that turns a downstream-owned ``(artifact_id, expected)``
    producer into a registered file-exchange receiver tool.
    """
    if fxctx.sink is None:
        raise ValueError(
            f"register_file_exchange_receiver({tool_name!r}): fxctx has "
            "no sink — set sink= when calling register_file_exchange"
        )

    def _wrap(fn):
        from functools import wraps

        @wraps(fn)
        async def _wrapped(*args, **kwargs) -> IntakeTicket:
            artifact_id, expected = await fn(*args, **kwargs)
            return await upload_receiver_mint(
                artifact_id,
                token_store=fxctx.token_store,
                base_url=fxctx.base_url,
                ttl=fxctx.config.file_exchange_token_ttl,
                expected=expected,
            )

        # Task 11 adds the taskSupport annotation here.
        mcp.tool(name=tool_name)(_wrapped)
        return _wrapped

    return _wrap
```

- [ ] **Step 4.4: Run — expect PASS.**

- [ ] **Step 4.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_helpers.py \
        tests/_file_exchange/test_helpers.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): register_file_exchange_receiver decorator (#148)

Symmetric to register_file_exchange_provider but on the upload-receiver
side. Decorator turns a downstream (artifact_id, expected) producer
into an MCP tool that returns an IntakeTicket. ValueError at decoration
time if fxctx.sink is None.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5 — Fetcher generated tool

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_helpers.py`
- Modify: `tests/_file_exchange/test_helpers.py`

- [ ] **Step 5.1: Write the failing tests.**

```python
async def test_fetcher_generated_tool_dispatches_to_download_consume(monkeypatch):
    """The fetcher tool selects a source descriptor and dispatches to the
    download fetcher when descriptor.transport == "download"."""
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
    )

    _helpers.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)

    # Build a TransferHandle with one download descriptor.
    from datetime import datetime, timezone
    from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
    from fastmcp_pvl_core._file_exchange._wire import (
        DownloadSource,
        TransferHandle,
    )

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(size=4),
        sources=[
            DownloadSource(
                transport="download",
                url="https://peer.test/fx/d/abc",
                expiresAt=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
        ],
    )

    captured: dict = {}

    async def fake_dl_fetch(h, d, s, *, config):
        captured["handle"] = h
        captured["descriptor"] = d
        captured["sink"] = s
        captured["config"] = config

    monkeypatch.setattr(_helpers, "download_fetcher_consume", fake_dl_fetch)

    tool = await mcp.get_tool("consume_transfer")
    result = await tool.fn(handle=handle)
    assert result is None
    assert captured["handle"] is handle
    assert captured["descriptor"].transport == "download"
    assert captured["sink"] is fxctx.sink
    assert captured["config"] is fxctx.config


def test_fetcher_without_sink_raises_value_error():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        source=_Source(),  # source only
    )
    with pytest.raises(ValueError):
        _helpers.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)


async def test_fetcher_no_usable_descriptor_raises_transfer_error():
    """When ``select_source`` returns None (no descriptor in the handle
    satisfies pvl-core's known transports), the generated tool raises
    FileExchangeTransferError(NO_USABLE_DESCRIPTOR)."""
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        sink=_Sink(),
    )
    _helpers.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)

    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._errors import (
        FileExchangeTransferError,
    )
    from fastmcp_pvl_core._file_exchange._wire import (
        TransferHandle,
        UnknownTransportDescriptor,
    )
    from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(size=4),
        sources=[UnknownTransportDescriptor(transport="future-transport")],
    )

    tool = await mcp.get_tool("consume_transfer")
    with pytest.raises(FileExchangeTransferError) as ei:
        await tool.fn(handle=handle)
    assert ei.value.code == TransferErrorCode.NO_USABLE_DESCRIPTOR
```

- [ ] **Step 5.2: Run — expect failure.**

- [ ] **Step 5.3: Implement.**

Append to `_helpers.py`:

```python
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._download import download_fetcher_consume
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._filesystem import filesystem_fetcher_consume
from fastmcp_pvl_core._file_exchange._selection import select_source
from fastmcp_pvl_core._file_exchange._wire import TransferHandle


def register_file_exchange_fetcher(
    mcp: FastMCP,
    tool_name: str,
    fxctx: FileExchangeContext,
) -> None:
    """Generate and register the file-exchange fetcher tool.

    The tool accepts a ``TransferHandle`` (Pydantic-validated by FastMCP's
    normal tool-arg handling), selects a usable source descriptor, and
    dispatches to the appropriate per-transport fetcher (``filesystem``
    or ``download``). Bytes land in ``fxctx.sink``; the tool returns
    ``None``.
    """
    if fxctx.sink is None:
        raise ValueError(
            f"register_file_exchange_fetcher({tool_name!r}): fxctx has "
            "no sink — set sink= when calling register_file_exchange"
        )

    async def _consume_transfer(handle: TransferHandle) -> None:
        descriptor = select_source(handle)
        if descriptor is None:
            raise FileExchangeTransferError(
                TransferErrorCode.NO_USABLE_DESCRIPTOR,
                transport=None,
                detail="no usable source in handle",
            )
        if descriptor.transport == "filesystem":
            await filesystem_fetcher_consume(
                handle, descriptor, fxctx.sink, config=fxctx.config
            )
        elif descriptor.transport == "download":
            await download_fetcher_consume(
                handle, descriptor, fxctx.sink, config=fxctx.config
            )
        else:
            # select_source filters UnknownTransportDescriptor; defensive
            # guard for any forward-compat wire payload that slips through.
            raise FileExchangeTransferError(
                TransferErrorCode.NO_USABLE_DESCRIPTOR,
                transport=descriptor.transport,
                detail=f"unsupported transport {descriptor.transport!r}",
            )

    _consume_transfer.__name__ = tool_name
    # Task 11 adds the taskSupport annotation here.
    mcp.tool(name=tool_name)(_consume_transfer)
```

(Look up `TransferErrorCode.NO_USABLE_DESCRIPTOR` — verify the exact enum spelling against `src/fastmcp_pvl_core/_file_exchange/_codes.py` and use whatever the actual constant is. If the code doesn't exist yet, use `TRANSFER_FAILED` as the closest existing match and flag in the commit that a new code is warranted.)

- [ ] **Step 5.4: Run — expect PASS.**

- [ ] **Step 5.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_helpers.py \
        tests/_file_exchange/test_helpers.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): register_file_exchange_fetcher generated tool (#148)

Not a decorator — pvl-core generates the tool body because its input
is the spec-defined TransferHandle and there's nothing domain-specific
for downstream to write. Tool selects a source descriptor via
select_source and dispatches to either filesystem_fetcher_consume or
download_fetcher_consume. Bytes land in fxctx.sink; returns None.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6 — Sender generated tool

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_helpers.py`
- Modify: `tests/_file_exchange/test_helpers.py`

- [ ] **Step 6.1: Write the failing tests.**

```python
async def test_sender_generated_tool_dispatches_to_upload_consume(monkeypatch):
    """The sender tool selects a sink descriptor and dispatches to the
    upload sender when descriptor.transport == "upload"."""
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        source=_Source(),
    )

    _helpers.register_file_exchange_sender(mcp, "send_to_receiver", fxctx)

    from datetime import datetime, timezone
    from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
    from fastmcp_pvl_core._file_exchange._wire import IntakeTicket, UploadSink

    ticket = IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId="art-1",
        sinks=[
            UploadSink(
                transport="upload",
                url="https://peer.test/fx/u/abc",
                expiresAt=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
        ],
    )

    captured: dict = {}

    async def fake_up_send(descriptor, source, key, *, config):
        captured["descriptor"] = descriptor
        captured["source"] = source
        captured["key"] = key
        captured["config"] = config

    monkeypatch.setattr(_helpers, "upload_sender_consume", fake_up_send)

    tool = await mcp.get_tool("send_to_receiver")
    result = await tool.fn(ticket=ticket, key="local-doc-key")
    assert result is None
    assert captured["descriptor"].transport == "upload"
    assert captured["source"] is fxctx.source
    assert captured["key"] == "local-doc-key"
    assert captured["config"] is fxctx.config


def test_sender_without_source_raises_value_error():
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://my.example",
        sink=_Sink(),  # sink only
    )
    with pytest.raises(ValueError):
        _helpers.register_file_exchange_sender(mcp, "send_to_receiver", fxctx)
```

- [ ] **Step 6.2: Run — expect failure.**

- [ ] **Step 6.3: Implement.**

Append to `_helpers.py`:

```python
from fastmcp_pvl_core._file_exchange._filesystem import filesystem_sender_consume
from fastmcp_pvl_core._file_exchange._selection import select_sink
from fastmcp_pvl_core._file_exchange._upload import upload_sender_consume
from fastmcp_pvl_core._file_exchange._wire import IntakeTicket


def register_file_exchange_sender(
    mcp: FastMCP,
    tool_name: str,
    fxctx: FileExchangeContext,
) -> None:
    """Generate and register the file-exchange sender tool.

    The tool accepts an ``IntakeTicket`` plus a ``key`` (the local
    identifier for the artifact being sent), selects a usable sink
    descriptor, and dispatches to the appropriate per-transport sender
    (``filesystem`` or ``upload``). Returns ``None``.
    """
    if fxctx.source is None:
        raise ValueError(
            f"register_file_exchange_sender({tool_name!r}): fxctx has "
            "no source — set source= when calling register_file_exchange"
        )

    async def _send_to_receiver(ticket: IntakeTicket, key: str) -> None:
        descriptor = select_sink(ticket)
        if descriptor is None:
            raise FileExchangeTransferError(
                TransferErrorCode.NO_USABLE_DESCRIPTOR,
                transport=None,
                detail="no usable sink in ticket",
            )
        if descriptor.transport == "filesystem":
            await filesystem_sender_consume(
                descriptor, fxctx.source, key, config=fxctx.config
            )
        elif descriptor.transport == "upload":
            await upload_sender_consume(
                descriptor, fxctx.source, key, config=fxctx.config
            )
        else:
            raise FileExchangeTransferError(
                TransferErrorCode.NO_USABLE_DESCRIPTOR,
                transport=descriptor.transport,
                detail=f"unsupported transport {descriptor.transport!r}",
            )

    _send_to_receiver.__name__ = tool_name
    # Task 11 adds the taskSupport annotation here.
    mcp.tool(name=tool_name)(_send_to_receiver)
```

(Same `NO_USABLE_DESCRIPTOR` verification as Task 5.)

- [ ] **Step 6.4: Run — expect PASS.**

- [ ] **Step 6.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_helpers.py \
        tests/_file_exchange/test_helpers.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): register_file_exchange_sender generated tool (#148)

Mirror of the fetcher helper. Generates a tool that takes an
IntakeTicket plus a local key, selects a sink descriptor, and
dispatches to filesystem_sender_consume or upload_sender_consume.
The key is the second tool argument because the caller decides which
local artifact to send.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7 — `taskSupport="optional"` metadata on every helper-registered tool

Plumbs the per-tool taskSupport annotation that Task 0's empirical probe identified.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_helpers.py`
- Modify: `tests/_file_exchange/test_helpers.py`

- [ ] **Step 7.1: Write the failing test.**

```python
async def test_helpers_inject_task_support_optional():
    """All four role helpers must annotate the registered tool with
    ``taskSupport="optional"`` (§14). The exact placement —
    ``annotations`` vs ``meta`` — is determined empirically per Task 0;
    this test reads back the surface that Task 0 identified."""
    cfg = _cfg()
    mcp = FastMCP("t")
    fxctx = _helpers.register_file_exchange(
        mcp,
        config=cfg,
        base_url="https://route.test",
        source=_Source(),
        sink=_Sink(),
    )

    @_helpers.register_file_exchange_provider(mcp, "p1", fxctx)
    async def p1(x: str) -> tuple[ArtifactMetadata, str]:
        return ArtifactMetadata(), x

    @_helpers.register_file_exchange_receiver(mcp, "r1", fxctx)
    async def r1(x: str):
        return x, None

    _helpers.register_file_exchange_fetcher(mcp, "f1", fxctx)
    _helpers.register_file_exchange_sender(mcp, "s1", fxctx)

    for name in ("p1", "r1", "f1", "s1"):
        tool = await mcp.get_tool(name)
        # Pick the attribute path that Task 0 verified — adjust as needed.
        ann = getattr(tool, "annotations", None) or {}
        meta = getattr(tool, "meta", None) or {}
        # Accept either landing.
        combined = {**(meta if isinstance(meta, dict) else {}),
                    **(dict(ann) if isinstance(ann, dict) else {})}
        # The exact JSON path under combined depends on the upstream wire
        # shape (e.g. {"taskSupport": "optional"} flat, or
        # {"execution": {"taskSupport": "optional"}} nested). Adapt the
        # predicate to match what Task 0's probe identified.
        assert (
            combined.get("taskSupport") == "optional"
            or combined.get("execution", {}).get("taskSupport") == "optional"
        ), f"tool {name!r} missing taskSupport annotation: {combined!r}"
```

- [ ] **Step 7.2: Run — expect failure.**

- [ ] **Step 7.3: Implement.**

In `_helpers.py`, add a module-level constant + helper:

```python
# §14: per-tool annotation declaring the file-exchange tool *may* be
# submitted as a task. Exact placement (annotations vs meta) verified
# in Task 0; the constant centralises the value for all four helpers.
_TASK_SUPPORT_ANNOTATION: dict[str, str] = {"taskSupport": "optional"}


def _tool_kwargs() -> dict[str, object]:
    """Common ``mcp.tool(...)`` kwargs for every helper-registered tool.

    The taskSupport annotation rides on ``annotations`` (or ``meta``,
    pending Task 0's empirical decision). Centralised so future changes
    only edit one site.
    """
    return {"annotations": _TASK_SUPPORT_ANNOTATION}
```

Then update each of the four helpers' `mcp.tool(name=tool_name)(...)` call sites to `mcp.tool(name=tool_name, **_tool_kwargs())(...)`.

If Task 0's probe says `meta` is the right place, replace `"annotations"` with `"meta"` inside `_tool_kwargs`. If the upstream spec calls for a nested `{"execution": {"taskSupport": "optional"}}` shape, change the constant accordingly.

- [ ] **Step 7.4: Run — expect PASS.**

- [ ] **Step 7.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/_helpers.py \
        tests/_file_exchange/test_helpers.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): inject taskSupport=optional on every role tool (#148)

Per §14: every tool the four role helpers register now carries the
taskSupport=optional annotation. Single _TASK_SUPPORT_ANNOTATION
constant + _tool_kwargs() helper centralises the value so future
spec-shape changes edit one site.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8 — Public re-exports

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Modify: `tests/test_file_exchange_namespace.py`

- [ ] **Step 8.1: Write the failing test.**

Append to `tests/test_file_exchange_namespace.py`:

```python
def test_file_exchange_umbrella_helpers_reexported():
    from fastmcp_pvl_core import file_exchange

    for name in (
        "FileExchangeContext",
        "register_file_exchange",
        "register_file_exchange_provider",
        "register_file_exchange_receiver",
        "register_file_exchange_fetcher",
        "register_file_exchange_sender",
    ):
        assert hasattr(file_exchange, name), name
        assert name in file_exchange.__all__, name
```

- [ ] **Step 8.2: Run — expect failure.**

- [ ] **Step 8.3: Add the re-exports.**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`:

1. Add a new import block (alphabetical placement — `_helpers` after `_filesystem`):

```python
from fastmcp_pvl_core._file_exchange._helpers import (
    FileExchangeContext,
    register_file_exchange,
    register_file_exchange_fetcher,
    register_file_exchange_provider,
    register_file_exchange_receiver,
    register_file_exchange_sender,
)
```

2. Insert each name into `__all__` in alphabetical order.

In `src/fastmcp_pvl_core/file_exchange.py`: mirror the same additions in the top import block and in `__all__`.

- [ ] **Step 8.4: Run — expect PASS.**

```bash
uv run pytest tests/_file_exchange tests/test_file_exchange_namespace.py -q
```

- [ ] **Step 8.5: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add src/fastmcp_pvl_core/_file_exchange/__init__.py \
        src/fastmcp_pvl_core/file_exchange.py \
        tests/test_file_exchange_namespace.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): re-export #148 umbrella helpers (#148)

FileExchangeContext + register_file_exchange + register_file_exchange_*
land in the public surface. Namespace test extended to cover the new
names.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9 — End-to-end provider → fetcher

**Files:**
- Create: `tests/_file_exchange/test_helpers_e2e.py`

- [ ] **Step 9.1: Write the e2e test.**

```python
# tests/_file_exchange/test_helpers_e2e.py
"""End-to-end: two pvl-core-built servers using the #148 umbrella helpers."""

from __future__ import annotations

import hashlib
import io
from typing import BinaryIO

import httpx
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _download, _helpers
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _InMemSource:
    def __init__(self, payload: bytes, mime: str = "application/pdf") -> None:
        self.payload = payload
        self.mime = mime

    async def open_artifact(self, key: str):
        return io.BytesIO(self.payload), ArtifactMetadata(mimeType=self.mime)


class _InMemSink:
    def __init__(self) -> None:
        self.received: tuple[str | None, ArtifactMetadata, bytes] | None = None

    async def store_artifact(
        self, artifact_id: str | None, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        self.received = (artifact_id, metadata, stream.read())


def _cfg() -> ServerConfig:
    return ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=1024 * 1024,
        file_exchange_http_timeout=30.0,
    )


async def test_e2e_provider_to_fetcher_via_umbrella(monkeypatch):
    """Server A registers a provider tool that offers a report; server B
    registers a fetcher tool. The fetcher tool, handed the provider's
    TransferHandle, pulls the bytes into B's sink."""
    payload = b"Provider over umbrella helpers PDF body"

    # Server A — offers reports.
    mcp_a = FastMCP("A")
    fxctx_a = _helpers.register_file_exchange(
        mcp_a,
        config=_cfg(),
        base_url="https://a.test",
        source=_InMemSource(payload),
    )

    @_helpers.register_file_exchange_provider(mcp_a, "get_report", fxctx_a)
    async def get_report(report_id: str) -> tuple[ArtifactMetadata, str]:
        return ArtifactMetadata(size=len(payload), mimeType="application/pdf"), report_id

    # Server B — fetches.
    mcp_b = FastMCP("B")
    sink_b = _InMemSink()
    fxctx_b = _helpers.register_file_exchange(
        mcp_b,
        config=_cfg(),
        base_url="https://b.test",
        sink=sink_b,
    )
    _helpers.register_file_exchange_fetcher(mcp_b, "consume_transfer", fxctx_b)

    # Patch guarded_stream so B's fetcher routes its HTTP GET to A's ASGI app.
    transport_a = httpx.ASGITransport(app=mcp_a.http_app())
    client_a = httpx.AsyncClient(transport=transport_a, base_url="https://a.test")

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_gs(method, url, *, config, transport, headers=None, content=None):
        resp = await client_a.request(method, url, headers=headers)

        class _R:
            def __init__(self, r: httpx.Response) -> None:
                self.status = r.status_code

            async def aiter_bytes(self):
                yield resp.content

        yield _R(resp)

    monkeypatch.setattr(_download, "guarded_stream", fake_gs)

    # Run the flow.
    provider_tool = await mcp_a.get_tool("get_report")
    handle = await provider_tool.fn(report_id="rpt-1")

    fetcher_tool = await mcp_b.get_tool("consume_transfer")
    await fetcher_tool.fn(handle=handle)

    await client_a.aclose()

    assert sink_b.received is not None
    aid, meta, body = sink_b.received
    assert body == payload
    assert meta.size == len(payload)
    assert meta.digest == "sha-256:" + hashlib.sha256(payload).hexdigest()
```

- [ ] **Step 9.2: Run — expect PASS.**

```bash
uv run pytest tests/_file_exchange/test_helpers_e2e.py -v
```

If the test fails for a transport-wiring reason (the `fake_gs` shape doesn't match what `download_fetcher_consume` actually expects from `guarded_stream`), inspect `tests/_file_exchange/test_download_e2e.py` for the canonical patch shape and align.

- [ ] **Step 9.3: Commit.**

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src
git add tests/_file_exchange/test_helpers_e2e.py
git commit -m "$(cat <<'EOF'
test(file-exchange): umbrella helpers provider→fetcher e2e (#148)

Two-server e2e exercising the full path through the new helpers:
provider decorator mints a TransferHandle on A, fetcher generated
tool consumes it on B, bytes land in B's sink. Patches
guarded_stream so B's HTTP GET routes to A's in-process ASGI app.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10 — End-to-end receiver → sender

**Files:**
- Modify: `tests/_file_exchange/test_helpers_e2e.py`

- [ ] **Step 10.1: Append the second e2e test.**

```python
async def test_e2e_receiver_to_sender_via_umbrella(monkeypatch):
    """Mirror of the download e2e: server B registers a receiver tool that
    mints an IntakeTicket; server A registers a sender tool that consumes
    that ticket and pushes A's bytes to B."""
    payload = b'{"hello":"umbrella"}'

    # Server B — accepts uploads.
    mcp_b = FastMCP("B")
    sink_b = _InMemSink()
    fxctx_b = _helpers.register_file_exchange(
        mcp_b,
        config=_cfg(),
        base_url="https://b.test",
        sink=sink_b,
    )

    @_helpers.register_file_exchange_receiver(mcp_b, "accept_doc", fxctx_b)
    async def accept_doc(case_id: str):
        return f"case-{case_id}-doc", None

    # Server A — sends.
    mcp_a = FastMCP("A")
    fxctx_a = _helpers.register_file_exchange(
        mcp_a,
        config=_cfg(),
        base_url="https://a.test",
        source=_InMemSource(payload, mime="application/json"),
    )
    _helpers.register_file_exchange_sender(mcp_a, "send_to_receiver", fxctx_a)

    # Patch A's outbound guard to land on B's ASGI app.
    transport_b = httpx.ASGITransport(app=mcp_b.http_app())
    client_b = httpx.AsyncClient(transport=transport_b, base_url="https://b.test")

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_gs(method, url, *, config, transport, headers=None, content=None):
        body = b""
        if content is not None:
            async for chunk in content:
                body += chunk
        resp = await client_b.request(method, url, headers=headers, content=body)

        class _R:
            status = resp.status_code

        yield _R()

    from fastmcp_pvl_core._file_exchange import _upload

    monkeypatch.setattr(_upload, "guarded_stream", fake_gs)

    # Run.
    receiver_tool = await mcp_b.get_tool("accept_doc")
    ticket = await receiver_tool.fn(case_id="42")

    sender_tool = await mcp_a.get_tool("send_to_receiver")
    await sender_tool.fn(ticket=ticket, key="local-doc")

    await client_b.aclose()

    assert sink_b.received is not None
    aid, meta, body = sink_b.received
    assert aid == "case-42-doc"
    assert body == payload
    assert meta.mimeType == "application/json"
```

- [ ] **Step 10.2: Run — expect PASS.**

- [ ] **Step 10.3: Commit.**

```bash
git add tests/_file_exchange/test_helpers_e2e.py
git commit -m "$(cat <<'EOF'
test(file-exchange): umbrella helpers receiver→sender e2e (#148)

Mirror of the provider→fetcher e2e on the upload side. Receiver
decorator on B mints an IntakeTicket; sender generated tool on A
pushes A's bytes to B's sink via the patched guarded_stream.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11 — README section

**Files:**
- Modify: `README.md`

- [ ] **Step 11.1: Add a `## File-exchange extension` section.**

Locate the existing README section ordering (likely after the auth / logging sections). Insert:

````markdown
## File-exchange extension

`fastmcp-pvl-core` ships the shared implementation of the
[`mcp-file-exchange-ext`](https://github.com/pvliesdonk/mcp-file-exchange-ext)
v0.1 protocol — capability-URL-based transfer of artifact bytes between
MCP servers. Four roles:

- **Provider**: this server *offers* an artifact via a tool; the response
  carries a `TransferHandle`.
- **Fetcher**: this server *pulls* an artifact handed to it by a peer.
- **Receiver**: this server *accepts* an artifact via a tool; the response
  carries an `IntakeTicket`.
- **Sender**: this server *pushes* an artifact to a peer.

Minimal wire-up:

```python
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange

mcp = FastMCP("my-server")
config = ...  # ServerConfig

fxctx = file_exchange.register_file_exchange(
    mcp,
    config=config,
    base_url="https://my-server.example",
    source=my_source,
)

@file_exchange.register_file_exchange_provider(mcp, "get_report", fxctx)
async def get_report(report_id: str):
    meta = await lookup_meta(report_id)
    return meta, report_id
```

See [`docs/file-exchange.md`](docs/file-exchange.md) for the implementation
notes and [`docs/file-exchange-adoption.md`](docs/file-exchange-adoption.md)
for one worked example per role.
````

- [ ] **Step 11.2: Commit.**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): file-exchange extension section (#148)

Front-door section for the file-exchange umbrella helpers — what the
extension is, four roles in one line each, one minimal wire-up
snippet, links to the implementation notes and the adoption guide.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12 — `docs/file-exchange.md` (pvl-core implementation notes)

**Files:**
- Create: `docs/file-exchange.md`

- [ ] **Step 12.1: Write the file.**

```markdown
# File-exchange extension — pvl-core implementation notes

This document describes pvl-core's *implementation* of the
[`mcp-file-exchange-ext`](https://github.com/pvliesdonk/mcp-file-exchange-ext)
v0.1 protocol. The wire spec lives upstream and is vendored as
`src/fastmcp_pvl_core/_file_exchange/_schema/file-exchange.json`. **This
file is not a wire spec** — anything that affects byte-on-the-wire
behaviour belongs upstream.

## Architecture

Four roles, two HTTP routes, one token store, four hook-mounted entry
points.

| Role     | Direction       | What it does                                |
|----------|-----------------|---------------------------------------------|
| Provider | Server → Peer   | Offer an artifact via a tool; response = `TransferHandle` |
| Fetcher  | Peer → Server   | Pull a peer's artifact handed to a fetcher tool |
| Receiver | Server → Peer   | Accept an artifact via a tool; response = `IntakeTicket` |
| Sender   | Peer → Server   | Push a local artifact to a peer's receiver  |

`register_file_exchange(mcp, ...)` mounts the two HTTP routes
(`/fx/d/{token}` GET and `/fx/u/{token}` PUT) and builds the
`CapabilityTokenStore` backed by the KV factory. The per-role helpers
register MCP tools on top.

## Setup walkthrough

```python
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig

mcp = FastMCP("my-server")
config = ServerConfig(
    kv_store_url="memory://",          # or redis://, etc.
    file_exchange_token_ttl=3600.0,
    file_exchange_max_artifact_size=10 * 1024 * 1024,
    file_exchange_allowed_networks=("10.0.0.0/8",),
    file_exchange_http_timeout=30.0,
)

fxctx = file_exchange.register_file_exchange(
    mcp,
    config=config,
    base_url="https://my-server.example",
    source=my_source,    # required if any provider or sender helper used
    sink=my_sink,        # required if any receiver or fetcher helper used
)

# Per-role registrations. See the adoption guide for one example per role.
```

## Operator knobs (`ServerConfig.file_exchange_*`)

| Field | Default | What it bounds |
|---|---|---|
| `file_exchange_token_ttl` | `3600.0` | Maximum lifetime of a capability token. Per-mint TTL is clamped to this ceiling. |
| `file_exchange_max_artifact_size` | `None` | Operator cap on body size (bytes); applied alongside per-mint `expected.maxSize`. |
| `file_exchange_allowed_networks` | `()` | CIDR allow-list for outbound fetcher/sender HTTP. Empty tuple denies all outbound — opt-in by design. |
| `file_exchange_http_timeout` | `30.0` | Connect/read/write timeout for outbound HTTP, in seconds. |

## Error model

All transport-layer failures raise
`fastmcp_pvl_core._file_exchange._errors.FileExchangeTransferError`
with a `code` from the §13 envelope (`TransferErrorCode`). The
umbrella helpers do not wrap or swallow these — they propagate
through the FastMCP tool layer and reach the MCP client as a typed
error response.

The §13 codes pvl-core emits:

- `TRANSFER_FAILED` — generic transport failure (network drop, disk
  full, sink raised).
- `NOT_ACCESSIBLE` — SSRF guard refusal; peer-side authz failure.
- `TOO_LARGE` — body exceeds the operator or per-mint cap.
- `SIZE_MISMATCH` — declared size vs. observed bytes disagree.
- `DIGEST_MISMATCH` — `Content-Digest` did not match the received bytes,
  or `requireDigest` lists a different algorithm than the client sent.
- `NO_USABLE_DESCRIPTOR` — selection algorithm found no descriptor in
  a handle/ticket whose transport pvl-core supports.

## See also

- `docs/file-exchange-adoption.md` — one worked example per role.
- `docs/superpowers/specs/2026-05-27-file-exchange-146-failure-modes.md`
  — the failure-mode matrix that drove the upload data plane.
- `docs/superpowers/specs/2026-05-28-content-digest-restructure.md` —
  the Content-Digest pipeline architecture.
- Upstream wire spec: <https://github.com/pvliesdonk/mcp-file-exchange-ext>.
```

- [ ] **Step 12.2: Commit.**

```bash
git add docs/file-exchange.md
git commit -m "$(cat <<'EOF'
docs: file-exchange.md implementation notes (#148)

pvl-core's implementation story for the file-exchange extension:
architecture sketch, setup walkthrough, operator-knob reference,
error model, pointers to the upstream wire spec and the local
failure-mode/restructure design docs. Explicitly NOT a wire spec.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13 — `docs/file-exchange-adoption.md` (adoption guide)

**Files:**
- Create: `docs/file-exchange-adoption.md`

- [ ] **Step 13.1: Write the file.**

```markdown
# File-exchange adoption guide

One minimal worked example per role. Each example uses an in-memory
source/sink so the example doesn't drag in a storage backend; replace
those with real implementations when adopting.

For the protocol overview, see [`docs/file-exchange.md`](file-exchange.md).

## 1. Provider — offering an artifact

A server that offers reports. The MCP tool's input is a domain-specific
`report_id`; the response is the `TransferHandle` peers pass to their
fetcher.

```python
import io
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core.file_exchange import ArtifactMetadata


class _MemSource:
    def __init__(self, reports: dict[str, bytes]) -> None:
        self._reports = reports

    async def open_artifact(self, key: str):
        body = self._reports[key]
        return io.BytesIO(body), ArtifactMetadata(mimeType="application/pdf")


mcp = FastMCP("report-server")
source = _MemSource({"rpt-1": b"…PDF bytes…"})
fxctx = file_exchange.register_file_exchange(
    mcp,
    config=ServerConfig(kv_store_url="memory://"),
    base_url="https://reports.example",
    source=source,
)


@file_exchange.register_file_exchange_provider(mcp, "get_report", fxctx)
async def get_report(report_id: str) -> tuple[ArtifactMetadata, str]:
    body = source._reports[report_id]  # real impl: lookup_meta(report_id)
    return ArtifactMetadata(size=len(body), mimeType="application/pdf"), report_id
```

## 2. Fetcher — importing a report from a peer

A server that consumes a `TransferHandle` handed to it via the
fetcher tool and stores the bytes in its sink.

```python
from typing import BinaryIO
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core.file_exchange import ArtifactMetadata


class _MemSink:
    def __init__(self) -> None:
        self.imports: dict[str, bytes] = {}

    async def store_artifact(
        self, artifact_id, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        self.imports[artifact_id or "anonymous"] = stream.read()


mcp = FastMCP("import-server")
sink = _MemSink()
fxctx = file_exchange.register_file_exchange(
    mcp,
    config=ServerConfig(
        kv_store_url="memory://",
        file_exchange_allowed_networks=("10.0.0.0/8",),
    ),
    base_url="https://imports.example",
    sink=sink,
)
file_exchange.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)
```

The peer-facing tool signature is `consume_transfer(handle: TransferHandle) -> None`. Wire-format dicts are validated by Pydantic automatically.

## 3. Receiver — accepting uploads

A server that mints an `IntakeTicket` for peers to push to.

```python
from typing import BinaryIO
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core.file_exchange import ArtifactConstraints, ArtifactMetadata


class _MemSink:
    def __init__(self) -> None:
        self.intake: dict[str, bytes] = {}

    async def store_artifact(
        self, artifact_id, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        if artifact_id is not None:
            self.intake[artifact_id] = stream.read()


mcp = FastMCP("intake-server")
sink = _MemSink()
fxctx = file_exchange.register_file_exchange(
    mcp,
    config=ServerConfig(
        kv_store_url="memory://",
        file_exchange_max_artifact_size=10 * 1024 * 1024,
    ),
    base_url="https://intake.example",
    sink=sink,
)


@file_exchange.register_file_exchange_receiver(mcp, "accept_attachment", fxctx)
async def accept_attachment(case_id: str) -> tuple[str, ArtifactConstraints | None]:
    return f"case-{case_id}-attachment", ArtifactConstraints(maxSize=10 * 1024 * 1024)
```

## 4. Sender — sending to a peer

A server that pushes a local artifact to a peer's receiver. The sender
tool takes the `IntakeTicket` the peer's receiver returned plus the
local `key` for the artifact being sent.

```python
import io
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core.file_exchange import ArtifactMetadata


class _MemSource:
    def __init__(self, docs: dict[str, bytes]) -> None:
        self._docs = docs

    async def open_artifact(self, key: str):
        return io.BytesIO(self._docs[key]), ArtifactMetadata(mimeType="application/json")


mcp = FastMCP("export-server")
source = _MemSource({"local-doc-1": b'{"hello":"world"}'})
fxctx = file_exchange.register_file_exchange(
    mcp,
    config=ServerConfig(
        kv_store_url="memory://",
        file_exchange_allowed_networks=("10.0.0.0/8",),
    ),
    base_url="https://exports.example",
    source=source,
)
file_exchange.register_file_exchange_sender(mcp, "send_to_intake", fxctx)
```

The peer-facing tool signature is
`send_to_intake(ticket: IntakeTicket, key: str) -> None`.

## A single server in multiple roles

A server can register all four helpers. Pass both `source=` and `sink=`
on the setup call and register the helpers you want.
```

- [ ] **Step 13.2: Commit.**

```bash
git add docs/file-exchange-adoption.md
git commit -m "$(cat <<'EOF'
docs: file-exchange adoption guide (#148)

One minimal worked example per role (provider, fetcher, receiver,
sender) plus a "single server in multiple roles" note. Each example
uses an in-memory hook so the example doesn't drag in a storage
backend.

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14 — CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 14.1: Inspect the existing CHANGELOG shape.**

```bash
head -40 CHANGELOG.md
```

Note the unreleased-section conventions (heading level, list style).

- [ ] **Step 14.2: Add an entry under the unreleased section.**

```markdown
### Added

- File-exchange umbrella helpers (#148): `register_file_exchange` setup
  call plus `register_file_exchange_provider` / `_receiver` / `_fetcher`
  / `_sender` per-role helpers. Provider and receiver are decorators on
  downstream-owned tool bodies; fetcher and sender are fully-generated
  tool registrations. Every helper-registered tool carries the §14
  `taskSupport="optional"` annotation; the setup call declares the
  server-level `tasks.requests.tools.call` capability.
- `docs/file-exchange.md` — pvl-core's implementation notes for the
  file-exchange extension.
- `docs/file-exchange-adoption.md` — one worked example per role.
```

- [ ] **Step 14.3: Commit.**

```bash
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs(changelog): file-exchange umbrella helpers entry (#148)

Refs #148.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 15 — Multi-Python sanity + open PR

**Files:** none (workflow only)

- [ ] **Step 15.1: Run the full quality gate on 3.10 and 3.13.**

```bash
uv run pytest tests/_file_exchange tests/test_file_exchange_namespace.py -q
uv run --python 3.10 pytest tests/_file_exchange tests/test_file_exchange_namespace.py -q
uv run --python 3.13 pytest tests/_file_exchange tests/test_file_exchange_namespace.py -q
uv run ruff format --check .
uv run ruff check src tests
uv run mypy src
```

Every command must succeed.

- [ ] **Step 15.2: Push.**

```bash
git push -u origin feat/148-umbrella-helpers
```

- [ ] **Step 15.3: Open the PR.**

```bash
gh pr create --title "feat(file-exchange): #148 umbrella helpers + Tasks integration + adoption docs" \
  --body "$(cat <<'EOF'
## Summary

Final EPIC #138 child task. Adds the opinionated tool-registration layer on top of the data-plane primitives merged across #167 / #168 / #169 / #170 / #171 / #173 / #175.

- \`register_file_exchange(mcp, *, config, base_url, source=None, sink=None) -> FileExchangeContext\` — setup call: builds the token store, mounts the routes via the existing \`register_file_exchange_routes\`, declares the server-level Tasks capability.
- \`@register_file_exchange_provider(mcp, tool_name, fxctx)\` — decorator: downstream returns \`(metadata, key)\`; the wrapper mints a \`TransferHandle\`.
- \`@register_file_exchange_receiver(mcp, tool_name, fxctx)\` — decorator: downstream returns \`(artifact_id, expected)\`; the wrapper mints an \`IntakeTicket\`.
- \`register_file_exchange_fetcher(mcp, tool_name, fxctx)\` — generates a tool that takes a \`TransferHandle\`, selects a source descriptor, dispatches to the right transport's fetcher.
- \`register_file_exchange_sender(mcp, tool_name, fxctx)\` — generates a tool that takes an \`IntakeTicket\` + a local key, selects a sink descriptor, dispatches to the right transport's sender.
- Every helper-registered tool carries \`taskSupport="optional"\` per §14.

## Documentation

- \`README.md\` — \`## File-exchange extension\` section.
- \`docs/file-exchange.md\` — pvl-core implementation notes.
- \`docs/file-exchange-adoption.md\` — one worked example per role.
- \`CHANGELOG.md\` — unreleased entry.

## Design

\`docs/superpowers/specs/2026-05-28-file-exchange-148-umbrella-helpers-design.md\`.

## Test plan

- [x] Per-role unit tests in \`tests/_file_exchange/test_helpers.py\`.
- [x] Two-server e2e tests (provider→fetcher and receiver→sender) in \`tests/_file_exchange/test_helpers_e2e.py\`.
- [x] Namespace test extended to cover the new public surface.
- [x] \`uv run pytest tests/_file_exchange tests/test_file_exchange_namespace.py\` on Python 3.10 and 3.13.
- [x] \`uv run ruff format --check . && uv run ruff check src tests\`.
- [x] \`uv run mypy src\`.

Closes #148.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `register_file_exchange` setup call | Task 1 (skeleton), Task 2 (Tasks capability) |
| `FileExchangeContext` dataclass | Task 1 |
| Provider decorator | Task 3 |
| Receiver decorator | Task 4 |
| Fetcher generated tool | Task 5 |
| Sender generated tool | Task 6 |
| `taskSupport="optional"` per tool | Task 7 |
| Public re-exports | Task 8 |
| Per-helper unit tests | Tasks 1, 3, 4, 5, 6 |
| End-to-end tests | Tasks 9, 10 |
| README section | Task 11 |
| `docs/file-exchange.md` | Task 12 |
| Adoption guide | Task 13 |
| CHANGELOG entry | Task 14 |
| Multi-Python verification + push | Task 15 |

**Placeholder scan:** The plan defers exact MCP wire-shape decisions (Task 0's `taskSupport` placement) to an explicit verification step rather than guessing. Tasks 2 and 7 reference Task 0's output. The `TransferErrorCode.NO_USABLE_DESCRIPTOR` constant is verified against the existing `_codes.py` in Tasks 5 and 6 — this is a verification instruction, not a placeholder.

**Type consistency:** `FileExchangeContext(token_store, base_url, config, source, sink)` field order is identical in Tasks 1, 3, 4, 5, 6, 9, 10. Provider returns `tuple[ArtifactMetadata, str]`, receiver returns `tuple[str, ArtifactConstraints | None]`, fetcher takes `TransferHandle`, sender takes `(IntakeTicket, str)` — consistent throughout.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-28-file-exchange-148-umbrella-helpers.md`.** Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks, fast iteration.

**2. Inline Execution** — execute in this session with `superpowers:executing-plans`, batch with checkpoints.

Which approach?
