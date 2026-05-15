# Sender-side http_upload Primitive — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the sender-side `http_upload` primitive — `register_file_exchange_upload_sender` + the `upload` MCP tool — that resolves an opaque `origin_id` to a file-like byte source via a downstream hook and POSTs the bytes to a receiver-issued URL.

**Architecture:** Two production files. `_file_exchange_protocol.py`: the capability builder gains a `source`-role emitter. `file_exchange.py`: the resolver hook types, the `UploadSenderHandle`, the `register_file_exchange_upload_sender` helper, and the `upload` tool. Purely additive — no existing helper, tool, or capability shape changes. The `upload` tool **returns** `transfer_failed`-shaped error dicts on failure (mirroring `fetch_file`, which returns rather than raises).

**Tech Stack:** Python 3.10–3.13, `fastmcp`, `httpx`, `uv`, `pytest`, `ruff`, `mypy`. Repo `/mnt/code/fastmcp-pvl-core`, branch `impl/upload-sender-issue-85`.

**Design doc:** `docs/superpowers/specs/2026-05-15-upload-sender-design.md` (issue #85).

---

## File Structure

- `src/fastmcp_pvl_core/_file_exchange_protocol.py` — `_FileExchangeCapabilityBuilder` gains `_http_upload_source_tool` field, `set_http_upload_source()`, and an extended `_build_http_upload_block()` emitting both `source` and `sink`.
- `src/fastmcp_pvl_core/file_exchange.py` — new: `ResolvedSource` dataclass, `ByteSourceResolver` type alias, `UploadSenderHandle` dataclass, `_DEFAULT_UPLOAD_SENDER_TOOL` constant, `register_file_exchange_upload_sender()` helper (with the `upload` tool nested inside it). Plus `__all__` additions.
- `src/fastmcp_pvl_core/__init__.py` — re-export the new public names.
- `tests/test_file_exchange_upload_sender.py` — new test file.

**Design decision resolved here:** the design doc §1 listed `enabled` as an `UploadSenderHandle` field while also stating the helper is never gated. Those conflict; the plan resolves it — the sender helper *always* registers (the `upload` tool needs only outbound HTTP, available on every transport), so `UploadSenderHandle` carries no `enabled` field.

---

## Task 1: Capability builder — `http_upload.source` emitter

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange_protocol.py` — `_FileExchangeCapabilityBuilder` (field list ~line 469; add a method after `set_http_upload_sink` ~line 508; rewrite `_build_http_upload_block` ~lines 552-565)
- Test: `tests/test_file_exchange_capability_merge.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_file_exchange_capability_merge.py`:

```python
def test_builder_http_upload_source_only() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_upload_source(tool_name="upload")
    cap = b.build()
    assert cap is not None
    assert cap.to_capability_dict()["transfer_methods"]["http_upload"] == {
        "source": {"tool": "upload"},
    }


def test_builder_http_upload_both_roles() -> None:
    """A server that both sends and receives advertises both sub-keys."""
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_upload_source(tool_name="upload")
    b.set_http_upload_sink(
        tool_name="create_upload_link", max_bytes=10, max_ttl_seconds=60
    )
    cap = b.build()
    assert cap is not None
    block = cap.to_capability_dict()["transfer_methods"]["http_upload"]
    assert block["source"] == {"tool": "upload"}
    assert block["sink"]["tool"] == "create_upload_link"
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_file_exchange_capability_merge.py -q`
Expected: FAIL — `_FileExchangeCapabilityBuilder` has no `set_http_upload_source`.

- [ ] **Step 3: Add the field**

In `_file_exchange_protocol.py`, in `_FileExchangeCapabilityBuilder`'s field list, add `_http_upload_source_tool` immediately before `_http_upload_sink_tool`:

```python
    _http_upload_source_tool: str | None = None
    _http_upload_sink_tool: str | None = None
```

- [ ] **Step 4: Add the `set_http_upload_source` method**

Immediately after the `set_http_upload_sink` method, add:

```python
    def set_http_upload_source(self, *, tool_name: str) -> None:
        """Record the ``http_upload`` sender (``source``) tool — POSTs bytes
        to a receiver-issued upload URL."""
        self._http_upload_source_tool = tool_name
```

- [ ] **Step 5: Rewrite `_build_http_upload_block` to emit both roles**

Replace the whole `_build_http_upload_block` method with:

```python
    def _build_http_upload_block(self) -> dict[str, Any] | None:
        """Build ``transfer_methods.http_upload`` with ``source`` / ``sink`` roles.

        Returns ``None`` when the server fills neither ``http_upload`` role.
        """
        block: dict[str, Any] = {}
        if self._http_upload_source_tool is not None:
            block["source"] = {"tool": self._http_upload_source_tool}
        if self._http_upload_sink_tool is not None:
            sink: dict[str, Any] = {
                "tool": self._http_upload_sink_tool,
                "max_bytes": self._http_upload_max_bytes,
                "max_ttl_seconds": self._http_upload_max_ttl_seconds,
            }
            if self._http_upload_accepts is not None:
                sink["accepts"] = list(self._http_upload_accepts)
            block["sink"] = sink
        return block or None
```

- [ ] **Step 6: Run the capability tests — expect pass**

Run: `uv run pytest tests/test_file_exchange_capability_merge.py -q`
Expected: PASS (the new tests plus all pre-existing ones — the `sink`-only path is unchanged).

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange_protocol.py tests/test_file_exchange_capability_merge.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): capability builder emits http_upload.source role (refs #85)

Add set_http_upload_source to _FileExchangeCapabilityBuilder and
extend _build_http_upload_block to emit source and/or sink — so a
sender-only server advertises http_upload: {source: {tool}} and a
dual-role server advertises both sub-keys. Additive; the sink-only
path is unchanged.

Refs #85.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: The sender helper, `upload` tool, types, handle, exports

**Files:**
- Modify: `src/fastmcp_pvl_core/file_exchange.py` — add the constant, types, handle, and helper near the existing upload code (after `register_file_exchange_upload`, ~line 1861); extend `__all__` (~line 1864)
- Modify: `src/fastmcp_pvl_core/__init__.py` — re-export the new public names
- Test: `tests/test_file_exchange_upload_sender.py` (new)

`file_exchange.py` already imports `httpx`, `inspect`, `asyncio`, `logging` (as `logger`), `dataclass`, `Any`, `Callable`, `Awaitable`; and defines `_ssrf_guard`, `FetchTransportError`, `_upload_transfer_failed`, `ExchangeURI`, `ExchangeURIError`, `env`, `_get_or_create_builder`, `_emit_capability`, `_DEFAULT_HTTP_FETCH_TIMEOUT`. Add `from typing import BinaryIO` if not already imported.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_file_exchange_upload_sender.py`:

```python
"""Tests for register_file_exchange_upload_sender — the http_upload sender."""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    ResolvedSource,
    register_file_exchange_upload_sender,
)


def _resolver(payload: bytes, content_type: str | None = None):
    """A byte_source returning the given payload for any origin_id."""

    def resolve(origin_id: str) -> ResolvedSource:
        return ResolvedSource(
            stream=io.BytesIO(payload),
            content_type=content_type,
            size_bytes=len(payload),
        )

    return resolve


def _mock_transport(handler):
    """Wrap a request handler as an httpx MockTransport, monkeypatched in."""
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_registration_adds_upload_tool() -> None:
    mcp = FastMCP(name="t")
    handle = register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=_resolver(b"x")
    )
    assert handle.namespace == "ns"
    assert handle.tool_name == "upload"
    assert "upload" in {t.name for t in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_capability_advertises_http_upload_source() -> None:
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=_resolver(b"x")
    )
    builder = mcp._pvl_file_exchange_builder  # type: ignore[attr-defined]
    cap = builder.build()
    assert cap is not None
    assert cap.to_capability_dict()["transfer_methods"]["http_upload"] == {
        "source": {"tool": "upload"},
    }


@pytest.mark.asyncio
async def test_upload_success_returns_status_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["body"] = request.content
        posted["content_type"] = request.headers.get("content-type")
        return httpx.Response(201, json={"saved": "ok"})

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND",
        byte_source=_resolver(b"PAYLOAD", content_type="application/pdf"),
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "https://recv.test/ns/uploads/tok", "origin_id": "doc-1"}
    )
    payload = result.structured_content or {}
    assert payload == {"status": 201, "body": {"saved": "ok"}}
    assert posted["body"] == b"PAYLOAD"
    assert posted["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_upload_content_type_param_overrides_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ct"] = request.headers.get("content-type")
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND",
        byte_source=_resolver(b"x", content_type="application/pdf"),
    )
    tool = await mcp.get_tool("upload")
    await tool.run(
        {
            "url": "https://recv.test/u/t",
            "origin_id": "d",
            "content_type": "text/markdown",
        }
    )
    assert seen["ct"] == "text/markdown"  # param wins over resolver


@pytest.mark.asyncio
async def test_upload_ssrf_guard_rejects_loopback_url() -> None:
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "http://169.254.169.254/u/t", "origin_id": "d"}
    )
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert payload["method"] == "http_upload"
    assert payload["origin_id"] == "d"


@pytest.mark.asyncio
async def test_upload_resolver_value_error_returns_transfer_failed() -> None:
    def bad_resolver(origin_id: str) -> ResolvedSource:
        raise ValueError("unknown origin_id")

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=bad_resolver
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "https://recv.test/u/t", "origin_id": "d"}
    )
    payload = result.structured_content or {}
    assert payload["error"] == "transfer_failed"
    assert "unknown origin_id" in payload["message"]


@pytest.mark.asyncio
async def test_upload_4xx_transfer_failed_body_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = {
        "error": "transfer_failed",
        "method": "http_upload",
        "receiver_server": "vault",
        "origin_id": "d",
        "message": "destination rejected",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=envelope)

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )
    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=_resolver(b"x")
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "https://recv.test/u/t", "origin_id": "d"}
    )
    assert (result.structured_content or {}) == envelope


@pytest.mark.asyncio
async def test_upload_async_resolver_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(
        "fastmcp_pvl_core.file_exchange._upload_sender_transport",
        httpx.MockTransport(handler),
        raising=False,
    )

    async def aresolve(origin_id: str) -> ResolvedSource:
        return ResolvedSource(stream=io.BytesIO(b"y"), content_type=None, size_bytes=1)

    mcp = FastMCP(name="t")
    register_file_exchange_upload_sender(
        mcp, namespace="ns", env_prefix="TEST_SEND", byte_source=aresolve
    )
    tool = await mcp.get_tool("upload")
    result = await tool.run(
        {"url": "https://recv.test/u/t", "origin_id": "d"}
    )
    assert (result.structured_content or {})["status"] == 200
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_file_exchange_upload_sender.py -q`
Expected: FAIL — `register_file_exchange_upload_sender` / `ResolvedSource` not importable.

- [ ] **Step 3: Add the constant, types, and handle**

In `file_exchange.py`, near the other `_DEFAULT_UPLOAD_*` constants (~line 1397), add:

```python
_DEFAULT_UPLOAD_SENDER_TOOL = "upload"
_DEFAULT_UPLOAD_SEND_TIMEOUT_SECONDS = 300.0
```

After the `PreLinkValidator` alias (~line 1402), add the resolver types:

```python
@dataclass(frozen=True)
class ResolvedSource:
    """The bytes a sender's ``upload`` tool will POST.

    Returned by a :data:`ByteSourceResolver`. ``stream`` is a file-like
    binary object pvl-core reads in chunks and streams into the POST
    body, then closes. ``content_type`` is the resource's MIME type if
    the downstream knows it (used unless the ``upload`` caller passes an
    explicit ``content_type``). ``size_bytes``, when known, lets pvl-core
    set a ``Content-Length`` header.
    """

    stream: BinaryIO
    content_type: str | None = None
    size_bytes: int | None = None


ByteSourceResolver = Callable[
    [str],
    "ResolvedSource | Awaitable[ResolvedSource]",
]


@dataclass(frozen=True)
class UploadSenderHandle:
    """Handle returned by :func:`register_file_exchange_upload_sender`.

    Attributes:
        namespace: The server's logical name.
        tool_name: The name the sender tool was registered under —
            always ``"upload"``; pvl-core owns this shape.
    """

    namespace: str
    tool_name: str
```

The `upload` tool issues outbound POSTs with a per-call `httpx.AsyncClient`. To let tests inject a mock, add a module-level transport hook near the constants:

```python
# Test seam: when set, the upload sender routes POSTs through this
# httpx transport instead of the real network. Production leaves it None.
# httpx.AsyncBaseTransport is what httpx.AsyncClient(transport=...) expects;
# httpx.MockTransport subclasses it, so tests can inject one.
_upload_sender_transport: httpx.AsyncBaseTransport | None = None
```

- [ ] **Step 4: Add `register_file_exchange_upload_sender` and the `upload` tool**

In `file_exchange.py`, after `register_file_exchange_upload` (~line 1861), add:

```python
def register_file_exchange_upload_sender(
    mcp: FastMCP,
    *,
    namespace: str,
    env_prefix: str,
    byte_source: ByteSourceResolver,
) -> UploadSenderHandle:
    """Wire the MCP File Exchange ``http_upload`` *sender* side onto ``mcp``.

    Registers an ``upload`` MCP tool that resolves an opaque ``origin_id``
    to a file-like byte source (via ``byte_source``) and POSTs the bytes
    to a receiver-issued upload URL. Counterpart to
    :func:`register_file_exchange_upload` (the receiver side).

    Unlike the receiver, the sender is **not** gated on transport or a
    base URL — POSTing needs only outbound HTTP, which a stdio MCP
    server has. The ``upload`` tool is always registered.

    Args:
        mcp: The :class:`fastmcp.FastMCP` server instance.
        namespace: Logical server name.
        env_prefix: Per-server env var prefix. The sender reads
            ``{PREFIX}_UPLOAD_SEND_TIMEOUT`` (seconds, float; default
            300) for the outbound-POST timeout.
        byte_source: Domain hook — ``(origin_id) -> ResolvedSource`` (sync
            or async). Resolves the sender's opaque ``origin_id`` to the
            bytes to push. Raise ``ValueError`` for a caller-facing
            rejection (unknown / not-permitted ``origin_id``); it is
            surfaced as a ``transfer_failed`` envelope. Any other
            exception is logged at ERROR and propagates as a server bug.

    Returns:
        An :class:`UploadSenderHandle`.
    """
    send_timeout = _DEFAULT_UPLOAD_SEND_TIMEOUT_SECONDS
    timeout_raw = env(env_prefix, "UPLOAD_SEND_TIMEOUT")
    if timeout_raw:
        try:
            send_timeout = float(timeout_raw)
        except ValueError as exc:
            raise ConfigurationError(
                f"{env_prefix}_UPLOAD_SEND_TIMEOUT must be a positive "
                f"number (seconds); got {timeout_raw!r}"
            ) from exc
        if send_timeout <= 0:
            raise ConfigurationError(
                f"{env_prefix}_UPLOAD_SEND_TIMEOUT must be positive; "
                f"got {send_timeout}"
            )

    builder = _get_or_create_builder(mcp, namespace=namespace)
    builder.set_http_upload_source(tool_name=_DEFAULT_UPLOAD_SENDER_TOOL)
    _emit_capability(mcp)

    @mcp.tool(name=_DEFAULT_UPLOAD_SENDER_TOOL, tags={"write"})
    async def upload(
        url: str,
        origin_id: str,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        r"""POST the bytes identified by ``origin_id`` to a receiver-issued URL.

        Args:
            url: The receiver-issued POST endpoint, from a prior
                ``create_upload_link`` call.
            origin_id: The sender's opaque stable handle for the bytes to
                push. Validated against the spec's segment rules (no
                ``/``, ``\``, ``.``, ``..``, control bytes,
                leading/trailing whitespace); resolved to bytes by the
                server's ``byte_source`` hook.
            content_type: Optional MIME type for the POST ``Content-Type``
                header. If omitted, the resolver's reported type is used,
                else ``application/octet-stream``.

        Returns:
            On success, ``{"status": <int>, "body": <receiver response>}``.
            On failure, a ``transfer_failed`` envelope.
        """
        try:
            ExchangeURI.validate_segment(origin_id, role="json_param")
        except ExchangeURIError as exc:
            return _upload_transfer_failed(
                receiver_server="", origin_id=origin_id, message=str(exc)
            )
        try:
            _ssrf_guard(url)
        except FetchTransportError as exc:
            return _upload_transfer_failed(
                receiver_server="", origin_id=origin_id, message=str(exc)
            )
        try:
            if inspect.iscoroutinefunction(byte_source):
                resolved = await byte_source(origin_id)
            else:
                resolved = await asyncio.to_thread(byte_source, origin_id)
                if inspect.isawaitable(resolved):
                    resolved = await resolved
        except ValueError as exc:
            return _upload_transfer_failed(
                receiver_server="", origin_id=origin_id, message=str(exc)
            )
        except Exception:
            logger.exception(
                "byte_source resolver raised non-ValueError (origin_id=%r) "
                "— server-side bug, not a caller error",
                origin_id,
            )
            raise

        effective_ct = (
            content_type or resolved.content_type or "application/octet-stream"
        )
        headers = {"Content-Type": effective_ct}
        if resolved.size_bytes is not None:
            headers["Content-Length"] = str(resolved.size_bytes)

        def _chunks() -> Iterator[bytes]:
            try:
                while True:
                    chunk = resolved.stream.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                resolved.stream.close()

        # One POST attempt only — the receiver consumes its URL token on
        # the first attempt (success or failure); a retry returns 404.
        client_kwargs: dict[str, Any] = {
            "timeout": send_timeout,
            "follow_redirects": False,
        }
        if _upload_sender_transport is not None:
            client_kwargs["transport"] = _upload_sender_transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.post(url, content=_chunks(), headers=headers)
        except httpx.HTTPError as exc:
            return _upload_transfer_failed(
                receiver_server="",
                origin_id=origin_id,
                message=f"upload POST failed: {exc}",
            )

        status = resp.status_code
        ct = resp.headers.get("content-type", "")
        if "json" in ct.lower():
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
        else:
            body = resp.text

        if 200 <= status < 300:
            return {"status": status, "body": body}
        # 4xx/5xx: pass a receiver transfer_failed envelope through verbatim;
        # otherwise synthesise one.
        if isinstance(body, dict) and body.get("error") == "transfer_failed":
            return body
        return _upload_transfer_failed(
            receiver_server="",
            origin_id=origin_id,
            message=f"upload POST returned HTTP {status}",
        )

    return UploadSenderHandle(
        namespace=namespace, tool_name=_DEFAULT_UPLOAD_SENDER_TOOL
    )
```

Add `Iterator` to the `typing` / `collections.abc` imports if not present (alongside the existing `Callable` / `Awaitable` import).

- [ ] **Step 5: Extend `__all__`**

In `file_exchange.py`, add to the `__all__` list (keep it sorted):

```python
    "ByteSourceResolver",
    "ResolvedSource",
    "UploadSenderHandle",
    "register_file_exchange_upload_sender",
```

- [ ] **Step 6: Re-export from the package `__init__.py`**

In `src/fastmcp_pvl_core/__init__.py`, add `register_file_exchange_upload_sender`, `UploadSenderHandle`, `ResolvedSource`, `ByteSourceResolver` to the `from .file_exchange import (...)` block and to the package `__all__`, mirroring exactly how `register_file_exchange_upload` / `UploadHandle` are imported and exported there.

- [ ] **Step 7: Run the sender tests — expect pass**

Run: `uv run pytest tests/test_file_exchange_upload_sender.py -q`
Expected: PASS — all 7 tests. If a test fails, fix the implementation (not the test, unless the test itself is wrong).

- [ ] **Step 8: Commit**

```bash
git add src/fastmcp_pvl_core/file_exchange.py src/fastmcp_pvl_core/__init__.py tests/test_file_exchange_upload_sender.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): add register_file_exchange_upload_sender + upload tool (refs #85)

The sender side of the http_upload method. register_file_exchange_upload_sender
registers an `upload` MCP tool that resolves an opaque origin_id to a
file-like byte source via the byte_source domain hook, then POSTs the
bytes to a receiver-issued URL. SSRF-guards the URL, streams the body,
sets Content-Type (param > resolver > octet-stream) and Content-Length
when known, makes one POST attempt, and returns {status, body} or a
transfer_failed envelope (mirroring fetch_file — returns, never raises).
Not gated on transport: a stdio server can register it.

Refs #85.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Full-suite sweep and quality gate

- [ ] **Step 1: Sync and run the full gate**

```bash
uv sync --all-extras
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

- [ ] **Step 2: Fix any finding**

The change is purely additive, so the full suite should already be green; `ruff` / `mypy` findings are most likely in the new code — a missing import (`BinaryIO`, `Iterator`), an unused import, or a type annotation gap. Fix each minimally. `ruff format` issues → `uv run ruff format .`. Re-run the failing command until all four are clean.

- [ ] **Step 3: Commit (only if Step 2 changed anything)**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore(file-exchange): quality-gate fixes for the upload sender (refs #85)

<one line naming what was fixed>

Refs #85.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If Steps 1–2 found nothing to fix, skip the commit — report only.

---

## Task 4: Verify, preflight-circus, draft PR

- [ ] **Step 1: Verify**

Confirm: `grep -rn 'register_file_exchange_upload_sender\|UploadSenderHandle\|ResolvedSource' src/fastmcp_pvl_core/__init__.py` shows the new exports; the `upload` tool is registered (covered by the tests); `git diff origin/main..HEAD --stat` touches only `_file_exchange_protocol.py`, `file_exchange.py`, `__init__.py`, and the two test files (plus the design + plan docs).

- [ ] **Step 2: Invoke the `preflight-circus` skill**

Run `preflight-circus` on the cumulative diff (`git diff origin/main...HEAD`). Address every finding at confidence ≥ 80 before opening the PR; re-run until clean.

- [ ] **Step 3: Open the draft PR**

```bash
git push -u origin impl/upload-sender-issue-85
gh pr create --draft --title "impl: sender-side http_upload primitive — register_file_exchange_upload_sender (#85)" --body "$(cat <<'EOF'
## Summary

Implements the sender (`source`) side of the `http_upload` transfer method — the counterpart of #74's receiver side, against the v0.3.0 spec corrected by #93.

- **`register_file_exchange_upload_sender(mcp, *, namespace, env_prefix, byte_source)`** — three domain-hook kwargs; registers the `upload` MCP tool. Not gated on transport: a stdio MCP server can register it (sending needs only outbound HTTP).
- **The `upload` tool** — `url` / `origin_id` / `content_type`; resolves the opaque `origin_id` to a file-like byte source via the `byte_source` hook, SSRF-guards the URL, streams the body in one POST, returns `{status, body}` on success or a `transfer_failed` envelope on failure (mirroring `fetch_file` — returns, never raises).
- **`ByteSourceResolver` / `ResolvedSource(stream, content_type, size_bytes)`** — the domain hook; sync and async; a resolver `ValueError` becomes a `transfer_failed` envelope.
- **Capability** — `set_http_upload_source` on the builder; advertises `http_upload: {source: {tool: "upload"}}`, and both sub-keys when paired with #74's receiver.

Purely additive — no existing helper, tool, or capability shape changes.

## Test plan

- [x] `uv run pytest` — green (new `test_file_exchange_upload_sender.py` + builder tests).
- [x] `uv run ruff format --check .` / `ruff check .` / `mypy src` — clean.
- [ ] CI green.
- [ ] Bot review clean.

Closes #85.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Watch CI + claude-review; flip ready when clean**

Read `claude-review`'s body, address findings within the one-round cap, flip ready (`gh pr ready <N>`) once CI is green and the bot says LGTM.

---

## Summary

Three substantive tasks, two production files. Task 1 adds the `http_upload.source` capability emitter. Task 2 adds the resolver types, the handle, the helper, and the `upload` tool. Task 3 is the quality gate. Task 4 verifies and opens the PR.

## What changes

- `_file_exchange_protocol.py` — `_FileExchangeCapabilityBuilder` gains a `source`-role emitter (additive; `sink`-only behaviour unchanged).
- `file_exchange.py` — new types, handle, helper, and `upload` tool.
- `__init__.py` — new re-exports.

## What does NOT change

- `register_file_exchange_upload` (the #74 receiver), `register_file_exchange`, the `http`/`exchange` methods.
- The capability shape for any existing role.
- The spec.

## Local review

`preflight-circus` runs on the cumulative diff; clean at the ≥80 bar before the PR opens.

## Test plan

- [ ] `uv run pytest` green on the full suite.
- [ ] `ruff format --check` / `ruff check` / `mypy src` clean.
- [ ] The `upload` tool: `{status, body}` on 2xx; `transfer_failed` envelope on SSRF rejection, resolver `ValueError`, transport failure, and non-envelope 4xx/5xx; a receiver `transfer_failed` 4xx body passed through verbatim.
- [ ] `content_type` precedence: param > resolver > `application/octet-stream`.
- [ ] `Content-Length` set when `size_bytes` known.
- [ ] Sync and async `byte_source` both work.
- [ ] Capability advertises `http_upload: {source: {tool: "upload"}}`; dual-role both-sub-keys case works.
- [ ] CI green; bot review clean.

## Out of scope

- Any change to the receiver side (#74), the `http` method, or `exchange`.
- Retry / resumable upload — the one-time-token contract makes a failed POST terminal.

## Acceptance (from #85 / the design doc)

- [ ] `register_file_exchange_upload_sender` — three domain-hook kwargs; `{PREFIX}_UPLOAD_SEND_TIMEOUT` operator env var.
- [ ] The `upload` tool — `url` / `origin_id` / `content_type`; `{status, body}` return; `transfer_failed` on failure; one POST attempt.
- [ ] `ByteSourceResolver` / `ResolvedSource`; sync and async; resolver `ValueError` → `transfer_failed`.
- [ ] SSRF guard on the POST URL; streamed body; `Content-Type` precedence; `Content-Length` from `size_bytes`.
- [ ] `set_http_upload_source` on the builder; `http_upload.source = {tool: "upload"}`; dual-role case works.
- [ ] The helper is not gated on HTTP-server capability.
- [ ] Tests pass; `ruff` + `mypy` clean.
