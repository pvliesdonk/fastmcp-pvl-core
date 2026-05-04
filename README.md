# fastmcp-pvl-core

Shared FastMCP infrastructure for the `pvliesdonk/*-mcp` server family:
auth, middleware, logging, config helpers, server-factory building blocks.

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

### Remote debugging in containers

Containerised consumers can opt into a remote Python debugger by calling
`maybe_start_debugpy()` early in their CLI entrypoint:

```python
from fastmcp_pvl_core import configure_logging_from_env, maybe_start_debugpy

def main() -> None:
    configure_logging_from_env()
    maybe_start_debugpy()  # no-op unless DEBUG_PORT is set
    ...
```

Environment contract:

- `DEBUG_PORT` — TCP port to listen on. Unset, blank, or any value that
  parses to `0` is a silent no-op. Non-numeric or out-of-`1..65535`
  values log a `WARNING` and the helper returns without raising.
- `DEBUG_WAIT` — when truthy (`1`/`true`/`yes`/`on`, case-insensitive),
  block startup until the IDE attaches. Default is non-blocking.
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
> `DEBUG_PORT` in environments where the port is reachable solely
> from a trusted developer workstation, e.g. `kubectl port-forward`,
> `docker run -p 127.0.0.1:5678:5678` (loopback bind), or an SSH
> tunnel. Never publish the debug port on a public network.

## License

MIT
