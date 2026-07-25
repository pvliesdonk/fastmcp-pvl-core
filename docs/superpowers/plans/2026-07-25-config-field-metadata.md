# ServerConfig field metadata + `server_config_surface()` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every `ServerConfig` env field self-describing metadata (help text, tags, wizard hints) and expose it through a new `server_config_surface()` accessor, so downstream projects can *generate* their config documentation instead of hand-copying it.

**Architecture:** `ServerConfig` is already a frozen stdlib dataclass whose 18 fields map 1:1 onto the 18 env suffixes `from_env` reads. This plan converts all 18 declarations to `field(default=…, metadata={…})` and adds a `ConfigField` record plus a `server_config_surface()` accessor that returns them **in declaration order** as a tuple. Declaration order matters: it gives consumers a deterministic iteration order, which the existing `frozenset`-returning `server_config_env_suffixes()` cannot.

**Tech Stack:** Python 3.10+, stdlib `dataclasses` only (no new dependencies), pytest, mypy strict, ruff.

**Upstream spec:** `docs/superpowers/specs/2026-07-25-config-generation-and-ownership-model-design.md` in the `fastmcp-server-template` repo, Stage 0. This is a **cross-repo prerequisite**: template Stages 1–3 cannot merge until this ships as v4.5.0.

## Global Constraints

- **Python floor is `>=3.10`.** No `StrEnum` (3.11+), no `match` statements. `requires-python` in `pyproject.toml` is `>=3.10` and ruff `target-version = "py310"`.
- **No new dependencies.** Field metadata is stdlib `dataclasses` only.
- **mypy runs on `src` only, in strict mode** (`uv run mypy src`). Test files carry no return-type annotations — match the existing style in `tests/test_config.py`, which has none.
- **`from __future__ import annotations` is already at the top of `_config.py`.** Consequently `dataclasses.Field.type` is a **string**, not a type object. Code that reads `f.type` must treat it as `str`.
- **Behaviour must not change.** All 18 current defaults are immutable, so converting `x: T = value` to `x: T = field(default=value, metadata={…})` is behaviour-preserving. Do not alter any default value.
- **Help text must not restate a scalar default.** `ConfigField.default` already carries it structurally, and the downstream generator renders both. The one deliberate exception is `transport`, where naming the accepted literal values *is* the documentation.
- **Gate before every commit:** `uv run ruff format --check .` · `uv run ruff check .` · `uv run mypy src` · `uv run pytest`
- **Branch from fresh `origin/main`.** As of writing that is `4cf1755`. Do **not** branch from the local `chore/uv-lock-sync-4.3.0`, whose commit already merged as #228.
- **Do not run the release.** Release dispatch is human-only. This plan ends at a merged PR; publishing v4.5.0 is the maintainer's manual step.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/fastmcp_pvl_core/_config.py` | `ServerConfig`, its field metadata, `ConfigField`, `server_config_surface()`, the env-suffix set | Modify |
| `src/fastmcp_pvl_core/__init__.py` | public re-exports and `__all__` | Modify |
| `tests/test_config.py` | `ServerConfig` tests, including the new `TestServerConfigSurface` class | Modify |

No new files. `_config.py` is the correct home: the metadata lives on the field declarations it describes, and the accessor is the reader of those same declarations.

---

## Task 1: `ConfigField` record + `server_config_surface()` accessor

Build the accessor first, against the *current* metadata-free declarations. It returns 18 records with empty `help` and `tags`, which is correct at this point — Task 2 fills them in. This ordering means Task 2's tests can assert on real content through a shipped, tested interface.

**Files:**
- Modify: `src/fastmcp_pvl_core/_config.py`
- Modify: `src/fastmcp_pvl_core/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ServerConfig` (existing), `server_config_env_suffixes()` (existing).
- Produces:
  - `ConfigField` — frozen dataclass with fields `suffix: str`, `name: str`, `type_name: str`, `default: object`, `help: str`, `tags: tuple[str, ...]`, `inferred: bool`, `wizard: Mapping[str, object]`.
  - `server_config_surface() -> tuple[ConfigField, ...]` — declaration-ordered.
  - Both exported from `fastmcp_pvl_core`.

- [ ] **Step 1: Write the failing tests**

First replace lines 1–18 of `tests/test_config.py` — the whole header through the
closing paren of the import block — with exactly this. The file currently imports
only the *names* `dataclass, field` from `dataclasses`, not the module, and the
tests below need `dataclasses.fields` and `dataclasses.FrozenInstanceError`:

```python
"""Tests for ServerConfig."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from fastmcp_pvl_core import (
    ConfigField,
    ConfigurationError,
    ServerConfig,
    build_bearer_auth,
    domain_env_suffixes,
    env,
    env_float,
    env_int,
    server_config_env_suffixes,
    server_config_surface,
)
```

Two existing tests import `server_config_env_suffixes` inside the method body
(around lines 365 and 389 before this edit). Leave those local imports alone —
they keep working, and removing them is not this task's business.

Then append this class at the end of the file:

```python
class TestServerConfigSurface:
    def test_surface_returns_config_field_records(self):
        assert all(isinstance(c, ConfigField) for c in server_config_surface())

    def test_covers_every_field_in_declaration_order(self):
        """Declaration order is the contract — it is what makes generated output stable."""
        surface = server_config_surface()
        assert tuple(c.name for c in surface) == tuple(
            f.name for f in dataclasses.fields(ServerConfig)
        )

    def test_returns_eighteen_fields(self):
        assert len(server_config_surface()) == 18

    def test_suffix_is_the_upper_cased_field_name(self):
        assert all(c.suffix == c.name.upper() for c in server_config_surface())

    def test_suffixes_match_the_env_suffix_set(self):
        """The surface and the existing frozenset describe the same 18 vars."""
        assert {c.suffix for c in server_config_surface()} == server_config_env_suffixes()

    def test_scalar_default_is_carried_through(self):
        host = next(c for c in server_config_surface() if c.name == "host")
        assert host.default == "127.0.0.1"

    def test_default_factory_is_resolved_to_a_value(self):
        """oidc_required_scopes uses default_factory=tuple; the surface reports ()."""
        scopes = next(
            c for c in server_config_surface() if c.name == "oidc_required_scopes"
        )
        assert scopes.default == ()

    def test_type_name_is_the_annotation_string(self):
        port = next(c for c in server_config_surface() if c.name == "port")
        assert port.type_name == "int"

    def test_records_are_frozen(self):
        record = server_config_surface()[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.help = "mutated"  # type: ignore[misc]

    def test_order_is_stable_under_hash_randomisation(self):
        """Guards the generated-output byte-stability failure mode.

        server_config_env_suffixes() returns a frozenset, whose iteration order
        varies between processes because CPython randomises string hashing. The
        surface must not inherit that.
        """
        program = (
            "from fastmcp_pvl_core import server_config_surface;"
            "print(','.join(c.suffix for c in server_config_surface()))"
        )
        outputs = set()
        for seed in ("1", "2", "3"):
            result = subprocess.run(
                [sys.executable, "-c", program],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            )
            outputs.add(result.stdout.strip())
        assert len(outputs) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py::TestServerConfigSurface -v`

Expected: collection error — `ImportError: cannot import name 'ConfigField' from 'fastmcp_pvl_core'`.

- [ ] **Step 3: Add `ConfigField` and `server_config_surface()` to `_config.py`**

Add `from collections.abc import Mapping` to the imports at the top of `src/fastmcp_pvl_core/_config.py`. Then insert both definitions immediately **after** the `server_config_env_suffixes()` function and **before** `domain_env_suffixes()`. Placement after the `ServerConfig` class is required — `dataclasses.fields(ServerConfig)` needs the class to exist.

```python
@dataclass(frozen=True)
class ConfigField:
    """One env-configurable :class:`ServerConfig` field and its metadata.

    ``help`` deliberately does **not** restate a scalar default: ``default``
    carries it structurally, and a documentation generator renders both. The
    one exception is ``transport``, where naming the accepted literal values is
    the documentation.
    """

    suffix: str
    """Env suffix, i.e. the part after ``{PREFIX}_`` — e.g. ``BASE_URL``."""

    name: str
    """Python field name — e.g. ``base_url``."""

    type_name: str
    """The annotation as written, e.g. ``str | None``."""

    default: object
    """The declared default. ``default_factory`` fields report the built value."""

    help: str
    """One-or-two-sentence description. Empty when undocumented."""

    tags: tuple[str, ...]
    """Semantic tags used to route this field into documentation sections.

    Layout-agnostic by design: core says *what* a field is about, never which
    file it belongs in. A field may carry several tags, and appearing in more
    than one destination is intentional.
    """

    inferred: bool
    """True when the value is derived rather than set directly, so no control
    should be offered for it."""

    wizard: Mapping[str, object]
    """Presentation hints for a config wizard — e.g. ``group``, ``secret``,
    ``when``. Empty for inferred fields."""


def server_config_surface() -> tuple[ConfigField, ...]:
    """Return every :class:`ServerConfig` env field, in declaration order.

    Declaration order is part of the contract: a consumer that renders this
    tuple produces byte-identical output on every run. Prefer this over
    :func:`server_config_env_suffixes`, which returns a ``frozenset`` whose
    iteration order varies between processes under hash randomisation.

    Covers the same 18 variables as :func:`server_config_env_suffixes`, adding
    each field's type, default, help text, tags, and wizard hints.
    """
    records: list[ConfigField] = []
    for f in dataclasses.fields(ServerConfig):
        if f.default is not dataclasses.MISSING:
            default: object = f.default
        elif f.default_factory is not dataclasses.MISSING:
            default = f.default_factory()
        else:  # pragma: no cover — every current field has a default
            default = None

        tags = tuple(str(tag) for tag in f.metadata.get("tags", ()))

        # ``metadata={"wizard": "inferred"}`` is the shorthand for a field with
        # no control; anything else is a mapping of presentation hints.
        raw_wizard = f.metadata.get("wizard", {})
        inferred = raw_wizard == "inferred"
        wizard: dict[str, object] = {}
        if not inferred and raw_wizard:
            wizard = {str(k): v for k, v in raw_wizard.items()}

        records.append(
            ConfigField(
                suffix=f.name.upper(),
                name=f.name,
                type_name=f.type if isinstance(f.type, str) else str(f.type),
                default=default,
                help=str(f.metadata.get("help", "")),
                tags=tags,
                inferred=inferred,
                wizard=wizard,
            )
        )
    return tuple(records)
```

- [ ] **Step 4: Export both symbols**

In `src/fastmcp_pvl_core/__init__.py`, add `ConfigField` and `server_config_surface` to the existing `from ._config import (...)` block, and add both to `__all__`. Both lists are alphabetically sorted — insert accordingly: `"ConfigField"` sorts before `"ServerConfig"`; `"server_config_surface"` sorts immediately after `"server_config_env_suffixes"`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py::TestServerConfigSurface -v`

Expected: 10 passed.

- [ ] **Step 6: Run the full gate**

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

Expected: all clean. If `ruff format --check` fails, run `uv run ruff format .` and re-run the gate.

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_config.py src/fastmcp_pvl_core/__init__.py tests/test_config.py
git commit -m "feat(config): add ConfigField + server_config_surface() accessor

Exposes every ServerConfig env field in declaration order with its type,
default, help, tags, and wizard hints. Declaration order is contractual:
it gives consumers byte-stable output, which the frozenset returned by
server_config_env_suffixes() cannot.

Metadata values themselves land in the next commit.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01As25PstUQeTR47J5SP5g7P"
```

---

## Task 2: Populate metadata on all 18 fields

**Files:**
- Modify: `src/fastmcp_pvl_core/_config.py` (the `ServerConfig` class body)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ConfigField`, `server_config_surface()` from Task 1.
- Produces: no new symbols. Every `ConfigField` now carries non-empty `help` and at least one tag; `auth_mode` is the only field with `inferred=True`.

- [ ] **Step 1: Write the failing tests**

Append these methods to the `TestServerConfigSurface` class created in Task 1.

```python
    def test_every_field_is_documented(self):
        undocumented = [c.name for c in server_config_surface() if not c.help]
        assert undocumented == []

    def test_every_field_is_tagged(self):
        untagged = [c.name for c in server_config_surface() if not c.tags]
        assert untagged == []

    def test_auth_mode_is_the_only_inferred_field(self):
        """AUTH_MODE is derived from which auth vars are set, so it gets no control."""
        assert [c.name for c in server_config_surface() if c.inferred] == ["auth_mode"]

    def test_inferred_field_carries_no_wizard_hints(self):
        auth_mode = next(c for c in server_config_surface() if c.name == "auth_mode")
        assert auth_mode.wizard == {}

    def test_base_url_carries_several_tags(self):
        """A field can honestly belong to several documentation sections."""
        base_url = next(c for c in server_config_surface() if c.name == "base_url")
        assert set(base_url.tags) == {"server", "oidc", "apps"}

    def test_secret_fields_are_marked(self):
        secrets = {c.suffix for c in server_config_surface() if c.wizard.get("secret")}
        assert secrets == {
            "BEARER_TOKEN",
            "OIDC_CLIENT_SECRET",
            "OIDC_JWT_SIGNING_KEY",
        }

    def test_oidc_fields_share_the_oidc_tag(self):
        tagged = {c.suffix for c in server_config_surface() if "oidc" in c.tags}
        assert tagged == {
            "BASE_URL",
            "OIDC_CONFIG_URL",
            "OIDC_CLIENT_ID",
            "OIDC_CLIENT_SECRET",
            "OIDC_AUDIENCE",
            "OIDC_REQUIRED_SCOPES",
            "OIDC_JWT_SIGNING_KEY",
            "OIDC_VERIFY_ACCESS_TOKEN",
        }

    def test_kv_store_url_is_readme_prominent(self):
        """The consuming README shows a 3-row curated table; this is its core row."""
        kv = next(c for c in server_config_surface() if c.name == "kv_store_url")
        assert "readme" in kv.tags

    def test_defaults_are_unchanged_by_the_metadata_migration(self):
        """Behaviour guard: converting to field(default=...) must not alter values."""
        config = ServerConfig()
        assert config.host == "127.0.0.1"
        assert config.port == 8000
        assert config.transport == "stdio"
        assert config.oidc_required_scopes == ()
        assert config.oidc_verify_access_token is False
        assert config.bearer_default_subject == DEFAULT_BEARER_SUBJECT
        assert config.base_url is None
```

`DEFAULT_BEARER_SUBJECT` is **not** exported from the package and no test
currently imports it. Add this line directly below the
`from fastmcp_pvl_core import (...)` block:

```python
from fastmcp_pvl_core._config import DEFAULT_BEARER_SUBJECT
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py::TestServerConfigSurface -v`

Expected: `test_every_field_is_documented` fails listing all 18 field names; `test_every_field_is_tagged` likewise; the tag/secret/inferred tests fail on empty sets.

- [ ] **Step 3: Replace the `ServerConfig` field declarations**

In `src/fastmcp_pvl_core/_config.py`, replace the entire run of 18 field declarations in the `ServerConfig` class body — from `transport: Transport = "stdio"` through `bearer_default_subject: str = DEFAULT_BEARER_SUBJECT` — with the block below.

Two existing code comments are **deleted** by this replacement because their content moves into `help`: the four-line comment above `event_store_url` and the two-line comment above `bearer_default_subject`. That is intentional — the help text is now the single home for that explanation. Do not leave the comments behind.

Field order is unchanged. `server` is not a field of `ServerConfig` and does not appear here.

```python
    transport: Transport = field(
        default="stdio",
        metadata={
            "help": (
                "Transport the server speaks: ``stdio`` for local Claude "
                "Desktop/Code, ``http`` or ``sse`` for a network server."
            ),
            "tags": ("server",),
            # Emitted by a routing select in the wizard, not a free-text field.
            "wizard": {"control": "emit"},
        },
    )
    host: str = field(
        default="127.0.0.1",
        metadata={
            "help": "Interface the HTTP server binds to.",
            "tags": ("server",),
            "wizard": {"group": "Server", "when": "server"},
        },
    )
    port: int = field(
        default=8000,
        metadata={
            "help": "TCP port for the HTTP server.",
            "tags": ("server",),
            "wizard": {"group": "Server", "when": "server"},
        },
    )
    base_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "Public base URL of the deployed server, e.g. "
                "``https://mcp.example.com``. Required for OIDC and for MCP "
                "Apps resource URLs."
            ),
            "tags": ("server", "oidc", "apps"),
            "wizard": {"when": "server"},
        },
    )

    bearer_token: str | None = field(
        default=None,
        metadata={
            "help": (
                "Single shared bearer token; any non-empty value enables "
                "bearer auth."
            ),
            "tags": ("auth", "bearer"),
            "wizard": {"when": "bearer", "secret": True},
        },
    )

    oidc_config_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "OIDC discovery document URL, e.g. "
                "``https://auth.example.com/.well-known/openid-configuration``."
            ),
            "tags": ("auth", "oidc"),
            "wizard": {"when": "oidc"},
        },
    )
    oidc_client_id: str | None = field(
        default=None,
        metadata={
            "help": "OIDC client identifier registered with the provider.",
            "tags": ("auth", "oidc"),
            "wizard": {"when": "oidc"},
        },
    )
    oidc_client_secret: str | None = field(
        default=None,
        metadata={
            "help": "OIDC client secret registered with the provider.",
            "tags": ("auth", "oidc"),
            "wizard": {"when": "oidc", "secret": True},
        },
    )
    oidc_audience: str | None = field(
        default=None,
        metadata={
            "help": (
                "Expected ``aud`` claim; tokens issued for another audience "
                "are rejected."
            ),
            "tags": ("auth", "oidc"),
            "wizard": {"group": "Auth", "when": "oidc"},
        },
    )
    oidc_required_scopes: tuple[str, ...] = field(
        default_factory=tuple,
        metadata={
            "help": "Comma-separated scopes a caller must present to be authorised.",
            "tags": ("auth", "oidc"),
            "wizard": {"group": "Auth", "when": "oidc"},
        },
    )
    oidc_jwt_signing_key: str | None = field(
        default=None,
        metadata={
            "help": (
                "Signing key for issued JWTs. Required on Linux/Docker — the "
                "generated fallback is ephemeral and invalidates every token "
                "on restart. Generate with ``openssl rand -hex 32``."
            ),
            "tags": ("auth", "oidc"),
            "wizard": {"when": "oidc", "secret": True},
        },
    )
    oidc_verify_access_token: bool = field(
        default=False,
        metadata={
            "help": "Validate the access token instead of the id token.",
            "tags": ("auth", "oidc"),
            "wizard": {"group": "Auth", "when": "oidc"},
        },
    )

    kv_store_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "Persistent-state backend URL for pvl-core subsystems: "
                "``file:///path`` survives restarts, ``memory://`` is "
                "ephemeral and for development only."
            ),
            "tags": ("persistence", "readme"),
            "wizard": {"group": "Persistence", "when": "server"},
        },
    )
    event_store_url: str | None = field(
        default=None,
        metadata={
            "help": (
                "Legacy override for HTTP resumability, honoured by "
                "``build_event_store`` and ``build_kv_store`` only when "
                "``kv_store_url`` is unset. Prefer ``kv_store_url`` for new "
                "deployments — a single URL drives every pvl-core subsystem "
                "that needs persistent state."
            ),
            "tags": ("persistence",),
            "wizard": {"group": "Persistence", "when": "server"},
        },
    )
    app_domain: str | None = field(
        default=None,
        metadata={
            "help": "Public domain that serves MCP Apps UI resources.",
            "tags": ("apps",),
            "wizard": {"group": "MCP Apps", "when": "server"},
        },
    )

    auth_mode: str | None = field(
        default=None,
        metadata={
            "help": (
                "Resolved authentication mode. Inferred from which auth "
                "variables are set, so no dedicated control is offered for it."
            ),
            "tags": ("auth",),
            "wizard": "inferred",
        },
    )

    bearer_tokens_file: Path | None = field(
        default=None,
        metadata={
            "help": (
                "Path to a TOML file mapping bearer tokens to subjects; "
                "overrides the single-token ``bearer_token`` mode."
            ),
            "tags": ("auth", "bearer"),
            "wizard": {"group": "Auth", "when": "bearer"},
        },
    )
    bearer_default_subject: str = field(
        default=DEFAULT_BEARER_SUBJECT,
        metadata={
            "help": (
                "Subject assigned to the single-token bearer mode; ignored "
                "when ``bearer_tokens_file`` is set, since mapped mode carries "
                "per-token subjects."
            ),
            "tags": ("auth", "bearer"),
            "wizard": {"group": "Auth", "when": "bearer"},
        },
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`

Expected: all pass, including the pre-existing `ServerConfig` tests. The pre-existing ones passing is the behaviour-preservation evidence.

- [ ] **Step 5: Confirm the migrated comments are gone**

```bash
grep -n 'Legacy override, honoured by build_event_store' src/fastmcp_pvl_core/_config.py
grep -n 'Subject for the single-token bearer mode' src/fastmcp_pvl_core/_config.py
```

Expected: no output from either. Both explanations now live in `help`. If either prints a line, delete that comment.

- [ ] **Step 6: Run the full gate**

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_config.py tests/test_config.py
git commit -m "feat(config): document and tag all 18 ServerConfig env fields

Each field now carries help text, semantic tags, and wizard presentation
hints, so consumers can generate config docs instead of hand-copying the
env surface. Tags are layout-agnostic: core says what a field is about,
never which file documents it, and a field may carry several.

auth_mode is marked inferred — it is derived from which auth variables
are set, so no control should be offered for it. That removes the need
for consumers to maintain a local exception list for it.

The event_store_url and bearer_default_subject explanatory comments move
into their help text; help is now the single home for both.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01As25PstUQeTR47J5SP5g7P"
```

---

## Task 3: Derive the env-suffix set from the fields

The same duplication this whole effort removes downstream also exists here: `_SERVER_CONFIG_ENV_SUFFIXES` is a hand-maintained 18-string `frozenset` literal that must be edited in lockstep with the field declarations. Since suffix is mechanically `name.upper()` for every field, the literal is derivable — and the AST-scan guard becomes a *stronger* invariant once it compares `from_env`'s reads against the fields themselves.

**This task is independently cuttable.** Tasks 1 and 2 fully satisfy the upstream spec's Stage 0 without it. Skip it if you want the smallest possible release.

**Files:**
- Modify: `src/fastmcp_pvl_core/_config.py:173-194` (the literal) — anchor by the grep in Step 2 rather than by line number, since Task 2 shifted the file
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ServerConfig`, `server_config_surface()`.
- Produces: no new symbols. `server_config_env_suffixes()` keeps its exact signature and return type, `frozenset[str]`.

- [ ] **Step 1: Write the failing test**

Append to the existing `TestServerConfigEnvSuffixes` class in `tests/test_config.py`.

```python
    def test_suffix_set_is_derived_from_the_fields(self):
        """No hand-maintained literal: the set must fall out of the declarations."""
        from fastmcp_pvl_core import server_config_env_suffixes

        assert server_config_env_suffixes() == {
            f.name.upper() for f in dataclasses.fields(ServerConfig)
        }
```

- [ ] **Step 2: Run it and confirm it passes for the wrong reason**

Run: `uv run pytest tests/test_config.py::TestServerConfigEnvSuffixes -v`

Expected: **PASS**, because the hand-maintained literal currently happens to agree with the fields. That is the point — this test pins the invariant *before* the refactor, so it will catch a mistake during it. Confirm the literal is still there and is what you are about to replace:

```bash
grep -n '"BEARER_DEFAULT_SUBJECT",' src/fastmcp_pvl_core/_config.py
```

Expected: one line inside the `_SERVER_CONFIG_ENV_SUFFIXES` literal.

- [ ] **Step 3: Replace the literal with a derived set**

Replace the whole `_SERVER_CONFIG_ENV_SUFFIXES` assignment — the comment block above it plus the 18-line `frozenset({...})` literal — with:

```python
# Derived from the ``ServerConfig`` field declarations: every field is read
# from ``{PREFIX}_{NAME.upper()}`` by ``from_env``, so the field list is the
# single source of truth. ``TestServerConfigEnvSuffixes`` checks this against
# an AST scan of ``from_env``, which catches a field that is never read and a
# read that has no field.
#
# Keep every ``from_env`` read in the ``env(prefix, "LITERAL")`` form: a suffix
# built from a variable or passed by keyword would not be seen by that scan.
_SERVER_CONFIG_ENV_SUFFIXES: frozenset[str] = frozenset(
    f.name.upper() for f in dataclasses.fields(ServerConfig)
)
```

- [ ] **Step 4: Run the suffix tests**

Run: `uv run pytest tests/test_config.py::TestServerConfigEnvSuffixes -v`

Expected: all pass, including the pre-existing `test_matches_what_from_env_actually_reads`, which now compares the AST scan of `from_env` against the field-derived set.

- [ ] **Step 5: Confirm the literal is gone**

```bash
grep -n '"OIDC_VERIFY_ACCESS_TOKEN",' src/fastmcp_pvl_core/_config.py
```

Expected: no output. The only remaining mention of these suffixes as string literals should be in `from_env`'s `env(prefix, "…")` calls and in tests.

- [ ] **Step 6: Run the full gate**

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_config.py tests/test_config.py
git commit -m "refactor(config): derive env-suffix set from field declarations

_SERVER_CONFIG_ENV_SUFFIXES was a hand-maintained 18-string literal that
had to be edited in lockstep with the fields. Suffix is mechanically
name.upper() for every field, so the literal is derivable and the
duplicate is removed.

The AST-scan guard gets stronger as a result: it now compares what
from_env actually reads against the field declarations, catching both a
field that is never read and a read with no backing field.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01As25PstUQeTR47J5SP5g7P"
```

---

## Wrap-up

- [ ] **File the issue this PR closes**

Every PR closes at least one issue. Run this first and note the number it prints:

```bash
gh issue create \
  --title "ServerConfig fields carry no metadata, so consumers hand-copy the env surface" \
  --body "$(cat <<'BODY'
## Summary

`ServerConfig`'s 18 fields expose only name, type, and default. Help text,
grouping, secret-ness, and which documentation section a variable belongs to
exist nowhere machine-readable, so every consumer hand-copies the env surface
into its own wizard spec, env examples, and docs tables — and those copies go
stale when this library adds a field.

`server_config_env_suffixes()` gives the *set* of variables but returns a
`frozenset`, whose iteration order varies between processes, so it cannot drive
generated output.

## Proposal

- Carry `help`, semantic `tags`, and wizard presentation hints in each field's
  `metadata=` mapping (stdlib `dataclasses`, no new dependency).
- Add `server_config_surface() -> tuple[ConfigField, ...]` returning the fields
  in declaration order, so generated output is byte-stable.
- Mark `auth_mode` as inferred — it is derived from which auth variables are
  set, so consumers should not have to maintain a local exception list for it.

Tags stay layout-agnostic: this library says what a field is *about*, never
which file documents it.

## Context

Prerequisite for Stage 0 of the config-generation design in
`fastmcp-server-template`.

— 🤖 _Automated post by Claude Code (agent) via the account owner's GitHub token; agent analysis/proposal, not a personal directive from the account owner._
BODY
)"
```

- [ ] **Open the PR**

Substitute the issue number from the previous step for `<ISSUE>` below. The body must end with the agent-attribution signature line.

```bash
gh pr create --base main \
  --title "feat(config): field metadata + server_config_surface() for generated config docs" \
  --body "$(cat <<'BODY'
Closes #<ISSUE>

Gives every `ServerConfig` env field self-describing metadata — help text,
semantic tags, wizard presentation hints — and exposes it as
`server_config_surface()`, returning the 18 fields in declaration order.

Consumers can now generate their config documentation (wizard spec, env
examples, docs tables) instead of hand-copying the env surface. Declaration
order is contractual: it gives byte-stable generated output, which the
`frozenset` from `server_config_env_suffixes()` cannot.

`auth_mode` is marked `inferred`, so consumers no longer need a local
exception list for it.

Also removes core's own copy of the duplication this enables removing
downstream: `_SERVER_CONFIG_ENV_SUFFIXES` is now derived from the field
declarations rather than hand-maintained.

Behaviour is unchanged — all defaults are immutable, so the conversion to
`field(default=..., metadata=...)` is behaviour-preserving, and the
pre-existing `ServerConfig` tests pass untouched.

Prerequisite for Stage 0 of the config-generation design in
`fastmcp-server-template`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01As25PstUQeTR47J5SP5g7P

— 🤖 _Automated post by Claude Code (agent) via the account owner's GitHub token; agent analysis/proposal, not a personal directive from the account owner._
BODY
)"
```

- [ ] **Do not merge and do not release.** Merging is human-only, and `v4.5.0` publication is a manual `workflow_dispatch` the maintainer runs. Report the PR URL and stop.

## Downstream note

Once v4.5.0 is published, `fastmcp-server-template` Stage 1 consumes `server_config_surface()` and raises its core floor to `>=4.5.0`. Template Stage 1 can be *written* before this releases but cannot merge until it does.
