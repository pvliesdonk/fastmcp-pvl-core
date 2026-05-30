# Design: MCP Apps helpers (`_apps.py`)

**Date:** 2026-05-30  
**Issues:** pvliesdonk/fastmcp-pvl-core#63  
**Related:** pvliesdonk/markdown-vault-mcp#527 (root cause — downstream concern, not pvl-core)

---

## Problem

Downstream servers using FastMCP's MCP Apps feature must import from two private
FastMCP modules (`fastmcp.server.providers.addressing`, `fastmcp.apps.config`) that
carry no stability guarantee and trigger `reportMissingImports` in pyright. Every
consumer reimplements the same wrapping boilerplate. pvl-core is the right place to
absorb this once.

## What the protocol says

App tools are **always visible in `tools/list`** regardless of client capability.
`visibility=["app"]` in `AppConfig` controls only the model/UI distinction (backend
callback tools hidden from the LLM) — it does not hide entry-point tools from
non-capable clients. The canonical pattern for non-capable clients is a **behavioral
fallback at call time** via `ctx.client_supports_extension(UI_EXTENSION_ID)`, not
hiding the tool from the listing. Operator-level hiding (e.g. for known-headless
deployments) is downstream's responsibility via `mcp.disable(tags={...})` — pvl-core
does not need to abstract this.

## Design

### Module

New `src/fastmcp_pvl_core/_apps.py`. Three public functions, all exported from
`__init__.py` and `__all__`.

### Private-import guard

Both private fastmcp imports are wrapped in a single `try/except ImportError` at
module level. The re-raised error names the version pin so the message is actionable:

```python
try:
    from fastmcp.server.providers.addressing import hash_tool, hashed_backend_name
    from fastmcp.apps.config import UI_EXTENSION_ID
except ImportError as exc:
    raise ImportError(
        "fastmcp.server.providers.addressing and fastmcp.apps.config are required "
        "(fastmcp >= 3.3.1). Pin fastmcp accordingly in pyproject.toml."
    ) from exc
```

pvl-core already pins `fastmcp>=3.3.1,<4`, so this guard is defensive; it protects
against accidental downgrade or a future refactor that moves these symbols.

### API

```python
def app_tool_meta(app_name: str, tool_name: str) -> dict[str, object]:
    """Build the meta= dict for an app-only backend tool registration.

    Returns the dict that @mcp.tool(meta=...) expects for a tool whose
    visibility=["app"] so it is callable from the app UI but hidden from
    the LLM's tool list. Downstream passes the result directly to meta=:

        @mcp.tool(
            meta=app_tool_meta("vault", "vault_list"),
            app=AppConfig(resource_uri=..., visibility=["app"]),
        )
        async def vault_list(...): ...
    """

def app_tool_address(app_name: str, tool_name: str) -> str:
    """Return the hashed callable name for use in SPA HTML rewrites.

    The returned string is the name the MCP Apps JS SDK uses when calling
    back to the server. Downstream uses it to rewrite static SPA HTML
    literals at import time so they survive server composition renames:

        html = re.sub(
            r"vault___(vault_[a-z_]+)",
            lambda m: app_tool_address("vault", m.group(1)),
            html,
        )
    """

def client_supports_apps(ctx: Context) -> bool:
    """Return True if the connected client advertises the MCP Apps extension.

    Thin wrapper around ctx.client_supports_extension(UI_EXTENSION_ID) so
    downstream does not import UI_EXTENSION_ID from fastmcp internals.
    Returns False when called outside a request context.
    """
```

`client_supports_apps` is **sync** — `ctx.client_supports_extension` is sync (confirmed
against FastMCP 3.3.1).

### Exports

Added to `__init__.py` import and `__all__` in alphabetical order:
`app_tool_address`, `app_tool_meta`, `client_supports_apps`.

## Tests (`tests/test_apps.py`)

| Test | Assertion |
|---|---|
| `app_tool_meta` round-trip | returned `_tool_hash` matches `hash_tool(app_name, tool_name)` directly |
| `app_tool_meta` structure | dict has keys `fastmcp.app` and `fastmcp._tool_hash` at correct paths |
| `app_tool_address` round-trip | result parses back via `parse_hashed_backend_name` to `(*, tool_name)` |
| `app_tool_address` different pairs differ | two distinct `(app, tool)` pairs produce different addresses |
| `client_supports_apps` delegates True | patches `ctx.client_supports_extension` returning True → returns True |
| `client_supports_apps` delegates False | patches returning False → returns False |
| `client_supports_apps` passes UI_EXTENSION_ID | asserts the exact extension ID passed to `client_supports_extension` |
| ImportError guard | patches `sys.modules` to absent the private module → re-raised ImportError message contains version pin string |

## Out of scope

- `app_config(resource_uri, ...)` factory — downstream uses `AppConfig` directly; thin
  factory adds no value and would need to import `AppConfig` type
- Session-level disable/enable helpers — the protocol intends tools to always appear and
  degrade at call time; operator-level hiding is downstream's `mcp.disable(tags=...)` 
- Tag constant (`APPS_UI_TAG`) — no standard tag concept in the MCP Apps protocol
