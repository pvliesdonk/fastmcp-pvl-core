# ADR 0003 — OpenTelemetry export: pvl-core owns none of the SDK wiring

- **Status:** Accepted (classification for [#314]; no implementation in
  this document, and none proposed)
- **Date:** 2026-09-10
- **Deciders:** pvl-core maintainers
- **Relates to:** [#314] (the classification request), [#313] (health
  routes — the other "operators cannot see inside a running server" gap),
  ADR 0002 (the `CLAUDE.md` classification test applied to a kwarg surface)

> This is an **implementor design**, not a wire specification. Nothing
> here describes bytes negotiated with a foreign implementation — the
> wire authority for trace export is the OTLP specification, owned by
> OpenTelemetry. Per `CLAUDE.md`, that is why this lives in `docs/adr/`
> and not `docs/specs/`.

---

## 1. Context

[#314] was filed as a one-liner — *"Can we export open telemetry
data?"* — and elaborated into a classification request. FastMCP 4 emits
MCP spans through the OpenTelemetry **API**, but nothing turns them into
exported data. The question this ADR answers is narrow:

> Which part of the OpenTelemetry SDK/exporter wiring, if any, is
> pvl-core-shaped?

The three candidate answers were a pvl-core shape decision (pvl-core
picks the wiring once, downstream calls it), operator configuration (env
vars only, no pvl-core code), or neither. This ADR settles that
question. It does not adopt a telemetry feature, and it proposes no
implementation.

## 2. Research findings

**Version and environment caveat.** The probes below ran in a throwaway
scratch virtualenv (`/tmp/otel-probe`) on Python 3.10
with `fastmcp` 4.0.3, `opentelemetry-sdk` 1.44.0, `opentelemetry-distro`
0.65b0, and `opentelemetry-exporter-otlp-proto-http` 1.44.0. This repo's
own `.venv` pins `fastmcp` 4.0.0 and carries `opentelemetry-api` 1.41.0
only. The findings about *FastMCP* were re-checked against the installed
4.0.0; the findings about the SDK were not, because the SDK is not
installed here at all. Nothing was built to keep.

Except where §2.5 says otherwise, the probes pinned
`OTEL_METRICS_EXPORTER=none` and `OTEL_LOGS_EXPORTER=none` so that only
the traces pipeline was under test. That is a deviation from the
distro's defaults, and §2.5 records what those defaults do instead.

### 2.1 FastMCP ships no SDK bootstrap and no telemetry extra

`fastmcp/telemetry.py` states in its own module docstring that it "uses
only the opentelemetry-api package, so telemetry is a no-op unless the
user installs an OpenTelemetry SDK and configures exporters". The only
`opentelemetry.sdk` references in the package are inside that docstring
and one comment — there is no bootstrap function to delegate to.
`Provides-Extra` in the installed 4.0.0 metadata lists `anthropic`,
`apps`, `azure`, `code-mode`, `gemini`, `openai`, `tasks` — no
telemetry extra.

This matters because it rules out the cheapest possible answer. Where
`_logging.py` can delegate to FastMCP's own `configure_logging`, there
is no equivalent for telemetry.

### 2.2 The OpenTelemetry SDK already ships the bootstrap

A FastMCP server with **no OpenTelemetry import anywhere in its source**,
launched as `opentelemetry-instrument python server.py`, produced the
full MCP span set — `server/discover`, `tools/call add`, `tools/list`
and their `MCP send` counterparts — each carrying
`service.name: probe-svc` taken from `OTEL_SERVICE_NAME`.

The mechanism is `opentelemetry-distro`'s configurator: it reads the
`OTEL_*` environment, constructs the `TracerProvider`, span processor,
sampler and exporter, installs the provider globally, and auto-loads any
installed instrumentation libraries — all before the application module
is imported.

This is the decisive finding. The bootstrap that a pvl-core helper would
have written already exists, is maintained by OpenTelemetry, and is
configured entirely through a standard operator contract.

### 2.3 Zero-code coverage extends to both of #314's open tail items

[#314] listed two `[unverified]` items as possibly needing separate
work. Both are covered by the same wrapper, still with zero application
code:

- **Non-MCP HTTP routes** (the transfer routes, and the [#313] health
  routes) are outside FastMCP's MCP spans. With
  `opentelemetry-instrumentation-starlette` installed, a `GET /health`
  request produced a span with `http.route: /health`, parented in the
  same trace as its `http send` children.
- **Log ↔ trace correlation** with `OTEL_PYTHON_LOG_CORRELATION=true`
  injected `trace_id`, `span_id`, `resource.service.name` and
  `trace_sampled` into log records emitted inside a span.

### 2.4 The family's launch shape accommodates the wrapper

Every family server runs a console script under a shell entrypoint:
`CMD ["scholar-mcp", "serve", "--transport", "http", ...]` with
`ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]`. That entrypoint
ends in `exec gosu appuser "$@"` (root path) and `exec "$@"` (non-root
path) — two seams where an `opentelemetry-instrument` prefix can be
applied conditionally.

Both seams are in the template's `docker-entrypoint.sh.jinja`, not in
pvl-core.

### 2.5 Three sharp edges the operator posture must document

- **The gRPC default.** `OTEL_TRACES_EXPORTER=otlp` resolves to
  `otlp_proto_grpc`. With only the HTTP exporter installed, bootstrap
  raises `RuntimeError: Requested component 'otlp_proto_grpc' not found`,
  prints a traceback — **and the server continues running with no
  traces**. `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` (or
  `OTEL_TRACES_EXPORTER=otlp_proto_http`) is required alongside the
  HTTP-only exporter package.
- **Unreachable collector — visibility depends on correlation.** The
  exporter logs a `Transient error … Connection refused … retrying in
  Ns` WARNING per attempt with exponential backoff, then an
  `ERROR: Failed to export span batch` per dropped batch. Whether an
  operator ever sees them is not obvious:

  - With `OTEL_PYTHON_LOG_CORRELATION=true`, `logging.basicConfig`
    installs a root stderr `StreamHandler` and the failures are loud.
  - With correlation off, the observed root handler set was
    `[<LoggingHandler>]` — the SDK's OTLP log handler and nothing
    else. Because root *has* a handler, `logging.lastResort` never
    fires, and the same run against an unreachable collector printed
    **nothing at all**. Export failure is then effectively silent.

  Neither case involves anything pvl-core configures: root acquires
  those handlers from the SDK bootstrap, not from
  `configure_logging_from_env`, which sets only the root *level*. The
  silent case is the one worth warning operators about — a server that
  looks healthy while exporting no traces.
- **The distro enables all three signals.** `opentelemetry-distro`
  `setdefault`s `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER` and
  `OTEL_LOGS_EXPORTER` to `otlp`, and `OTEL_EXPORTER_OTLP_PROTOCOL` to
  `grpc`. A traces-only posture must therefore pin metrics and logs to
  `none` explicitly: left at the default, the logs pipeline attaches an
  OTLP handler to the root logger and ships the application's log
  records to the collector. Verified — the recipe without those pins
  produced `/v1/logs` export attempts alongside `/v1/traces`, plus
  `LoggingHandler.emit detected recursive logging` when the collector
  was unreachable. Relatedly, an absent `OTEL_EXPORTER_OTLP_ENDPOINT`
  does **not** mean "no export": the HTTP exporter falls back to
  `http://localhost:4318/`.

### 2.6 One genuinely pvl-core-shaped gap (out of scope here)

Log correlation reaches application loggers but **not** anything under
the `fastmcp.*` namespace. Verified: inside a recording span,
`app.domain` logged with `trace_id=6c1b… span_id=3e4d…` attached, while
`fastmcp.server` logged through a Rich handler with no trace fields.

The mechanism is FastMCP's `configure_logging`, which pvl-core's
`configure_logging_from_env` delegates to: it attaches a handler with a
bare `%(message)s` formatter to the `fastmcp` logger and sets
`propagate = False`, so those records never reach the root handler that
the logging instrumentor reconfigured.

The blast radius is larger than "FastMCP's internals". pvl-core's own
request-logging middleware logs to `fastmcp.middleware.requests`
(`_logging_middleware.py:26`), so the family's primary structured
request log — `tool_call_started`, `tool_call_completed`,
`tool_call_failed`, with its `duration_ms` and `error_type` fields —
is exactly the stream that loses trace correlation. Joining a slow
tool call to its trace is the obvious reason to turn correlation on,
and it is the case that does not work.

This sits at a seam pvl-core owns, so it is the one finding here that
could become pvl-core work. It is **not** part of this classification —
it is a logging defect, not SDK/exporter wiring — and was filed
separately as [#319] (§7).

**Resolved in [#319].** The middleware now reads the ambient span itself
and stamps `trace_id` / `span_id` onto its own lines, in both text and
JSON modes, so the stream described above is correlated without
`OTEL_PYTHON_LOG_CORRELATION` and without touching FastMCP's handler.
Records emitted by FastMCP *itself* are still uncorrelated; that part
remains FastMCP's formatter to own.

## 3. Decision

**pvl-core owns no OpenTelemetry SDK, exporter, or bootstrap code.**
Trace export is operator/container configuration.

> **Amended 2026-09-10, resolving [#319].** As originally written this
> paragraph said "no OpenTelemetry code and no OpenTelemetry
> dependency". That was about SDK/exporter wiring, but read literally it
> also barred the §2.6 follow-up this ADR itself filed. pvl-core now
> declares `opentelemetry-api` and imports it in one place: the
> request-logging middleware reads the ambient span to stamp `trace_id`
> and `span_id` on its own log lines. The API is a guaranteed transitive
> (the `mcp` SDK depends on it unconditionally) and creates no span,
> exports nothing, and configures no provider. Everything below about
> the SDK, exporters and the bootstrap stands unchanged.

Applying the `CLAUDE.md` classification test to the SDK/exporter wiring:

> *Would pvl-core be wrong to make this decision itself?*

There is no decision left to make. The endpoint, protocol, sampler,
resource attributes, batching parameters and service name are all
already operator-controlled through standard `OTEL_*` variables that the
SDK reads natively. Operator-side configuration is a separate axis from
the kwarg surface — env vars, not code — and this axis is entirely
populated by a contract pvl-core does not own and should not re-express.

A pvl-core helper would additionally be **pure duplication**. It is not
that such a helper is impossible: called from `main()` after the
application's imports, `StarletteInstrumentor().instrument_app(app)`
instruments an already-constructed app and
`LoggingInstrumentor().instrument(set_logging_format=True)` patches the
record factory — verified, both produced §2.3's `http.route` span and
trace-id injection with no process wrapper. The objection is that the
helper would have to name and wire each instrumentor by hand, and grow
a new branch for every library the family later adds, to re-derive what
`opentelemetry-distro` discovers automatically from installed entry
points. Under the `CLAUDE.md` test that is code with no domain-specific
content — pvl-core re-expressing someone else's decision.

Consequently pvl-core ships:

- no `configure_telemetry_from_env` or equivalent seam,
- no `telemetry` optional extra,
- no `<PREFIX>_`-namespaced telemetry environment variables,
- no spans or span attributes of its own.

What pvl-core does ship is **documentation of the family's one posture**
— a `### Telemetry` section in `README.md`, next to `### Logging`,
recording the operator contract and the two sharp edges from §2.5. That
is consistent with how `### Logging` already documents `FASTMCP_LOG_LEVEL`,
a variable pvl-core also does not own.

`FASTMCP_TELEMETRY_MODE` is likewise an operator/container concern. Its
import-time read (`fastmcp/__init__.py` instantiates `Settings()` at
import) does not constrain anything, because pvl-core exposes no surface
that would want to change it after import. Its default `native` costs
nothing when no SDK is installed: span creation is a no-op.

## 4. The operator contract (documented, not implemented)

| Variable | Owner | Effect |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OpenTelemetry | Collector base URL. Absent ⇒ falls back to `http://localhost:4318/` (§2.5), not "no export". |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | OpenTelemetry | Must be `http/protobuf` with the HTTP-only exporter; the distro defaults it to `grpc` (§2.5). |
| `OTEL_TRACES_EXPORTER` | OpenTelemetry | Already defaulted to `otlp` by the distro; `none` disables traces only. |
| `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER` | OpenTelemetry | Both default to `otlp`. Pin to `none` for the traces-only posture (§2.5). |
| `OTEL_SERVICE_NAME` | OpenTelemetry | Populates `service.name`. |
| `OTEL_RESOURCE_ATTRIBUTES` | OpenTelemetry | Arbitrary resource attributes, including `service.name=…`. |
| `OTEL_PYTHON_LOG_CORRELATION` | OpenTelemetry | `true` injects trace/span ids into log records (§2.6 caveat) and installs a stderr handler via `logging.basicConfig`. |
| `OTEL_SDK_DISABLED` | OpenTelemetry | `true` disables the SDK wholesale. |
| `FASTMCP_TELEMETRY_MODE` | FastMCP | `native` (default) / `propagation_only` / `off`. Read at import. |

Service identity comes from the operator environment —
`OTEL_SERVICE_NAME`, or `service.name` within
`OTEL_RESOURCE_ATTRIBUTES`. Either way it satisfies the foldability rule
directly: no runtime resolution of pvl-core's own distribution name is
involved, because pvl-core resolves nothing.

## 5. Alternatives rejected

**A — a `configure_telemetry_from_env(env_prefix, service_name)` helper
at the `configure_logging_from_env` seam.** This was the expected
answer before the probes. Rejected because it duplicates
`opentelemetry-distro`'s configurator without contributing anything
domain-specific (§3) — hand-wiring instrumentors that the distro finds
by entry point, and needing a pvl-core release each time the family
adds a library worth instrumenting. It would also introduce a
`service_name` argument whose only job is to reproduce
`OTEL_SERVICE_NAME`.

Note this is *not* rejected on the grounds that it cannot work.
Post-import instrumentation is real: `instrument_app()` on a built app
and `LoggingInstrumentor().instrument()` both function from `main()`
(§3). Rejecting it for an impossibility it does not have would be a
weaker argument, and a falsifiable one.

**B — a `telemetry` optional extra pinning the SDK, exporter and
instrumentors, with no code.** A legitimate "the family picks one
posture" argument, and the closest call here. Rejected because pvl-core
would carry beta-versioned contrib packages (`0.65b0`) that it never
imports, coupling its release cadence to OpenTelemetry contrib churn and
making it the owner of Starlette-instrumentor ↔ FastMCP compatibility
for the whole family. The `debug` extra is not this precedent:
`maybe_start_debugpy` imports `debugpy`. Drift prevention across the
family already has a home — the `fastmcp-server-template` copier
template, which owns the image's package set and propagates via
`copier update`.

**C — pvl-core-owned spans on its own seams** (transfer routes, the jobs
surface, auth mode resolution). Deferred, not rejected on principle.
FastMCP's MCP-level instrumentation plus ASGI instrumentation covers the
request path end to end today; there is no observed gap motivating
hand-written spans. Revisit only when a concrete diagnostic question
cannot be answered from the existing span set.

## 6. Consequences

- A family server gets traces by installing the OpenTelemetry packages
  in its image and prefixing its command with `opentelemetry-instrument`.
  No pvl-core release is required to enable telemetry, and none can
  block it.
- pvl-core's dependency surface, public API and `__all__` are unchanged
  by [#314]. There is no version bump associated with this ADR.
- The family's coherence on telemetry is enforced by the template, not
  by pvl-core code. If servers drift, the fix lands in the template.
- pvl-core takes on no responsibility for OpenTelemetry version
  compatibility.

## 7. Follow-ups

Filed separately rather than folded into [#314], per the scope boundary
in the issue:

1. **Template** — [fastmcp-server-template#606]: conditional
   `opentelemetry-instrument` prefix at the two `exec` seams in
   `docker-entrypoint.sh.jinja`, the image package set, and the operator
   documentation for the `OTEL_*` contract.
2. ~~**pvl-core**~~ — **resolved.** [#319]: the `fastmcp.*` log-correlation gap from §2.6,
   which costs the request-logging middleware's own `tool_call_*` stream
   its trace and span ids. A logging-seam defect, independent of whether
   telemetry is ever adopted.

[#319]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/319
[fastmcp-server-template#606]: https://github.com/pvliesdonk/fastmcp-server-template/issues/606

## 8. Limitations of this study

Stated so a later reader does not over-read the evidence:

- Probes ran against a synthetic FastMCP server in a scratch venv, not
  a template-generated server.
- The wrapper was **not** exercised through `gosu appuser "$@"`, nor in
  a container at all.
- Metrics and logs over OTLP were out of scope, per [#314]. Traces only.
- Zero-code instrumentation normally activates via a process wrapper, so
  a **stdio** server launched by an MCP client (`uvx scholar-mcp`) needs
  the wrapper in the client's own command configuration. It is not the
  only route: pointing `PYTHONPATH` at the SDK's
  `opentelemetry/instrumentation/auto_instrumentation` directory
  activates the same `sitecustomize` bootstrap with no wrapper binary
  (verified — full span set, env vars only). Noted as a limitation of
  the recommended posture, not as a driver; the family's deployment
  target is containers over HTTP.

[#313]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/313
[#314]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/314
