# Root logging ownership — one handler chain, one format

**Date:** 2026-09-11
**Issues:** umbrella issue to be filed; resolves
[#323](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/323) and
[fastmcp-server-template#608](https://github.com/pvliesdonk/fastmcp-server-template/issues/608)
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
- **Container line-wrapping** (template#608). Rich defaults to 80 columns on
  a non-TTY; structured lines wrap across three rows.
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
unchanged byte for byte. When stderr is not a TTY, pvl-core sets the Rich
`Console` width to 200 columns instead of accepting the 80-column default —
the width at which the #608 investigation found a structured line stops
wrapping — so a structured line renders on one row (template#608).

**JSON mode.** Plain `StreamHandler` on stderr with a JSON formatter; one
object per line. Envelope, in order:

- `ts` (ISO-8601 UTC), `level`, `logger`
- middleware records: `event`, then the event's fields
- all other records: `message` (the formatted message)
- `trace_id`, `span_id` when a valid span context is in scope
- `exception` when the record carries a traceback

`uvicorn.access` records, recognised by uvicorn's argument shape (§4), are
emitted with `client`, `method`, `path`, `status` fields rather than a
formatted string.

**Layering change.** The middleware stops rendering. It attaches the event
name and field dict to the record under a namespaced attribute, and the
formatter renders them. The middleware's `structured=` constructor flag and
the `FASTMCP_ENABLE_RICH_LOGGING` read in `_middleware.py:40-43` are removed.

**Known limitation.** The family logging standard
(`logging-standard` skill: "event name as first token, then key=value pairs
via `%s` formatting") produces pre-rendered text. In JSON mode, domain records
therefore appear as `{"message": "event_name key=value …"}` — valid JSON, not
exploded fields. Structured domain fields would need a new producer-side
convention in the standard; that is out of scope here.

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
in deployed containers, and the auto default produces JSON on a non-TTY.

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
  exception, trace ids); non-TTY Rich does not wrap a long line.
- **Bridge** — exactly one `WARNING`, naming the prefixed variable, only when
  the fallback is used.
- **Noise policy** — per-logger levels at INFO and DEBUG, including httpx and
  httpcore.

Run locally on Python 3.10 and on the top of the CI matrix; TTY detection
and Rich's width handling are interpreter- and environment-sensitive.

## §8 Staging and release

Sequenced PRs to `main`, one major release, one template cutover.

1. **PR 1 — topology** (breaking). Root ownership with the Rich renderer
   reproduced at root, fastmcp neutralised, `configure_logging_from_env(env_prefix)`
   and the bridge, §4 policy, the new fixture.
2. **PR 2 — `run_http` and `ServerConfig.shutdown_grace_s`** (breaking).
3. **PR 3 — rendering** (breaking). JSON formatter, auto selection, non-TTY
   width, middleware emits fields; `structured=` and
   `FASTMCP_ENABLE_RICH_LOGGING` removed. Closes template#608.
4. **PR 4 — #323 recipe**, from a literal run; README and ADR 0003 note.
   Folds into PR 1 if small. Closes #323.
5. **Release** the major version.
6. **Template cutover**, one PR: delete the `_root` handler block and the
   `-v` httpx block; call `configure_logging_from_env(_ENV_PREFIX, verbose=...)`
   and `run_http`; rename logging variables in `compose.yml`, with
   `<PREFIX>_LOG_FORMAT=rich` present but commented; update the
   `logging-standard` skill to describe format as a renderer choice.
7. **Downstream issues**: markdown-vault-mcp (delete `_http_logging.py`;
   graceful-shutdown value now from pvl-core), scholar-mcp (inherited httpx
   copy).

## Out of scope

- Structured fields for domain log records (§3, known limitation).
- Any OpenTelemetry code in pvl-core (ADR 0003).
- Changes to the middleware's event vocabulary.
