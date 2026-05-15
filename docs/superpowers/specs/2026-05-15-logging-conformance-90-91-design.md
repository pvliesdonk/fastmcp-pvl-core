# Logging conformance — issues #90 & #91

**Date:** 2026-05-15
**Issues:** [#90](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/90),
[#91](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/91)
**Origin:** downstream logging audit `pvliesdonk/markdown-vault-mcp#493`

## Problem

The downstream audit inspected the runtime log stream of a deployed server
against the family logging standard (codified in the template-owned
`CLAUDE.md`): a bare snake_case **event name as the first token**, then
`key=value` pairs, lazy `%s` formatting (never f-strings), standard
`DEBUG`/`INFO`/`WARNING`/`ERROR` levels.

First-party code conforms. Two sources do not, and together they dominate the
operator log stream:

1. **The FastMCP middleware stack** wired by `wire_middleware_stack`
   (`TimingMiddleware`, `LoggingMiddleware`/`StructuredLoggingMiddleware`,
   `ErrorHandlingMiddleware`) emits prose-first, f-string, `event=`-prefixed
   lines — and never surfaces the tool name, so every tool call logs
   identically as `method=tools/call`. A single failed tool call emits four
   records for one failure. → **issue #90**
2. **Third-party transport/SDK loggers** (`uvicorn.access`,
   `mcp.server.lowlevel.server`) emit non-conforming `INFO` lines we cannot
   rewrite — one or two extra noise lines per request. → **issue #91**

## Scope & PR structure

Two independent changes shipped as **two separate PRs** from one worktree:

- **PR 1 — #91** (`_logging.py`): demote the noisy third-party loggers.
  Small, no conflict surface; landed first.
- **PR 2 — #90** (`_logging_middleware.py` + `_middleware.py`): the conforming
  tool-aware middleware. Branched fresh from `main` after PR 1.

Each PR closes its own issue.

---

## Issue #91 — demote noisy third-party loggers

### Change

In `src/fastmcp_pvl_core/_logging.py`, add a module constant:

```python
_NOISY_THIRD_PARTY_LOGGERS = ("uvicorn.access", "mcp.server.lowlevel.server")
```

In `configure_logging_from_env`, after the root level is resolved and
`configure_logging(level)` has run, demote each noisy logger by one effective
notch:

```python
noisy_level = logging.NOTSET if level == logging.DEBUG else logging.WARNING
for name in _NOISY_THIRD_PARTY_LOGGERS:
    logging.getLogger(name).setLevel(noisy_level)
```

### Behaviour

- **Root at INFO (default) / WARNING / ERROR** — the two loggers are pinned to
  `WARNING`; their `INFO` chatter (`uvicorn.access` request lines,
  `Processing request of type ...`) drops, while genuine warnings/errors still
  pass.
- **Root at DEBUG** — the loggers are set to `NOTSET`, restoring parent
  inheritance so all records flow again.
- **Idempotent** — re-resolving on every call flips the loggers cleanly
  between `WARNING` and `NOTSET`; no state leak across calls.
- `uvicorn.error` is deliberately **not** demoted — it carries real bind /
  startup failures.

The noisy-logger set is a plain module constant. An override hook (extra arg /
env var) is explicitly out of scope; it can be added later if a downstream
needs it.

### Documentation

`configure_logging_from_env`'s docstring gains a short paragraph documenting
the demotion. README logging section notes that `uvicorn.access` and the MCP
SDK request lines are demoted below `INFO` and reappear at `DEBUG`.

### Acceptance

- [ ] At `INFO` (default), no `uvicorn.access` (`INFO: <ip> - "POST ..."`)
      lines and no `Processing request of type` lines.
- [ ] At `DEBUG`, both reappear.
- [ ] `uvicorn.error` and genuine warnings/errors unaffected at all levels.
- [ ] Docstring + README updated.

---

## Issue #90 — conforming tool-aware logging middleware

### Maintainer decisions (resolved during brainstorming)

- **`ErrorHandlingMiddleware` is dropped entirely.** It was installed with
  `transform_errors=False`, making it a pure logging side-effect that
  duplicates the new middleware's `*_failed` line. Its `error_counts` (never
  surfaced anywhere) and the unused `transform_errors` path go with it. The
  new middleware owns the catch/traceback role.
- **New class location:** a new module `_logging_middleware.py`, keeping
  `_middleware.py` as pure stack-wiring.
- **Output modes:** the new middleware keeps the `FASTMCP_ENABLE_RICH_LOGGING`
  switch — bare-event-name `key=value` text in rich mode, an equivalent JSON
  object in structured mode (preserving the log-aggregation capability
  `StructuredLoggingMiddleware` provided).
- **Kwarg surface:** `wire_middleware_stack` becomes **zero-kwarg**. Both
  `transform_errors` (dead — only fed `ErrorHandlingMiddleware`) and
  `include_traceback` are removed. Per the `CLAUDE.md` kwarg-classification
  test, traceback verbosity is something pvl-core *can* decide (infer from log
  level), so it is not an override kwarg — `wire_middleware_stack` infers it
  internally.

### New module — `src/fastmcp_pvl_core/_logging_middleware.py`

One class, `RequestLoggingMiddleware`, subclassing FastMCP's `Middleware`.

**Single logging point.** It overrides **only** `on_message` — the outermost
dispatch hook, invoked exactly once per message. Overriding a method-specific
hook (e.g. `on_call_tool`) *in addition* would double-log, because the base
`Middleware` dispatch chains `on_message` → `on_request` → `on_call_tool` and
every overridden hook fires. A single `on_message` override is the one clean
logging point for every message type.

The tool name is read inside `on_message` via
`getattr(context.message, "name", None)` when `context.method == "tools/call"`
(the message is already `CallToolRequestParams` at this stage).

**Constructor:**

```python
RequestLoggingMiddleware(
    *,
    structured: bool = False,
    include_traceback: bool = False,
    logger: logging.Logger | None = None,
)
```

Default logger name: `fastmcp.middleware.requests`.

### Event vocabulary

First token is the bare event name. The `*_started` line carries the
identifier, the method, and the source; `*_completed` / `*_failed` carry the
identifier plus timing (and, on failure, the error).

| Condition | started | completed | failed |
|---|---|---|---|
| `method == "tools/call"` | `tool_call_started` | `tool_call_completed` | `tool_call_failed` |
| otherwise | `<type>_started` | `<type>_completed` | `<type>_failed` |

`<type>` is `request` or `notification`, taken from `context.type`.

Field set per line:

- **started** — `tool=<name>` (tool calls) or `method=<method>` (otherwise);
  tool-call lines additionally carry `method=tools/call` and `source=<source>`;
  non-tool lines carry `source=<source>`.
- **completed** — identifier (`tool=` / `method=`) + `duration_ms`.
- **failed** — identifier + `duration_ms` + `error_type=<ExceptionClass>` +
  `error="<message>"` (quoted).

Examples (rich mode):

```
tool_call_started   tool=read method=tools/call source=client
tool_call_completed tool=read duration_ms=68.57
tool_call_failed    tool=read duration_ms=109.84 error_type=ValueError error="Section '1.3. ...' not found"

request_started   method=initialize source=client
request_completed method=initialize duration_ms=2.10
request_failed    method=initialize duration_ms=5.0 error_type=RuntimeError error="..."
```

### Output modes

Selected by `structured` (which `wire_middleware_stack` derives from
`FASTMCP_ENABLE_RICH_LOGGING`):

- **rich** (`structured=False`) — bare-event-name-first `key=value` text, built
  with lazy `%s` formatting, no f-strings.
- **structured** (`structured=True`) — a JSON object per record carrying the
  same keys (`event`, `tool` / `method`, `source`, `duration_ms`,
  `error_type`, `error`).

### Error path

`on_message` wraps `call_next` in `try`/`except`:

- timestamp before the call, compute `duration_ms` after;
- on success, emit the `*_completed` line at the middleware's level;
- on exception, emit exactly one `*_failed` line at `ERROR`, then **re-raise
  unchanged** — error→response conversion is FastMCP's own job, not this
  middleware's;
- when `include_traceback` is set, the `*_failed` log call passes
  `exc_info=True` so the handler renders the traceback (a Rich panel in rich
  mode, plain text in structured mode).

Net effect: one failed tool call emits exactly one conforming `*_failed` line
plus one traceback — replacing today's four records (`event=request_error` +
`Request ... failed after` + `Error in tools/call` + the Rich panel).

### `wire_middleware_stack` rewire — `src/fastmcp_pvl_core/_middleware.py`

The stack collapses from three middlewares to one. `ErrorHandlingMiddleware`
and `TimingMiddleware` are removed; only `RequestLoggingMiddleware` is
installed.

```python
def wire_middleware_stack(mcp: FastMCP) -> None:
    include_traceback = logging.getLogger().isEnabledFor(logging.DEBUG)
    rich_raw = os.environ.get("FASTMCP_ENABLE_RICH_LOGGING", "true")
    structured = not parse_bool(rich_raw)
    mcp.add_middleware(
        RequestLoggingMiddleware(
            structured=structured,
            include_traceback=include_traceback,
        )
    )
```

The module docstring and `wire_middleware_stack`'s docstring are rewritten —
they no longer describe a "three-middleware stack".

### Cross-repo impact

`wire_middleware_stack` is consumed by `markdown-vault-mcp`,
`image-generation-mcp`, and `paperless-mcp`. This is a deliberate, documented
behaviour change: the `Request X completed in Yms` line disappears, replaced
by the conforming `*_completed duration_ms=` line; the zero-kwarg signature
means any call site passing `include_traceback`/`transform_errors` must drop
those arguments. Downstream adoption is tracked at the successor to
`markdown-vault-mcp#493`.

### Future enhancement (out of scope)

Opt-in **argument surfacing** — `tool=<name>` plus a bounded whitelist of safe
argument values supplied by the domain server as a parameter on
`wire_middleware_stack` (default empty, env-gated). Deferred until the base
middleware here is stable; tracked separately.

### Documentation

- README logging section: the conforming event vocabulary, the
  `wire_middleware_stack` behaviour change, the new zero-kwarg signature.
- Docstrings: `_logging_middleware.py` module + `RequestLoggingMiddleware`
  class; `_middleware.py` module + `wire_middleware_stack`.
- **No `docs/specs/` change.** Per `CLAUDE.md`, spec docs under `docs/specs/`
  describe wire format between independently developed servers. This is
  pvl-core's own implementation of how it logs — an implementor concern, not a
  protocol extension.

### Acceptance

- [ ] `wire_middleware_stack` installs only `RequestLoggingMiddleware`;
      `TimingMiddleware` and `ErrorHandlingMiddleware` removed.
- [ ] Tool-call logs carry `tool=<name>`; `grep -oP 'tool=\w+'` over a log
      sample yields real tool names.
- [ ] Every middleware-emitted line is bare-event-name-first, `%s`-formatted,
      no f-strings — in both rich and structured modes.
- [ ] A failed tool call emits exactly one conforming `*_failed` line (plus
      traceback), not three.
- [ ] `wire_middleware_stack` is zero-kwarg; `transform_errors` and
      `include_traceback` removed.
- [ ] README + docstrings updated.

---

## Testing

TDD throughout — tests precede implementation in each task.

### Issue #91 — `test_logging.py`

Add cases to the existing file:

- noisy loggers pinned to `WARNING` when root is INFO / WARNING / ERROR;
- noisy loggers at `NOTSET` when root is DEBUG;
- `uvicorn.error` untouched at every level;
- idempotency across repeated `configure_logging_from_env` calls (DEBUG → INFO
  → DEBUG flips cleanly).

### Issue #90

**`test_middleware.py` — rewritten.** The three-middleware order test, the
`_INSTALLED_BY_HELPER` tuple, and any `transform_errors` / `include_traceback`
kwarg tests are removed. New assertions: exactly one `RequestLoggingMiddleware`
is installed; `FASTMCP_ENABLE_RICH_LOGGING` toggles its `structured` attribute.

**New `RequestLoggingMiddleware` tests.** Unit-test `on_message` directly with
a constructed `MiddlewareContext` and a fake `call_next` — fast, no server
spin-up. Coverage:

- `tool_call_*` vs `request_*` / `notification_*` event vocabulary;
- `tool=` extracted from `params.name` on `tools/call`;
- `method=` + `source=` on `*_started`;
- `duration_ms` present on `*_completed` and `*_failed`;
- `error_type=` and quoted `error=` on `*_failed`; exception re-raised;
- `exc_info` passed to the log call when `include_traceback` is set;
- rich text output vs structured JSON output shape.

All five `CLAUDE.md` local checks (`uv sync --all-extras`, `pytest`,
`ruff format --check`, `ruff check`, `mypy src`) pass before each PR opens.
