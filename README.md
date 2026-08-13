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
    ServerConfig, build_auth, build_instructions,
    wire_middleware_stack, env,
)

config = ServerConfig.from_env("MY_APP")
mcp = FastMCP(
    name="my-app",
    instructions=build_instructions(read_only=False, env_prefix="MY_APP", domain_line="…"),
    auth=build_auth(config),
)
wire_middleware_stack(mcp)
```

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

`configure_logging_from_env` resolves the log level from the `-v` CLI flag
(forces `DEBUG`), then `FASTMCP_LOG_LEVEL`, then defaults to `INFO`.

At `INFO` and above, two noisy third-party loggers are demoted to `WARNING`
so they do not flood the operator log stream:

- `uvicorn.access` — the `INFO: <ip> - "POST /mcp ..."` HTTP access log.
- `mcp.server.lowlevel.server` — the MCP SDK's `Processing request of
  type ...` line.

Both reappear at `DEBUG` (`-v` or `FASTMCP_LOG_LEVEL=DEBUG`). `uvicorn.error`
is never demoted — it carries genuine bind / startup failures.

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

### Background task backend

SEP-1686 task support (`fastmcp[tasks]` / Docket) is a pvl-core **base
dependency** — nearly every family server carries long-running tools, and
fastmcp raises `ImportError` at registration time for any `task=True` tool
when pydocket is missing, so the ~10 MB is deliberately always present.
Servers that register task-enabled tools call `configure_task_backend` once
before `mcp.run(...)`:

```python
from fastmcp_pvl_core import configure_task_backend

configure_task_backend("MY_APP", config)
```

Backend selection then follows pvl-core's unified surface: an explicit
`MY_APP_TASKS_URL` (`memory://` or `redis://`) wins; otherwise a `redis://`
`MY_APP_KV_STORE_URL` is reused for the task queue too, so one variable
configures every stateful subsystem *and* tasks; otherwise fastmcp's
`memory://` default applies (in-process, lost on restart — fine for
development, not for a multi-process deployment). The Docket queue name is
derived from the env prefix so family servers sharing one Redis do not share
a queue. The helper degrades to a no-op in the degenerate case of a
stripped fork or an incompatible pydocket pin.

The remaining Docket worker tunables are native fastmcp variables,
deliberately not wrapped: `FASTMCP_DOCKET_CONCURRENCY`,
`FASTMCP_DOCKET_WORKER_NAME`, `FASTMCP_DOCKET_REDELIVERY_TIMEOUT`,
`FASTMCP_DOCKET_RECONNECTION_DELAY`, `FASTMCP_DOCKET_MINIMUM_CHECK_INTERVAL`.
`FASTMCP_DOCKET_URL` / `FASTMCP_DOCKET_NAME` also keep working as native
escape hatches when the pvl-core surface leaves them untouched.

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
    configure_logging_from_env()
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
