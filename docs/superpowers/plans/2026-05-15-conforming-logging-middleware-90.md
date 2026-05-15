# Conforming Tool-Aware Logging Middleware (#90) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace FastMCP's `LoggingMiddleware` + `TimingMiddleware` + `ErrorHandlingMiddleware` in `wire_middleware_stack` with one conforming, tool-aware logging middleware that emits family-standard log lines with timing carried inline.

**Architecture:** A new `RequestLoggingMiddleware` (in a new module `_logging_middleware.py`) overrides only `on_message` — the single outermost dispatch hook — so each MCP message yields exactly one `*_started` line and one terminal `*_completed`/`*_failed` line. Tool calls surface `tool=<name>`; non-tool messages use a `request_*`/`notification_*` vocabulary. `wire_middleware_stack` collapses to installing this one middleware and becomes zero-kwarg.

**Tech Stack:** Python `logging`, FastMCP middleware API, pytest (`asyncio_mode = auto`), ruff, mypy.

**Issue:** [#90](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/90). **Spec:** `docs/superpowers/specs/2026-05-15-logging-conformance-90-91-design.md`.

This is PR 2 of 2. **Branch fresh from `main` after PR 1 (#91) has merged** — `git fetch origin main && git checkout -b <branch> origin/main`. PR 1 adds the README `### Logging` section that Task 4 below extends.

## File Structure

- **Create** `src/fastmcp_pvl_core/_logging_middleware.py` — `RequestLoggingMiddleware` and its formatting helpers. Single responsibility: turn an MCP message lifecycle into conforming log records.
- **Modify** `src/fastmcp_pvl_core/_middleware.py` — `wire_middleware_stack` rewired to install only `RequestLoggingMiddleware`; zero-kwarg signature; new module + function docstrings.
- **Create** `tests/test_logging_middleware.py` — behaviour tests for `RequestLoggingMiddleware`.
- **Modify** `tests/test_middleware.py` — rewritten for the one-middleware stack.
- **Modify** `README.md` — extend the `### Logging` section with the event vocabulary.

`RequestLoggingMiddleware` stays private (underscore module, not exported from `__init__.py`) — `wire_middleware_stack` is the public entry point; downstream never instantiates the middleware directly.

---

### Task 1: Create `RequestLoggingMiddleware`

**Files:**
- Create: `tests/test_logging_middleware.py`
- Create: `src/fastmcp_pvl_core/_logging_middleware.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_logging_middleware.py` with this exact content:

```python
"""Tests for RequestLoggingMiddleware."""

from __future__ import annotations

import json
import logging

import pytest
from fastmcp.server.middleware.middleware import MiddlewareContext

from fastmcp_pvl_core._logging_middleware import RequestLoggingMiddleware

_LOGGER_NAME = "fastmcp.middleware.requests"


class _ToolParams:
    """Minimal stand-in for CallToolRequestParams — only ``.name`` is read."""

    def __init__(self, name: str) -> None:
        self.name = name


def _context(*, method, message=None, type_="request", source="client"):
    return MiddlewareContext(
        message=message, method=method, type=type_, source=source
    )


async def _ok_call_next(context):
    return "result"


def _failing_call_next(exc):
    async def _call_next(context):
        raise exc

    return _call_next


async def test_tool_call_started_and_completed(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = await mw.on_message(ctx, _ok_call_next)
    assert result == "result"
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0].startswith("tool_call_started ")
    assert "tool=read" in messages[0]
    assert "method=tools/call" in messages[0]
    assert "source=client" in messages[0]
    assert messages[1].startswith("tool_call_completed ")
    assert "tool=read" in messages[1]
    assert "duration_ms=" in messages[1]


async def test_request_uses_method_vocabulary(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="initialize")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0].startswith("request_started ")
    assert "method=initialize" in messages[0]
    assert messages[1].startswith("request_completed ")
    assert "method=initialize" in messages[1]
    assert "duration_ms=" in messages[1]


async def test_notification_uses_notification_vocabulary(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="notifications/initialized", type_="notification")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)
    assert caplog.records[0].getMessage().startswith("notification_started ")
    assert caplog.records[1].getMessage().startswith("notification_completed ")


async def test_tool_call_failed_line(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("bad section here")))
    failed = caplog.records[-1]
    msg = failed.getMessage()
    assert msg.startswith("tool_call_failed ")
    assert "tool=read" in msg
    assert "duration_ms=" in msg
    assert "error_type=ValueError" in msg
    assert 'error="bad section here"' in msg
    assert failed.levelno == logging.ERROR


async def test_failed_error_value_unquoted_when_no_whitespace(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("oneword")))
    assert "error=oneword" in caplog.records[-1].getMessage()


async def test_include_traceback_attaches_exc_info(caplog):
    mw = RequestLoggingMiddleware(include_traceback=True)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("boom")))
    assert caplog.records[-1].exc_info is not None


async def test_no_traceback_when_include_traceback_false(caplog):
    mw = RequestLoggingMiddleware(include_traceback=False)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("boom")))
    assert caplog.records[-1].exc_info is None


async def test_unknown_tool_name_falls_back(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=object())
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)
    assert "tool=unknown" in caplog.records[0].getMessage()


async def test_structured_mode_emits_json(caplog):
    mw = RequestLoggingMiddleware(structured=True)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await mw.on_message(ctx, _ok_call_next)
    started = json.loads(caplog.records[0].getMessage())
    assert started["event"] == "tool_call_started"
    assert started["tool"] == "read"
    assert started["method"] == "tools/call"
    assert started["source"] == "client"
    completed = json.loads(caplog.records[1].getMessage())
    assert completed["event"] == "tool_call_completed"
    assert completed["tool"] == "read"
    assert "duration_ms" in completed


async def test_structured_mode_failed_json(caplog):
    mw = RequestLoggingMiddleware(structured=True)
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError):
            await mw.on_message(ctx, _failing_call_next(ValueError("bad section here")))
    failed = json.loads(caplog.records[-1].getMessage())
    assert failed["event"] == "tool_call_failed"
    assert failed["error_type"] == "ValueError"
    assert failed["error"] == "bad section here"


async def test_exception_is_reraised_unchanged(caplog):
    mw = RequestLoggingMiddleware()
    ctx = _context(method="tools/call", message=_ToolParams("read"))
    sentinel = ValueError("propagate me")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        with pytest.raises(ValueError) as excinfo:
            await mw.on_message(ctx, _failing_call_next(sentinel))
    assert excinfo.value is sentinel
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_logging_middleware.py -v`
Expected: collection error / all FAIL with `ModuleNotFoundError: No module named 'fastmcp_pvl_core._logging_middleware'`.

- [ ] **Step 3: Implement `RequestLoggingMiddleware`**

Create `src/fastmcp_pvl_core/_logging_middleware.py` with this exact content:

```python
"""Conforming, tool-aware request-logging middleware.

Emits family-standard log lines for every MCP message: a bare
snake_case event name as the first token, followed by ``key=value``
pairs. Tool calls surface the tool name via ``tool=<name>``; request
duration is carried inline on the terminal (``*_completed`` /
``*_failed``) line. This middleware replaces FastMCP's
``LoggingMiddleware`` and ``TimingMiddleware`` in
:func:`fastmcp_pvl_core.wire_middleware_stack`.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

_DEFAULT_LOGGER_NAME = "fastmcp.middleware.requests"


def _duration_ms(start: float) -> float:
    """Elapsed wall-clock milliseconds since *start*, rounded to 2 dp."""
    return round((time.perf_counter() - start) * 1000, 2)


def _render_value(value: object) -> str:
    """Render a field value for the rich (text) output mode.

    Strings containing whitespace are double-quoted so the surrounding
    ``key=value`` structure stays unambiguous; everything else renders
    bare.
    """
    text = str(value)
    if any(char.isspace() for char in text):
        return '"' + text + '"'
    return text


def _render_fields(fields: dict[str, object]) -> str:
    """Join an ordered field mapping into ``key=value`` text."""
    return " ".join(
        key + "=" + _render_value(value) for key, value in fields.items()
    )


class RequestLoggingMiddleware(Middleware):
    """Logs every MCP message as a conforming, tool-aware event pair.

    Overrides only :meth:`on_message` — the single outermost dispatch
    hook — so each message produces exactly one ``*_started`` line and
    one terminal (``*_completed`` / ``*_failed``) line. Overriding a
    method-specific hook in addition would double-log.

    Tool calls (``tools/call``) use the ``tool_call_*`` event vocabulary
    and carry ``tool=<name>``; every other message uses ``request_*`` or
    ``notification_*`` keyed by ``method=``.

    Args:
        structured: When ``True``, emit one JSON object per record (for
            log aggregators). When ``False`` (default), emit
            bare-event-name-first ``key=value`` text.
        include_traceback: When ``True``, ``*_failed`` records carry
            ``exc_info`` so the log handler renders a traceback.
        logger: Logger to emit through. Defaults to a logger named
            ``fastmcp.middleware.requests``.
    """

    def __init__(
        self,
        *,
        structured: bool = False,
        include_traceback: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.structured = structured
        self.include_traceback = include_traceback
        self.logger = logger or logging.getLogger(_DEFAULT_LOGGER_NAME)

    async def on_message(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        is_tool_call = context.method == "tools/call"
        if is_tool_call:
            event_base = "tool_call"
            id_key = "tool"
            id_val: str = getattr(context.message, "name", None) or "unknown"
        else:
            event_base = context.type
            id_key = "method"
            id_val = context.method or "unknown"

        started_fields: dict[str, object] = {id_key: id_val}
        if is_tool_call:
            started_fields["method"] = "tools/call"
        started_fields["source"] = context.source
        self._emit(event_base + "_started", started_fields, logging.INFO)

        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            self._emit(
                event_base + "_failed",
                {
                    id_key: id_val,
                    "duration_ms": _duration_ms(start),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                logging.ERROR,
                exc_info=self.include_traceback,
            )
            raise
        self._emit(
            event_base + "_completed",
            {id_key: id_val, "duration_ms": _duration_ms(start)},
            logging.INFO,
        )
        return result

    def _emit(
        self,
        event: str,
        fields: dict[str, object],
        level: int,
        *,
        exc_info: bool = False,
    ) -> None:
        """Emit one conforming record in the configured output mode."""
        if self.structured:
            payload: dict[str, object] = {"event": event}
            payload.update(fields)
            self.logger.log(level, "%s", json.dumps(payload), exc_info=exc_info)
        else:
            self.logger.log(
                level, "%s %s", event, _render_fields(fields), exc_info=exc_info
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_logging_middleware.py -v`
Expected: all 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_logging_middleware.py tests/test_logging_middleware.py
git commit -m "$(cat <<'EOF'
feat(logging): add conforming tool-aware RequestLoggingMiddleware

Refs #90

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Rewrite `test_middleware.py` for the one-middleware stack

This task writes the failing tests for the rewired `wire_middleware_stack`; Task 3 makes them pass. Splitting them keeps each commit a clean red→green step.

**Files:**
- Modify: `tests/test_middleware.py` (full rewrite)

- [ ] **Step 1: Replace `tests/test_middleware.py` entirely**

Overwrite `tests/test_middleware.py` with this exact content:

```python
"""Tests for wire_middleware_stack."""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from fastmcp_pvl_core import wire_middleware_stack
from fastmcp_pvl_core._logging_middleware import RequestLoggingMiddleware


def _request_logging_mws(mcp: FastMCP) -> list[RequestLoggingMiddleware]:
    """Return the RequestLoggingMiddleware instances wire_middleware_stack added.

    FastMCP may pre-install its own middlewares (e.g. DereferenceRefsMiddleware);
    this filters to only the one wire_middleware_stack is responsible for.
    """
    return [m for m in mcp.middleware if isinstance(m, RequestLoggingMiddleware)]


def test_installs_single_request_logging_middleware():
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    assert len(_request_logging_mws(mcp)) == 1


def test_rich_mode_when_rich_unset(monkeypatch):
    monkeypatch.delenv("FASTMCP_ENABLE_RICH_LOGGING", raising=False)
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    assert _request_logging_mws(mcp)[0].structured is False


def test_rich_mode_when_rich_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("FASTMCP_ENABLE_RICH_LOGGING", "true")
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    assert _request_logging_mws(mcp)[0].structured is False


def test_structured_mode_when_rich_disabled(monkeypatch):
    monkeypatch.setenv("FASTMCP_ENABLE_RICH_LOGGING", "false")
    mcp = FastMCP(name="t")
    wire_middleware_stack(mcp)
    assert _request_logging_mws(mcp)[0].structured is True


def test_include_traceback_inferred_from_debug_log_level():
    """include_traceback is inferred True when the root logger is at DEBUG."""
    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.DEBUG)
    try:
        mcp = FastMCP(name="t")
        wire_middleware_stack(mcp)
        assert _request_logging_mws(mcp)[0].include_traceback is True
    finally:
        root.setLevel(prev)


def test_include_traceback_inferred_off_when_root_above_debug():
    """include_traceback is inferred False when the root logger sits above DEBUG."""
    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.WARNING)
    try:
        mcp = FastMCP(name="t")
        wire_middleware_stack(mcp)
        assert _request_logging_mws(mcp)[0].include_traceback is False
    finally:
        root.setLevel(prev)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_middleware.py -v`
Expected: FAIL — `wire_middleware_stack` still installs three FastMCP middlewares and not `RequestLoggingMiddleware`, so `_request_logging_mws` returns an empty list and `[0]` raises `IndexError`.

---

### Task 3: Rewire `wire_middleware_stack`

**Files:**
- Modify: `src/fastmcp_pvl_core/_middleware.py` (full rewrite)

- [ ] **Step 1: Replace `src/fastmcp_pvl_core/_middleware.py` entirely**

Overwrite `src/fastmcp_pvl_core/_middleware.py` with this exact content:

```python
"""FastMCP middleware stack installation.

Installs the conforming request-logging middleware on a FastMCP
instance. The rich-vs-structured output mode is controlled by the
``FASTMCP_ENABLE_RICH_LOGGING`` environment variable.
"""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

from fastmcp_pvl_core._env import parse_bool
from fastmcp_pvl_core._logging_middleware import RequestLoggingMiddleware


def wire_middleware_stack(mcp: FastMCP) -> None:
    """Install the standard logging middleware on a FastMCP instance.

    Installs a single :class:`RequestLoggingMiddleware`, which emits
    family-conforming, tool-aware log lines — a bare event name as the
    first token, then ``key=value`` pairs — with request timing carried
    inline on the terminal line.

    Output mode is selected by ``FASTMCP_ENABLE_RICH_LOGGING`` (default
    ``true``): rich mode emits ``key=value`` text; structured mode emits
    one JSON object per record for log aggregators.

    Traceback inclusion on failure records is inferred from the root
    logger — tracebacks are emitted when it is at ``DEBUG`` or below.
    Call this *after* CLI / log-level setup so the inference sees the
    right level.

    Args:
        mcp: The :class:`FastMCP` instance to install middleware on.
    """
    include_traceback = logging.getLogger().isEnabledFor(logging.DEBUG)
    rich_raw = os.environ.get("FASTMCP_ENABLE_RICH_LOGGING", "true")
    mcp.add_middleware(
        RequestLoggingMiddleware(
            structured=not parse_bool(rich_raw),
            include_traceback=include_traceback,
        )
    )
```

- [ ] **Step 2: Run the middleware tests to verify they pass**

Run: `uv run pytest tests/test_middleware.py tests/test_logging_middleware.py -v`
Expected: all tests PASS.

- [ ] **Step 3: Run the full test suite to catch regressions**

Run: `uv run pytest`
Expected: all tests PASS. The previous `wire_middleware_stack` kwargs (`include_traceback`, `transform_errors`) are gone; confirm no other test or module passed them — `grep -rn "transform_errors" tests/ src/` should return nothing.

- [ ] **Step 4: Commit**

```bash
git add src/fastmcp_pvl_core/_middleware.py tests/test_middleware.py
git commit -m "$(cat <<'EOF'
feat(logging): rewire wire_middleware_stack to the conforming middleware

Collapse the three-middleware stack (ErrorHandling, Timing, Logging) to
a single RequestLoggingMiddleware. wire_middleware_stack becomes
zero-kwarg: transform_errors is dropped with ErrorHandlingMiddleware,
and include_traceback is inferred internally from the root log level.

Refs #90

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Extend the README `### Logging` section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Append the event vocabulary to the `### Logging` section**

PR 1 (#91) added a `### Logging` section ending with the paragraph that begins `Both reappear at \`DEBUG\``. In `README.md`, immediately after that paragraph and before the next `###` heading, insert:

````markdown
`wire_middleware_stack` installs a single conforming request-logging
middleware. Every line it emits starts with a bare snake_case event name,
followed by `key=value` pairs, with request timing carried inline:

```
tool_call_started   tool=read method=tools/call source=client
tool_call_completed tool=read duration_ms=68.57
tool_call_failed    tool=read duration_ms=109.84 error_type=ValueError error="Section '1.3' not found"
```

Non-tool messages use a generic `request_*` / `notification_*` vocabulary
keyed by `method=`. Set `FASTMCP_ENABLE_RICH_LOGGING=false` to emit one JSON
object per record instead of `key=value` text — for log aggregators such as
the ELK stack or Splunk.

````

- [ ] **Step 2: Verify the section reads coherently**

Run: `grep -n "tool_call_started\|### Logging" README.md`
Expected: the `tool_call_started` example sits inside the existing `### Logging` section.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(logging): document the conforming event vocabulary

Refs #90

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Local checks and open the PR

**Files:** none (verification + PR).

- [ ] **Step 1: Run the full local check suite**

Run each, expecting success:

```bash
uv sync --all-extras
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: pytest all green; ruff format reports no changes; ruff check passes; mypy reports no issues. Fix anything that fails and re-run before proceeding.

- [ ] **Step 2: Run the pre-flight review circus**

Invoke the `preflight-circus` skill on the cumulative diff `BASE..HEAD` (where `BASE = $(git merge-base HEAD origin/main)`). Address every finding at confidence >= 80 locally — do not push until the skill's status is `clean`.

- [ ] **Step 3: Push and open the PR as a draft**

```bash
git push -u origin HEAD
gh pr create --draft --title "feat(logging): conforming tool-aware logging middleware" --body "$(cat <<'EOF'
## Summary

- Add `RequestLoggingMiddleware` — a conforming, tool-aware logging middleware that emits bare-event-name-first `key=value` lines (or JSON when `FASTMCP_ENABLE_RICH_LOGGING=false`), with request timing carried inline and `tool=<name>` surfaced on tool calls.
- Rewire `wire_middleware_stack` to install only this middleware, dropping FastMCP's `LoggingMiddleware`, `TimingMiddleware`, and `ErrorHandlingMiddleware`.
- `wire_middleware_stack` is now zero-kwarg: `transform_errors` is dropped with `ErrorHandlingMiddleware`; `include_traceback` is inferred internally from the root log level.
- A failed tool call now emits exactly one conforming `*_failed` line (plus traceback) instead of four redundant records.

Closes #90

## Test plan

- [ ] `uv run pytest` — new `RequestLoggingMiddleware` tests and rewritten `wire_middleware_stack` tests pass.
- [ ] Tool-call logs carry `tool=<name>`.
- [ ] A failed tool call emits one `*_failed` line, not three.
- [ ] Rich and structured (JSON) output modes both conform.

## Cross-repo impact

`wire_middleware_stack` is consumed by `markdown-vault-mcp`, `image-generation-mcp`, and `paperless-mcp`. Deliberate behaviour change: the `Request X completed in Yms` line is replaced by the conforming `*_completed duration_ms=` line; any call site passing `include_traceback` / `transform_errors` must drop those arguments. Downstream adoption tracked at the `markdown-vault-mcp#493` successor.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Verify bot reviews and CI**

After the push, wait for `claude-review` and CI. Read the `claude-review` body (not just the check status). If a bot finds anything, re-invoke `preflight-circus` on the new diff before pushing a fix. Once local review was clean, bot bodies say LGTM, and CI is green, flip the PR ready with `gh pr ready <N>`. Merging is human-only.

---

## Self-Review

- **Spec coverage:**
  - *New module / `RequestLoggingMiddleware` / single `on_message` / event vocabulary / output modes / error path* → Task 1 (implementation + 11 behaviour tests).
  - *`wire_middleware_stack` rewire / three→one collapse / zero-kwarg / docstrings* → Tasks 2–3.
  - *README event-vocabulary docs* → Task 4. *No `docs/specs/` change* — correctly omitted (implementation, not wire protocol).
  - *Acceptance checklist* — single middleware installed (Task 2 `test_installs_single_request_logging_middleware`); `tool=<name>` surfaced (Task 1 `test_tool_call_started_and_completed`); bare-event-first / no f-strings, rich + structured (Task 1 text + JSON tests); one `*_failed` line (Task 1 `test_tool_call_failed_line` asserts exactly one failed record); zero-kwarg (Task 3 Step 3 grep). All map to tasks.
- **Placeholder scan:** no TBD / TODO / vague steps; every code and doc step shows full content.
- **Type consistency:** `RequestLoggingMiddleware` constructor (`structured`, `include_traceback`, `logger`) and public attributes (`.structured`, `.include_traceback`, `.logger`) are referenced identically in Task 1 (definition), Task 1 tests, and Task 2 tests. `_render_value` / `_render_fields` / `_duration_ms` / `_emit` are defined and called consistently. The `on_message` signature matches FastMCP's base `Middleware.on_message`.
- **Cross-PR ordering:** Task 4 depends on PR 1's README `### Logging` section; the plan header mandates branching from `main` after PR 1 merges, and Task 4 anchors its insertion on PR 1's closing paragraph rather than a line number.
