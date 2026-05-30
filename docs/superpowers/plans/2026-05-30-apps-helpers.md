# MCP Apps Helpers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `_apps.py` to pvl-core exposing three thin helpers — `app_tool_meta`, `app_tool_address`, `client_supports_apps` — that wrap private fastmcp internals so downstream servers don't import them directly.

**Architecture:** Single new module `_apps.py` with a module-level `ImportError` guard around both private fastmcp imports. Three pure/thin functions; no state. Exported from `__init__.py` alongside existing helpers.

**Tech Stack:** Python 3.10+, fastmcp ≥ 3.3.1 (already pinned), pytest, unittest.mock

---

## File map

| Action | Path | Purpose |
|---|---|---|
| Create | `src/fastmcp_pvl_core/_apps.py` | Three helpers + ImportError guard |
| Create | `tests/test_apps.py` | All tests for the new module |
| Modify | `src/fastmcp_pvl_core/__init__.py` | Import and re-export three names |

---

## Task 1: Failing tests for `app_tool_meta` and `app_tool_address`

**Files:**
- Create: `tests/test_apps.py`

- [ ] **Write the failing tests**

Create `tests/test_apps.py` with this exact content:

```python
"""Tests for MCP Apps helpers."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from fastmcp.server.providers.addressing import (
    hash_tool,
    hashed_backend_name,
    parse_hashed_backend_name,
)
from fastmcp.apps.config import UI_EXTENSION_ID

from fastmcp_pvl_core import app_tool_address, app_tool_meta, client_supports_apps


class TestAppToolMeta:
    def test_returns_correct_structure(self):
        result = app_tool_meta("vault", "vault_list")
        assert isinstance(result, dict)
        assert "fastmcp" in result
        inner = result["fastmcp"]
        assert "app" in inner
        assert "_tool_hash" in inner

    def test_app_name_stored(self):
        result = app_tool_meta("vault", "vault_list")
        assert result["fastmcp"]["app"] == "vault"

    def test_tool_hash_matches_hash_tool(self):
        result = app_tool_meta("vault", "vault_list")
        assert result["fastmcp"]["_tool_hash"] == hash_tool("vault", "vault_list")

    def test_different_inputs_produce_different_hashes(self):
        a = app_tool_meta("vault", "vault_list")
        b = app_tool_meta("vault", "vault_search")
        assert a["fastmcp"]["_tool_hash"] != b["fastmcp"]["_tool_hash"]

    def test_app_name_varies_independently(self):
        a = app_tool_meta("vault", "vault_list")
        b = app_tool_meta("other", "vault_list")
        assert a["fastmcp"]["app"] != b["fastmcp"]["app"]
        assert a["fastmcp"]["_tool_hash"] != b["fastmcp"]["_tool_hash"]


class TestAppToolAddress:
    def test_matches_hashed_backend_name(self):
        result = app_tool_address("vault", "vault_list")
        assert result == hashed_backend_name("vault", "vault_list")

    def test_parses_back_to_tool_name(self):
        result = app_tool_address("vault", "vault_list")
        _, tool_name = parse_hashed_backend_name(result)
        assert tool_name == "vault_list"

    def test_different_pairs_produce_different_addresses(self):
        a = app_tool_address("vault", "vault_list")
        b = app_tool_address("vault", "vault_search")
        assert a != b

    def test_address_contains_tool_name_suffix(self):
        result = app_tool_address("vault", "vault_list")
        assert result.endswith("_vault_list")


class TestClientSupportsApps:
    def test_returns_true_when_extension_supported(self):
        ctx = MagicMock()
        ctx.client_supports_extension.return_value = True
        assert client_supports_apps(ctx) is True

    def test_returns_false_when_extension_not_supported(self):
        ctx = MagicMock()
        ctx.client_supports_extension.return_value = False
        assert client_supports_apps(ctx) is False

    def test_passes_ui_extension_id(self):
        ctx = MagicMock()
        ctx.client_supports_extension.return_value = False
        client_supports_apps(ctx)
        ctx.client_supports_extension.assert_called_once_with(UI_EXTENSION_ID)


class TestImportErrorGuard:
    def test_import_error_message_names_version(self):
        import importlib

        addressing_key = "fastmcp.server.providers.addressing"
        apps_module_key = "fastmcp_pvl_core._apps"

        # Pop _apps so it re-imports from scratch; suppress the private module
        saved_addressing = sys.modules.get(addressing_key)
        saved_apps_module = sys.modules.pop(apps_module_key, None)

        try:
            sys.modules[addressing_key] = None  # type: ignore[assignment]
            with pytest.raises(ImportError, match="3.3.1"):
                importlib.import_module("fastmcp_pvl_core._apps")
        finally:
            if saved_addressing is not None:
                sys.modules[addressing_key] = saved_addressing
            else:
                sys.modules.pop(addressing_key, None)
            if saved_apps_module is not None:
                sys.modules[apps_module_key] = saved_apps_module
```

- [ ] **Run tests to verify they fail (ImportError — module doesn't exist yet)**

```bash
uv run pytest tests/test_apps.py -x 2>&1 | tail -15
```

Expected: `ImportError: cannot import name 'app_tool_address' from 'fastmcp_pvl_core'`

---

## Task 2: Implement `_apps.py`

**Files:**
- Create: `src/fastmcp_pvl_core/_apps.py`

- [ ] **Write the implementation**

Create `src/fastmcp_pvl_core/_apps.py` with this exact content:

```python
"""MCP Apps helpers — wraps private fastmcp addressing and capability APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    from fastmcp.apps.config import UI_EXTENSION_ID
    from fastmcp.server.providers.addressing import hash_tool, hashed_backend_name
except ImportError as exc:
    raise ImportError(
        "fastmcp.server.providers.addressing and fastmcp.apps.config are required "
        "(fastmcp >= 3.3.1). Pin fastmcp accordingly in pyproject.toml."
    ) from exc

if TYPE_CHECKING:
    from fastmcp.server.context import Context


def app_tool_meta(app_name: str, tool_name: str) -> dict[str, object]:
    """Build the meta= dict for an app-only backend tool registration.

    Returns the dict that @mcp.tool(meta=...) expects for a tool whose
    visibility=["app"] makes it callable from the app UI but hidden from
    the LLM's tool list. Pass the result directly to meta=:

        @mcp.tool(
            meta=app_tool_meta("vault", "vault_list"),
            app=AppConfig(resource_uri=..., visibility=["app"]),
        )
        async def vault_list(...): ...
    """
    return {"fastmcp": {"app": app_name, "_tool_hash": hash_tool(app_name, tool_name)}}


def app_tool_address(app_name: str, tool_name: str) -> str:
    """Return the hashed callable name for use in SPA HTML rewrites.

    The returned string is the name the MCP Apps JS SDK uses when calling
    back to the server. Use it to rewrite static SPA HTML literals at
    import time so they survive server-composition renames:

        html = re.sub(
            r"vault___(vault_[a-z_]+)",
            lambda m: app_tool_address("vault", m.group(1)),
            html,
        )
    """
    return hashed_backend_name(app_name, tool_name)


def client_supports_apps(ctx: Context) -> bool:
    """Return True if the connected client advertises the MCP Apps extension.

    Thin wrapper around ctx.client_supports_extension(UI_EXTENSION_ID) so
    downstream does not import UI_EXTENSION_ID from fastmcp internals.
    Returns False when called outside a request context.
    """
    return ctx.client_supports_extension(UI_EXTENSION_ID)
```

- [ ] **Run tests to verify they still fail (not exported yet)**

```bash
uv run pytest tests/test_apps.py -x 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'app_tool_address' from 'fastmcp_pvl_core'`

---

## Task 3: Export from `__init__.py`

**Files:**
- Modify: `src/fastmcp_pvl_core/__init__.py`

- [ ] **Add import and `__all__` entries**

In `src/fastmcp_pvl_core/__init__.py`, add after the existing `from fastmcp_pvl_core._auth import (` block (keep alphabetical order among imports):

```python
from fastmcp_pvl_core._apps import app_tool_address, app_tool_meta, client_supports_apps
```

And in `__all__`, add these three names in alphabetical position (between `"AuthzDenied"` and `"ConfigurationError"`):

```python
    "app_tool_address",
    "app_tool_meta",
    "client_supports_apps",
```

- [ ] **Run all tests to verify green**

```bash
uv run pytest tests/test_apps.py -v 2>&1 | tail -25
```

Expected: all tests in `test_apps.py` pass.

- [ ] **Run full suite**

```bash
uv run pytest 2>&1 | tail -8
```

Expected: all tests pass (390 + new test_apps.py tests).

- [ ] **Run type check and linters**

```bash
uv run mypy src && uv run ruff check . && uv run ruff format --check .
```

Expected: no errors. If ruff reports an import-order issue, run `uv run ruff check --fix .` first.

- [ ] **Commit**

```bash
git add src/fastmcp_pvl_core/_apps.py tests/test_apps.py src/fastmcp_pvl_core/__init__.py
git commit -m "feat(apps): add app_tool_meta, app_tool_address, client_supports_apps helpers

Wraps fastmcp.server.providers.addressing and fastmcp.apps.config behind
a stable pvl-core surface so downstream servers don't import private
fastmcp internals directly. All three functions exported from package root.

Closes #63"
```

---

## Task 4: Push and open PR

- [ ] **Create feature branch and push**

```bash
git checkout -b feat/63-apps-helpers
git push -u origin feat/63-apps-helpers
```

- [ ] **Open PR**

```bash
gh pr create \
  --title "feat(apps): app_tool_meta, app_tool_address, client_supports_apps helpers" \
  --body "$(cat <<'EOF'
## Summary

- Adds \`_apps.py\` with three thin helpers wrapping private fastmcp internals
- \`app_tool_meta(app_name, tool_name)\` — builds the \`meta=\` dict for backend (app-only) tool registration
- \`app_tool_address(app_name, tool_name)\` — returns the hashed callable name for SPA HTML rewrites
- \`client_supports_apps(ctx)\` — wraps \`ctx.client_supports_extension(UI_EXTENSION_ID)\` so downstream doesn't import \`UI_EXTENSION_ID\` from fastmcp internals
- Module-level \`ImportError\` guard names the fastmcp version pin in the error message
- All three exported from package root in \`__all__\`

## Test plan

- [ ] \`app_tool_meta\` structure and hash round-trip (5 cases)
- [ ] \`app_tool_address\` round-trip via \`parse_hashed_backend_name\`, distinct pairs produce distinct addresses (4 cases)
- [ ] \`client_supports_apps\` delegates True/False and passes \`UI_EXTENSION_ID\` (3 cases)
- [ ] ImportError guard: re-raised error message contains version pin string
- [ ] Full suite green: \`uv run pytest\` + \`uv run mypy src\` + \`uv run ruff check .\`

Closes #63

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
