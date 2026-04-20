# fastmcp-pvl-core

Shared FastMCP infrastructure for the `pvliesdonk/*-mcp` server family:
auth, middleware, logging, config helpers, server-factory building blocks.

## Status

Early 0.x. API may change on minor bumps until 1.0.

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

## License

MIT
