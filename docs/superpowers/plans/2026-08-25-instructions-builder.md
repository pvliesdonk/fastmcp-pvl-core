# InstructionsBuilder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `build_instructions()` with a per-server `InstructionsBuilder` that core features and domain code feed prioritised, tool-bound snippets into, finalised once into `mcp.instructions` under a core-owned env contract.

**Architecture:** One builder per `FastMCP` instance, reached via `instructions_for(mcp)` (weak-keyed registry, no new kwargs on any `register_*`). `finalize_instructions(mcp, config, env_prefix=...)` computes the operator-exposed tool set from `ServerConfig` (pvl-core owns the allow/deny rule; FastMCP has no sync query), prunes snippets whose tools are hidden or absent, requires exactly one identity, serialises by `(priority, insertion)`, applies `_INSTRUCTIONS_EXTRA` (append) / legacy `_INSTRUCTIONS` (replace + one `WARNING`), sets `mcp.instructions`, and freezes. `register_transfer_routes` and `register_job_tools` contribute their cross-tool workflow snippets.

**Tech Stack:** Python 3.10–3.13, FastMCP 3.x, pytest (async tests run under the repo's existing asyncio config), ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-25-instructions-builder-design.md`

## Global Constraints

- Relative intra-package imports only (`from ._x import …`); no runtime self-name lookups (CLAUDE.md "keep foldable").
- No new kwargs on `register_*` helpers; the builder is plumbing, not a domain hook.
- `build_instructions` is **removed**; the release commit must carry `!` (semantic-release major).
- `{P}_INSTRUCTIONS` keeps full-replace semantics (legacy) and logs exactly one `WARNING` per finalize.
- Serialised text: snippets joined by a blank line, no markdown headings.
- Priority anchors: `IDENTITY=0, DOCS=100, CAPABILITIES=200, WORKFLOWS=300, INSTANCE=400, OPERATOR=500`.
- Before every push: `uv sync --all-extras && uv run pytest && uv run ruff format --check . && uv run ruff check . && uv run mypy src`.
- Work on branch `feat/283-instructions-builder` (spec already committed there). Stage explicit paths; never `git add -A`.

---

## File map

| File | Responsibility |
|---|---|
| Create `src/fastmcp_pvl_core/_instructions.py` | constants, `_Snippet`, `InstructionsBuilder`, `instructions_for`, `finalize_instructions` |
| Modify `src/fastmcp_pvl_core/_visibility.py` | add `exposed_tool_names(mcp, config)` — the one place the allow/deny rule is evaluated outside `apply_tool_visibility` |
| Modify `src/fastmcp_pvl_core/_factory.py` | delete `build_instructions` |
| Modify `src/fastmcp_pvl_core/__init__.py` | export new names, drop `build_instructions` |
| Modify `src/fastmcp_pvl_core/_transfer/register.py` | add the transfer workflow snippet |
| Modify `src/fastmcp_pvl_core/_jobs/register.py` | add the jobs polling snippet in `register_job_tools` |
| Modify `README.md` | usage example |
| Create `tests/test_instructions.py` | builder + finalize unit tests |
| Modify `tests/test_visibility.py` | `exposed_tool_names` tests |
| Modify `tests/test_factory.py` | delete `TestBuildInstructions` |
| Modify `tests/test_transfer_register.py`, `tests/test_jobs.py` | snippet tests |
| Create `tests/test_instructions_integration.py` | real-server end-to-end |

---

### Task 1: Builder core (`add`, `identity`, `documentation`, registry, render)

**Files:**
- Create: `src/fastmcp_pvl_core/_instructions.py`
- Test: `tests/test_instructions.py`

**Interfaces:**
- Produces: `IDENTITY, DOCS, CAPABILITIES, WORKFLOWS, INSTANCE, OPERATOR: int`; `class InstructionsBuilder` with `add(text, *, priority, tools=()) -> None`, `identity(text) -> None`, `documentation(url) -> None`, `_render(exposed: frozenset[str]) -> str` (internal, used by Task 3), `_snippets: list[_Snippet]`, `_frozen: bool`; `instructions_for(mcp) -> InstructionsBuilder`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_instructions.py
"""Unit tests for the InstructionsBuilder (spec: 2026-08-25-instructions-builder-design.md)."""

from __future__ import annotations

import logging

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    CAPABILITIES,
    DOCS,
    IDENTITY,
    INSTANCE,
    OPERATOR,
    WORKFLOWS,
    ConfigurationError,
    InstructionsBuilder,
    ServerConfig,
    finalize_instructions,
    instructions_for,
)

ALL = frozenset({"alpha", "beta", "gamma"})


class TestAnchors:
    def test_anchor_order(self):
        assert IDENTITY < DOCS < CAPABILITIES < WORKFLOWS < INSTANCE < OPERATOR
        assert (IDENTITY, DOCS, CAPABILITIES, WORKFLOWS, INSTANCE, OPERATOR) == (
            0, 100, 200, 300, 400, 500
        )


class TestAdd:
    def test_orders_by_priority_then_insertion(self):
        b = InstructionsBuilder()
        b.add("second", priority=WORKFLOWS)
        b.add("first", priority=IDENTITY)
        b.add("third", priority=WORKFLOWS)
        b.add("between", priority=CAPABILITIES + 10)
        assert b._render(ALL) == "first\n\nbetween\n\nsecond\n\nthird"

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    def test_rejects_blank_text(self, text: str):
        b = InstructionsBuilder()
        with pytest.raises(ConfigurationError, match="empty"):
            b.add(text, priority=WORKFLOWS)

    def test_strips_surrounding_whitespace(self):
        b = InstructionsBuilder()
        b.add("  padded  \n", priority=IDENTITY)
        assert b._render(ALL) == "padded"

    def test_identity_is_add_at_identity_priority(self):
        b = InstructionsBuilder()
        b.add("later", priority=DOCS)
        b.identity("Who I am.")
        assert b._render(ALL) == "Who I am.\n\nlater"

    def test_documentation_is_core_shaped_sentence_at_docs(self):
        b = InstructionsBuilder()
        b.identity("X.")
        b.add("caps", priority=CAPABILITIES)
        b.documentation("https://example.test/llms.txt")
        assert b._render(ALL) == (
            "X.\n\nFull documentation for this server: "
            "https://example.test/llms.txt\n\ncaps"
        )

    def test_documentation_rejects_blank_url(self):
        with pytest.raises(ConfigurationError, match="empty"):
            InstructionsBuilder().documentation("  ")


class TestPrune:
    def test_snippet_without_tools_is_kept(self):
        b = InstructionsBuilder()
        b.add("kept", priority=WORKFLOWS)
        assert b._render(frozenset()) == "kept"

    def test_snippet_whose_tools_are_all_exposed_is_kept(self):
        b = InstructionsBuilder()
        b.add("kept", priority=WORKFLOWS, tools={"alpha", "beta"})
        assert b._render(ALL) == "kept"

    def test_snippet_with_one_missing_tool_is_dropped(self):
        b = InstructionsBuilder()
        b.add("dropped", priority=WORKFLOWS, tools={"alpha", "zeta"})
        b.add("kept", priority=WORKFLOWS)
        assert b._render(ALL) == "kept"

    def test_drop_is_logged_at_debug_naming_the_tool(self, caplog: pytest.LogCaptureFixture):
        b = InstructionsBuilder()
        b.add("dropped", priority=WORKFLOWS, tools={"zeta"})
        with caplog.at_level(logging.DEBUG, logger="fastmcp_pvl_core._instructions"):
            b._render(ALL)
        assert any("zeta" in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records)


class TestRegistry:
    def test_same_server_same_builder(self):
        mcp = FastMCP("t")
        assert instructions_for(mcp) is instructions_for(mcp)

    def test_different_servers_different_builders(self):
        assert instructions_for(FastMCP("a")) is not instructions_for(FastMCP("b"))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_instructions.py -q`
Expected: FAIL at import — `ImportError: cannot import name 'IDENTITY'`.

- [ ] **Step 3: Implement the builder core**

```python
# src/fastmcp_pvl_core/_instructions.py
"""Composable MCP server instructions.

Instructions carry what no single tool description can carry: identity, a
documentation pointer, the capability map, cross-tool workflows, enforced
instance facts, and operator context. Core features and domain code each
add snippets to the one builder per server; :func:`finalize_instructions`
prunes snippets whose tools the operator hid, serialises by priority, applies
the env contract, and sets ``FastMCP.instructions``.

Design: ``docs/superpowers/specs/2026-08-25-instructions-builder-design.md``.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ._errors import ConfigurationError

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ._config import ServerConfig

logger = logging.getLogger(__name__)

#: Named anchors on the priority scale. Priority is the mechanism; a
#: contributor that wants "just after the capability map" writes
#: ``CAPABILITIES + 10``.
IDENTITY = 0
DOCS = 100
CAPABILITIES = 200
WORKFLOWS = 300
INSTANCE = 400
OPERATOR = 500


@dataclass(frozen=True)
class _Snippet:
    text: str
    priority: int
    tools: frozenset[str]
    seq: int


class InstructionsBuilder:
    """Ordered, tool-aware collection of instruction snippets for one server.

    Obtain it with :func:`instructions_for`; do not construct one per call
    site. Every ``add`` is a plain string with a priority and the tool names
    it references. Rendering happens once, in :func:`finalize_instructions`.
    """

    def __init__(self) -> None:
        self._snippets: list[_Snippet] = []
        self._frozen = False
        self._result: str | None = None

    def add(self, text: str, *, priority: int, tools: Iterable[str] = ()) -> None:
        """Add one snippet.

        Args:
            text: Model-facing prose. Surrounding whitespace is stripped.
            priority: Sort key; ties keep insertion order. Use the anchors
                (``IDENTITY`` … ``OPERATOR``) or an offset from one.
            tools: Tool names the snippet references. If any is hidden or
                absent at finalize, the whole snippet is dropped.

        Raises:
            ConfigurationError: *text* is empty or whitespace.
            RuntimeError: The builder was already finalized.
        """
        if self._frozen:
            raise RuntimeError("instructions already finalized; add snippets before finalize_instructions")
        cleaned = text.strip()
        if not cleaned:
            raise ConfigurationError("instruction snippet text is empty")
        self._snippets.append(
            _Snippet(cleaned, priority, frozenset(tools), len(self._snippets))
        )

    def identity(self, text: str) -> None:
        """Add the one-line identity (``priority=IDENTITY``). Exactly one is required."""
        self.add(text, priority=IDENTITY)

    def documentation(self, url: str) -> None:
        """Add the documentation pointer in pvl-core's fixed shape (``priority=DOCS``)."""
        cleaned = url.strip()
        if not cleaned:
            raise ConfigurationError("documentation url is empty")
        self.add(f"Full documentation for this server: {cleaned}", priority=DOCS)

    def _render(self, exposed: frozenset[str]) -> str:
        """Prune against *exposed* tool names and serialise. No env, no identity check."""
        kept: list[_Snippet] = []
        for s in self._snippets:
            missing = s.tools - exposed
            if missing:
                logger.debug(
                    "instructions: dropping snippet at priority %d; tool(s) not exposed: %s",
                    s.priority,
                    ", ".join(sorted(missing)),
                )
                continue
            kept.append(s)
        kept.sort(key=lambda s: (s.priority, s.seq))
        return "\n\n".join(s.text for s in kept)


_builders: weakref.WeakKeyDictionary[FastMCP, InstructionsBuilder] = weakref.WeakKeyDictionary()


def instructions_for(mcp: FastMCP) -> InstructionsBuilder:
    """Return the builder for *mcp*, creating it on first use.

    One builder per server instance; ``register_*`` helpers and domain code
    reach it through the ``mcp`` they already hold, so no helper grows a kwarg.
    """
    builder = _builders.get(mcp)
    if builder is None:
        builder = InstructionsBuilder()
        _builders[mcp] = builder
    return builder
```

Also add a **temporary** stub so the test module imports (replaced in Task 3):

```python
def finalize_instructions(mcp: FastMCP, config: ServerConfig, *, env_prefix: str) -> str:  # pragma: no cover
    raise NotImplementedError
```

And export from `src/fastmcp_pvl_core/__init__.py`: add the import block

```python
from ._instructions import (
    CAPABILITIES,
    DOCS,
    IDENTITY,
    INSTANCE,
    OPERATOR,
    WORKFLOWS,
    InstructionsBuilder,
    finalize_instructions,
    instructions_for,
)
```

and add each name to `__all__` (keep `__all__` alphabetically sorted as it is now — uppercase constants sort before `ConfigurationError`; check `ruff` doesn't reorder). Leave `build_instructions` in place for now (removed in Task 4).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_instructions.py -q`
Expected: all tests in `TestAnchors`, `TestAdd`, `TestPrune`, `TestRegistry` PASS.

- [ ] **Step 5: Lint/type, commit**

```bash
uv run ruff format src tests && uv run ruff check . && uv run mypy src
git add src/fastmcp_pvl_core/_instructions.py src/fastmcp_pvl_core/__init__.py tests/test_instructions.py
git commit -m "feat(instructions): InstructionsBuilder core with priority anchors and per-server registry (#283)"
```

---

### Task 2: `exposed_tool_names(mcp, config)` in `_visibility.py`

**Files:**
- Modify: `src/fastmcp_pvl_core/_visibility.py` (append after `apply_tool_visibility`)
- Test: `tests/test_visibility.py` (append a class)

**Interfaces:**
- Consumes: `_index_tools_by_name(mcp) -> dict[str, list[Tool]]` from `._icons` (exists; raises `RuntimeError` on FastMCP API drift).
- Produces: `exposed_tool_names(mcp: FastMCP, config: ServerConfig) -> frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_visibility.py` (it already defines `build_server()` registering `alpha`, `beta`, `gamma`, and `visible_tools(mcp)` which lists through a real `Client`):

```python
from fastmcp_pvl_core._visibility import exposed_tool_names


class TestExposedToolNames:
    """The sync rule must agree with what a real client sees after apply_tool_visibility."""

    async def test_no_filter_exposes_all(self):
        mcp = build_server()
        cfg = ServerConfig()
        apply_tool_visibility(mcp, cfg)
        assert exposed_tool_names(mcp, cfg) == frozenset(await visible_tools(mcp))

    async def test_denylist(self):
        mcp = build_server()
        cfg = ServerConfig(tools_deny=("beta", "nonexistent"))
        apply_tool_visibility(mcp, cfg)
        assert exposed_tool_names(mcp, cfg) == frozenset(await visible_tools(mcp)) == {"alpha", "gamma"}

    async def test_allowlist(self):
        mcp = build_server()
        cfg = ServerConfig(tools_allow=("gamma", "nonexistent"))
        apply_tool_visibility(mcp, cfg)
        assert exposed_tool_names(mcp, cfg) == frozenset(await visible_tools(mcp)) == {"gamma"}

    def test_both_set_raises_like_apply(self):
        with pytest.raises(ConfigurationError):
            exposed_tool_names(build_server(), ServerConfig(tools_allow=("a",), tools_deny=("b",)))
```

Check the file's existing imports: `ServerConfig`, `apply_tool_visibility`, `pytest` are imported; add `ConfigurationError` (`from fastmcp_pvl_core import ConfigurationError`) if it is not.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_visibility.py -q -k ExposedToolNames`
Expected: FAIL — `ImportError: cannot import name 'exposed_tool_names'`.

- [ ] **Step 3: Implement**

Append to `src/fastmcp_pvl_core/_visibility.py` (add `from ._icons import _index_tools_by_name` to the imports):

```python
def exposed_tool_names(mcp: FastMCP, config: ServerConfig) -> frozenset[str]:
    """Tool names *mcp* exposes under the operator allow-/denylist in *config*.

    Evaluates the same rule :func:`apply_tool_visibility` installs, without
    asking FastMCP: it stores ``disable``/``enable`` as async transforms with
    no synchronous query, and ``make_server`` is synchronous. Registered names
    come from the same enumeration ``register_tool_icons`` uses.

    Raises:
        ConfigurationError: ``tools_allow`` and ``tools_deny`` are both set.
        RuntimeError: FastMCP's component enumeration changed shape.
    """
    allow = config.tools_allow
    deny = config.tools_deny
    if allow and deny:
        raise ConfigurationError(
            "tools_allow and tools_deny are both set; set at most one — "
            "an allowlist already expresses every exclusion."
        )
    registered = frozenset(_index_tools_by_name(mcp))
    if allow:
        return registered & frozenset(allow)
    if deny:
        return registered - frozenset(deny)
    return registered
```

Refactor `apply_tool_visibility` to raise the identical message via the same check if that keeps one string (optional; do not change its behaviour).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_visibility.py -q`
Expected: PASS (all, including existing).

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && uv run ruff check . && uv run mypy src
git add src/fastmcp_pvl_core/_visibility.py tests/test_visibility.py
git commit -m "feat(visibility): exposed_tool_names evaluates the operator rule synchronously (#283)"
```

---

### Task 3: `finalize_instructions` — identity, env contract, freeze

**Files:**
- Modify: `src/fastmcp_pvl_core/_instructions.py` (replace the stub)
- Test: `tests/test_instructions.py` (append)

**Interfaces:**
- Consumes: `InstructionsBuilder._render`, `instructions_for`, `exposed_tool_names` (Task 2), `env(prefix, name)` from `._env`.
- Produces: `finalize_instructions(mcp, config, *, env_prefix) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_instructions.py`:

```python
def _server(*tool_names: str) -> FastMCP:
    mcp = FastMCP("t")
    for name in tool_names:
        mcp.tool(name=name)(lambda: "x")
    return mcp


class TestFinalize:
    def test_sets_mcp_instructions_and_returns_it(self):
        mcp = _server("alpha")
        instructions_for(mcp).identity("Ident.")
        text = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        assert text == "Ident."
        assert mcp.instructions == "Ident."

    def test_zero_identities_raise(self):
        mcp = _server()
        with pytest.raises(ConfigurationError, match="identity"):
            finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")

    def test_two_identities_raise(self):
        mcp = _server()
        b = instructions_for(mcp)
        b.identity("one")
        b.identity("two")
        with pytest.raises(ConfigurationError, match="identity"):
            finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")

    def test_prunes_operator_hidden_and_absent_tools(self):
        mcp = _server("alpha", "beta")
        b = instructions_for(mcp)
        b.identity("Ident.")
        b.add("uses alpha", priority=WORKFLOWS, tools={"alpha"})
        b.add("uses beta", priority=WORKFLOWS, tools={"beta"})
        b.add("uses ghost", priority=WORKFLOWS, tools={"ghost"})
        cfg = ServerConfig(tools_deny=("beta",))
        text = finalize_instructions(mcp, cfg, env_prefix="MY_APP")
        assert text == "Ident.\n\nuses alpha"

    def test_freezes_and_is_idempotent(self, caplog: pytest.LogCaptureFixture):
        mcp = _server()
        b = instructions_for(mcp)
        b.identity("Ident.")
        first = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        with pytest.raises(RuntimeError, match="finalized"):
            b.add("late", priority=WORKFLOWS)
        caplog.clear()
        second = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        assert first == second == mcp.instructions
        assert caplog.records == []


class TestEnvContract:
    def test_extra_appended_at_operator(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS_EXTRA", "  Vault uses PARA.  ")
        mcp = _server()
        b = instructions_for(mcp)
        b.add("instance fact", priority=INSTANCE)
        b.identity("Ident.")
        assert finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP") == (
            "Ident.\n\ninstance fact\n\nVault uses PARA."
        )

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_whitespace_extra_is_unset(self, monkeypatch: pytest.MonkeyPatch, value: str):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS_EXTRA", value)
        mcp = _server()
        instructions_for(mcp).identity("Ident.")
        assert finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP") == "Ident."

    def test_legacy_replaces_everything_and_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS", "Operator text.")
        mcp = _server()
        instructions_for(mcp).identity("Ident.")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._instructions"):
            text = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        assert text == mcp.instructions == "Operator text."
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "MY_APP_INSTRUCTIONS" in msg and "MY_APP_INSTRUCTIONS_EXTRA" in msg
        assert "ignored" not in msg

    def test_legacy_wins_over_extra_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS", "Operator text.")
        monkeypatch.setenv("MY_APP_INSTRUCTIONS_EXTRA", "extra")
        mcp = _server()
        instructions_for(mcp).identity("Ident.")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._instructions"):
            text = finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP")
        assert text == "Operator text."
        assert any("ignored" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    def test_whitespace_legacy_is_unset(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS", "   ")
        mcp = _server()
        instructions_for(mcp).identity("Ident.")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._instructions"):
            assert finalize_instructions(mcp, ServerConfig(), env_prefix="MY_APP") == "Ident."
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_legacy_skips_identity_requirement(self, monkeypatch: pytest.MonkeyPatch):
        """A verbatim operator text is complete on its own; do not fail a
        deployment that never added an identity because the legacy var is set."""
        monkeypatch.setenv("MY_APP_INSTRUCTIONS", "Operator text.")
        assert finalize_instructions(_server(), ServerConfig(), env_prefix="MY_APP") == "Operator text."

    def test_prefix_trailing_underscore_normalised(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MY_APP_INSTRUCTIONS_EXTRA", "extra")
        a, b = _server(), _server()
        instructions_for(a).identity("I.")
        instructions_for(b).identity("I.")
        assert finalize_instructions(a, ServerConfig(), env_prefix="MY_APP") == finalize_instructions(
            b, ServerConfig(), env_prefix="MY_APP_"
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_instructions.py -q -k "Finalize or EnvContract"`
Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement `finalize_instructions`**

Replace the stub in `_instructions.py`. Add imports `from ._env import env` and `from ._visibility import exposed_tool_names` (runtime imports — `_visibility` already imports `_config`; no cycle with `_instructions` because `_visibility` does not import it).

```python
def finalize_instructions(mcp: FastMCP, config: ServerConfig, *, env_prefix: str) -> str:
    """Render the server's instructions once, apply the env contract, and set them.

    Call after :func:`apply_tool_visibility`, and after every ``register_*``
    helper and domain contribution. Order of operations:

    1. exposed tools = :func:`exposed_tool_names` (registered ∧ operator rule)
    2. drop every snippet whose ``tools`` are not all exposed (``DEBUG`` per drop)
    3. exactly one identity snippet must remain
    4. serialise by ``(priority, insertion)``, blank-line separated
    5. ``{P}_INSTRUCTIONS_EXTRA`` appended at ``OPERATOR``; legacy
       ``{P}_INSTRUCTIONS`` replaces the whole text with one ``WARNING``
    6. set ``mcp.instructions``, cache, freeze the builder

    A second call returns the cached string without re-reading env or
    logging. Per-subject auth visibility is out of scope (see the spec).

    Args:
        mcp: The server whose builder to finalize.
        config: Universal server config; only ``tools_allow`` / ``tools_deny``
            are read.
        env_prefix: Env-var prefix, with or without a trailing underscore.

    Returns:
        The final instructions string, also set on ``mcp.instructions``.

    Raises:
        ConfigurationError: No identity, more than one identity, or both
            visibility lists set.
        RuntimeError: FastMCP's component enumeration changed shape.
    """
    builder = instructions_for(mcp)
    if builder._result is not None:
        return builder._result

    prefix = env_prefix.rstrip("_")
    legacy = (env(prefix, "INSTRUCTIONS") or "").strip()
    extra = (env(prefix, "INSTRUCTIONS_EXTRA") or "").strip()

    if legacy:
        logger.warning(
            "%s_INSTRUCTIONS replaces all generated guidance and is deprecated; "
            "use %s_INSTRUCTIONS_EXTRA to add context.%s",
            prefix,
            prefix,
            f" {prefix}_INSTRUCTIONS_EXTRA is set and was ignored." if extra else "",
        )
        text = legacy
    else:
        if extra:
            builder.add(extra, priority=OPERATOR)
        exposed = exposed_tool_names(mcp, config)
        identities = [s for s in builder._snippets if s.priority == IDENTITY]
        if len(identities) != 1:
            raise ConfigurationError(
                f"instructions need exactly one identity snippet, found {len(identities)}; "
                "call instructions_for(mcp).identity(...) once"
            )
        text = builder._render(exposed)

    mcp.instructions = text
    builder._result = text
    builder._frozen = True
    return text
```

Note the identity count is taken **before** pruning by design: identity has no tools, so pruning cannot remove it, and counting before keeps the error message about contributors, not visibility.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_instructions.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && uv run ruff check . && uv run mypy src
git add src/fastmcp_pvl_core/_instructions.py tests/test_instructions.py
git commit -m "feat(instructions): finalize_instructions with pruning, identity check, and env contract (#283)"
```

---

### Task 4: Remove `build_instructions`; exports, README

**Files:**
- Modify: `src/fastmcp_pvl_core/_factory.py:32-70` (delete function)
- Modify: `src/fastmcp_pvl_core/__init__.py:45-49, 142` (drop import and `__all__` entry)
- Modify: `tests/test_factory.py:17-57` (delete `TestBuildInstructions`; drop `build_instructions` from the import list)
- Modify: `README.md:157-167`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instructions.py`:

```python
def test_build_instructions_is_gone():
    import fastmcp_pvl_core

    assert not hasattr(fastmcp_pvl_core, "build_instructions")
    assert "build_instructions" not in fastmcp_pvl_core.__all__
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_instructions.py -q -k gone`
Expected: FAIL — attribute exists.

- [ ] **Step 3: Delete and re-point**

- In `_factory.py` delete `def build_instructions(...)` through its `return (...)` (lines 32–70). Keep `build_event_store` and `compute_app_domain`. Remove any import that becomes unused (`ruff` will flag).
- In `__init__.py` remove `build_instructions` from the `._factory` import and from `__all__`.
- In `tests/test_factory.py` delete `class TestBuildInstructions` and the `build_instructions` import.
- In `README.md` replace the usage block:

```python
from fastmcp import FastMCP
from fastmcp_pvl_core import (
    ServerConfig, apply_tool_visibility, build_auth,
    finalize_instructions, instructions_for, wire_middleware_stack,
)

config = ServerConfig.from_env("MY_APP")
mcp = FastMCP(name="my-app", auth=build_auth(config))
wire_middleware_stack(mcp)

instructions_for(mcp).identity("A widget service.")
instructions_for(mcp).documentation("https://example.com/my-app/llms.txt")
# ... register tools; core register_* helpers add their own workflow snippets ...
apply_tool_visibility(mcp, config)
finalize_instructions(mcp, config, env_prefix="MY_APP")
```

and add, directly under it, a short subsection:

```markdown
### Instructions (model-facing guidance)

Instructions carry what no single tool description can carry: identity, a
documentation pointer, cross-tool workflows, and enforced instance facts.
Add snippets with `instructions_for(mcp).add(text, priority=..., tools=...)`;
a snippet naming a tool the operator hid via `TOOLS_ALLOW`/`TOOLS_DENY` is
dropped at `finalize_instructions`. Operators add deployment context with
`{PREFIX}_INSTRUCTIONS_EXTRA`. `{PREFIX}_INSTRUCTIONS` (legacy) still
replaces the whole text and logs a deprecation warning.
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: PASS, clean.

- [ ] **Step 5: Commit (breaking)**

```bash
git add src/fastmcp_pvl_core/_factory.py src/fastmcp_pvl_core/__init__.py tests/test_factory.py tests/test_instructions.py README.md
git commit -m "feat(instructions)!: remove build_instructions in favour of InstructionsBuilder (#283)

BREAKING CHANGE: build_instructions() is gone. Call instructions_for(mcp).identity(...) and finalize_instructions(mcp, config, env_prefix=...) after apply_tool_visibility. The FastMCP(instructions=...) kwarg is no longer passed by consumers."
```

---

### Task 5: Transfer workflow snippet

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py` (before `return links` at the end of `register_transfer_routes`)
- Test: `tests/test_transfer_register.py`

**Interfaces:**
- Consumes: `instructions_for` from `.._instructions`; `WORKFLOWS`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transfer_register.py` (it already has `_register()` returning `(mcp, sink, validate)`, and imports `ServerConfig`):

```python
from fastmcp_pvl_core import finalize_instructions, instructions_for


class TestInstructionsSnippet:
    def test_workflow_snippet_present_when_both_tools_exposed(self):
        mcp, _, _ = _register()
        instructions_for(mcp).identity("T.")
        text = finalize_instructions(
            mcp, ServerConfig(base_url="https://x.example.com", kv_store_url="memory://"), env_prefix="T"
        )
        assert "create_upload_link" in text and "create_download_link" in text
        assert "single-use" in text

    def test_snippet_dropped_when_either_tool_hidden(self):
        mcp, _, _ = _register()
        instructions_for(mcp).identity("T.")
        cfg = ServerConfig(
            base_url="https://x.example.com", kv_store_url="memory://", tools_deny=("create_upload_link",)
        )
        text = finalize_instructions(mcp, cfg, env_prefix="T")
        assert "create_upload_link" not in text and "create_download_link" not in text
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_transfer_register.py -q -k InstructionsSnippet`
Expected: first test FAILS (`"create_upload_link" in text` is False).

- [ ] **Step 3: Implement**

In `_transfer/register.py`, add `from .._instructions import WORKFLOWS, instructions_for` to the imports, and immediately before `return links` at the end of `register_transfer_routes`:

```python
    instructions_for(mcp).add(
        "To upload a file, call create_upload_link and then PUT the bytes to "
        "the returned URL; to download, call create_download_link and GET the "
        "returned URL. Each link is a single-use capability URL that expires: "
        "do not reuse it, share it, or call the link tools speculatively.",
        priority=WORKFLOWS,
        tools={"create_download_link", "create_upload_link"},
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_transfer_register.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && uv run ruff check . && uv run mypy src
git add src/fastmcp_pvl_core/_transfer/register.py tests/test_transfer_register.py
git commit -m "feat(transfer): contribute the upload/download workflow to server instructions (#283)"
```

---

### Task 6: Jobs polling snippet

**Files:**
- Modify: `src/fastmcp_pvl_core/_jobs/register.py` (`register_job_tools`, after the `@mcp.tool(name=JOB_POLL_TOOL_NAME, ...)` registration)
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: `instructions_for`, `WORKFLOWS` from `.._instructions`; `JOB_POLL_TOOL_NAME` (`"get_job_result"`) already imported.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs.py` (it has `_jobs()` and imports `FastMCP`, `ServerConfig`, `register_job_tools`):

```python
from fastmcp_pvl_core import finalize_instructions, instructions_for
from fastmcp_pvl_core._jobs.records import JOB_POLL_TOOL_NAME


class TestInstructionsSnippet:
    def test_polling_guidance_present(self):
        mcp = FastMCP("t")
        register_job_tools(mcp, _jobs())
        instructions_for(mcp).identity("T.")
        text = finalize_instructions(mcp, ServerConfig(kv_store_url="memory://"), env_prefix="T")
        assert JOB_POLL_TOOL_NAME in text and "job id" in text

    def test_dropped_when_poll_tool_hidden(self):
        mcp = FastMCP("t")
        register_job_tools(mcp, _jobs())
        instructions_for(mcp).identity("T.")
        cfg = ServerConfig(kv_store_url="memory://", tools_deny=(JOB_POLL_TOOL_NAME,))
        assert JOB_POLL_TOOL_NAME not in finalize_instructions(mcp, cfg, env_prefix="T")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_jobs.py -q -k InstructionsSnippet`
Expected: first test FAILS.

- [ ] **Step 3: Implement**

In `_jobs/register.py` add `from .._instructions import WORKFLOWS, instructions_for`; at the end of `register_job_tools` (after the polling tool is registered, before the function returns):

```python
    instructions_for(mcp).add(
        "A long-running tool returns a job id when this client cannot run it "
        f"as a task; poll {JOB_POLL_TOOL_NAME} with that id until the status "
        "is completed or failed, honouring retry_after_s, instead of invoking "
        "the tool again.",
        priority=WORKFLOWS,
        tools={JOB_POLL_TOOL_NAME},
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_jobs.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && uv run ruff check . && uv run mypy src
git add src/fastmcp_pvl_core/_jobs/register.py tests/test_jobs.py
git commit -m "feat(jobs): contribute the polling workflow to server instructions (#283)"
```

---

### Task 7: End-to-end integration test

**Files:**
- Create: `tests/test_instructions_integration.py`

**Interfaces:**
- Consumes: everything above; `Client` from `fastmcp`; `_RecordingSink`/`_RecordingValidator`-style stubs (define local copies — do not import from another test module).

- [ ] **Step 1: Write the test**

```python
"""Real server, real visibility, real initialize: the instructions a client
receives are the pruned, finalized text (spec §Testing / integration)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastmcp import Client, FastMCP

from fastmcp_pvl_core import (
    JobsConfig,
    ServerConfig,
    TransferConfig,
    TransferReadResult,
    apply_tool_visibility,
    build_jobs,
    finalize_instructions,
    instructions_for,
    register_job_tools,
    register_long_running_tool,
    register_transfer_routes,
)
from fastmcp_pvl_core._jobs.records import JOB_POLL_TOOL_NAME


class _Sink:
    async def read(self, handle: str) -> TransferReadResult:
        return TransferReadResult(b"x", "text/plain", "x.txt")

    async def write(self, handle: str, body: bytes) -> Mapping[str, Any]:
        return {"stored": handle}


async def _validate(ref: str, kind: str) -> str:
    return f"{kind}:{ref}"


async def test_client_receives_pruned_finalized_instructions(monkeypatch):
    monkeypatch.setenv("APP_INSTRUCTIONS_EXTRA", "This deployment is a demo.")
    config = ServerConfig(
        base_url="https://x.example.com",
        kv_store_url="memory://",
        tools_deny=("create_upload_link",),
    )
    mcp = FastMCP("app")
    instructions_for(mcp).identity("A demo server.")
    register_transfer_routes(
        mcp,
        config,
        TransferConfig(ttl_default_s=10, ttl_max_s=20, grace_ttl_s=5, lease_s=5, max_upload_bytes=1024),
        sink=_Sink(),
        validate=_validate,
    )
    jobs = build_jobs(config, JobsConfig(soft_deadline_s=0.1, result_ttl_s=60.0))

    @register_long_running_tool(mcp, jobs, name="slow")
    async def slow() -> str:
        return "done"

    register_job_tools(mcp, jobs)
    apply_tool_visibility(mcp, config)
    text = finalize_instructions(mcp, config, env_prefix="APP")

    async with Client(mcp) as client:
        received = client.initialize_result.instructions
        listed = {t.name for t in await client.list_tools()}

    assert received == text == mcp.instructions
    assert text.startswith("A demo server.")
    assert JOB_POLL_TOOL_NAME in text                # job tool exposed → snippet kept
    assert "create_download_link" not in text        # transfer snippet dropped: upload hidden
    assert text.endswith("This deployment is a demo.")
    assert "create_upload_link" not in listed and JOB_POLL_TOOL_NAME in listed
```

Check `register_long_running_tool`'s decorator accepts `name=` via `**tool_kwargs` (it forwards to `mcp.tool`); `build_jobs(config, jobs_config)` signature is as used in `tests/test_jobs.py::_jobs`.

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_instructions_integration.py -q`
Expected: PASS. If `client.initialize_result` is `None`, read it right after entering the context (it is populated on enter in FastMCP 3.3 — verified in this session).

- [ ] **Step 3: Full local checks on both Python ends**

```bash
uv sync --all-extras
uv run pytest -q
uv run --python 3.13 pytest -q
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_instructions_integration.py
git commit -m "test(instructions): end-to-end initialize carries pruned, finalized instructions (#283)"
```

---

### Task 8: Downstream follow-ups (issues, not code)

**Files:** none in this repo.

- [ ] **Step 1: File the template issue** on `pvliesdonk/fastmcp-server-template` (under the #131 umbrella): `FastMCP(...)` drops `instructions=`; replace the `env(...) or build_instructions(...)` line with `instructions_for(mcp).identity("{{ domain_description }}")` after construction and `finalize_instructions(mcp, config.server, env_prefix=_ENV_PREFIX)` after `apply_tool_visibility`; document `_INSTRUCTIONS_EXTRA` and mark `_INSTRUCTIONS` legacy in `docs/configuration.md.jinja`. Blocked on the pvl-core major release.

- [ ] **Step 2: File the markdown-vault-mcp issue**: move `_instructions.py` composition to `instructions_for(mcp)` calls (identity, gated `add`s with `tools=`), delete `config.instructions` and the post-construction `mcp.instructions = …` assignment. Note scholar-mcp and image-generation-mcp need only the template line change.

- [ ] **Step 3: Link both from #283** in a comment, then open the pvl-core PR with `Closes #283`.
