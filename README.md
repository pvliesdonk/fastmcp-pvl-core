# fastmcp-pvl-core

The opinionated shared implementation for the `pvliesdonk/*-mcp`
server family. `fastmcp-pvl-core` owns the shape of cross-cutting
concerns — auth, middleware, logging, config, and server-factory
builders — and exposes narrow hooks to downstream servers for
domain-specific behaviour. Downstream conforms
to the shape; pvl-core does not adapt to downstream preferences. See
[Design principles](#design-principles) for the rationale and the
classification test that follows from it.

## Ecosystem

- [`fastmcp-server-template`](https://github.com/pvliesdonk/fastmcp-server-template) —
  copier template that scaffolds new FastMCP servers on top of this library.
- Active consumers:
  [`markdown-vault-mcp`](https://github.com/pvliesdonk/markdown-vault-mcp),
  [`scholar-mcp`](https://github.com/pvliesdonk/scholar-mcp),
  [`image-generation-mcp`](https://github.com/pvliesdonk/image-generation-mcp).
- Public API changes here propagate to consumers via periodic
  `copier update` runs against the template.
- See the template's README for the update flow and the expected project
  shape.

## Design principles

`fastmcp-pvl-core` is not a buffet of helpers downstream picks from
à la carte. It is the load-bearing layer that fixes the shape of
cross-cutting concerns across the server family so the family stays
coherent as it grows. Five principles follow from that role; a
sixth keeps the exit clean for forks that leave the family.

### Shape decisions live in pvl-core

Tool names, parameter shapes, route structures, capability
declarations, error envelopes, environment-variable contracts —
pvl-core picks one shape and downstream conforms. If two downstream
servers would each prefer a different shape, the resolution is for
pvl-core to pick one and migrate the others to it, not for pvl-core
to grow an override kwarg.

### Hooks expose domain-specific behaviour only

A hook like *"where in my storage model do these bytes go?"* is
appropriate — pvl-core cannot know the answer for a particular
downstream. A hook like *"what should this tool be called?"* or
*"what HTTP status code should an oversize body return?"* is not —
those are shape decisions pvl-core owns, and downstream accepts them.

The test for any proposed kwarg on a `register_*` helper, `Build*`
factory, or middleware constructor: **would pvl-core be wrong to
make this decision itself?** If pvl-core could pick a sensible value
and downstream has no domain-specific basis to disagree, pvl-core
picks it — no kwarg. If pvl-core *literally cannot* answer because
the answer is about the downstream's domain, the kwarg exists and is
not optional unless the entire feature is opt-in. There is no third
bucket of "pvl-core has a default but downstream can override."

Operator-side configuration (TTL ceilings, max body sizes, listening
ports, debug flags) is a separate axis — environment variables, not
kwargs. The kwarg surface is purely domain hooks.

If a proposed kwarg mixes the two — a legitimate hook bundled with an
override of shape — split it: keep the hook, drop the override. PRs
that grow override kwargs disguised as hooks are rejected.

### Spec docs are protocol extensions, not design docs

Files under `docs/specs/` describe the wire format and behaviour
requirements between independently developed servers — what bytes
move between systems and under what rules. Implementation choices
that pvl-core happens to make (lazy materialisation strategies, route
mechanics, framework-specific helpers, downstream tool naming and
registration mechanics) do not belong in a spec doc; they belong in
pvl-core's own implementor docs and code comments. Real spec gaps are
resolved through a proper spec evolution — a new release with the
version field bumped — not through inline amendments to a published
version.

### Pre-existing downstream conflicts resolve by migration

If a downstream server has already shipped a different *shape* (a
differently named tool, a divergent parameter, a custom error
envelope), the resolution is for the downstream to migrate.
pvl-core does not grow a compatibility shim to spare downstream the
migration cost, even when the migration is large. If the migration
cannot land immediately, file a tracked downstream issue and ship
the breaking change in pvl-core anyway — the umbrella tracker
coordinates the cutover and the
[`fastmcp-server-template`](https://github.com/pvliesdonk/fastmcp-server-template)
scaffold updates carry the new shape forward to fresh consumers.

This applies to *shape* divergence (the things owned by pvl-core).
Domain-specific divergence between downstreams is expected and does
not require any migration — downstreams are *supposed* to differ in
domain logic.

### Downstream reuses pvl-core; it does not reimplement the protocol

Downstream servers reuse pvl-core's implementation of the shared
cross-cutting protocols — auth, logging, and the rest. They do not
reimplement a wire protocol independently. The specs under
`docs/specs/` are the wire authority; pvl-core is their single shared
implementation. No implementation is "the reference" — not pvl-core's
either; the spec is.

If pvl-core's implementation is wrong, or diverges from a spec, the fix
is to correct pvl-core centrally — one change, every downstream follows
— or to evolve the spec. A downstream that believes pvl-core is wrong
files the issue against pvl-core; it does not fork the behaviour and
reimplement it locally.

### Keep pvl-core cleanly foldable

A fork is not a downstream. The MIT licence lets anyone vendor
pvl-core into their own tree — to take over a single server when the
family is no longer maintained, or to run their own opinionated
variant. That exit ramp is kept cheap on purpose: the seams that make
pvl-core foldable (relative intra-package imports, no runtime lookups
of its own package name, identity passed in rather than hard-coded, a
narrow public surface) are the same seams that keep it a clean
load-bearing layer. Foldability is a modularity property, not a
coherence compromise — and never an excuse to flatten pvl-core's own
abstractions "in case someone forks"; collapsing those is fork-side
work.

> Planning to fork and cut the dependency? See [docs/forking.md](docs/forking.md)
> for the fold-in recipe and what a single-server fork can safely collapse.

## API stability

This package is stable at 2.x and follows
[semantic versioning](https://semver.org/): breaking changes bump the
major version, new features bump the minor, bugfixes bump the patch.
"Public API" means symbols re-exported from the top-level
`fastmcp_pvl_core` package (see `__all__`), which intentionally
covers both the runtime surface (auth, middleware, factory builders,
env/config helpers) and the CLI parser helpers consumed by downstream
`server.py` entrypoints. Modules prefixed with `_` are internal and
may change without a major-version bump.

## Install

```bash
uv add fastmcp-pvl-core
# If you use RemoteAuthProvider mode:
uv add "fastmcp-pvl-core[remote-auth]"
# For attaching a remote Python debugger inside a container image:
uv add "fastmcp-pvl-core[debug]"
```

## Usage

See `src/fastmcp_pvl_core/` for the full surface. Typical usage:

```python
from fastmcp import FastMCP
from fastmcp_pvl_core import (
    InstructionRole, ServerConfig, apply_tool_visibility, build_auth,
    finalize_instructions, instructions_for, wire_middleware_stack,
)

config = ServerConfig.from_env("MY_APP")
mcp = FastMCP(name="my-app", auth=build_auth(config))
wire_middleware_stack(mcp)

instructions = instructions_for(mcp)
instructions.identity("my-app", "A widget service.")
instructions.add(
    "This instance is READ-ONLY.",
    role=InstructionRole.INSTANCE,
)
instructions.documentation("https://example.com/my-app/llms.txt")
# ... register tools; core register_* helpers add their own workflow snippets ...
apply_tool_visibility(mcp, config)
finalize_instructions(mcp, config, env_prefix="MY_APP")
```

### Instructions (model-facing guidance)

Instructions carry what no single tool description can carry: identity, a
documentation pointer, cross-tool workflows, and enforced instance facts.

| Role | Owner | Meaning | Tool dependencies |
|---|---|---|---|
| `IDENTITY` | pvl-core shape, template values | Deployed server and product identity | forbidden |
| `ROUTING` | operator | Data or domain this deployment serves | forbidden |
| `INSTANCE` | domain/core | Enforced configuration facts and limits | allowed when the fact depends on named tools |
| `POLICY` | operator | Deployment-specific behavioral policy | forbidden |
| `CAPABILITIES` | domain/core | Classes of work the server performs | allowed |
| `WORKFLOWS` | domain/core | How multiple tools compose | allowed |
| `DOCUMENTATION` | template/core | Where complete documentation lives | forbidden |

Add domain snippets with
`instructions_for(mcp).add(text, role=..., requires_tools=...)`. General
contributors use only `INSTANCE`, `CAPABILITIES`, and `WORKFLOWS`; pvl-core
reserves identity, operator routing/policy, and documentation so their shape
and ownership stay consistent. A snippet requiring a tool FastMCP hides through
global provider, mount, namespace, or ordered visibility transforms is dropped
at `finalize_instructions`. Per-session transforms and per-subject authorization
remain outside this static instruction string; guidance for an auth-gated tool
must read naturally when that tool is unavailable to the current caller.
Finalization runs during synchronous server construction, before entering an
event loop, so FastMCP can evaluate its asynchronous global listing path safely.

The rendered survival order is deployment identity, operator routing, enforced
instance facts, operator policy, capabilities, workflows, then documentation.
The operator environment contract is:

- `{PREFIX}_SERVER_NAME` is passed by the server factory to
  `identity(server_name, product_description)` and identifies the deployment.
- `{PREFIX}_INSTANCE_DESCRIPTION` concisely describes which material or
  responsibility distinguishes this instance for routing.
- `{PREFIX}_INSTRUCTIONS_EXTRA` supplies deployment-specific behavioral policy.
- `{PREFIX}_INSTRUCTIONS` is a deprecated full replacement. When set, it
  ignores both additive operator variables and logs a warning naming them.

pvl-core measures instructions in UTF-16 code units, matching JavaScript
`String.length`. Generated guidance targets at most 1,536 units, reserving 512
units for normal operator routing and policy within Claude Code's known 2,048
unit boundary. Exceeding either threshold logs a role-level warning; pvl-core
does not truncate instructions or fail startup. Use `utf16_code_units`,
`GENERATED_INSTRUCTIONS_TARGET_UTF16`, and
`CLAUDE_CODE_INSTRUCTIONS_LIMIT_UTF16` to enforce the same profile in tests.

### Tool visibility (operator allow-/denylist)

Every exposed tool costs context in the connecting MCP client, so operators
can trim what an instance exposes with two env vars, each a comma-separated
list of explicit tool names:

- `{PREFIX}_TOOLS_ALLOW` — the instance exposes *only* these tools.
- `{PREFIX}_TOOLS_DENY` — these tools are hidden.

Hidden tools disappear from `tools/list` **and** are rejected on
`tools/call`. Setting both variables is a startup `ConfigurationError` (an
allowlist already expresses every exclusion). Individual names matching no
registered tool are inert, so one operator config survives releases that add
or remove tools — but an allowlist that leaves *zero* tools exposed (fully
mistyped or fully stale) logs a startup `WARNING`, since that would
otherwise present as a silent total tool outage. Resources, resource
templates, and prompts are unaffected.

Servers wire it in with one call, after any visibility adjustments of their
own so the operator's lists win:

```python
from fastmcp_pvl_core import apply_tool_visibility

apply_tool_visibility(mcp, config)   # config: ServerConfig.from_env("MY_APP")
```

### Logging

`configure_logging_from_env(env_prefix, *, verbose=False)` is pvl-core's
console logging owner. It installs one handler chain — its shape depends on
the resolved output format, below — on the **root** logger, and neutralises
FastMCP's own logging (`fastmcp.settings.log_enabled = False`) so every
logger in the process, `fastmcp.*` included, propagates into that one chain
instead of rendering through its own. Repeated calls leave exactly one
chain at root, and nothing pvl-core installs writes to stdout.

That ownership is complete under stdio and HTTP alike. A server started
through [`run_http`](#serving-over-http-run_http) pins `log_config=None`,
so uvicorn never runs its own `dictConfig` and never reinstalls a handler
on `uvicorn.access`/`uvicorn.error` — those loggers stay on the root chain
this module installed, exactly like every other logger in the process.

The log level resolves in this order:

1. `verbose=True` (the `-v` CLI flag) forces `DEBUG`.
2. Otherwise `{PREFIX}_LOG_LEVEL`, case-insensitive.
3. Otherwise the legacy `FASTMCP_LOG_LEVEL` — a migration bridge, honoured
   only when `{PREFIX}_LOG_LEVEL` is unset, and kept for one major release.
   Using it logs a single `log_level_env_deprecated` warning naming the
   prefixed replacement; when both variables are set, `{PREFIX}_LOG_LEVEL`
   wins silently.
4. Otherwise `INFO`.

An unrecognised level name falls back to `INFO` rather than raising.

#### Output format

`{PREFIX}_LOG_FORMAT` picks how every record in the process renders, case-
insensitively:

- **`rich`** — a `RichHandler` pair (one for normal records, one that
  renders only tracebacks), producing a human-readable `event key=value`
  line per record.
- **`json`** — a single handler emitting one JSON object per record, for a
  log aggregator such as the ELK stack or Splunk.
- **Unset or unrecognised** — auto: `rich` when stderr is a terminal,
  `json` otherwise. The case that matters in practice is a container: its
  stderr is a pipe, not a terminal, so it gets JSON with no configuration
  at all. pytest and CI runners are non-terminals too — a downstream test
  suite that asserts Rich-shaped stderr needs `{PREFIX}_LOG_FORMAT=rich`
  set explicitly (`tests/test_logging.py` in this repo does exactly that).

Both renderers recover a record's fields the same way: by parsing the
template the developer wrote (`record.msg`) against [the log-call
grammar](#the-log-call-grammar) and pairing each placeholder with its
value from `record.args`, never by re-reading a value out of rendered
text — so a value containing whitespace or a quote cannot corrupt the
result, and a type like `int` or `bool` survives into JSON instead of
becoming a string. A record whose call does not follow the grammar falls
back to its plain formatted message — `message` in JSON, the formatted
text in Rich — the same way for both modes. That fallback is the common
case for third-party records (uvicorn, the MCP SDK, FastMCP itself) and,
today, for most of pvl-core's own calls too — see [the log-call
grammar](#the-log-call-grammar) for the current count and the tracking
issue.

One call, both modes, captured from an actual run of
`configure_logging_from_env`:

```
cache_write key="user profile" ttl=3600 hit=True
```

```json
{"ts": "2026-09-16T18:34:26.055997+00:00", "level": "INFO", "logger": "demo.cache", "event": "cache_write", "key": "user profile", "ttl": 3600, "hit": true}
```

The quoting around `"user profile"` is the same rule the request-logging
middleware and JSON mode already applied — a value containing whitespace
or a `"` renders quoted so the line stays one unambiguous `key=value`
record.

At `INFO` and above, three noisy third-party loggers are demoted — never
below the operator's own chosen level — so they do not flood the operator
log stream: `httpx`, `httpcore`, and `mcp.server.lowlevel.server` — the MCP
SDK's `Processing request of type ...` line. All three reappear (`NOTSET`)
at `DEBUG`. `uvicorn.error` is never touched, at any level — it carries
genuine bind / startup failures.

`uvicorn.access` (the HTTP access log) gets a filter instead of a demotion,
because a level cannot express "failures only". At every level except
`DEBUG` the filter keeps only records with status `>= 400` — a `401` from
auth, a `404`, a `413`, a readiness `503` — and drops the `200`s that would
otherwise duplicate the request-logging middleware's own lines. Its own
level stays `NOTSET`, inheriting root: raising `{PREFIX}_LOG_LEVEL` above
`INFO` is meant to silence access lines entirely, kept or not — the filter
decides *which* requests are worth a line, the level decides *whether* the
operator wants request lines at all. At `DEBUG` the filter
is always still installed and keeps every status, `200` included — the
redaction is exactly as unconditional as at any other level, it is only
*which* requests reach the log that verbosity changes.

Every record the filter sees is also rewritten, because uvicorn logs the
full `path?query`:

- **The query string is stripped entirely.** No route in this family carries
  diagnostic query parameters — the ones that do are the OAuth routes, where
  it is an authorization code or PKCE material.
- **The segment after `/transfer/` is masked** to `transfer/<redacted>`.
  pvl-core's transfer token lives in the path, and an expired link produces
  exactly the 4xx this filter keeps by default; a *live* link produces a
  `2xx`, which is visible only at `DEBUG` — so the redaction has to hold
  there too.

Both redactions apply unconditionally, including at `DEBUG`: whether a
request line is worth logging is a preference the level and the
status filter both express, but whether a credential may appear in that
line is not a preference at all, so it is never tied to verbosity.

In JSON mode, a kept `uvicorn.access` record renders as fields rather than
a `message` string — `client`, `method`, `path` (already redacted by the
filter above) and `status` (an `int`, not a formatted code) — since the
filter parses and attaches them itself: uvicorn owns that record's
template, so it can never conform to [the log-call
grammar](#the-log-call-grammar) the way a first-party call can. Captured
from an actual filtered record:

```json
{"ts": "2026-09-16T18:30:55.624266+00:00", "level": "INFO", "logger": "uvicorn.access", "client": "127.0.0.1:54321", "method": "GET", "path": "/transfer/<redacted>", "status": 404}
```

One logger is capped in the other direction. `docket.worker` — pydocket's
background-task worker, which every consumer inherits through the
`fastmcp[tasks]` base dependency — logs a record per poll iteration at its
250 ms default check interval, roughly 2500 lines/minute on a queue that
never receives a job. At `DEBUG` it is pinned to `INFO`, so its startup and
lifecycle records still appear while the idle poll trace does not; at every
other level it is untouched. An operator debugging the task queue itself
restores the full stream after the call:

```python
configure_logging_from_env("MY_APP", verbose=True)
logging.getLogger("docket.worker").setLevel(logging.DEBUG)
```

Handler ownership is exclusive over the console only: pvl-core removes any
pre-existing root handler that writes to `stdout`/`stderr` (the
double-render source when `opentelemetry-instrument` has installed one) but
leaves every other handler at root untouched. An OTLP, file, or syslog
handler an operator attached at root survives `configure_logging_from_env`
— and, because `fastmcp.*` now propagates instead of rendering through its
own handlers, it receives `fastmcp.*` records too, not just the domain's
own.

`build_auth` announces the resolved auth mode once per call — once per server
in the normal case — on every resolution path, whether the mode came from
`AUTH_MODE` or from auto-detection:

```
auth_mode_resolved mode=oidc-proxy source=auto-detected
auth_mode_resolved mode=remote source=explicit
```

The level is chosen by the provider `build_auth` ends up with, not by the
mode. Any server that ends up with no provider accepts unauthenticated
connections, and that announces at `WARNING` rather than `INFO` so an
operator sees it without raising the log level:

```
auth_mode_resolved mode=none source=auto-detected — server accepts unauthenticated connections
auth_mode_resolved mode=oidc-proxy source=explicit — server accepts unauthenticated connections
```

The second line is the case a mode-derived level would miss: `AUTH_MODE`
selected `oidc-proxy`, the client credentials were absent, and the builder
returned no provider — so the server starts unauthenticated while its
resolved mode says otherwise. That fall-through is tracked as #316; the
announcement makes it visible but does not change whether the server starts.

A builder that raises announces too, before the exception propagates, so a
server that fails to start still says which mode it was building:

```
auth_mode_resolved mode=remote source=explicit — auth provider construction failed; server will not start
```

`resolve_auth_mode` never announces the mode. It is a public export a caller
may invoke any number of times, so announcing from inside it would emit the
line once per call rather than once per server. Its one remaining log is an
`auth_mode_unknown` warning when `AUTH_MODE` names a value it does not
recognise.

`wire_middleware_stack` installs a single conforming request-logging
middleware. Every line it emits starts with a bare snake_case event name,
followed by `key=value` pairs, with request timing carried inline:

```
tool_call_started   tool=read method=tools/call source=client
tool_call_completed tool=read duration_ms=68.57
tool_call_failed    tool=read duration_ms=109.84 error_type=ValueError error="Section '1.3' not found"
```

Non-tool messages use a generic `request_*` / `notification_*` vocabulary
keyed by `method=`. Rendering is process-wide — see [Output
format](#output-format) above.

When an OpenTelemetry span is in scope, every line also carries the ids
needed to join it to that trace:

```
tool_call_completed tool=read duration_ms=68.57 trace_id=dc538b4bb2b968a6017c12d54c45bfb8 span_id=7108b0b132270b25
```

This needs no configuration. Both fields are omitted whenever no valid
span context is in scope — the usual case with no OpenTelemetry SDK
installed — so an untraced server's output is byte-identical to the
lines above.

"No SDK" and "no span" are not quite the same thing, though: FastMCP
extracts an inbound `traceparent` from request `_meta` without requiring
an SDK, so a client that propagates trace context gets correlated lines
even from an otherwise untraced server. In that case `span_id` is the
caller's span, because the server created none of its own.

See [Telemetry](#telemetry-opentelemetry-traces) for enabling export.

### Serving over HTTP (`run_http`)

`run_http(app, *, config, host=None, port=None)` replaces a direct
`uvicorn.run(...)` call. The caller still builds the ASGI app itself —
`run_http` only runs it:

```python
from fastmcp_pvl_core import ServerConfig, build_event_store, run_http

config = ServerConfig.from_env("MY_APP")
app = mcp.http_app(
    path="/mcp",
    event_store=build_event_store("MY_APP", config),
)
run_http(app, config=config)
```

Three settings are pvl-core's to pin, not the operator's or the caller's:

- **`log_config=None`.** uvicorn's default `dictConfig` reinstalls its own
  handler on `uvicorn.access`/`uvicorn.error` at server start, undoing the
  root chain `configure_logging_from_env` installed. Pinning it to `None`
  means uvicorn never runs that `dictConfig` and never reinstalls a
  handler, so the [Logging](#logging) section's guarantees hold under
  HTTP exactly as they do under stdio.
- **`lifespan="on"`.** FastMCP's startup and shutdown hooks run through the
  ASGI lifespan protocol; a server started with this off is broken, not
  differently configured, so it is not a choice pvl-core leaves open.
- **`access_log` is deliberately left alone.** It keeps uvicorn's own
  default rather than being pinned to `True` or `False`, because whether an
  access line is worth printing is "failures only", which a boolean cannot
  express. That decision is made by the `_AccessLogFilter` installed on
  `uvicorn.access` instead — see [Logging](#logging).

`{PREFIX}_SHUTDOWN_GRACE_S` (default `3`, minimum `0`) sets
`timeout_graceful_shutdown`: how long, in seconds, a SIGTERM may spend
draining in-flight requests before the server exits. Set it no higher than
the orchestrator's own termination grace period (Kubernetes'
`terminationGracePeriodSeconds` or equivalent) — a value that exceeds it
just means the orchestrator does the killing instead of uvicorn doing the
draining.

`host` and `port` come from `config` (itself `{PREFIX}_HOST` /
`{PREFIX}_PORT`, defaulting to `127.0.0.1` / `8000`) unless the caller
passes an explicit override — typically a `--host`/`--port` CLI flag that
outranks the environment. `None` means "not given" and falls back to
`config`; `0` is a real value, "bind any free port", and is never treated
as unset.

### The log-call grammar

Every first-party log call follows one shape, so a log consumer can read it
as fields rather than a sentence:

```python
logger.info("cache_write key=%s ttl=%d", key, ttl)
```

An event name in snake_case, then `name=value` fields — each value either a
single `%`-conversion or a fixed token with no space, `%` or `=`. Prose, a
compound placeholder (`attempt=%d/%d`), a unit suffix (`waiting=%.1fs`) and
`%%` are all outside it.

`find_nonconforming_log_calls` reports calls that break it, so a project can
fail its build rather than find out from an aggregator:

```python
from pathlib import Path

from fastmcp_pvl_core import find_nonconforming_log_calls


def test_log_calls_conform():
    src = Path(__file__).parents[1] / "src"
    assert find_nonconforming_log_calls(src) == []
```

(`parents[1]` assumes the test file lives at `tests/test_*.py`, one level
below the project root that contains `src/`; adjust the index to match
where your test file actually sits.) `find_nonconforming_log_calls` raises
`NotADirectoryError` if the path does not exist or is not a directory,
rather than reporting a clean, unscanned tree as conforming.

It parses source with `ast` and imports nothing from the tree it scans. Each
violation carries `path`, `line`, `reason` and — except for an f-string or
other non-literal message, where it is `None` — the offending `template`.

pvl-core's own log calls do not yet follow this grammar — 36 of them predate
it — and migrating the codebase is tracked as [#328](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/328).

### Telemetry (OpenTelemetry traces)

pvl-core ships **no** OpenTelemetry SDK, exporter, or bootstrap code.
Trace export is operator and container configuration, not a library
concern — see [ADR 0003](docs/adr/0003-opentelemetry-classification.md)
for the reasoning. This section records the posture so the family
converges on one way of doing it.

FastMCP instruments itself using the OpenTelemetry *API* only, so its MCP
spans are a no-op until an SDK is installed and configured. The supported
way to supply one is OpenTelemetry's own zero-code wrapper — no
application code, and no `import opentelemetry` anywhere in your server:

```dockerfile
CMD ["opentelemetry-instrument", "my-mcp", "serve", "--transport", "http"]
```

with the SDK packages in the image:

```
opentelemetry-distro
opentelemetry-exporter-otlp-proto-http
opentelemetry-instrumentation-starlette   # spans for non-MCP HTTP routes
opentelemetry-instrumentation-logging     # trace ids in log records
```

The wrapper discovers and activates every installed instrumentor for
you. pvl-core offers no `configure_telemetry_from_env()` equivalent
because such a helper would have to hand-wire each instrumentor by name
and gain a new branch for every library the family adds — duplicating
`opentelemetry-distro` with nothing domain-specific of its own.

**`opentelemetry-distro` turns on all three signals.** It `setdefault`s
`OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER` *and* `OTEL_LOGS_EXPORTER`
to `otlp`, and the protocol to `grpc`.

This section describes **traces first**, not traces forever — metrics and
logs are intended too, sequenced behind traces rather than excluded. Start
by pinning the other two off, so a first rollout has one signal to reason
about, and turn them on deliberately:

```
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318
OTEL_SERVICE_NAME=my-mcp
OTEL_METRICS_EXPORTER=none
OTEL_LOGS_EXPORTER=none
OTEL_PYTHON_LOG_CORRELATION=true
```

Leaving `OTEL_LOGS_EXPORTER` at its default ships **your application's
log records to the collector**, because the logs pipeline attaches an
OTLP handler to the root logger. That may well be what you want, but it
is easy to enable by accident before you have decided.

It is also, today, **incomplete**: because the handler sits on the *root*
logger and FastMCP sets `propagate = False` on the `fastmcp` logger,
enabling log export silently drops the whole `fastmcp.*` namespace —
including the request log shown under [Logging](#logging). Application
records are exported; the server's own structured request stream is not.
Tracked as [#323](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/323).

| Variable | Effect |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector base URL. **Absent ⇒ falls back to `http://localhost:4318`**, not "off". |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | **Set to `http/protobuf`** with the HTTP exporter above; the distro defaults it to `grpc`. |
| `OTEL_TRACES_EXPORTER` | Already `otlp` under the distro. Set `none` to disable traces (does not affect logs/metrics). |
| `OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` | Set `none` for a traces-only posture (see above). |
| `OTEL_SERVICE_NAME` | Populates `service.name`. `OTEL_RESOURCE_ATTRIBUTES=service.name=…` sets it too. |
| `OTEL_PYTHON_LOG_CORRELATION` | `true` injects `trace_id` / `span_id` into log records — and calls `logging.basicConfig`, adding a stderr handler with OpenTelemetry's own text format. |
| `OTEL_SDK_DISABLED` | `true` disables the SDK wholesale. |
| `FASTMCP_TELEMETRY_MODE` | `native` (default), `propagation_only`, or `off`. Read at import — set it in the container environment, not in code. |

Three failure modes are worth recognising before you enable this:

- **`OTEL_TRACES_EXPORTER=otlp` means gRPC by default.** Paired with the
  HTTP-only exporter package it raises at startup, prints a traceback,
  and the server then runs on **with no traces at all**. Setting
  `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` is what prevents this.
- **An unreachable collector may say nothing.** The exporter logs a
  `Transient error … Connection refused … retrying in Ns` warning per
  attempt plus an error per dropped batch — but whether those reach
  stderr depends on `OTEL_PYTHON_LOG_CORRELATION`, which is what
  installs a stderr handler on the root logger. With correlation on
  they are loud (and multiply across `/v1/traces` and `/v1/logs` if the
  logs pipeline is left enabled). With it off, the root logger's only
  handler is the SDK's own OTLP one, and a server exporting nothing
  looks perfectly healthy. Verify your first deployment against the
  collector rather than trusting the logs.
- **Correlation reformats the log stream.** The stderr handler it
  installs uses OpenTelemetry's text format, which mixes with the
  one-JSON-object-per-record output described above under
  `{PREFIX}_LOG_FORMAT=json`.

`OTEL_PYTHON_LOG_CORRELATION` reaches your own loggers but **not**
anything under the `fastmcp.*` namespace, because FastMCP attaches a bare
`%(message)s` handler to that logger and stops propagation.

pvl-core's request log is the exception, and it needs no variable at all:
`wire_middleware_stack`'s `tool_call_*` / `request_*` / `notification_*`
lines stamp `trace_id` and `span_id` themselves whenever a valid span is
in scope, in both text and JSON modes. FastMCP's *own* records remain
uncorrelated.

### Health and readiness routes

`register_health_routes` serves two unauthenticated routes so a container
orchestrator can probe for more than an open socket. They sit outside the MCP
mount and outside auth, which is what a probe needs: the MCP endpoint still
answers `401` while these answer normally.

```python
from fastmcp_pvl_core import register_health_routes

register_health_routes(
    mcp, config,
    server_version=__version__,        # the name comes from mcp.name
    http_path=args.http_path,          # the same value passed to mcp.run
    env_prefix="MY_APP",
    checks={"upstream_key": lambda: keepalive.last_ok},   # optional
)
```

The two routes answer different questions, and conflating them is how a
temporarily unreachable Redis gets "fixed" by restarting a healthy process:

| Route | Question | Behaviour |
|---|---|---|
| `<prefix>/health` | Is the process serving? | Static, no I/O, `200` while it serves. A failure means restart me. |
| `<prefix>/health/ready` | Can it do its job? | Runs every check; `503` if any fails. A failure means take me out of rotation. |

`<prefix>` is the mount path with a conventional trailing `mcp` segment
removed, so `/myserver/mcp` publishes `/myserver/health` and the default `/mcp`
publishes `/health`. A mount that is not the conventional segment keeps its
whole path, so `/scholar` publishes `/scholar/health` rather than colliding with
every other single-segment mount at the root. This is derived rather than fixed
at the host root because a custom route registers at the ASGI app root
regardless of where MCP is mounted, and two servers sharing a hostname would
otherwise collide on `/health`.

`checks` is a domain hook. pvl-core registers one check of its own, `kv_store`,
which writes a short-lived key so a backend that has silently vanished is
detected — a read alone returns `None` from a store whose directory has been
deleted, which is the failure this route exists to catch, while a write raises.
There is no readback: a write that returns has been accepted on every supported
backend, and reading it back would assume read-your-write, which DynamoDB's
default `get_item` and a Mongo secondary read do not give. It covers the event
store too, since both resolve through the same factory. It does *not* cover the
task backend, which resolves `FASTMCP_DOCKET_URL` and `tasks_url` ahead of the
shared KV URL and opens its own client, nor a backend that fell back to
`memory://` at startup because its state directory was unusable. pvl-core cannot know
whether *your* upstream API key is still valid or your index has loaded, so
those are yours to supply. A check is any zero-arg callable, sync or
async: returning falsy or raising means not-ready, and a raise never fails the
route. Checks run concurrently, so a probe costs the slowest check rather than
their sum, and async ones are bounded by a five-second ceiling so a blackholed
backend cannot hold a probe open until the OS TCP timeout. A synchronous check
that blocks is beyond reach, which is why anything touching the network should
be async. The name `kv_store` is reserved.

A check answers "does this make the server unable to serve". Whether a given
domain signal clears that bar is your judgment — there is deliberately no
severity knob, so a partial degradation you would rather not take the server
out of rotation for belongs in `get_server_info` instead.

`<PREFIX>_HEALTH_DETAIL` decides how much the bodies say, because the bodies are
readable by anyone who can reach the port. An unrecognised value warns and falls
back to `standard`.

| Level | `/health` | `/health/ready` |
|---|---|---|
| `status` | `{"status": "ok"}` | `{"status": "ready"}` |
| `standard` (default) | adds `server` name and version | adds `checks`, a boolean per check |
| `full` | same as `standard` | adds `errors`, naming the exception type and reason for each check that *raised* |

A check that simply returned falsy has no reason to report, so it appears in
`checks` and not in `errors`; `full` adds nothing over `standard` for it.

Use `full` only where the port is reachable from a trusted network. It strips
userinfo and query strings from every URL in a reason — backend URLs in this
codebase carry credentials — but a reason is upstream text, and trusting it
fully is a choice you make per deployment. The stripping also stops at `/`, so a
password carrying a raw `/` (which RFC 3986 requires to be percent-encoded)
survives it.

### Background task backend

SEP-2663 task support (`fastmcp[tasks]`, the `fastmcp-tasks` extension on
Docket) is a pvl-core **base dependency** — nearly every family server
carries long-running tools, and fastmcp refuses to start a server carrying
`task=True` tools when no tasks extension is registered, so the ~10 MB is
deliberately always present. Servers that register task-enabled tools call
`configure_task_backend` once before `mcp.run(...)`; it registers the tasks
extension on the server with the resolved backend:

```python
from fastmcp_pvl_core import configure_task_backend

configure_task_backend(mcp, "MY_APP", config)
```

Backend selection then follows pvl-core's unified surface: an explicit
`MY_APP_TASKS_URL` (`memory://` or `redis://`) wins; otherwise a `redis://`
`MY_APP_KV_STORE_URL` is reused for the task queue too, so one variable
configures every stateful subsystem *and* tasks; otherwise fastmcp's
`memory://` default applies (in-process, lost on restart — fine for
development, not for a multi-process deployment). The Docket queue name is
derived from the env prefix so family servers sharing one Redis do not share
a queue. The helper degrades to a no-op in the degenerate case of a
stripped fork or an incompatible pydocket pin — a server that still
registers `task=`-enabled tools then fails at startup with fastmcp's own
missing-extension error — and returns the registered extension otherwise.

The remaining Docket worker tunables are native fastmcp variables,
deliberately not wrapped: `FASTMCP_DOCKET_CONCURRENCY`,
`FASTMCP_DOCKET_WORKER_NAME`, `FASTMCP_DOCKET_REDELIVERY_TIMEOUT`,
`FASTMCP_DOCKET_RECONNECTION_DELAY`, `FASTMCP_DOCKET_MINIMUM_CHECK_INTERVAL`.
`FASTMCP_DOCKET_URL` / `FASTMCP_DOCKET_NAME` also keep working as native
escape hatches when the pvl-core surface leaves them untouched — read from
the process environment; a value supplied only via the extension's optional
dotenv file is overridden by pvl-core's derived URL and queue name.

### Long-running tools (dual mode)

A tool that may outlive the client's request timeout registers once and
gets both behaviours: protocol-native SEP-2663 task execution when the
request is task-augmented, and foreground execution with soft-deadline
promotion to a pollable background job otherwise:

```python
from fastmcp_pvl_core import (
    JobsConfig, build_jobs, register_job_tools, register_long_running_tool,
)

jobs_config = JobsConfig.from_env("MY_APP")   # MY_APP_JOBS_* knobs
jobs = build_jobs(config, jobs_config)

@register_long_running_tool(mcp, jobs, tags={"reports"})
async def build_report(paths: list[str]) -> dict:
    ...  # domain work; may take minutes

register_job_tools(mcp, jobs)  # the one generic get_job_result tool
```

A call that beats `MY_APP_JOBS_SOFT_DEADLINE_S` returns its result
inline; a slower one immediately returns a job handle
(`{"status": "working", "job_id": ..., "poll_with": "get_job_result",
...}`) and finishes in the background — results are retrievable via
`get_job_result` until `MY_APP_JOBS_RESULT_TTL_S` expires, scoped to the
calling subject.

A server whose long-running tool the wrapper cannot express (its own
promotion decision, a handle minted from a route) composes on the same
mechanics without the wrapper — `from fastmcp_pvl_core.jobs import
build_jobs` and use `jobs.run_with_deadline(...)` / `jobs.start(...)`
inside its own tool. For intentional, runtime deferrals such as an
upstream rate limit, `jobs.defer(...)` adds the client-visible reason and
first-poll interval. The handles resolve through the same generic polling
tool. Do not reach into `fastmcp_pvl_core._jobs` internals; the `jobs`
namespace is the supported seam.

The downstream contract — payload shapes, inline-failure semantics,
scoping/retention limits, and the path-2 rules — lives in the docstrings
of `register_long_running_tool`, `register_job_tools`, `Jobs`, and
`build_jobs` (they are the authority a coding agent reads first);
[`docs/jobs.md`](docs/jobs.md) is the same contract as a narrative
implementation guide.

### Per-user subject mapping (bearer auth)

Bearer auth has two modes:

- **Single token** — `MY_APP_BEARER_TOKEN=<token>` accepts one shared token.
  Authenticated callers all share the same subject (default
  `"bearer-anon"`; override with `MY_APP_BEARER_DEFAULT_SUBJECT=<value>`).

- **Mapped tokens** — `MY_APP_BEARER_TOKENS_FILE=/path/to/tokens.toml`
  loads a token→subject map at startup. Each token resolves to a distinct
  subject string for downstream attribution (audit logs, ACLs, request
  metadata).

```toml
# tokens.toml
[tokens]
"ghp_alice_xxxxxxxx" = "user:alice@example.com"
"sk_ci_yyyyyyyy"     = "service:ci-bot"
```

If both `MY_APP_BEARER_TOKEN` and `MY_APP_BEARER_TOKENS_FILE` are set,
the file wins and a `WARNING` is logged. Subject strings are opaque to
the library; the `<kind>:<id>` convention (`user:`, `service:`,
`token:`) is documentation only.

If `MY_APP_BEARER_TOKENS_FILE` is set but the file is missing,
unparseable, or schema-invalid, the loader raises
`fastmcp_pvl_core.ConfigurationError` at startup — the server fails
fast rather than silently denying every request. The exception type
is part of the public API; downstream code can `import` and `except`
it as a stable contract.

`MY_APP_BEARER_DEFAULT_SUBJECT` only applies when bearer auth runs in
single-token mode (either standalone or as the bearer side of `multi`
mode alongside OIDC). It is ignored when `MY_APP_BEARER_TOKENS_FILE`
is set, including in `multi` mode — mapped mode uses the per-token
subjects from the TOML file.

### OIDC scopes — requested vs. required

Two different questions, two different settings:

- **What a client should ask the IdP for** — advertised in the server's
  protected-resource metadata (RFC 9728). pvl-core advertises
  `openid offline_access` by default. `offline_access` is what makes the
  IdP issue a **refresh token**; without it a session ends at
  access-token expiry and needs a human to complete a browser flow
  again.

- **What a token must carry to be accepted** —
  `MY_APP_OIDC_REQUIRED_SCOPES=<space- or comma-separated>`. This is a
  hard requirement checked on every request, so keep it minimal; a scope
  listed here is always advertised too, or clients would never request
  it and every token would fail the check.

Override the advertised set with
`MY_APP_OIDC_ADVERTISED_SCOPES=<space- or comma-separated>` when the
deployment needs something else — for example a registered client that
is not permitted `offline_access`, or extra claim scopes (`groups`,
`email`) that clients should request but that tokens are not *required*
to carry. `MY_APP_OIDC_REQUIRED_SCOPES` is still added on top.

pvl-core's own default is filtered against the IdP's published
`scopes_supported` (some providers reject an authorization request
outright with `invalid_scope` rather than ignoring an unknown scope); a
scope dropped that way is logged at `WARNING`. An operator-set
`MY_APP_OIDC_ADVERTISED_SCOPES` is used verbatim — a client-level
restriction is not visible in discovery, so the operator's list wins.

### Identifying the caller — `get_subject`

Tools, middleware, and resource handlers can call
`fastmcp_pvl_core.get_subject()` to retrieve the subject of the current
request without knowing which auth mode is active:

```python
from fastmcp_pvl_core import get_subject

@mcp.tool
def whoami() -> str:
    subject = get_subject()
    return subject or "anonymous"
```

Resolution order:

1. **Token present:** prefer `claims["sub"]` (OIDC's standard subject
   claim); fall back to `client_id` if `sub` is absent. The auth
   builders normalise `client_id` per mode:
   - `bearer-single` → `bearer_default_subject` (default `"bearer-anon"`).
   - `bearer-mapped` → the per-token subject from the TOML map.
   - OIDC modes (`oidc-proxy`, `remote`) → typically `claims["sub"]` wins
     (a real OIDC token always carries `sub`); the `client_id` fallback
     is defensive.
   - `multi` → bearer-validated requests follow the bearer path,
     OIDC-validated requests follow the OIDC path.
2. **No token, `auth_mode == "none"`:** returns the literal `"local"`.
3. **No token, auth required:** returns `None` — caller decides whether
   to fall back or error.

`fastmcp_pvl_core.get_current_auth_mode()` returns the mode `build_auth`
resolved, so a caller that needs to report or branch on it does not call
`resolve_auth_mode` a second time:

```python
from fastmcp_pvl_core import build_auth, get_current_auth_mode

auth = build_auth(config)
mcp = FastMCP(name="my-app", auth=auth)
mode = get_current_auth_mode()   # e.g. "oidc-proxy"
```

It reports the mode that was **resolved**, which is not the same question as
whether the server is authenticated — `auth_mode_resolved mode=oidc-proxy …
server accepts unauthenticated connections` is a reachable startup line
(#316). Do not test it against `"none"` to decide that; check whether
`build_auth` returned a provider, which is the same predicate pvl-core's own
warning uses.

`"none"` is a resolved mode and is distinct from `None`, which means
`build_auth` has not run in this context. The mode is stored in a
`ContextVar` with the same scoping caveats as `get_subject`: last writer
wins, so a process composing two servers reads the mode of whichever
`build_auth` ran last.

### Authorization (opt-in) — native auth checks

pvl-core builds on FastMCP's native authorization (`AuthCheck` +
`AuthMiddleware`). It ships factories for the two checks the framework
has no built-in for — subject→scope (the only per-token authz available
in bearer modes) and claim→scope (group/role authz for OIDC modes) —
plus an OR-combinator for `multi` mode. Scope- and tag-based patterns
use FastMCP's own `require_scopes` / `restrict_tag`.

Components opt in with `meta={"required_scope": "<scope>"}`; the checks
read it. Components without it are unrestricted.

```python
import os
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.server.middleware import AuthMiddleware
from fastmcp_pvl_core import (
    make_acl_check, make_claims_check, any_check, load_acl, parse_claim_grants,
)

# OIDC mode — claim-based (identity: name IdP groups to match scopes)
mcp = FastMCP(..., middleware=[AuthMiddleware(auth=make_claims_check("groups"))])

# bearer mode — static subject ACL
mcp = FastMCP(..., middleware=[AuthMiddleware(auth=make_acl_check(load_acl(Path("/etc/my-app/acl.toml"))))])

# multi mode — OR of both
raw = os.environ.get("MY_APP_AUTHZ_GRANTS")
grants = parse_claim_grants(raw) if raw else None
mcp = FastMCP(..., middleware=[AuthMiddleware(auth=any_check(
    make_acl_check(load_acl(Path("/etc/my-app/acl.toml"))),
    make_claims_check(os.environ.get("MY_APP_AUTHZ_CLAIM", "groups"), grants),
))])

@mcp.tool(meta={"required_scope": "write"})
async def edit_document(...): ...
```

ACL TOML schema (`load_acl`) and inline-JSON grants (`parse_claim_grants`):

```toml
[subjects]
"user:alice@example.com" = ["read", "write"]
"user:admin@example.com" = ["*"]          # wildcard scope
```

```json
{"app-writers": ["read", "write"], "app-admins": ["*"]}
```

Key properties:

- **Claim vs scope.** Claim-based authz reads OIDC *claims* (`groups`,
  `roles`) — the user's IdP-issued permissions — not OAuth *scopes*
  (which describe the client/token grant). Bearer tokens carry no usable
  claims, so use `make_acl_check` there.
- **Opt-in per component** via `meta["required_scope"]`; absent ⇒
  unrestricted.
- **`*` is the only special scope** ("any required scope passes").
- **Loaders fail fast** with `ConfigurationError`; never silent denial.
- **Loaded once at startup.** Restart to pick up changes.
- **`stdio` transport bypasses checks entirely** — FastMCP's
  `AuthMiddleware` short-circuits for stdio (no OAuth concept there), so
  every component is reachable.
- **On HTTP, install these checks only alongside an `AuthProvider`.**
  `AuthMiddleware` still runs without one, but every request then carries
  no token, so a component with `meta["required_scope"]` is denied
  outright (unannotated ones stay open). Authorization is meaningful only
  when authentication is configured.

### Remote debugging in containers

Containerised consumers can opt into a remote Python debugger by calling
`maybe_start_debugpy(env_prefix)` early in their CLI entrypoint, passing
the same per-app prefix the server uses for the rest of its config:

```python
from fastmcp_pvl_core import configure_logging_from_env, maybe_start_debugpy

def main() -> None:
    configure_logging_from_env("MY_APP")
    maybe_start_debugpy("MY_APP")  # no-op unless MY_APP_DEBUG_PORT is set
    ...
```

Environment contract (`{PREFIX}` matches the argument):

- `{PREFIX}_DEBUG_PORT` — TCP port to listen on. Unset, blank, or any
  value that parses to `0` is a silent no-op. Non-numeric or
  out-of-`1..65535` values log a `WARNING` and the helper returns
  without raising.
- `{PREFIX}_DEBUG_WAIT` — when truthy (`1`/`true`/`yes`/`on`,
  case-insensitive), block startup until the IDE attaches. Default is
  non-blocking.
- If `debugpy.listen()` itself fails (port in use, permission denied,
  debugpy-internal error), the helper logs a `WARNING` and continues —
  a debug-port problem must never crash the server.

Install the optional `debug` extra on images that need the listener:

```bash
uv add "fastmcp-pvl-core[debug]"   # quote brackets in zsh
# or, equivalently:
uv add debugpy
```

The helper logs a `WARNING` and continues if `debugpy` is unavailable,
so it is safe to ship in default scaffolds.

> ⚠️ **Security:** the listener binds `0.0.0.0` and debugpy's DAP
> protocol is **unauthenticated** — any peer that can reach the port
> has arbitrary code execution as the server process. Only enable
> `{PREFIX}_DEBUG_PORT` in environments where the port is reachable
> solely from a trusted developer workstation, e.g. `kubectl
> port-forward`, `docker run -p 127.0.0.1:5678:5678` (loopback bind),
> or an SSH tunnel. Never publish the debug port on a public network.

## License

MIT
