---
name: logging-standard
description: >-
  Use before adding or changing any logging call, log-emitting middleware,
  or logging bootstrap in src/fastmcp_pvl_core/: the message format, log
  levels, exception handling, and secret redaction every module follows,
  and which lines pvl-core owns on behalf of every downstream server.
---

# Logging standard

## Scope

This standard governs first-party code: `src/fastmcp_pvl_core/` and
`tests/`. pvl-core's position is different from a downstream's. A
downstream's standard declares the middleware lines out of scope because
pvl-core already emits them conforming; here, those lines are *ours*:

- **The request-logging middleware** (`_logging_middleware.py`) emits the
  family-standard line for every MCP message on every downstream: a
  snake_case event name first, then `key=value` pairs, `tool=<name>` on
  tool calls, the duration on the terminal `*_completed` / `*_failed`
  line. A change to it changes every server's log stream at once; treat
  its line shape, and its logger name, as shared shape.
- **The logging bootstrap** (`configure_logging_from_env` in
  `_logging.py`) owns the **root logger** for every downstream: the
  process's console handlers, level resolution, and third-party level and
  filter policy. A downstream attaches nothing of its own.

Third-party lines (`uvicorn.*`, `mcp.*`, `docket.*`, and FastMCP's own
records) are out of scope for formatting: pvl-core adjusts their *levels*
through the constants in `_logging.py` and never rewrites their wording.
The one exception is `uvicorn.access`, where a filter drops successful
requests and redacts credentials out of the request line — that is not
reformatting, it is refusing to log a secret. (In JSON mode the same
filter's parsed `client`/`method`/`path`/`status` are also emitted as
fields, since uvicorn owns that record's template and it can never
conform to the grammar below — see `_AccessLogFields` in
`_log_render.py`.)

## Framework

- Standard library `logging` only. Every module:
  `logger = logging.getLogger(__name__)`. No `print()` for operational
  output, no third-party logging libraries.
- The one exception: the request middleware logs on
  `fastmcp.middleware.requests`. FastMCP attaches handlers there at import
  time with `propagate = False`; `configure_logging_from_env` undoes that
  (it disables FastMCP's own logging config and resets the logger), so the
  records reach pvl-core's handlers at the root logger like everything
  else. The name is shared shape; do not "fix" it to `__name__`.
- A library does not configure logging at import. No handlers, no
  `basicConfig`, no level changes at module import. Root and third-party
  levels change only inside `configure_logging_from_env()`, which a
  downstream calls at startup.
- `{PREFIX}_LOG_LEVEL` is the single level control, where `{PREFIX}` is
  the env prefix the downstream passes in; the `-v` CLI flag forces
  `DEBUG`. `FASTMCP_LOG_LEVEL` is read only as a one-release deprecation
  bridge and warns when it is used. Do not add a second level variable.
- `{PREFIX}_LOG_FORMAT` picks the render mode: `rich` (human-readable
  `event key=value` text) or `json` (one object per record), case
  insensitive. Unset or unrecognised auto-detects — `rich` when stderr is
  a TTY, `json` otherwise — so a container gets JSON with no
  configuration and a test suite (stderr is not a TTY under pytest) gets
  JSON too unless a test sets this explicitly. See "Testing log output"
  below.

## Log levels

| Level | Use for |
|-------|---------|
| `DEBUG` | Internals: cache hits, parameter values, config resolution, a skipped optional path |
| `INFO` | Startup and configuration decisions an operator needs to see (`auth_mode_resolved`, the chosen KV backend), and lifecycle events of work pvl-core owns (a job starting, a transfer link claimed or rejected) |
| `WARNING` | Degraded but continuing: a fallback taken, missing optional config, unexpected data |
| `ERROR` | Failures affecting the primary result, with `exc_info=True` when the traceback matters |

`INFO` is every downstream's default stream, so anything logged there
multiplies across the family. Per-message detail the request middleware
already covers, and anything a loop emits on every iteration, goes to
`DEBUG`.

## Exception handling

- Operator misconfiguration detected at build or load time raises
  `ConfigurationError` (`_errors.py`) so the server fails fast. Do not
  log it and continue with a degraded default.
- No bare `except:`. `except Exception` only at a boundary whose failure
  must not take down startup or a request, with a comment saying why
  (the existing `# noqa: BLE001 — <reason>` form) and a log call carrying
  `exc_info=True`.
- Optional enrichment failures: catch, log at `DEBUG` with
  `exc_info=True`, continue. This holds inside a tool too: a failed
  optional extra is not an outcome of the call.
- Inside a tool pvl-core registers, and in the request-logging
  middleware's `*_failed` line, the level of a call that could not return
  its result comes from the `designing-tool-outcomes` skill: INFO when only
  the model has to act (an unknown job id, a rejected `ref`), WARNING or
  ERROR for a server fault. The rows of the table above are not a
  substitute for that classification. The tool raises every outcome it
  can name as a `ToolError` with that `log_level`; `tool_boundary`
  (`_tool_boundary.py`) logs everything else once as `tool_failed` at
  ERROR with the traceback.
  The middleware's `*_failed` line takes its level from that `ToolError`.
  A failure FastMCP raises before the tool body runs (unknown tool,
  invalid arguments) keeps its own type and is still recorded at ERROR.
- A traceback is an emit path the boundary cannot redact: it does not know
  which exception carries a secret. The site that handles a
  credential-bearing value re-raises `from None` with its own message
  before the exception can reach a boundary (ADR 0005 §2.5).
- Re-raise with `from exc` to keep the cause, or `from None` when the
  cause's message would carry a secret (see redaction below).

## Message format

- Pseudo-structured: `logger.info("event_name key=%s", value)`. The event
  name is the first token (snake_case), followed by `key=value` pairs
  through `%s` formatting.
- Never use f-strings in log calls; they defeat lazy formatting, and they
  quietly degrade rendering too: an f-string has already substituted its
  values into `record.msg` before `bind_record` (`_log_render.py`) ever
  sees it, so even a field that happens to still look like `key=3600` is
  recovered as the *literal string* `"3600"`, not the `int` the caller
  actually had — the type information a `%`-placeholder would have
  preserved is gone by the time the grammar can parse it. A substituted
  value containing a space, `%`, or `=` breaks the shape outright and the
  whole record falls back to its plain formatted message instead.
- A call that does not follow this grammar renders as a formatted
  message rather than fields — `message` in JSON mode, the formatted text
  in Rich mode — in both cases losing the field structure a conforming
  call gets for free. `find_nonconforming_log_calls` reports calls that
  break it — only within its receiver scope, a module-level `logger =
  logging.getLogger(...)` name used with a level method; `self.logger`,
  an imported logger, and `logger.log(...)` are outside what it can see
  — so a clean report is not a proof every call conforms. Within that
  scope pvl-core's own code does conform, and
  `tests/test_log_conformance.py` fails the build if a new call breaks
  the grammar ([#328](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/328)).
- No first-party line predates this format any more. If you add one that
  breaks it, the conformance test fails and names the file, line and
  template — fix the call rather than adding an exemption; the checker
  has no allowlist, which is where its value comes from.

## Secrets and redaction

Operator-supplied values reach log lines and exception messages, and both
are emit paths: an exception message is rendered in every traceback that
carries it.

- Never log a token, password, or secret value. Log its presence
  (`token=<redacted>`) or a non-reversible descriptor.
- Never log a full operator-supplied URL: userinfo carries credentials and
  query strings carry tokens. Log `scheme` or the parsed hostname.
- When you fix a leak on one emit path, sweep every other path that
  handles the same value in the same change.
- `SecretMaskFilter` masks `Authorization` header values in formatted
  messages, but only on a logger or handler it has been attached to, and
  pvl-core attaches it nowhere itself. It is an opt-in safety net for
  downstreams, not a licence to log unredacted values.

## Testing log output

`caplog` captures at the root logger. `caplog.at_level(level,
logger="x")` only sets that logger's level; records from sibling loggers
are still captured, so assert on the record's `name` when the test is
about one logger. FastMCP sets `propagate=False` on `fastmcp` at import,
so records under `fastmcp.*` (the request middleware's included) reach
`caplog` only through the autouse `_fastmcp_logger_propagates` fixture in
`tests/conftest.py` — or after a call to `configure_logging_from_env`,
which restores propagation for the whole process.

pytest's own stderr capture is not a terminal, so a test that calls
`configure_logging_from_env` without setting `{PREFIX}_LOG_FORMAT` gets
JSON output under auto-detection, not Rich — set the env var explicitly
(`monkeypatch.setenv`) when a test needs to assert on Rich-shaped output.
