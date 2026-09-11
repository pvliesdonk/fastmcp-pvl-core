# Root logging ownership — one handler chain, one format

**Date:** 2026-09-11
**Issues:** umbrella issue to be filed; resolves
[#323](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/323);
blocked on the template side by
[fastmcp-server-template#611](https://github.com/pvliesdonk/fastmcp-server-template/issues/611);
supersedes the stopgap in
[fastmcp-server-template#609](https://github.com/pvliesdonk/fastmcp-server-template/pull/609)
(which closed [#608](https://github.com/pvliesdonk/fastmcp-server-template/issues/608))
**Related:** [ADR 0003](../../adr/0003-opentelemetry-classification.md),
[logging conformance #90/#91](2026-05-15-logging-conformance-90-91-design.md)

## Problem

A deployed family server's stderr carries three independent handler chains,
each with its own owner and format:

| Chain | Owner | Format |
|---|---|---|
| `fastmcp.*`, including pvl-core's request middleware (`fastmcp.middleware.requests`) | fastmcp, at **import time** (`fastmcp/__init__.py:20`) | `RichHandler` pair, `propagate=False` |
| `uvicorn`, `uvicorn.error`, `uvicorn.access` | uvicorn's `dictConfig(LOGGING_CONFIG)` at server start | `INFO:     …`, `propagate=False`; the access handler writes **stdout** |
| everything else (`<module>.*`, the MCP SDK, httpx, …) | the template's `_root` callback (`cli.py.jinja:44-48`) | `%(levelname)s %(name)s: %(message)s` |

pvl-core configures levels but has never owned the root logger. The template's
`_root` docstring states the gap directly: `configure_logging_from_env` "does
NOT attach a handler to the root logger … Attach one here."

Observable consequences:

- **Redundant, noisy access log.** Every request prints uvicorn's
  `"POST /mcp HTTP/1.1" 200 OK` beside the middleware's
  `request_started`/`request_completed` pair, and every health probe prints a
  line. pvl-core's documented demotion of `uvicorn.access` to `WARNING`
  (`_logging.py:58-63`, `README.md:261`) is silently void: uvicorn's
  `Config.configure_logging()` runs after pvl-core and resets the level to
  `INFO` with its own handler. The existing tests (`test_logging.py:88`)
  assert the level immediately after pvl-core's call and never run uvicorn's
  configuration, so they pass on broken behaviour.
- **No process-wide format.** `FASTMCP_ENABLE_RICH_LOGGING=false` switches
  only the middleware to JSON, because the JSON string is built at the
  producer (`_logging_middleware.py:193-201`). An aggregator receives JSON
  middleware lines interleaved with uvicorn's and the domain's plain text.
- **OTLP log export misses `fastmcp.*`** (#323). An operator's OTLP handler
  at root never sees records from a logger with `propagate=False`.
- **Container output is neither readable nor parseable.** Rich defaults to 80
  columns on a non-TTY, so structured lines wrapped across three rows
  (template#608). The stopgap in template#609 sets
  `FASTMCP_ENABLE_RICH_LOGGING=false` in the image and systemd unit; its own
  proof output is `INFO: {"event": "request_completed", …}` — a level prefix
  in front of the JSON, from fastmcp's plain handler — next to
  `LEVEL name: message` lines from the template's root handler.
- **First-party records carry no parseable fields.** The family logging
  standard's `event key=%s` format is followed by 229 of 625 literal-format
  calls across the six template-generated servers, the template skeleton
  included, and nothing checks it (template#611).
- **Fragmented noise policy.** httpx/httpcore quieting exists in three
  mutually inconsistent downstream copies (see §4).

## Decision

**pvl-core owns the root logger.** It installs the process's only console
handler chain at root; every library namespace propagates into it. fastmcp
and uvicorn are each turned off at their own documented off-switch rather
than fought after the fact. Rendering (Rich or JSON), level policy, and
access-log policy are then decided once, in `configure_logging_from_env`, for
every record in the process.

Alternatives rejected:

- **A pvl-core-owned named logger instead of root.** Not possible: `fastmcp.*`
  and `uvicorn.*` are sibling top-level namespaces, and root is their only
  common ancestor.
- **Three chains sharing one formatter.** Uniform-looking output, but levels,
  filters and streams stay decided in three places, an OTLP handler still
  needs three attach points, and #323 remains unresolved.
- **A filter on `uvicorn.access` that survives uvicorn's `dictConfig`.**
  Works (filters survive non-incremental `dictConfig`), but addresses one
  symptom and leaves three chains in place.

Verified on fastmcp 4.0.0, uvicorn 0.44.0, Python 3.14. Every statement below
about library behaviour was probed on that stack.

## §1 Ownership and neutralisation

`configure_logging_from_env` becomes the sole owner of the root logger's
console output.

**fastmcp** is neutralised by setting `fastmcp.settings.log_enabled = False`,
removing every handler fastmcp attached to the `fastmcp` logger at import time
(two `RichHandler`s by default; one plain `StreamHandler` when
`FASTMCP_ENABLE_RICH_LOGGING=false` is set at import), restoring
`propagate=True`, and resetting the logger's level to `NOTSET`. The level
reset is load-bearing: fastmcp's import-time configuration sets the `fastmcp`
logger to `INFO` explicitly, and left in place it blocks every `fastmcp.*`
DEBUG record — the middleware's included — before it reaches root. The
`FASTMCP_LOG_LEVEL=DEBUG` environment write removed in §6 existed to work
around exactly this. The settings object is mutable at runtime. `configure_logging` returns immediately when
`log_enabled` is false (`utilities/logging.py:57`), so later calls — including
the one inside `temporary_log_level`, which is what reverted the parked #323
fix — can no longer reattach handlers or reset `propagate`. `log_enabled` is
read in exactly two places in fastmcp 4.0.0 (`__init__.py:20` and that
guard), so the flag has no effect outside logging configuration.

**uvicorn** is neutralised by passing `log_config=None` (see §2). uvicorn
then skips `dictConfig` entirely, and its three loggers remain
`handlers=[]`, `propagate=True`, `level=NOTSET`.

**The template's `_root` handler block is deleted** in the cutover; it
exists only because root was empty.

Invariants:

1. **stderr only.** Nothing pvl-core installs writes to stdout, which is the
   protocol channel under stdio transport.
2. **Idempotent.** pvl-core marks the handlers it installs and removes its
   own before installing on every call, so repeated calls — in either mode,
   and across a mode change — leave exactly one chain at root.
3. **Exclusive over the console, tolerant of everything else.** pvl-core
   removes pre-existing root handlers that write to the console (the
   double-render source when `opentelemetry-instrument` has installed a
   console handler), and leaves every other handler untouched — OTLP
   `LoggingHandler`, file, syslog. A handler writes to the console when its
   output stream is `sys.stdout`, `sys.stderr`, `sys.__stdout__` or
   `sys.__stderr__`, read from `StreamHandler.stream` or from
   `RichHandler.console.file` — `RichHandler` is not a `StreamHandler` and
   has no `.stream`. The rule keys on **stream identity, not handler type**:
   pytest's `LogCaptureHandler` is a `StreamHandler` subclass over a
   `StringIO`, so a type-based rule would detach `caplog` across the test
   suite.

**Tracebacks.** fastmcp's chain includes a compressed-traceback companion
handler (framework frames suppressed, 3-frame cap). In Rich mode pvl-core
reproduces this pair: the main handler filters out records carrying
`exc_info`, the traceback handler accepts only those. In JSON mode the
traceback becomes an `exception` string field.

## §2 The `run_http` seam

```python
run_http(app, *, config: ServerConfig, host: str | None = None, port: int | None = None) -> None
```

New export. The template calls it in place of `uvicorn.run(...)`; the
template keeps constructing the ASGI app (`server.http_app(path=...,
event_store=...)`) and hands it in. `run_http` does not call
`configure_logging_from_env` — the template's root callback already does, for
every subcommand.

Parameter classification (per `CLAUDE.md`):

| Parameter / uvicorn setting | Category | Value |
|---|---|---|
| `log_config` | shape — not overridable | `None` |
| `lifespan` | shape — not overridable | `"on"`; FastMCP's startup/shutdown hooks run through the ASGI lifespan protocol |
| `timeout_graceful_shutdown` | operator config | `config.shutdown_grace_s` |
| `host`, `port` | operator config | CLI override if given, else `config.host` / `config.port` |
| `app` | the thing being served | — |

There is **no `**uvicorn_kwargs` passthrough**. It would hand `log_config`
back to downstream and silently restore the uvicorn chain. A future need (TLS,
for instance) gets a named, classified parameter.

`run_http` also absorbs the `host if host is not None else config.server.host`
precedence currently duplicated in every downstream `cli.py`.

**`ServerConfig.shutdown_grace_s`** — new field, env
`<PREFIX>_SHUTDOWN_GRACE_S`, integer ≥ 0 (uvicorn's `timeout_graceful_shutdown`
is `int | None`), default **3**, parsed strictly via `env_int` like `PORT`.
Downstreams currently disagree: the template and scholar-mcp hardcode `0`,
markdown-vault-mcp hardcodes `3` so SIGTERM drains in-flight requests before a
container stops. pvl-core adopts `3`; `0` drops in-flight requests on every
container stop. It is operator config because it must agree with the
orchestrator's termination grace period.

Because uvicorn decides whether to emit access records via
`access_logger.hasHandlers()`, and `uvicorn.access` now propagates to a root
that has a handler, access records flow into the unified tree and §4 governs
them.

## §3 Rendering

One switch, process-wide, applied by the root handler's formatter to every
record.

**Env:** `<PREFIX>_LOG_FORMAT` = `rich` | `json`. Unset or unrecognised →
**auto**: `rich` when stderr is a TTY, `json` otherwise. A container gets
parseable output with no configuration; an interactive terminal gets Rich.

**Rich mode.** `RichHandler` at root plus the traceback companion (§1). The
middleware's documented `event key=value` line (`README.md:320-328`) is
unchanged byte for byte. Rich on a non-TTY is now only reached by an operator
explicitly setting `<PREFIX>_LOG_FORMAT=rich` in a container; pvl-core does
not adjust its width. template#609 found that forcing a wide console
(`COLUMNS=200`) stops the wrapping but pads every record with trailing spaces
and keeps the blank time and source columns, and documents `COLUMNS` as the
operator's escape hatch — that stands.

**JSON mode.** Plain `StreamHandler` on stderr with a JSON formatter; one
object per line. Envelope, in order:

- `ts` (ISO-8601 UTC), `level`, `logger`
- conforming records (§3a) — the middleware's and first-party code's alike:
  `event`, then the event's fields
- all other records: `message` (the formatted message)
- `trace_id`, `span_id` when a valid span context is in scope
- `exception` when the record carries a traceback

`uvicorn.access` records, recognised by uvicorn's argument shape (§4), are
emitted with `client`, `method`, `path`, `status` fields rather than a
formatted string.

**Layering change.** The middleware stops rendering. It logs through the
same grammar as first-party code (§3a), and the formatter renders the result.
The middleware's `structured=` constructor flag and the
`FASTMCP_ENABLE_RICH_LOGGING` read in `_middleware.py:40-43` are removed.

## §3a Fields from the log-call grammar

Every first-party record must reach JSON mode as real fields, not a
`message` blob. The family standard already writes
`logger.info("event_name key=%s", value)`, and stdlib keeps the two halves
apart on every record: `record.msg` is the template the developer wrote,
`record.args` holds the values. pvl-core parses the **template** — a literal
in source code, never the rendered text — and pairs each field with its
argument, so values keep their types and no value is ever re-parsed out of a
string.

**Grammar** (owned by pvl-core, one private module consumed by both
formatters and the conformance check):

```
template := event ( " " field )*
event    := [a-z][a-z0-9_]*
field    := name "=" ( placeholder | literal )
name     := [a-z][a-z0-9_]*
placeholder := one %-conversion (e.g. %s %d %r %.1f), consuming the next arg
literal  := one or more characters, none of them space, "%" or "="
```

A template conforms when it matches in full and its placeholders consume
exactly `record.args`. Decided cases the current standard leaves open
(template#611): a literal value (`status=configured`) conforms, as a string
field; a compound or suffixed placeholder (`attempt=%d/%d`, `waiting=%.1fs`)
does not — it is written as separate fields (`attempt=%d max_attempts=%d
waiting_s=%.1f`); any prose, including a prefix before the pairs, does not;
a `%%` escape does not (it is neither a placeholder nor a literal).

**Rendering a conforming record.** JSON: `event`, then each field — a
placeholder field carries its argument as-is when it is a JSON-native type
(`str`, `int`, `float`, `bool`, `None`) and `str(arg)` otherwise; a literal
field is a string. Rich: `event` followed by `key=value` pairs, each value
the placeholder's own conversion applied to its argument, then quoted by the
existing `_render_value` rule (whitespace or `"` → quoted and escaped). For
the middleware this is byte-identical to today; for first-party records the
only visible change is that a value containing whitespace or a `"` is now
quoted and escaped.

**A non-conforming record** renders as `message` in JSON and as its normal
formatted text in Rich. It is never dropped and never raises.

**Conformance check.** pvl-core exports
`find_nonconforming_log_calls(root: Path) -> list[LogCallViolation]`: it
parses every `*.py` under `root` with `ast` and reports each call to a
standard level method (`debug`, `info`, `warning`, `error`, `exception`,
`critical`) on the receiver the standard mandates
(`logger = logging.getLogger(__name__)`) whose first argument is not a
conforming literal — an f-string or non-literal first argument is a
violation. Each violation carries path, line and the offending template. It
reads source only; nothing is imported. The template runs it as a test
(template#611); a second implementation of the grammar in the template would
drift from the formatter's.

Parsing is per template string and cached, so the per-record cost is a
dictionary lookup.

## §4 Level and filter policy

All in `configure_logging_from_env`, on the unified tree:

| Logger | Policy |
|---|---|
| `uvicorn.access` | Filter: pass records with status ≥ 400, drop the rest; no filter at DEBUG |
| `mcp.server.lowlevel.server`, `httpx`, `httpcore` | `WARNING` unless DEBUG, then `NOTSET` |
| `docket.worker` | `INFO` when root is DEBUG, else `NOTSET` (unchanged) |
| `uvicorn.error` | never demoted — carries bind and startup failures |

**Access filter.** uvicorn emits access records as
`'%s - "%s %s HTTP/%s" %d'` with args
`(client_addr, method, path_with_query, http_version, status_code)`
(`protocols/http/h11_impl.py:483-488`). A record matching that shape (five
args, `int` at index 4) is judged by status; a non-matching record passes
unchanged. Net effect at INFO: `/health` and `/mcp` 200s disappear; a `401`
from auth, a `404`, a `413`, and a readiness `503` remain — none of which
reach the MCP middleware. The filter is installed on the `uvicorn.access`
logger; repeated calls leave exactly one instance, and a DEBUG call removes
it.

uvicorn logs every access record at `INFO`, whatever the status. At
`<PREFIX>_LOG_LEVEL=WARNING` or above, the level therefore drops 4xx/5xx
lines before the filter sees them, and no access lines appear at all. That is
intended: an operator who raised the level asked for less output.

**httpx/httpcore consolidation.** Three downstream copies currently disagree
on the same loggers in the same process:

- template `cli.py.jinja:49-53` and scholar-mcp `cli.py:54-55`: set `WARNING`
  **only** under `-v`, on the premise "Core doesn't own these deps";
- markdown-vault-mcp `_http_logging.py:26-32`: `WARNING` at INFO, `NOTSET` at
  DEBUG, called after `_root` — so under `-v` it undoes the template's
  setting.

The premise is wrong: httpx is a hard dependency of fastmcp, which pvl-core
pulls for the whole family, the same position as `docket.worker`, which
pvl-core already governs. markdown-vault-mcp's rule matches the existing
`_NOISY_THIRD_PARTY_LOGGERS` semantics exactly; adopting it adds two names to
that tuple. All three downstream copies are deleted in the cutover.

`_NOISY_THIRD_PARTY_LOGGERS` no longer includes `uvicorn.access`; its level
stays `NOTSET` and the filter decides.

## §5 #323 and the ADR 0003 boundary

ADR 0003 stands: pvl-core owns no OpenTelemetry wiring — no OTel dependency,
no exporter configuration, no handler. #323 is resolved by the topology:
`fastmcp.*` propagates to root, so an operator-attached OTLP handler receives
every record in the process.

pvl-core's obligations:

1. §1 invariant 3 — non-console handlers at root survive
   `configure_logging_from_env`. That invariant is the #323 fix and is tested
   as such.
2. A README recipe for OTLP log export, **written from a literal run of the
   documented steps**. Expectation to verify, not assert:
   `OTEL_PYTHON_LOG_CORRELATION=true` is no longer needed — it installed a root
   console handler at import time (the double-render source in the parked
   investigation), and #322 already stamps `trace_id`/`span_id` on
   middleware lines.
3. ADR 0003 gets a "resolved by" note for #323; its content is unchanged.

## §6 Environment contract

`configure_logging_from_env(env_prefix: str, *, verbose: bool = False)` —
`env_prefix` is new and required, matching `ServerConfig.from_env(env_prefix)`
and keeping identity a caller argument (`CLAUDE.md`, foldability).

| Variable | Values | Unset / invalid | Notes |
|---|---|---|---|
| `<PREFIX>_LOG_LEVEL` | `DEBUG` `INFO` `WARNING` `ERROR` `CRITICAL`, case-insensitive | `INFO` | `verbose=True` forces `DEBUG` |
| `<PREFIX>_LOG_FORMAT` | `rich` `json`, case-insensitive | auto (§3) | — |
| `<PREFIX>_SHUTDOWN_GRACE_S` | integer ≥ 0 | default `3`; invalid → `ConfigurationError` | on `ServerConfig` |
| `FASTMCP_LOG_LEVEL` | as `<PREFIX>_LOG_LEVEL` | — | **migration bridge**: used only when `<PREFIX>_LOG_LEVEL` is unset, with one `WARNING` naming the prefixed variable; removed in the next major |
| `FASTMCP_ENABLE_RICH_LOGGING` | — | — | no longer read by pvl-core; see below |

`FASTMCP_ENABLE_RICH_LOGGING` is not bridged. Its only use was selecting JSON
in deployed containers — which is what template#609 sets it for in the image
and systemd unit — and the auto default produces JSON on a non-TTY. The
template cutover removes #609's default.

Deleted with the rename: the `os.environ["FASTMCP_LOG_LEVEL"] = "DEBUG"`
write on the verbose path (`_logging.py:80`), which existed only to align
fastmcp's own loggers, and the `-v` help text in `_cli.py:64` that describes
it.

`<PREFIX>_LOG_LEVEL` and `<PREFIX>_LOG_FORMAT` are read directly by
`configure_logging_from_env`, which runs before `ServerConfig` is loaded; they
are listed in the env-surface documentation alongside `ServerConfig`'s
variables.

## §7 Testing

The existing tests assert state after pvl-core's call. The new tests assert
**what reaches a handler after the real library sequence has run**.

**Fixture.** `_restore_noisy_levels` is replaced by a fixture that snapshots
and restores: root handlers and level; the `fastmcp` logger's handlers,
`propagate` and level; `uvicorn`, `uvicorn.error` and `uvicorn.access`
handlers, `propagate`, level and filters; the noisy and flood loggers' levels;
`fastmcp.settings.log_enabled`.

**Contract matrix**, pinned in the first commit of each PR: mode
(`rich` / `json` / auto × TTY / non-TTY) × level (DEBUG / INFO) × env
(prefixed set / only `FASTMCP_LOG_LEVEL` / both / neither / invalid) ×
pre-existing root handlers (none / console / non-console / `caplog`).

Named tests:

- **Topology** — one console chain at root; `fastmcp` handler-less,
  propagating, level `NOTSET`; idempotent in Rich mode, in JSON mode, and
  across a mode change; nothing written to stdout.
- **DEBUG reaches root** — at `<PREFIX>_LOG_LEVEL=DEBUG`, a
  `fastmcp.middleware.requests` DEBUG record reaches the root handler.
- **#323 blocker regression** — `temporary_log_level("DEBUG")` then
  `configure_logging()` leave `fastmcp` handler-less and propagating.
- **#323 fix** — a non-console handler attached at root beforehand survives
  and receives `fastmcp.*`, `uvicorn.*` and domain records.
- **`caplog` survives** `configure_logging_from_env`.
- **uvicorn sequence** — after `uvicorn.Config(..., log_config=None)
  .configure_logging()`, uvicorn's loggers are handler-less and propagating.
- **`run_http` config** — the `uvicorn.Config` it builds carries
  `log_config=None`, `lifespan="on"`, the configured grace, and the correct
  host/port precedence; tested without binding a port.
- **End to end** — `run_http` on an ephemeral port; request `/health` and a
  missing path; assert the 200 is absent and the 404 present at the handler.
- **Access policy** — 200 dropped; 401 and `/health` 503 kept; all kept at
  DEBUG; none at WARNING; non-conforming record passes.
- **Rendering** — Rich middleware line byte-identical to `README.md:320-328`;
  in JSON mode every record parses (middleware, domain, uvicorn access,
  exception, trace ids).
- **Grammar** — table-driven over every decided case in §3a: plain
  placeholder, non-`%s` conversion, literal value, compound placeholder,
  unit suffix, prose prefix, arg-count mismatch, `%%`. A conforming
  first-party record yields typed JSON fields and the quoted Rich form; a
  non-conforming one yields `message` and never raises.
- **Conformance check** — reports f-strings, non-literal first arguments and
  non-conforming literals with path and line; ignores calls on other
  receivers; imports nothing from the scanned tree.
- **Bridge** — exactly one `WARNING`, naming the prefixed variable, only when
  the fallback is used.
- **Noise policy** — per-logger levels at INFO and DEBUG, including httpx and
  httpcore.

Run locally on Python 3.10 and on the top of the CI matrix; TTY detection
and Rich's width handling are interpreter- and environment-sensitive.

## §8 Staging and release

Sequenced PRs to `main`, one major release, one template cutover — preceded
by one non-breaking release so template#611 and the downstream migration can
start immediately.

0. **PR 0 — grammar and conformance check** (non-breaking, `feat:`). The
   §3a grammar module and `find_nonconforming_log_calls`, with no change to
   runtime logging. Released on its own as a minor version, because it is the
   only part of this design that does not depend on the topology and it
   unblocks template#611 now.
1. **PR 1 — topology** (breaking). Root ownership with the Rich renderer
   reproduced at root, fastmcp neutralised, `configure_logging_from_env(env_prefix)`
   and the bridge, §4 policy, the new fixture.
2. **PR 2 — `run_http` and `ServerConfig.shutdown_grace_s`** (breaking).
3. **PR 3 — rendering** (breaking). JSON formatter and Rich field rendering
   over the §3a grammar, auto selection, middleware logs through the grammar;
   `structured=` and `FASTMCP_ENABLE_RICH_LOGGING` removed.
4. **PR 4 — #323 recipe**, from a literal run; README and ADR 0003 note.
   Folds into PR 1 if small. Closes #323.
5. **Release** the major version.
6. **template#611**, after PR 0's release and independent of PRs 1–4: the
   `logging-standard` skill states the §3a grammar; the template runs
   `find_nonconforming_log_calls` over `src/` as a test; the skeleton's own
   non-conforming calls are rewritten. Consequence, accepted: each
   downstream's `copier update` PR fails that test until the downstream's
   calls conform — per-repo migration issues track it.
7. **Template cutover**, one PR, after the major release: delete the `_root`
   handler block and the `-v` httpx block; call
   `configure_logging_from_env(_ENV_PREFIX, verbose=...)` and `run_http`;
   remove template#609's `FASTMCP_ENABLE_RICH_LOGGING=false` default from the
   image and systemd unit; rename logging variables in `compose.yml`, with
   `<PREFIX>_LOG_FORMAT=rich` present but commented; update the
   `logging-standard` skill's Scope and Framework sections for the new env
   contract and renderer choice.
8. **Downstream issues**: per-repo log-call migration for the six
   template-generated servers (template#611's table); markdown-vault-mcp also
   deletes `_http_logging.py` and takes the graceful-shutdown value from
   pvl-core; scholar-mcp drops its inherited httpx copy.

## Out of scope

- Fields for third-party records (httpx, `uvicorn.error`, the MCP SDK):
  their templates are not ours, so they render as `message`. `uvicorn.access`
  is the one exception (§3).
- Any OpenTelemetry code in pvl-core (ADR 0003).
- Changes to the middleware's event vocabulary.
