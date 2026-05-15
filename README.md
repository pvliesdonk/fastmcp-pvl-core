# fastmcp-pvl-core

The opinionated shared implementation for the `pvliesdonk/*-mcp`
server family. `fastmcp-pvl-core` owns the shape of cross-cutting
concerns — auth, middleware, logging, config, server-factory builders,
the file-exchange wire surface — and exposes narrow hooks to
downstream servers for domain-specific behaviour. Downstream conforms
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
coherent as it grows. Four principles follow from that role.

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
that grow override kwargs disguised as hooks are rejected. The
worked example is `register_file_exchange` in
`src/fastmcp_pvl_core/file_exchange.py`: five kwargs, all domain
hooks, every operator value on an env var.

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
version. The opening of
[`docs/specs/file-exchange.md`](docs/specs/file-exchange.md) is the
worked example of this distinction.

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

### Authorization (opt-in) — `AuthorizationMiddleware`

Tools, resources, and prompts can opt into per-subject access control by
setting `meta={"required_scope": "<scope>"}` at registration. A
configured `AuthorizationMiddleware` enforces the static check and
filters `list_*` responses to what the caller can use:

```python
from pathlib import Path
from fastmcp_pvl_core import (
    AuthorizationMiddleware, load_acl, make_acl_authorizer, check_authorization,
)

authorizer = make_acl_authorizer(load_acl(Path("/etc/my-app/acl.toml")))
mcp.add_middleware(AuthorizationMiddleware(authorizer=authorizer))

@mcp.tool(meta={"required_scope": "write"})
async def edit_document(project_id: str, doc_id: str, body: str) -> None:
    # Coarse "write" gate already passed at middleware. Per-project gate here:
    check_authorization(f"write:{project_id}")
    ...
```

ACL TOML schema (loaded by `load_acl`):

```toml
[subjects]
"user:alice@example.com" = ["read", "write"]
"user:admin@example.com" = ["*"]              # wildcard scope
"service:ci-bot"         = ["read"]
"local"                  = ["*"]              # stdio mode
```

Key properties:

- **Opt-in per component.** Tools / resources / prompts without
  `meta["required_scope"]` are unrestricted regardless of caller.
- **`*` is the only library-treated special scope** ("any required
  scope passes"). All other scopes are opaque strings; downstream chooses
  the vocabulary.
- **Subject-side wildcards (`*` as an ACL key) are rejected at load
  time.**
- **`load_acl` fails fast** with `ConfigurationError` on every malformed
  condition — never silent denial.
- **ACL is loaded once at startup.** Restart to pick up changes.
- **Authorization scopes are application-level** and distinct from the
  OAuth scopes carried in tokens.
- **Subject is logged on every deny** at WARNING. The wire-side payload
  *omits* the subject by default to limit cross-user info disclosure;
  pass `AuthorizationMiddleware(..., expose_subject_in_error=True)` to
  include it (e.g. for internal-only servers).

For the full design rationale and deviations from the originating
issue, see
[`docs/specs/authorization-submodule.md`](docs/specs/authorization-submodule.md).

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

### File Exchange

Servers that produce or consume binary artefacts (PDFs, images,
documents) wire the MCP File Exchange spec with a single call. The
download direction registers a `create_download_link` tool and the
artifact HTTP route:

```python
from fastmcp_pvl_core import register_file_exchange

handle = register_file_exchange(
    mcp,
    namespace="vault",
    env_prefix="MARKDOWN_VAULT_MCP",
    produces=["text/markdown"],
)
```

For the inbound direction (local agent pushes a file *into* the
server), use the symmetric helper:

```python
from fastmcp_pvl_core import register_file_exchange_upload

register_file_exchange_upload(
    mcp,
    namespace="vault",
    env_prefix="MARKDOWN_VAULT_MCP",
    receiver=_my_upload_receiver,
    pre_link_validator=_validate_target_path,
)
```

This mints one-time `POST` URLs via a `create_upload_link` tool and
dispatches the bytes to your receiver. See File Exchange spec
§"Inbound HTTP transfer" (v0.4 amendments) at
[`docs/specs/file-exchange.md`](docs/specs/file-exchange.md) for the
wire contract.

Both helpers cooperate on the same `experimental.file_exchange`
capability — registering both on one FastMCP instance advertises a
single capability whose `transfer_methods.http` block carries both
`download` and `upload` sub-keys.

## License

MIT
