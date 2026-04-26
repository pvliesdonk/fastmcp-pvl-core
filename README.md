# fastmcp-pvl-core

Shared FastMCP infrastructure for the `pvliesdonk/*-mcp` server family:
auth, middleware, logging, config helpers, server-factory building blocks.

## Ecosystem

- [`fastmcp-server-template`](https://github.com/pvliesdonk/fastmcp-server-template) —
  copier template that scaffolds new FastMCP servers on top of this library.
- Active consumers (as of 2026-04):
  [`markdown-vault-mcp`](https://github.com/pvliesdonk/markdown-vault-mcp),
  [`scholar-mcp`](https://github.com/pvliesdonk/scholar-mcp),
  [`image-generation-mcp`](https://github.com/pvliesdonk/image-generation-mcp).
- Public API changes here propagate to consumers via periodic
  `copier update` runs against the template.
- See the template's README for the update flow and the expected project
  shape.

## API stability

This package is stable at 1.x and follows
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
    auth=build_auth("MY_APP", config),
)
wire_middleware_stack(mcp)
```

## Specs

- [MCP File Exchange v0.3](docs/specs/file-exchange.md) — convention for
  cross-MCP-server file transfer (`exchange://` URIs over a shared
  volume + `http` fallback). The protocol surface (`FileRef`,
  `ExchangeURI`, `register_file_exchange_capability`) ships in
  `fastmcp_pvl_core`; the runtime is tracked in issue #21.

## License

MIT
