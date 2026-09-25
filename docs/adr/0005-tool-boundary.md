# ADR 0005 — The tool boundary: pvl-core owns how a tool call ends

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** pvl-core maintainers
- **Relates to:** [#364] (pvl-core's own tools log caller-side refusals at
  ERROR), [#363] (the middleware's `tool_call_failed` ignores `log_level`),
  [#369] and [#370] (background-job failures), the `designing-tool-outcomes`
  skill, pvliesdonk/fastmcp-server-template#676 (where the skill and its
  `tool_boundary` example come from)

> This is an **implementor design**, not a wire specification. The MCP
> specification leaves the choice between a result and an `isError` result
> to the server (`docs/reference/mcp-tool-outcomes-and-errors.md`); this
> records how pvl-core makes that choice for the family.

---

## 1. Context

The `designing-tool-outcomes` skill sorts every way a tool call can end into
four outcomes and fixes, for each, what the model receives and the log
level. Two of its rules need code, not only discipline:

- No exception may reach FastMCP's own handler, which logs ERROR with a
  traceback and, under `mask_error_details`, hides the reason from the
  model. The skill's answer is a `tool_boundary` wrapper that turns any
  exception that is not already a deliberate error into outcome 4.
- The level of the family's failure line follows who has to act. pvl-core's
  request-logging middleware logs every failure at ERROR ([#363]), so a
  tool that raises `ToolError(msg, log_level=logging.INFO)` still produces
  an ERROR line.

The template shipped the wrapper as an example in the skill, to be copied
into every downstream. The template PR left open whether it should instead
be a pvl-core helper.

## 2. Decision

### 2.1 `tool_boundary` is a pvl-core helper

pvl-core exports `tool_boundary` and `is_tool_boundary`. A copy in every
downstream is the reimplementation `AGENTS.md` exists to prevent: the fault
message, its log line and its level are the same decision for every server,
so pvl-core makes it once.

The decorator takes **no keyword arguments**. Under the classification test:

- the model-facing fault message, the log event and its level are shape;
- the tool's name comes from the function;
- "which of my exceptions mean not-found" is domain knowledge, but it
  already has a home: the tool body catches that exception and raises
  `ToolError(msg, log_level=logging.INFO)`. A mapping kwarg would be a
  second place for the same decision.

It must be a decorator on the function, not middleware. FastMCP writes its
own record for a failing call in the innermost dispatch, before any
middleware sees the exception (`docs/reference/mcp-tool-outcomes-and-errors.md`,
"How FastMCP 4.0.9 maps a tool's behaviour"). Only wrapping the function
keeps an unexpected exception from reaching FastMCP's catch-all.

### 2.2 What the boundary does

- A `FastMCPError` (`ToolError`, `ResourceError`, …) passes through
  unchanged. It is already a deliberate outcome, with its own message and
  `log_level`.
- An `MCPError` for a missing client capability (-32021) passes through
  too. SEP-2575 requires it on the wire as a JSON-RPC error, and FastMCP's
  own handler re-raises it for that reason; turning it into a result would
  tell the client the call went through.
- An upstream rate limit (an HTTP 429 status error) or timeout is outcome 4
  that heals itself: WARNING without a traceback, and a `ToolError` at
  WARNING carrying the message FastMCP's handler uses for the same two
  cases, so a wrapped tool tells the model no less than a bare one. Only the
  exception's type is logged; its text quotes the request URL.
- Any other `Exception`, including any other `MCPError`, is outcome 4.
  The boundary logs
  `tool_failed function=<name> error_type=<type>` at ERROR **with the
  traceback** on `fastmcp_pvl_core._tool_boundary`, then raises a
  `ToolError` with a fixed message that tells the model the request was
  fine and what to do: retry later, tell the user if it keeps failing.
- The `ToolError` is raised `from None`. The boundary's line already carries
  the traceback, so the chained cause would only print it a second time
  wherever a later handler logs with `exc_info`.
- `BaseException` that is not an `Exception` (cancellation) passes through.
- Sync and async functions are both supported; the wrapper keeps
  `functools.wraps` metadata, so FastMCP builds the same input and output
  schemas, injects `Context`, and still sees a coroutine function (required
  for `TaskConfig`).

Because the fault message is fixed, a wrapped tool never sends a crash's
own text to the model, whether or not `mask_error_details` is on.

### 2.3 Who carries the traceback

The boundary, on every path. A call can end three ways at runtime: inline
(the request-logging middleware sees it), after the jobs fallback moved it
to the background (only the jobs manager sees it), and as a native
SEP-2663 task (only FastMCP's task machinery sees it). The boundary is the
one place all three pass through, so the traceback is logged there, and the
other lines stay one-line summaries: the jobs manager's `job_failed` takes
the `ToolError`'s level and attaches no traceback ([#370]). The exception
is the middleware, which attaches one when `include_traceback` is on (a
DEBUG root logger). For that, `register_long_running_tool` wraps the domain
coroutine in its own boundary as well as the registered tool, and the jobs
manager applies the boundary's rule to work a downstream started with
`Jobs.start` without one: the fault message to the poller, ERROR with the
traceback in the log ([#369]).

### 2.4 The middleware's failure line follows `log_level`

`RequestLoggingMiddleware` logs `*_failed` at the exception's `log_level`
when it is a `FastMCPError`, and at ERROR otherwise. The event name, field
order and logger are unchanged. FastMCP converts an exception from a tool's
own body into a `ToolError` before the middleware sees it, so for that
case the level is whatever the tool (or its boundary) chose, which is also
the level of FastMCP's own record for the call, and `error_type` is
`ToolError`; the cause's type is on the boundary's `tool_failed` line.

A failure FastMCP raises before the body runs keeps its own type:
`NotFoundError` for an unknown tool name (not a `FastMCPError`, so ERROR),
FastMCP's `ValidationError` for arguments that fail the schema (a
`FastMCPError` at its default ERROR, although FastMCP logs its own record
at WARNING), and the -32021 `MCPError`. Those are caller-side outcomes the
middleware still records at ERROR; classifying them is not this ADR's
change.

### 2.5 The traceback is an operator-facing emit path

The boundary cannot know whether an exception's message carries a secret.
`logging-standard` already puts that duty at the site that raises: a module
handling a credential-bearing value re-raises `from None` with its own
message. The boundary adds no redaction of its own.

## 3. Consequences

- A fault on a wrapped tool produces three ERROR lines: the boundary's
  `tool_failed` with the traceback, FastMCP's `Error calling tool 'x'`, and
  the middleware's `tool_call_failed`. pvl-core adjusts third-party loggers
  only by level policy, and silencing `fastmcp.server.server` would also
  silence its other records, so FastMCP's line stays.
- An outcome 2 or 3 (`ToolError` at INFO) produces INFO lines only. An
  upstream rate limit or timeout produces WARNING lines only.
- The tools pvl-core registers carry the boundary. The transfer link tools'
  `validate` hook contract is settled separately ([#364]). The template
  replaces its copied example with the import.

[#363]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/363
[#364]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/364
[#369]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/369
[#370]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/370
