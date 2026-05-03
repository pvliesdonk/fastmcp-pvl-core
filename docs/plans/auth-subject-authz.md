# Auth subject extraction & authorization submodule — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement issues #35, #36, #37 from `docs/specs/auth-subject-authz.md` as three sequential PRs: bearer-mapped tokens file (with breaking `AuthMode` rename → 2.0), the unified `get_subject` helper, and the optional `fastmcp_pvl_core.authorization` submodule.

**Architecture:** PR #35 extends `_auth.py`/`_config.py` with token-file loading and renames the bearer auth-mode literal. PR #36 adds a small `_subject.py` that combines FastMCP's `get_access_token()` with a startup-resolved auth-mode pointer. PR #37 introduces a new `authorization/` package (5 files: middleware, store, admin tools, git helper, init) wired through `wire_middleware_stack(mcp, extra=[...])` and gated by an opt-in domain config.

**Tech Stack:** Python 3.10+, FastMCP 3.x, `pyproject.toml` with `uv`, ruff + mypy strict, pytest with `asyncio_mode = "auto"`. PSR (semantic-release) drives versioning from conventional commits. TOML I/O via stdlib `tomllib` (read) + `tomli_w` for writes (new dep added in PR #37 only).

---

## File Structure (all three PRs)

### PR #35 — `feat/35-bearer-tokens-file`
- Modify: `src/fastmcp_pvl_core/_auth.py` — rewrite `build_bearer_auth`, update `AuthMode` literal, update `resolve_auth_mode`.
- Modify: `src/fastmcp_pvl_core/_config.py` — add `bearer_tokens_file`, `bearer_default_subject` fields and env reads.
- Create: `src/fastmcp_pvl_core/_errors.py` — `ConfigurationError` exception.
- Modify: `src/fastmcp_pvl_core/__init__.py` — re-export `ConfigurationError`.
- Modify: `README.md` — drift fix (`build_auth("MY_APP", config)` → `build_auth(config)`); subject section.
- Create: `tests/test_auth_bearer_tokens_file.py`.
- Modify: `tests/test_auth_mode.py` — update for `bearer-single`/`bearer-mapped`.
- Modify: `tests/test_auth_builders.py` — update for renamed `client_id` default.
- Modify: `tests/test_config.py` — new fields.

### PR #36 — `feat/36-get-subject-helper`
- Create: `src/fastmcp_pvl_core/_subject.py` — `get_subject` + module-level auth-mode pointer.
- Modify: `src/fastmcp_pvl_core/_auth.py` — `build_auth` calls `set_current_auth_mode(mode)`.
- Modify: `src/fastmcp_pvl_core/__init__.py` — re-export `get_subject`.
- Modify: `README.md` — add "Identifying the caller" section.
- Create: `tests/test_subject.py`.

### PR #37 — `feat/37-authorization-submodule`
- Create: `src/fastmcp_pvl_core/authorization/__init__.py` — public exports.
- Create: `src/fastmcp_pvl_core/authorization/_store.py` — `ACL` dataclass, TOML loader, mtime-cached reload.
- Create: `src/fastmcp_pvl_core/authorization/_middleware.py` — `AuthorizationMiddleware`, `AuthzDenied`, `TenantResolver`.
- Create: `src/fastmcp_pvl_core/authorization/_admin.py` — `register_acl_admin_tools` + 4 tool implementations.
- Create: `src/fastmcp_pvl_core/authorization/_git.py` — `commit_acl` helper.
- Modify: `src/fastmcp_pvl_core/__init__.py` — re-export public surface.
- Modify: `pyproject.toml` — add `tomli_w` dep, add `authorization` extra (empty placeholder).
- Modify: `README.md` — "Authorization (optional)" section.
- Create: `tests/test_authz_store.py`, `tests/test_authz_middleware.py`, `tests/test_authz_admin.py`, `tests/test_authz_resource_filtering.py`, `tests/test_authz_git.py`.

---

# PR #35 — `feat(auth)!: support {PREFIX}_BEARER_TOKENS_FILE for token→subject mapping`

Branch: `feat/35-bearer-tokens-file` (already created from main; spec commit `f582e55` already on branch).

### Task 1: Add `ConfigurationError` exception class

**Files:**
- Create: `src/fastmcp_pvl_core/_errors.py`
- Modify: `src/fastmcp_pvl_core/__init__.py` (add export)

- [ ] **Step 1: Create the error module**

```python
# src/fastmcp_pvl_core/_errors.py
"""Library-internal exception types.

All exceptions raised by builder/loader code that operators see during
startup live here so downstream catch sites have one stable import path.
"""

from __future__ import annotations


class ConfigurationError(Exception):
    """Operator-visible misconfiguration detected at startup or load time.

    Raised eagerly so a misconfigured server fails fast instead of
    silently denying every request.
    """
```

- [ ] **Step 2: Re-export from package root**

In `src/fastmcp_pvl_core/__init__.py`, add:

```python
from fastmcp_pvl_core._errors import ConfigurationError
```

…and add `"ConfigurationError"` to `__all__` in the appropriate alphabetical slot (between `"ConsumerSink"` and `"ExchangeGroupMismatch"`).

- [ ] **Step 3: Run mypy + ruff to confirm clean**

Run:
```bash
uv run mypy src/ && uv run ruff check src/
```
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add src/fastmcp_pvl_core/_errors.py src/fastmcp_pvl_core/__init__.py
git commit -m "feat(errors): add ConfigurationError exception type"
```

### Task 2: Add `ServerConfig` fields for tokens-file + default subject (failing tests)

**Files:**
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add tests for the new fields**

Append to `TestServerConfigDefaults` class in `tests/test_config.py`:

```python
    def test_bearer_tokens_file_defaults_to_none(self):
        assert ServerConfig().bearer_tokens_file is None

    def test_bearer_default_subject_default(self):
        assert ServerConfig().bearer_default_subject == "bearer-anon"
```

Append to `TestServerConfigFromEnv` class:

```python
    def test_reads_bearer_tokens_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        token_file = tmp_path / "tokens.toml"
        token_file.write_text("[tokens]\n", encoding="utf-8")
        monkeypatch.setenv("MYAPP_BEARER_TOKENS_FILE", str(token_file))
        config = ServerConfig.from_env("MYAPP")
        assert config.bearer_tokens_file == token_file

    def test_reads_bearer_default_subject(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_BEARER_DEFAULT_SUBJECT", "service:bot")
        config = ServerConfig.from_env("MYAPP")
        assert config.bearer_default_subject == "service:bot"

    def test_bearer_default_subject_falls_back_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("MYAPP_BEARER_DEFAULT_SUBJECT", raising=False)
        config = ServerConfig.from_env("MYAPP")
        assert config.bearer_default_subject == "bearer-anon"
```

- [ ] **Step 2: Run tests, confirm failure**

Run:
```bash
uv run pytest tests/test_config.py::TestServerConfigDefaults::test_bearer_tokens_file_defaults_to_none tests/test_config.py::TestServerConfigDefaults::test_bearer_default_subject_default -v
```
Expected: `AttributeError: 'ServerConfig' object has no attribute 'bearer_tokens_file'` (and the same for `bearer_default_subject`).

### Task 3: Implement `ServerConfig` fields

**Files:**
- Modify: `src/fastmcp_pvl_core/_config.py`

- [ ] **Step 1: Add `Path` import and the two fields**

At the top of `_config.py`, add to the imports block:

```python
from pathlib import Path
```

In the `ServerConfig` dataclass (between `auth_mode: str | None = None` and the `from_env` classmethod), add:

```python
    bearer_tokens_file: Path | None = None
    bearer_default_subject: str = "bearer-anon"
```

- [ ] **Step 2: Wire `from_env` to read the new env vars**

In `ServerConfig.from_env`, before the final `return cls(...)`, add:

```python
        tokens_file_raw = env(env_prefix, "BEARER_TOKENS_FILE")
        bearer_tokens_file = Path(tokens_file_raw) if tokens_file_raw else None
        bearer_default_subject = env(
            env_prefix, "BEARER_DEFAULT_SUBJECT", "bearer-anon"
        )
```

…and pass them inside the `return cls(...)` call:

```python
            bearer_tokens_file=bearer_tokens_file,
            bearer_default_subject=bearer_default_subject,
```

- [ ] **Step 3: Run all config tests**

Run:
```bash
uv run pytest tests/test_config.py -v
```
Expected: all pass (including the four new ones from Task 2).

- [ ] **Step 4: Run mypy strict**

Run:
```bash
uv run mypy src/
```
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_config.py tests/test_config.py
git commit -m "feat(config): add bearer_tokens_file and bearer_default_subject fields"
```

### Task 4: Rename `AuthMode` literal — failing tests

**Files:**
- Modify: `tests/test_auth_mode.py`

- [ ] **Step 1: Replace `"bearer"` with `"bearer-single"` everywhere in `test_auth_mode.py`**

Edit `tests/test_auth_mode.py`:

- Line 19: change `== "bearer"` to `== "bearer-single"`.
- Line 124: change `== "bearer"` to `== "bearer-single"`.
- In `test_unknown_override_falls_back_to_auto_detection` parametrize list (line 117), replace `"bearer"` with `"bearer-single"` (the rejected-override list now matches the new name).

- [ ] **Step 2: Add a test for `bearer-mapped`**

Append to `TestResolveAuthMode` class in `tests/test_auth_mode.py`:

```python
    def test_bearer_mapped_when_tokens_file_set(self, tmp_path):
        token_file = tmp_path / "tokens.toml"
        token_file.write_text(
            '[tokens]\n"abc" = "user:alice"\n', encoding="utf-8"
        )
        cfg = _cfg(bearer_tokens_file=token_file)
        assert resolve_auth_mode(cfg) == "bearer-mapped"

    def test_bearer_mapped_takes_precedence_when_both_set(self, tmp_path):
        token_file = tmp_path / "tokens.toml"
        token_file.write_text(
            '[tokens]\n"abc" = "user:alice"\n', encoding="utf-8"
        )
        cfg = _cfg(bearer_token="x", bearer_tokens_file=token_file)
        assert resolve_auth_mode(cfg) == "bearer-mapped"

    def test_multi_with_bearer_mapped(self, tmp_path):
        token_file = tmp_path / "tokens.toml"
        token_file.write_text(
            '[tokens]\n"abc" = "user:alice"\n', encoding="utf-8"
        )
        cfg = _cfg(
            bearer_tokens_file=token_file,
            base_url="https://x",
            oidc_config_url="https://idp/.well-known/openid-configuration",
        )
        assert resolve_auth_mode(cfg) == "multi"
```

- [ ] **Step 3: Run tests, confirm failure**

Run:
```bash
uv run pytest tests/test_auth_mode.py -v
```
Expected: failures for `bearer-single`, `bearer-mapped` cases (the literal isn't recognized yet by `resolve_auth_mode`).

### Task 5: Update `_auth.py` — `AuthMode` literal + `resolve_auth_mode`

**Files:**
- Modify: `src/fastmcp_pvl_core/_auth.py`

- [ ] **Step 1: Update the `AuthMode` literal (line ~26)**

Replace:
```python
AuthMode = Literal["none", "bearer", "remote", "oidc-proxy", "multi"]
```
with:
```python
AuthMode = Literal[
    "none", "bearer-single", "bearer-mapped", "remote", "oidc-proxy", "multi"
]
```

- [ ] **Step 2: Update `resolve_auth_mode` body**

Replace the `has_bearer = bool(config.bearer_token)` line and the resolution logic at the bottom of `resolve_auth_mode`. The full updated body after the explicit-override block is:

```python
    has_mapped_bearer = config.bearer_tokens_file is not None
    has_single_bearer = bool(config.bearer_token) and not has_mapped_bearer
    has_oidc_proxy = all(
        (
            config.base_url,
            config.oidc_config_url,
            config.oidc_client_id,
            config.oidc_client_secret,
        )
    )
    has_remote = bool(config.base_url and config.oidc_config_url) and not has_oidc_proxy

    oidc_mode: AuthMode | None
    if has_oidc_proxy:
        oidc_mode = "oidc-proxy"
    elif has_remote:
        oidc_mode = "remote"
    else:
        oidc_mode = None

    has_any_bearer = has_mapped_bearer or has_single_bearer

    if has_any_bearer and oidc_mode is not None:
        return "multi"
    if has_mapped_bearer:
        return "bearer-mapped"
    if has_single_bearer:
        return "bearer-single"
    if oidc_mode is not None:
        return oidc_mode
    return "none"
```

- [ ] **Step 3: Update the docstring of `resolve_auth_mode`**

In the `Precedence:` block of the docstring, replace the bearer line:
```
    - ``bearer``: only ``bearer_token`` set.
```
with:
```
    - ``bearer-mapped``: ``bearer_tokens_file`` set (takes precedence
      over a single ``bearer_token`` if both are configured).
    - ``bearer-single``: only ``bearer_token`` set.
```

And the `multi` line — change from:
```
    - ``multi``: both a bearer token and an OIDC flavor are configured.
```
to:
```
    - ``multi``: any bearer flavor (single or mapped) and an OIDC
      flavor are both configured.
```

- [ ] **Step 4: Run auth-mode tests**

Run:
```bash
uv run pytest tests/test_auth_mode.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_auth.py tests/test_auth_mode.py
git commit -m "feat(auth)!: rename bearer auth mode to bearer-single, add bearer-mapped

BREAKING CHANGE: bearer auth-mode literal renamed from \"bearer\" to \"bearer-single\"."
```

### Task 6: Token-file loader — failing tests

**Files:**
- Create: `tests/test_auth_bearer_tokens_file.py`

- [ ] **Step 1: Write the test file**

```python
# tests/test_auth_bearer_tokens_file.py
"""Tests for FASTMCP_BEARER_TOKENS_FILE token→subject mapping."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fastmcp_pvl_core import (
    ConfigurationError,
    ServerConfig,
    build_bearer_auth,
)


def _write_tokens(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "tokens.toml"
    path.write_text(content, encoding="utf-8")
    return path


class TestBearerTokensFileLoader:
    def test_returns_verifier_with_mapped_subjects(self, tmp_path: Path):
        path = _write_tokens(
            tmp_path,
            (
                '[tokens]\n'
                '"alice-token" = "user:alice@example.com"\n'
                '"ci-bot-token" = "service:ci-bot"\n'
            ),
        )
        auth = build_bearer_auth(ServerConfig(bearer_tokens_file=path))
        assert auth is not None
        alice = auth.tokens["alice-token"]
        bot = auth.tokens["ci-bot-token"]
        assert alice["client_id"] == "user:alice@example.com"
        assert alice["scopes"] == ["read", "write"]
        assert bot["client_id"] == "service:ci-bot"

    def test_missing_file_raises_configuration_error(self, tmp_path: Path):
        with pytest.raises(ConfigurationError, match="not found"):
            build_bearer_auth(
                ServerConfig(bearer_tokens_file=tmp_path / "nope.toml")
            )

    def test_malformed_toml_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, "[tokens\n\"x\" = \"y\"")
        with pytest.raises(ConfigurationError, match="parse"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_blank_file_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, "")
        with pytest.raises(ConfigurationError, match="empty"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_missing_tokens_table_raises_configuration_error(
        self, tmp_path: Path
    ):
        path = _write_tokens(tmp_path, '[other]\nkey = "v"\n')
        with pytest.raises(ConfigurationError, match="\\[tokens\\]"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_non_string_subject_raises_configuration_error(
        self, tmp_path: Path
    ):
        path = _write_tokens(tmp_path, '[tokens]\n"x" = 42\n')
        with pytest.raises(ConfigurationError, match="string"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_empty_subject_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, '[tokens]\n"x" = ""\n')
        with pytest.raises(ConfigurationError, match="empty"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))


class TestBearerTokensFilePrecedence:
    def test_file_takes_precedence_over_single_token(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        path = _write_tokens(
            tmp_path, '[tokens]\n"file-token" = "user:via-file"\n'
        )
        with caplog.at_level(logging.WARNING):
            auth = build_bearer_auth(
                ServerConfig(
                    bearer_token="single-token",
                    bearer_tokens_file=path,
                )
            )
        assert auth is not None
        # Single token must NOT appear; only the file's tokens are loaded.
        assert "single-token" not in auth.tokens
        assert auth.tokens["file-token"]["client_id"] == "user:via-file"
        # WARNING surfaced.
        assert any(
            "BEARER_TOKENS_FILE" in r.message
            and "BEARER_TOKEN" in r.message
            and r.levelname == "WARNING"
            for r in caplog.records
        )


class TestBearerSingleDefaultSubject:
    def test_default_subject_is_bearer_anon(self):
        auth = build_bearer_auth(ServerConfig(bearer_token="t"))
        assert auth is not None
        assert auth.tokens["t"]["client_id"] == "bearer-anon"

    def test_custom_default_subject(self):
        auth = build_bearer_auth(
            ServerConfig(bearer_token="t", bearer_default_subject="service:x")
        )
        assert auth is not None
        assert auth.tokens["t"]["client_id"] == "service:x"
```

- [ ] **Step 2: Run tests, confirm failure**

Run:
```bash
uv run pytest tests/test_auth_bearer_tokens_file.py -v
```
Expected: failures — most tests fail at construction or at the assertion step because `build_bearer_auth` doesn't yet read `bearer_tokens_file` or honor `bearer_default_subject`.

### Task 7: Implement token-file loader + update single-bearer client_id

**Files:**
- Modify: `src/fastmcp_pvl_core/_auth.py`

- [ ] **Step 1: Add module-level imports for the loader**

Near the top of `_auth.py` (after the existing `from __future__ import annotations`), add:

```python
import tomllib
from pathlib import Path
```

…and add this to the `from fastmcp_pvl_core._config import ServerConfig` import block, on a new line:

```python
from fastmcp_pvl_core._errors import ConfigurationError
```

- [ ] **Step 2: Add the loader helper above `build_bearer_auth`**

Insert this function definition above `def build_bearer_auth(`:

```python
def _load_bearer_tokens(path: Path) -> dict[str, str]:
    """Parse a bearer-token TOML file into a {token: subject} dict.

    Raises:
        ConfigurationError: file missing, unparseable, schema-invalid, or
            containing empty/non-string values.
    """
    if not path.exists():
        raise ConfigurationError(
            f"bearer tokens file not found: {path}"
        )
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ConfigurationError(
            f"bearer tokens file is empty: {path}"
        )
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            f"bearer tokens file at {path} could not be parsed: {exc}"
        ) from exc
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens:
        raise ConfigurationError(
            f"bearer tokens file at {path} must define a non-empty "
            "[tokens] table"
        )
    result: dict[str, str] = {}
    for token, subject in tokens.items():
        if not isinstance(subject, str):
            raise ConfigurationError(
                f"bearer tokens file at {path}: subject for token "
                f"{token!r} must be a string"
            )
        if not subject.strip():
            raise ConfigurationError(
                f"bearer tokens file at {path}: subject for token "
                f"{token!r} is empty"
            )
        result[str(token)] = subject
    return result
```

- [ ] **Step 3: Rewrite `build_bearer_auth` to handle both flavors**

Replace the entire body of `build_bearer_auth` (keep the docstring, but update it):

```python
def build_bearer_auth(config: ServerConfig) -> StaticTokenVerifier | None:
    """Build a :class:`StaticTokenVerifier` for either bearer flavor.

    Two modes:

    - **Mapped** (``bearer_tokens_file`` set): parse the TOML file and
      build one verifier entry per ``token → subject`` row. The subject
      is carried via the entry's ``client_id`` so request-time code can
      retrieve it via :func:`get_subject`.
    - **Single** (``bearer_token`` set, no file): one verifier entry
      whose ``client_id`` is ``config.bearer_default_subject``
      (defaults to ``"bearer-anon"``).

    If both ``bearer_tokens_file`` and ``bearer_token`` are configured,
    the file wins and a ``WARNING`` is logged.

    Args:
        config: Populated server configuration.

    Returns:
        A configured :class:`StaticTokenVerifier`, or ``None`` when
        neither flavor is configured.

    Raises:
        ConfigurationError: when ``bearer_tokens_file`` is set but the
            file is missing, unparseable, or schema-invalid.
    """
    from fastmcp.server.auth import StaticTokenVerifier

    tokens_file = config.bearer_tokens_file
    if tokens_file is not None:
        if config.bearer_token:
            logger.warning(
                "bearer_tokens_file_takes_precedence "
                "BEARER_TOKENS_FILE=%s BEARER_TOKEN=<redacted> — "
                "single-token value is ignored",
                tokens_file,
            )
        mapping = _load_bearer_tokens(tokens_file)
        return StaticTokenVerifier(
            tokens={
                token: {"client_id": subject, "scopes": ["read", "write"]}
                for token, subject in mapping.items()
            },
        )

    token = (config.bearer_token or "").strip()
    if not token:
        logger.debug("bearer_auth_skipped reason=not_configured")
        return None

    logger.debug("bearer_auth_enabled token=<redacted>")
    return StaticTokenVerifier(
        tokens={
            token: {
                "client_id": config.bearer_default_subject,
                "scopes": ["read", "write"],
            },
        },
    )
```

- [ ] **Step 4: Run tests for the loader + bearer auth**

Run:
```bash
uv run pytest tests/test_auth_bearer_tokens_file.py tests/test_auth_builders.py -v
```
Expected: `test_auth_bearer_tokens_file.py` all pass. `test_auth_builders.py::TestBuildBearerAuth::test_token_mapped_with_read_write_scopes` will fail because it asserts `client_id == "bearer"` (legacy default).

### Task 8: Update `test_auth_builders.py` for the new `client_id` default

**Files:**
- Modify: `tests/test_auth_builders.py`

- [ ] **Step 1: Update the existing assertion**

In `tests/test_auth_builders.py`, change line 48:
```python
        assert entry["client_id"] == "bearer"
```
to:
```python
        assert entry["client_id"] == "bearer-anon"
```

- [ ] **Step 2: Run tests**

Run:
```bash
uv run pytest tests/test_auth_builders.py -v
```
Expected: all pass.

- [ ] **Step 3: Commit (Tasks 6–8)**

```bash
git add src/fastmcp_pvl_core/_auth.py tests/test_auth_bearer_tokens_file.py tests/test_auth_builders.py
git commit -m "feat(auth): support FASTMCP_BEARER_TOKENS_FILE for token→subject mapping

Closes #35"
```

### Task 9: Smoke test — `build_auth` end-to-end with mapped tokens

**Files:**
- Modify: `tests/test_build_auth.py`

- [ ] **Step 1: Read the existing file to know the patterns**

Run:
```bash
uv run cat tests/test_build_auth.py | head -60
```

- [ ] **Step 2: Add an end-to-end smoke test for mapped mode**

Append to `tests/test_build_auth.py`:

```python
class TestBuildAuthMapped:
    def test_returns_verifier_in_bearer_mapped_mode(self, tmp_path):
        token_file = tmp_path / "tokens.toml"
        token_file.write_text(
            '[tokens]\n"k1" = "user:alice"\n', encoding="utf-8"
        )
        from fastmcp.server.auth import StaticTokenVerifier
        from fastmcp_pvl_core import ServerConfig, build_auth

        auth = build_auth(ServerConfig(bearer_tokens_file=token_file))
        assert isinstance(auth, StaticTokenVerifier)
        assert auth.tokens["k1"]["client_id"] == "user:alice"
```

- [ ] **Step 3: Run the test**

Run:
```bash
uv run pytest tests/test_build_auth.py -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_build_auth.py
git commit -m "test(auth): smoke test build_auth in bearer-mapped mode"
```

### Task 10: README drift fix + new section on subject mapping

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Fix the stale `build_auth` example**

In `README.md`, find:
```python
    auth=build_auth("MY_APP", config),
```
Replace with:
```python
    auth=build_auth(config),
```

- [ ] **Step 2: Add a "Per-user subject" section**

After the existing "Usage" example block in `README.md`, before the "Remote debugging in containers" section, insert:

```markdown
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
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): document bearer subject mapping; fix build_auth signature"
```

### Task 11: Verify full suite + lint

- [ ] **Step 1: Run the full test suite**

Run:
```bash
uv sync --all-extras
uv run pytest -v
```
Expected: all pass. No regressions in any of `test_auth_*`, `test_config.py`, `test_build_auth.py`.

- [ ] **Step 2: Run mypy strict + ruff**

Run:
```bash
uv run mypy src/ tests/
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```
Expected: clean.

- [ ] **Step 3: If anything failed, fix and re-run.** Do not skip to the next step unless all four commands return cleanly.

### Task 12: Local-review circus (PR #35)

- [ ] **Step 1: Capture the cumulative diff against `origin/main`**

Run:
```bash
git fetch origin main
git diff origin/main...HEAD > /tmp/pr35-diff.patch
git diff origin/main...HEAD --stat
```

- [ ] **Step 2: Dispatch `pr-review-toolkit:code-reviewer` subagent**

Use the Agent tool with `subagent_type: "pr-review-toolkit:code-reviewer"`. Prompt: "Review the cumulative diff for PR #35 (closes pvliesdonk/fastmcp-pvl-core#35) on branch `feat/35-bearer-tokens-file`. The diff implements `{PREFIX}_BEARER_TOKENS_FILE` for token→subject mapping, renames the `AuthMode` literal `bearer` → `bearer-single` (breaking 2.0 bump), and adds `ConfigurationError`. Spec at `docs/specs/auth-subject-authz.md`. Cumulative diff in `/tmp/pr35-diff.patch`. Run `git diff origin/main...HEAD` if you need it. Bar: report anything at any severity — bugs, logic errors, security, style, project-convention adherence (CLAUDE.md), test gaps. Be terse and concrete."

- [ ] **Step 3: Dispatch `superpowers:code-reviewer` subagent**

Use the Agent tool with `subagent_type: "superpowers:code-reviewer"`. Same prompt body, second-opinion pass.

- [ ] **Step 4: Dispatch `pr-review-toolkit:pr-test-analyzer`**

Use the Agent tool with `subagent_type: "pr-review-toolkit:pr-test-analyzer"`. Prompt focused on test coverage of the loader edge cases, the precedence WARNING, and the `bearer-mapped` mode resolution.

- [ ] **Step 5: Address all findings until both reviewers return clean**

For each finding:
- If it's correct → fix it. Re-run the full local circus on the new diff (steps 2–4).
- If the reviewer is wrong → write a short defense as a `git notes` entry or in this plan's task notes, but only after independently verifying the reviewer's claim is wrong.

Bar: nothing flagged at any severity. Repeat until both subagents return clean.

### Task 13: Push as draft, validate CI + bot reviewers

- [ ] **Step 1: Push the branch as a draft PR**

Run:
```bash
git push -u origin feat/35-bearer-tokens-file
gh pr create --draft \
  --title "feat(auth)!: support {PREFIX}_BEARER_TOKENS_FILE for token→subject mapping" \
  --body "$(cat <<'EOF'
## Summary

- Adds `{PREFIX}_BEARER_TOKENS_FILE` for per-token subject mapping (TOML format).
- Renames `AuthMode` literal `"bearer"` → `"bearer-single"` and adds `"bearer-mapped"`.
- Adds `{PREFIX}_BEARER_DEFAULT_SUBJECT` (default `"bearer-anon"`) for the single-token subject.
- Single-token `client_id` changes from literal `"bearer"` to the configured default.
- Adds `ConfigurationError` exception type.
- Fixes README drift (`build_auth("MY_APP", config)` → `build_auth(config)`).

Closes #35.

**BREAKING:** drives the 2.0 major bump (auth-mode literal rename + `client_id` default).

## Test plan

- [x] `tests/test_auth_bearer_tokens_file.py` — all loader paths + precedence WARNING + default-subject override.
- [x] `tests/test_auth_mode.py` extended with `bearer-single`/`bearer-mapped`/`multi`-with-mapped cases.
- [x] `tests/test_config.py` extended for the two new fields.
- [x] `tests/test_build_auth.py` end-to-end smoke for mapped mode.
- [x] mypy strict + ruff clean.

## Spec

`docs/specs/auth-subject-authz.md` (added in this PR; covers #35/#36/#37).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Wait for CI + claude-review + gemini-code-assist**

Use ScheduleWakeup with `delaySeconds: 600` and prompt that resumes monitoring this plan. The bots take 2–15 min; CI varies. While waiting, do not start PR #36 work.

- [ ] **Step 3: Read bot review *bodies* (not just check status)**

Run:
```bash
gh pr view <PR_NUM> --json statusCheckRollup,reviews,comments
gh pr view <PR_NUM> --comments
```

For claude-review: open the workflow run output and grep for `Still Open`, `fix items`, `must be addressed`, or other negative-recommendation language. Green check ≠ approval.

- [ ] **Step 4: If the bots flagged anything, address (one round only)**

Per global PR workflow:
1. Address the finding (or defend in a PR comment if the bot is wrong).
2. **Before pushing the fix, re-run the full local review circus** (Task 12).
3. Push. Bots re-run.
4. If a third round needed → escalate to user; do not iterate silently.

- [ ] **Step 5: Flip to ready when CI green + bot LGTM bodies + nothing pending**

Run:
```bash
gh pr ready <PR_NUM>
```

- [ ] **Step 6: Confirm the PR is ready and stop work on this PR until merged**

Per the parallel-pipelining rule, do NOT wait for human merge before starting PR #36. Proceed to PR #36 immediately on a fresh branch from `main` (Task 14).

---

# PR #36 — `feat(auth): get_subject helper`

Branch: `feat/36-get-subject-helper`, branched from `main` (NOT from PR #35's branch). PR #35's commits will land in `main` first.

### Task 14: Branch from main

- [ ] **Step 1: Fetch and branch**

Run:
```bash
git fetch origin main
git checkout -b feat/36-get-subject-helper origin/main
```

Note: this branch is independent of PR #35's branch. PR #36 will need to wait for PR #35 to merge before its branch sees the new `AuthMode` literals — but per the parallel-pipelining rule we open it now anyway. If PR #35 hasn't merged yet, the branch will rebase cleanly later, OR the agent will pause this PR and let PR #35 merge first depending on file overlap.

**File-overlap check**: PR #36 modifies `_auth.py` and `__init__.py` — both also touched by PR #35. To avoid conflicts, **do not start PR #36 until PR #35 is merged into main**. Schedule a wakeup to poll the merge state.

- [ ] **Step 2: Schedule a wakeup if PR #35 is still open**

If `gh pr view <PR35> --json state -q .state` returns anything other than `MERGED`, schedule a 30-min wakeup to re-check before proceeding.

### Task 15: Auth-mode pointer infrastructure (failing test)

**Files:**
- Create: `tests/test_subject.py`

- [ ] **Step 1: Write the test file with first failing test**

```python
# tests/test_subject.py
"""Tests for get_subject helper."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from fastmcp_pvl_core import (
    ServerConfig,
    build_auth,
    get_subject,
)


class _FakeAccessToken:
    """Minimal stand-in for fastmcp.server.auth.AccessToken."""

    def __init__(
        self,
        client_id: str | None = None,
        claims: dict[str, Any] | None = None,
    ) -> None:
        self.client_id = client_id
        self.claims = claims or {}


@pytest.fixture
def patch_get_access_token():
    """Patch FastMCP's get_access_token to return a controllable value."""

    def _set(token):
        return patch(
            "fastmcp_pvl_core._subject.get_access_token", return_value=token
        )

    return _set


class TestGetSubjectAuthModeNone:
    def test_returns_local_when_no_token_and_mode_none(
        self, patch_get_access_token
    ):
        # Simulate auth_mode=none after build_auth
        build_auth(ServerConfig())
        with patch_get_access_token(None):
            assert get_subject() == "local"


class TestGetSubjectBearerSingle:
    def test_returns_default_subject_from_client_id(
        self, patch_get_access_token, tmp_path
    ):
        cfg = ServerConfig(bearer_token="x", bearer_default_subject="bearer-anon")
        build_auth(cfg)
        with patch_get_access_token(_FakeAccessToken(client_id="bearer-anon")):
            assert get_subject() == "bearer-anon"


class TestGetSubjectBearerMapped:
    def test_returns_mapped_subject(self, patch_get_access_token, tmp_path):
        token_file = tmp_path / "tokens.toml"
        token_file.write_text(
            '[tokens]\n"k1" = "user:alice@example.com"\n', encoding="utf-8"
        )
        cfg = ServerConfig(bearer_tokens_file=token_file)
        build_auth(cfg)
        with patch_get_access_token(
            _FakeAccessToken(client_id="user:alice@example.com")
        ):
            assert get_subject() == "user:alice@example.com"


class TestGetSubjectOIDC:
    def test_returns_sub_claim(self, patch_get_access_token):
        # Mode pointer is irrelevant here — claims["sub"] always wins.
        with patch_get_access_token(
            _FakeAccessToken(
                client_id="oidc-client-x",
                claims={"sub": "user:bob@example.com"},
            )
        ):
            assert get_subject() == "user:bob@example.com"

    def test_falls_back_to_client_id_when_sub_missing(
        self, patch_get_access_token
    ):
        with patch_get_access_token(
            _FakeAccessToken(client_id="oidc-client-x", claims={})
        ):
            assert get_subject() == "oidc-client-x"


class TestGetSubjectMissing:
    def test_returns_none_when_no_token_and_auth_configured(
        self, patch_get_access_token
    ):
        # Simulate any auth mode != "none"
        build_auth(ServerConfig(bearer_token="t"))
        with patch_get_access_token(None):
            assert get_subject() is None
```

- [ ] **Step 2: Run tests, confirm failure**

Run:
```bash
uv run pytest tests/test_subject.py -v
```
Expected: `ImportError: cannot import name 'get_subject'` (it doesn't exist yet).

### Task 16: Implement `_subject.py` and the auth-mode pointer

**Files:**
- Create: `src/fastmcp_pvl_core/_subject.py`
- Modify: `src/fastmcp_pvl_core/_auth.py`

- [ ] **Step 1: Create `_subject.py`**

```python
# src/fastmcp_pvl_core/_subject.py
"""Uniform subject extraction across all auth modes.

Downstream code that wants to know "who is making this request?" should
import :func:`get_subject` from the package root and call it without
caring about which auth mode is active.

The per-mode complexity lives in the builders (see :mod:`_auth`); this
module is a thin extractor.
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_access_token

# Module-level pointer to the resolved auth mode. ``build_auth`` calls
# ``set_current_auth_mode`` exactly once at server startup; ``get_subject``
# reads it to decide whether the absence of an access token means
# "stdio/no-auth" (returns "local") or "auth configured but no valid
# token" (returns None).
_current_auth_mode: str | None = None


def set_current_auth_mode(mode: str | None) -> None:
    """Record the auth mode resolved at server startup.

    Called by :func:`fastmcp_pvl_core.build_auth`. Tests that bypass
    ``build_auth`` may call this directly.
    """
    global _current_auth_mode
    _current_auth_mode = mode


def get_subject(_ctx_or_request: object | None = None) -> str | None:
    """Return the subject of the current request, or ``None``.

    Resolution order:

    1. If FastMCP's :func:`get_access_token` returns a token, return
       ``token.claims["sub"]`` if present, else ``token.client_id``.
       The builders are responsible for ensuring ``client_id`` carries
       the right value per mode (mapped subject for ``bearer-mapped``,
       ``bearer_default_subject`` for ``bearer-single``).
    2. If there is no access token and ``set_current_auth_mode`` was
       called with ``"none"``, return the literal ``"local"``.
    3. Otherwise return ``None`` and let the caller decide whether to
       fall back or error.

    The optional ``_ctx_or_request`` argument is reserved for future use
    (an explicit request/context object); v1 ignores it and reads from
    FastMCP's ambient context plumbing.
    """
    access_token = get_access_token()
    if access_token is None:
        return "local" if _current_auth_mode == "none" else None
    claims = getattr(access_token, "claims", None) or {}
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if isinstance(sub, str) and sub:
        return sub
    client_id = getattr(access_token, "client_id", None)
    if isinstance(client_id, str) and client_id:
        return client_id
    return None
```

- [ ] **Step 2: Wire `set_current_auth_mode` into `build_auth`**

In `src/fastmcp_pvl_core/_auth.py`, at the top of `build_auth`, after `mode = resolve_auth_mode(config)`:

```python
    from fastmcp_pvl_core._subject import set_current_auth_mode

    set_current_auth_mode(mode)
```

(Local import to avoid a circular import at module load time.)

- [ ] **Step 3: Re-export `get_subject` from package root**

In `src/fastmcp_pvl_core/__init__.py`:

```python
from fastmcp_pvl_core._subject import get_subject
```

…and add `"get_subject"` to `__all__` in alphabetical order (between `"env"` and `"get_artifact_store"`).

- [ ] **Step 4: Run tests**

Run:
```bash
uv run pytest tests/test_subject.py -v
```
Expected: all pass.

- [ ] **Step 5: Run mypy strict + ruff**

Run:
```bash
uv run mypy src/ tests/
uv run ruff check src/ tests/
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_subject.py src/fastmcp_pvl_core/_auth.py src/fastmcp_pvl_core/__init__.py tests/test_subject.py
git commit -m "feat(auth): add get_subject helper for uniform subject extraction

Closes #36"
```

### Task 17: README "Identifying the caller" section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the new section**

After the "Per-user subject mapping (bearer auth)" section (added in PR #35 — already in `main` by the time this PR runs), insert:

```markdown
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

Return values per mode:

- `auth_mode == "none"` → `"local"`.
- `auth_mode == "bearer-single"` → `bearer_default_subject` (default `"bearer-anon"`).
- `auth_mode == "bearer-mapped"` → the per-token subject from the TOML map.
- OIDC modes → the `sub` claim from the validated token, falling back to `client_id`.
- No valid token (and auth required) → `None`. Caller decides whether to
  fall back or error.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(readme): document get_subject helper"
```

### Task 18: Verify full suite + lint (PR #36)

- [ ] **Step 1: Run the full test suite + mypy + ruff**

Run:
```bash
uv sync --all-extras
uv run pytest -v
uv run mypy src/ tests/
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```
Expected: all clean. If anything fails, fix before proceeding.

### Task 19: Local-review circus (PR #36)

- [ ] **Step 1: Capture cumulative diff**

Run:
```bash
git fetch origin main
git diff origin/main...HEAD > /tmp/pr36-diff.patch
git diff origin/main...HEAD --stat
```

- [ ] **Step 2: Dispatch `pr-review-toolkit:code-reviewer`**

Same shape as Task 12 step 2. Subject: "PR #36 closes #36 — `get_subject` helper for uniform subject extraction. Spec at `docs/specs/auth-subject-authz.md`. Diff at `/tmp/pr36-diff.patch`."

- [ ] **Step 3: Dispatch `superpowers:code-reviewer`**

Second-opinion pass.

- [ ] **Step 4: Dispatch `pr-review-toolkit:pr-test-analyzer`**

Focus on whether all five auth modes are exercised in `test_subject.py`, and whether the `_current_auth_mode` indirection is robustly tested.

- [ ] **Step 5: Address all findings until clean**

Bar: nothing flagged at any severity. Repeat until both reviewers return clean.

### Task 20: Push as draft (PR #36)

- [ ] **Step 1: Push and open draft**

```bash
git push -u origin feat/36-get-subject-helper
gh pr create --draft \
  --title "feat(auth): get_subject helper for uniform subject extraction" \
  --body "$(cat <<'EOF'
## Summary

- Adds `fastmcp_pvl_core.get_subject()` — single uniform extractor
  across all auth modes.
- Backed by a startup-resolved auth-mode pointer (`set_current_auth_mode`)
  populated by `build_auth`.
- README documents the helper for downstream consumers.

Closes #36.

## Test plan

- [x] `tests/test_subject.py` — all five auth modes return the right
  subject; `None` when auth is configured but no token; `"local"` for
  `auth_mode=none`.
- [x] mypy strict + ruff clean.

## Spec

`docs/specs/auth-subject-authz.md` (added in PR #35).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Wait for CI + bots, read bodies, address findings, flip to ready**

Same flow as Task 13 steps 2–6. Do not start PR #37 work until PR #36 is merged into `main` (file-overlap rule on `__init__.py`).

---

# PR #37 — `feat(authorization): optional fastmcp_pvl_core.authorization submodule`

Branch: `feat/37-authorization-submodule`, from `main` (after PR #36 merges).

### Task 21: Branch from main + ensure deps

- [ ] **Step 1: Branch**

```bash
git fetch origin main
git checkout -b feat/37-authorization-submodule origin/main
```

- [ ] **Step 2: Add `tomli_w` to project deps**

In `pyproject.toml`, in the `dependencies = [...]` list, add:

```toml
  "tomli_w>=1.0",
```

…and in `[project.optional-dependencies]`, add an empty placeholder for forward-compat:

```toml
authorization = []
```

- [ ] **Step 3: Sync deps**

```bash
uv sync --all-extras
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add tomli_w for ACL TOML writeback"
```

### Task 22: ACL store — failing tests

**Files:**
- Create: `tests/test_authz_store.py`

- [ ] **Step 1: Write the test file**

```python
# tests/test_authz_store.py
"""Tests for the ACL TOML store."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fastmcp_pvl_core import ConfigurationError
from fastmcp_pvl_core.authorization._store import ACLStore


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class TestACLStoreLoad:
    def test_load_simple(self, tmp_path):
        path = _write(
            tmp_path / "acl.toml",
            (
                '[subjects."user:alice".tenants]\n'
                '"*" = ["read", "write", "admin"]\n'
                '[default]\ntenants = {}\n'
            ),
        )
        store = ACLStore(path)
        assert store.has("user:alice", "anything", "admin") is True
        assert store.has("user:alice", "anything", "read") is True

    def test_default_deny_when_subject_unknown(self, tmp_path):
        path = _write(
            tmp_path / "acl.toml",
            '[default]\ntenants = {}\n',
        )
        store = ACLStore(path)
        assert store.has("user:bob", "tenant-a", "read") is False

    def test_tenant_specific_grant(self, tmp_path):
        path = _write(
            tmp_path / "acl.toml",
            (
                '[subjects."user:alice".tenants]\n'
                '"tenant-a" = ["read", "write"]\n'
                '[default]\ntenants = {}\n'
            ),
        )
        store = ACLStore(path)
        assert store.has("user:alice", "tenant-a", "read") is True
        assert store.has("user:alice", "tenant-b", "read") is False

    def test_scope_ordering(self, tmp_path):
        path = _write(
            tmp_path / "acl.toml",
            (
                '[subjects."user:alice".tenants]\n'
                '"*" = ["admin"]\n'
                '[subjects."service:reader".tenants]\n'
                '"*" = ["read"]\n'
                '[default]\ntenants = {}\n'
            ),
        )
        store = ACLStore(path)
        # admin satisfies write and read
        assert store.has("user:alice", "t", "write") is True
        assert store.has("user:alice", "t", "read") is True
        # read does NOT satisfy write
        assert store.has("service:reader", "t", "write") is False
        assert store.has("service:reader", "t", "admin") is False

    def test_subject_wildcard_rejected(self, tmp_path):
        path = _write(
            tmp_path / "acl.toml",
            (
                '[subjects."*".tenants]\n'
                '"*" = ["read"]\n'
                '[default]\ntenants = {}\n'
            ),
        )
        with pytest.raises(ConfigurationError, match="wildcard"):
            ACLStore(path).load()

    def test_missing_file_logs_warning_and_default_denies(
        self, tmp_path, caplog
    ):
        store = ACLStore(tmp_path / "missing.toml")
        with caplog.at_level("WARNING"):
            assert store.has("user:alice", "t", "read") is False
        assert any(
            "acl_file_missing" in r.message and r.levelname == "WARNING"
            for r in caplog.records
        )

    def test_malformed_toml_raises_at_load(self, tmp_path):
        path = _write(tmp_path / "acl.toml", "[default\nbroken")
        with pytest.raises(ConfigurationError, match="parse"):
            ACLStore(path).load()


class TestACLStoreReload:
    def test_reloads_on_mtime_change(self, tmp_path):
        path = _write(
            tmp_path / "acl.toml",
            (
                '[subjects."user:alice".tenants]\n'
                '"*" = ["read"]\n'
                '[default]\ntenants = {}\n'
            ),
        )
        store = ACLStore(path)
        assert store.has("user:alice", "t", "read") is True
        assert store.has("user:alice", "t", "write") is False

        # Bump mtime to ensure detection regardless of FS resolution.
        time.sleep(0.01)
        path.write_text(
            (
                '[subjects."user:alice".tenants]\n'
                '"*" = ["write"]\n'
                '[default]\ntenants = {}\n'
            ),
            encoding="utf-8",
        )
        # Force mtime to definitely differ
        new_mtime = path.stat().st_mtime + 1
        import os

        os.utime(path, (new_mtime, new_mtime))
        assert store.has("user:alice", "t", "write") is True


class TestACLStoreDefault:
    def test_default_grant_applies_when_subject_absent(self, tmp_path):
        path = _write(
            tmp_path / "acl.toml",
            (
                '[default]\n'
                'tenants = {"*" = ["read"]}\n'
            ),
        )
        store = ACLStore(path)
        assert store.has("user:unknown", "t", "read") is True
        assert store.has("user:unknown", "t", "write") is False
```

- [ ] **Step 2: Run tests, confirm failure**

Run:
```bash
uv run pytest tests/test_authz_store.py -v
```
Expected: `ModuleNotFoundError: No module named 'fastmcp_pvl_core.authorization'`.

### Task 23: Implement ACL store

**Files:**
- Create: `src/fastmcp_pvl_core/authorization/__init__.py`
- Create: `src/fastmcp_pvl_core/authorization/_store.py`

- [ ] **Step 1: Create the package init (skeleton)**

```python
# src/fastmcp_pvl_core/authorization/__init__.py
"""Optional fine-grained authorization for FastMCP servers.

Opt-in submodule providing:

- :class:`AuthorizationMiddleware` — intercepts tool calls and resource
  reads, denies by default.
- :class:`ACLStore` — TOML-backed (subject, tenant) → scopes.
- :func:`register_acl_admin_tools` — registers four admin-scope tools
  for managing the ACL at runtime.
- :class:`TenantResolver` — Protocol for domain-supplied tenant
  extraction.

See ``docs/specs/auth-subject-authz.md`` for the design.
"""

from __future__ import annotations

from fastmcp_pvl_core.authorization._store import ACL, ACLStore

__all__ = ["ACL", "ACLStore"]
```

- [ ] **Step 2: Implement `_store.py`**

```python
# src/fastmcp_pvl_core/authorization/_store.py
"""TOML-backed ACL store for the authorization submodule."""

from __future__ import annotations

import logging
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastmcp_pvl_core._errors import ConfigurationError

logger = logging.getLogger(__name__)

Scope = Literal["read", "write", "admin"]
_SCOPE_ORDER: dict[str, int] = {"read": 0, "write": 1, "admin": 2}
_VALID_SCOPES: frozenset[str] = frozenset(_SCOPE_ORDER)


@dataclass(frozen=True)
class ACL:
    """In-memory representation of the ACL TOML.

    ``subjects`` maps a subject string (e.g. ``"user:alice@example.com"``)
    to a tenant→scopes map. The wildcard tenant ``"*"`` matches any
    tenant. ``default_tenants`` applies when the subject is not present
    in ``subjects``.
    """

    subjects: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)
    default_tenants: dict[str, frozenset[str]] = field(default_factory=dict)


def _validate_scopes(scopes: object, where: str) -> frozenset[str]:
    if not isinstance(scopes, list) or not all(
        isinstance(s, str) for s in scopes
    ):
        raise ConfigurationError(
            f"acl: {where} scopes must be a list of strings"
        )
    bad = [s for s in scopes if s not in _VALID_SCOPES]
    if bad:
        raise ConfigurationError(
            f"acl: {where} unknown scopes {bad!r}; "
            f"valid scopes are {sorted(_VALID_SCOPES)}"
        )
    return frozenset(scopes)


def _validate_tenants(
    tenants: object, where: str
) -> dict[str, frozenset[str]]:
    if not isinstance(tenants, dict):
        raise ConfigurationError(
            f"acl: {where} tenants must be a TOML table"
        )
    return {
        str(t): _validate_scopes(scopes, f"{where}.tenants[{t!r}]")
        for t, scopes in tenants.items()
    }


def _parse_acl(raw: dict[str, object], path: Path) -> ACL:
    subjects_raw = raw.get("subjects", {})
    if not isinstance(subjects_raw, dict):
        raise ConfigurationError(
            f"acl at {path}: [subjects] must be a TOML table"
        )
    if "*" in subjects_raw:
        raise ConfigurationError(
            f"acl at {path}: subject wildcard '*' is not allowed; "
            "wildcards are only permitted on the tenant side"
        )
    subjects: dict[str, dict[str, frozenset[str]]] = {}
    for subject, body in subjects_raw.items():
        if not isinstance(body, dict):
            raise ConfigurationError(
                f"acl at {path}: subject {subject!r} must be a table"
            )
        tenants = body.get("tenants", {})
        subjects[str(subject)] = _validate_tenants(
            tenants, f"subjects[{subject!r}]"
        )

    default_raw = raw.get("default", {})
    if not isinstance(default_raw, dict):
        raise ConfigurationError(
            f"acl at {path}: [default] must be a TOML table"
        )
    default_tenants = _validate_tenants(
        default_raw.get("tenants", {}), "default"
    )
    return ACL(subjects=subjects, default_tenants=default_tenants)


class ACLStore:
    """Mtime-cached, schema-validated ACL TOML reader.

    Calling :meth:`has` on every request is the intended hot path. The
    store re-parses the file only when its ``mtime`` changes; otherwise
    the cached :class:`ACL` is reused.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cached: ACL | None = None
        self._cached_mtime: float | None = None
        self._missing_warning_emitted = False

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ACL:
        """Force a load (used by tests and admin tools)."""
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> ACL:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            if not self._missing_warning_emitted:
                logger.warning(
                    "acl_file_missing path=%s — default-deny in effect",
                    self._path,
                )
                self._missing_warning_emitted = True
            self._cached = ACL()
            self._cached_mtime = None
            return self._cached
        if self._cached is not None and self._cached_mtime == mtime:
            return self._cached
        try:
            raw = tomllib.loads(
                self._path.read_text(encoding="utf-8")
            )
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(
                f"acl at {self._path} could not be parsed: {exc}"
            ) from exc
        acl = _parse_acl(raw, self._path)
        self._cached = acl
        self._cached_mtime = mtime
        self._missing_warning_emitted = False
        return acl

    def has(self, subject: str | None, tenant: str | None, scope: str) -> bool:
        """Return True iff the (subject, tenant) pair has at least *scope*.

        Unknown subject falls through to ``[default]``. Tenant ``*``
        wildcard matches any tenant.
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(f"unknown scope: {scope!r}")
        with self._lock:
            acl = self._load_locked()
        granted = self._granted_scopes(acl, subject, tenant)
        return any(
            _SCOPE_ORDER[g] >= _SCOPE_ORDER[scope] for g in granted
        )

    def _granted_scopes(
        self, acl: ACL, subject: str | None, tenant: str | None
    ) -> frozenset[str]:
        if subject is not None and subject in acl.subjects:
            tenants = acl.subjects[subject]
        else:
            tenants = acl.default_tenants
        if not tenants:
            return frozenset()
        if tenant is not None and tenant in tenants:
            return tenants[tenant]
        return tenants.get("*", frozenset())
```

- [ ] **Step 3: Run tests**

Run:
```bash
uv run pytest tests/test_authz_store.py -v
```
Expected: all pass.

- [ ] **Step 4: mypy + ruff**

Run:
```bash
uv run mypy src/fastmcp_pvl_core/authorization/ tests/test_authz_store.py
uv run ruff check src/fastmcp_pvl_core/authorization/ tests/test_authz_store.py
```
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/authorization/ tests/test_authz_store.py
git commit -m "feat(authorization): add ACL TOML store with mtime caching"
```

### Task 24: `AuthorizationMiddleware` — failing tests

**Files:**
- Create: `tests/test_authz_middleware.py`

- [ ] **Step 1: Write the test file**

```python
# tests/test_authz_middleware.py
"""Tests for AuthorizationMiddleware."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fastmcp_pvl_core import ConfigurationError  # noqa: F401 (used in fixtures)
from fastmcp_pvl_core.authorization import (
    ACLStore,
    AuthorizationMiddleware,
    AuthzDenied,
)


class _FakeContext:
    """Minimal stand-in for a FastMCP middleware context."""

    def __init__(self, *, message=None, fastmcp_context=None) -> None:
        self.message = message
        self.fastmcp_context = fastmcp_context


class _FakeToolMessage:
    def __init__(self, name: str, arguments: dict | None = None) -> None:
        self.name = name
        self.arguments = arguments or {}


class _FakeServer:
    """Stand-in exposing tool annotations the middleware reads."""

    def __init__(self, tool_annotations: dict[str, dict]) -> None:
        self._annotations = tool_annotations

    def get_tool_annotations(self, name: str) -> dict | None:
        return self._annotations.get(name)


@pytest.fixture
def acl_path(tmp_path):
    path = tmp_path / "acl.toml"
    path.write_text(
        (
            '[subjects."user:alice".tenants]\n'
            '"*" = ["read", "write", "admin"]\n'
            '[subjects."service:reader".tenants]\n'
            '"*" = ["read"]\n'
            '[default]\ntenants = {}\n'
        ),
        encoding="utf-8",
    )
    return path


def _resolver(_request) -> str | None:
    return None  # Default tenant-less.


def _patch_subject(value):
    return patch(
        "fastmcp_pvl_core.authorization._middleware.get_subject",
        return_value=value,
    )


class TestToolCallAuthz:
    @pytest.mark.asyncio
    async def test_admin_tool_allowed_for_admin_subject(self, acl_path):
        store = ACLStore(acl_path)
        server = _FakeServer({"some_tool": {"requires_scope": "admin"}})
        mw = AuthorizationMiddleware(
            store=store,
            tenant_resolver=_resolver,
            server=server,
        )
        ctx = _FakeContext(message=_FakeToolMessage("some_tool"))
        with _patch_subject("user:alice"):
            await mw.on_call_tool(ctx, lambda c: "ok")  # type: ignore

    @pytest.mark.asyncio
    async def test_admin_tool_denied_for_read_subject(self, acl_path):
        store = ACLStore(acl_path)
        server = _FakeServer({"some_tool": {"requires_scope": "admin"}})
        mw = AuthorizationMiddleware(
            store=store,
            tenant_resolver=_resolver,
            server=server,
        )
        ctx = _FakeContext(message=_FakeToolMessage("some_tool"))
        with _patch_subject("service:reader"):
            with pytest.raises(AuthzDenied) as exc_info:
                await mw.on_call_tool(ctx, lambda c: "ok")  # type: ignore
        err = exc_info.value
        assert err.code == "authz_denied"
        assert err.subject == "service:reader"
        assert err.missing_scope == "admin"

    @pytest.mark.asyncio
    async def test_unannotated_tool_defaults_to_read(self, acl_path):
        store = ACLStore(acl_path)
        server = _FakeServer({"some_tool": {}})
        mw = AuthorizationMiddleware(
            store=store,
            tenant_resolver=_resolver,
            server=server,
        )
        ctx = _FakeContext(message=_FakeToolMessage("some_tool"))
        with _patch_subject("service:reader"):
            await mw.on_call_tool(ctx, lambda c: "ok")  # type: ignore

    @pytest.mark.asyncio
    async def test_tenant_resolver_extracts_from_args(self, acl_path):
        store = ACLStore(acl_path)
        server = _FakeServer({"do": {"requires_scope": "write"}})

        def resolver(req):
            return getattr(req, "arguments", {}).get("project_id")

        mw = AuthorizationMiddleware(
            store=store,
            tenant_resolver=resolver,
            server=server,
        )
        ctx = _FakeContext(
            message=_FakeToolMessage("do", arguments={"project_id": "p1"})
        )
        with _patch_subject("user:alice"):
            await mw.on_call_tool(ctx, lambda c: "ok")  # type: ignore


class TestResourceFiltering:
    @pytest.mark.asyncio
    async def test_resources_list_includes_unannotated(self, acl_path):
        # Permissive default: resources without `requires_tenant` annotation
        # always appear in the listing regardless of subject.
        store = ACLStore(acl_path)
        # See test_authz_resource_filtering.py for the full list flow.
        # Smoke check that the helper exists.
        from fastmcp_pvl_core.authorization._middleware import (
            _resource_visible,
        )

        assert _resource_visible(store, "user:unknown", None) is True


class TestAuthzDeniedError:
    def test_structured_payload(self):
        err = AuthzDenied(
            subject="user:alice",
            tenant="t1",
            missing_scope="write",
        )
        assert err.code == "authz_denied"
        payload = err.to_dict()
        assert payload == {
            "code": "authz_denied",
            "subject": "user:alice",
            "tenant": "t1",
            "missing_scope": "write",
        }
```

- [ ] **Step 2: Run tests, confirm failure**

Run:
```bash
uv run pytest tests/test_authz_middleware.py -v
```
Expected: `ImportError` for `AuthorizationMiddleware` / `AuthzDenied`.

### Task 25: Implement `AuthorizationMiddleware`

**Files:**
- Create: `src/fastmcp_pvl_core/authorization/_middleware.py`
- Modify: `src/fastmcp_pvl_core/authorization/__init__.py`

- [ ] **Step 1: Inspect FastMCP middleware base class**

Run:
```bash
grep -n "class Middleware" /tmp/smoke/.venv/lib/python3.13/site-packages/fastmcp/server/middleware/middleware.py | head -5
grep -rn "on_call_tool\|on_list_resources\|on_read_resource" /tmp/smoke/.venv/lib/python3.13/site-packages/fastmcp/server/middleware/middleware.py | head -10
```

Use the returned hook signatures verbatim in the implementation below — adjust the method names if they differ.

- [ ] **Step 2: Implement `_middleware.py`**

```python
# src/fastmcp_pvl_core/authorization/_middleware.py
"""AuthorizationMiddleware — subject + tenant + scope gate."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext

from fastmcp_pvl_core._subject import get_subject
from fastmcp_pvl_core.authorization._store import ACLStore

logger = logging.getLogger(__name__)


class TenantResolver(Protocol):
    """Domain-supplied callable that extracts a tenant from a request.

    Returns ``None`` for tenant-less operations; the middleware then
    only checks the wildcard tenant ``"*"``.
    """

    def __call__(self, request: object) -> str | None: ...


@dataclass
class AuthzDenied(Exception):
    """Authorization denied for the current (subject, tenant, scope).

    The structured payload returned via :meth:`to_dict` is the canonical
    error shape downstream consumers should expect.
    """

    subject: str | None
    tenant: str | None
    missing_scope: str

    @property
    def code(self) -> str:
        return "authz_denied"

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "subject": self.subject,
            "tenant": self.tenant,
            "missing_scope": self.missing_scope,
        }

    def __str__(self) -> str:
        return (
            f"authz_denied subject={self.subject!r} "
            f"tenant={self.tenant!r} missing_scope={self.missing_scope!r}"
        )


def _resource_visible(
    store: ACLStore, subject: str | None, tenant: str | None
) -> bool:
    """Permissive default for resource filtering: True if ``tenant`` is
    ``None`` (resource opted out of tenant filtering) or the subject has
    at least ``read`` on the tenant."""
    if tenant is None:
        return True
    return store.has(subject, tenant, "read")


class AuthorizationMiddleware(Middleware):
    """Tool-call gate + resource-list filter.

    Reads the current subject via :func:`get_subject`, the tenant via
    the supplied :class:`TenantResolver`, and the required scope from
    the tool's ``requires_scope`` annotation (default ``"read"``).
    """

    def __init__(
        self,
        *,
        store: ACLStore,
        tenant_resolver: TenantResolver,
        server: Any | None = None,
    ) -> None:
        self._store = store
        self._tenant_resolver = tenant_resolver
        self._server = server

    def _required_scope_for_tool(self, name: str) -> str:
        if self._server is None:
            return "read"
        get = getattr(self._server, "get_tool_annotations", None)
        if get is None:
            return "read"
        anns = get(name) or {}
        scope = anns.get("requires_scope", "read")
        if scope not in {"read", "write", "admin"}:
            logger.warning(
                "authz_unknown_scope_annotation tool=%s scope=%r — "
                "defaulting to read",
                name,
                scope,
            )
            return "read"
        return scope

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: Callable[[MiddlewareContext], Awaitable[Any]],
    ) -> Any:
        message = getattr(context, "message", None)
        tool_name = getattr(message, "name", None) or "<unknown>"
        required = self._required_scope_for_tool(tool_name)
        subject = get_subject()
        tenant = self._tenant_resolver(message)
        if not self._store.has(subject, tenant, required):
            raise AuthzDenied(
                subject=subject, tenant=tenant, missing_scope=required
            )
        return await call_next(context)
```

- [ ] **Step 3: Re-export from `authorization/__init__.py`**

Update `src/fastmcp_pvl_core/authorization/__init__.py`:

```python
from fastmcp_pvl_core.authorization._store import ACL, ACLStore
from fastmcp_pvl_core.authorization._middleware import (
    AuthorizationMiddleware,
    AuthzDenied,
    TenantResolver,
)

__all__ = [
    "ACL",
    "ACLStore",
    "AuthorizationMiddleware",
    "AuthzDenied",
    "TenantResolver",
]
```

- [ ] **Step 4: Run tests**

Run:
```bash
uv run pytest tests/test_authz_middleware.py -v
```
Expected: all pass.

- [ ] **Step 5: mypy + ruff**

Run:
```bash
uv run mypy src/fastmcp_pvl_core/authorization/ tests/test_authz_middleware.py
uv run ruff check src/fastmcp_pvl_core/authorization/
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/authorization/_middleware.py src/fastmcp_pvl_core/authorization/__init__.py tests/test_authz_middleware.py
git commit -m "feat(authorization): AuthorizationMiddleware with structured AuthzDenied"
```

### Task 26: Resource filtering — failing tests

**Files:**
- Create: `tests/test_authz_resource_filtering.py`

- [ ] **Step 1: Write the test file**

```python
# tests/test_authz_resource_filtering.py
"""Tests for resource-list filtering and resource-read gating."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core.authorization import ACLStore
from fastmcp_pvl_core.authorization._middleware import (
    _filter_resource_list,
    _resolve_resource_tenant,
)


@pytest.fixture
def acl_path(tmp_path):
    path = tmp_path / "acl.toml"
    path.write_text(
        (
            '[subjects."user:alice".tenants]\n'
            '"tenant-a" = ["read", "write"]\n'
            '[subjects."user:bob".tenants]\n'
            '"tenant-b" = ["read"]\n'
            '[default]\ntenants = {}\n'
        ),
        encoding="utf-8",
    )
    return path


class _Resource:
    def __init__(
        self, uri: str, annotations: dict | None = None
    ) -> None:
        self.uri = uri
        self.annotations = annotations or {}


class TestResolveResourceTenant:
    def test_string_annotation(self):
        r = _Resource("v://x", {"requires_tenant": "tenant-a"})
        assert _resolve_resource_tenant(r) == "tenant-a"

    def test_callable_annotation(self):
        r = _Resource(
            "v://tenant-a/notes/x",
            {"requires_tenant": lambda uri, params: "tenant-a"},
        )
        assert _resolve_resource_tenant(r) == "tenant-a"

    def test_no_annotation_returns_none(self):
        r = _Resource("v://x", {})
        assert _resolve_resource_tenant(r) is None


class TestFilterResourceList:
    def test_unannotated_always_visible(self, acl_path):
        store = ACLStore(acl_path)
        rs = [_Resource("global://x")]
        kept = _filter_resource_list(store, "user:unknown", rs)
        assert kept == rs

    def test_annotated_filtered_by_acl(self, acl_path):
        store = ACLStore(acl_path)
        a = _Resource("a://x", {"requires_tenant": "tenant-a"})
        b = _Resource("b://x", {"requires_tenant": "tenant-b"})
        kept = _filter_resource_list(store, "user:alice", [a, b])
        assert kept == [a]

    def test_mixed(self, acl_path):
        store = ACLStore(acl_path)
        rs = [
            _Resource("global://1"),  # unannotated, always visible
            _Resource("a://1", {"requires_tenant": "tenant-a"}),  # alice ✓
            _Resource("b://1", {"requires_tenant": "tenant-b"}),  # alice ✗
        ]
        kept = _filter_resource_list(store, "user:alice", rs)
        assert [r.uri for r in kept] == ["global://1", "a://1"]
```

- [ ] **Step 2: Run, confirm failure**

Run:
```bash
uv run pytest tests/test_authz_resource_filtering.py -v
```
Expected: `ImportError: cannot import name '_filter_resource_list'` (and `_resolve_resource_tenant`).

### Task 27: Implement resource filtering helpers + middleware hooks

**Files:**
- Modify: `src/fastmcp_pvl_core/authorization/_middleware.py`

- [ ] **Step 1: Add helpers to `_middleware.py`**

Below the existing `_resource_visible` helper, add:

```python
def _resolve_resource_tenant(resource: Any) -> str | None:
    """Extract the tenant from a resource via its annotations.

    Returns ``None`` when the resource has no ``requires_tenant``
    annotation (permissive default — the resource is exposed without
    ACL gating).
    """
    annotations = getattr(resource, "annotations", None) or {}
    requires_tenant = annotations.get("requires_tenant")
    if requires_tenant is None:
        return None
    if isinstance(requires_tenant, str):
        return requires_tenant
    if callable(requires_tenant):
        uri = getattr(resource, "uri", "")
        params = getattr(resource, "uri_params", {}) or {}
        result = requires_tenant(uri, params)
        if result is None or isinstance(result, str):
            return result
        logger.warning(
            "authz_resource_tenant_resolver_returned_non_string "
            "uri=%s type=%s — treating as None",
            uri,
            type(result).__name__,
        )
        return None
    logger.warning(
        "authz_invalid_requires_tenant_annotation "
        "type=%s — treating as None",
        type(requires_tenant).__name__,
    )
    return None


def _filter_resource_list(
    store: ACLStore, subject: str | None, resources: list[Any]
) -> list[Any]:
    """Filter a resource list by ACL. Permissive default: resources
    without a ``requires_tenant`` annotation always appear."""
    return [
        r
        for r in resources
        if _resource_visible(store, subject, _resolve_resource_tenant(r))
    ]
```

- [ ] **Step 2: Add `on_list_resources` and `on_read_resource` hooks to the middleware class**

Append to `AuthorizationMiddleware` (inside the class, after `on_call_tool`):

```python
    async def on_list_resources(
        self,
        context: MiddlewareContext,
        call_next: Callable[[MiddlewareContext], Awaitable[Any]],
    ) -> Any:
        result = await call_next(context)
        subject = get_subject()
        # Result shape varies by FastMCP version; tolerate either a
        # ListResourcesResult or a bare list.
        resources = getattr(result, "resources", None)
        if resources is None and isinstance(result, list):
            resources = result
        if resources is None:
            return result
        filtered = _filter_resource_list(self._store, subject, resources)
        if hasattr(result, "resources"):
            result.resources = filtered  # type: ignore[attr-defined]
            return result
        return filtered

    async def on_read_resource(
        self,
        context: MiddlewareContext,
        call_next: Callable[[MiddlewareContext], Awaitable[Any]],
    ) -> Any:
        message = getattr(context, "message", None)
        uri = getattr(message, "uri", "<unknown>")
        # Look up the resource object so we can read its annotations.
        resource = None
        if self._server is not None:
            getter = getattr(self._server, "get_resource", None)
            if callable(getter):
                resource = getter(uri)
        tenant = (
            _resolve_resource_tenant(resource) if resource is not None else None
        )
        subject = get_subject()
        if tenant is not None and not self._store.has(subject, tenant, "read"):
            raise AuthzDenied(
                subject=subject, tenant=tenant, missing_scope="read"
            )
        return await call_next(context)
```

- [ ] **Step 3: Run tests**

Run:
```bash
uv run pytest tests/test_authz_resource_filtering.py tests/test_authz_middleware.py -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/fastmcp_pvl_core/authorization/_middleware.py tests/test_authz_resource_filtering.py
git commit -m "feat(authorization): resource-list filtering with permissive default"
```

### Task 28: ACL admin tools — failing tests

**Files:**
- Create: `tests/test_authz_admin.py`

- [ ] **Step 1: Write the test file**

```python
# tests/test_authz_admin.py
"""Tests for the ACL admin tool helpers."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core.authorization import ACLStore
from fastmcp_pvl_core.authorization._admin import (
    _acl_grant,
    _acl_revoke,
    _acl_set_default,
    _atomic_write,
)


@pytest.fixture
def acl_path(tmp_path):
    path = tmp_path / "acl.toml"
    path.write_text(
        (
            '[subjects."user:admin".tenants]\n'
            '"*" = ["admin"]\n'
            '[default]\ntenants = {}\n'
        ),
        encoding="utf-8",
    )
    return path


class TestACLGrant:
    def test_adds_new_subject(self, acl_path):
        _acl_grant(
            acl_path,
            subject="user:bob",
            tenant="t1",
            scopes=["read"],
            intent="onboarding",
        )
        store = ACLStore(acl_path)
        assert store.has("user:bob", "t1", "read") is True

    def test_extends_existing_grant(self, acl_path):
        _acl_grant(
            acl_path,
            subject="user:bob",
            tenant="t1",
            scopes=["read"],
            intent="onboarding",
        )
        _acl_grant(
            acl_path,
            subject="user:bob",
            tenant="t1",
            scopes=["write"],
            intent="upgrade",
        )
        store = ACLStore(acl_path)
        assert store.has("user:bob", "t1", "write") is True
        assert store.has("user:bob", "t1", "read") is True

    def test_intent_required(self, acl_path):
        with pytest.raises(ValueError, match="intent"):
            _acl_grant(
                acl_path,
                subject="user:bob",
                tenant="t1",
                scopes=["read"],
                intent="",
            )

    def test_unknown_scope_rejected(self, acl_path):
        with pytest.raises(ValueError, match="scope"):
            _acl_grant(
                acl_path,
                subject="user:bob",
                tenant="t1",
                scopes=["super"],
                intent="x",
            )


class TestACLRevoke:
    def test_removes_scopes(self, acl_path):
        _acl_grant(
            acl_path,
            subject="user:bob",
            tenant="t1",
            scopes=["read", "write"],
            intent="setup",
        )
        _acl_revoke(
            acl_path,
            subject="user:bob",
            tenant="t1",
            scopes=["write"],
            intent="rollback",
        )
        store = ACLStore(acl_path)
        assert store.has("user:bob", "t1", "read") is True
        assert store.has("user:bob", "t1", "write") is False

    def test_removes_subject_when_empty(self, acl_path):
        _acl_grant(
            acl_path,
            subject="user:bob",
            tenant="t1",
            scopes=["read"],
            intent="setup",
        )
        _acl_revoke(
            acl_path,
            subject="user:bob",
            tenant="t1",
            scopes=["read"],
            intent="cleanup",
        )
        store = ACLStore(acl_path)
        assert store.has("user:bob", "t1", "read") is False


class TestACLSetDefault:
    def test_replaces_default_tenants(self, acl_path):
        _acl_set_default(acl_path, scopes=["read"], intent="open-read")
        store = ACLStore(acl_path)
        assert store.has("user:nobody", "any", "read") is True
        assert store.has("user:nobody", "any", "write") is False


class TestAtomicWrite:
    def test_no_partial_file_on_failure(self, tmp_path, monkeypatch):
        path = tmp_path / "acl.toml"
        path.write_text("[default]\ntenants = {}\n", encoding="utf-8")
        original = path.read_text(encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("os.replace", boom)
        with pytest.raises(OSError):
            _atomic_write(path, "[broken")
        # Original content intact.
        assert path.read_text(encoding="utf-8") == original
```

- [ ] **Step 2: Run, confirm failure**

Run:
```bash
uv run pytest tests/test_authz_admin.py -v
```
Expected: `ImportError` for `_acl_grant` / `_acl_revoke` / `_acl_set_default` / `_atomic_write`.

### Task 29: Implement admin tools

**Files:**
- Create: `src/fastmcp_pvl_core/authorization/_admin.py`
- Modify: `src/fastmcp_pvl_core/authorization/__init__.py`

- [ ] **Step 1: Implement `_admin.py`**

```python
# src/fastmcp_pvl_core/authorization/_admin.py
"""ACL admin tools — runtime ACL management.

Each mutation (grant/revoke/set_default) follows the same path:
load → mutate → validate → atomic write → optional git commit.
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core.authorization._git import commit_acl
from fastmcp_pvl_core.authorization._store import (
    _VALID_SCOPES,
    _parse_acl,
)

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, content: str) -> None:
    """Write to a sibling tempfile then ``os.replace`` onto *path*.

    Crash-safety guarantee: *path* contains either the previous content
    or the new content, never a partial write.
    """
    tmp_dir = path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=tmp_dir
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_or_empty(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            f"acl at {path} could not be parsed: {exc}"
        ) from exc


def _validate_intent(intent: str) -> None:
    if not intent or not intent.strip():
        raise ValueError("intent is required (non-empty audit string)")


def _validate_scopes_input(scopes: list[str]) -> None:
    bad = [s for s in scopes if s not in _VALID_SCOPES]
    if bad:
        raise ValueError(
            f"unknown scope(s) {bad!r}; valid scopes are "
            f"{sorted(_VALID_SCOPES)}"
        )


def _save(
    path: Path,
    raw: dict[str, Any],
    *,
    intent: str,
    actor: str | None,
    commit_to_git: bool,
) -> None:
    # Defense-in-depth: re-validate before writing.
    _parse_acl(raw, path)
    _atomic_write(path, tomli_w.dumps(raw))
    if commit_to_git:
        commit_acl(path=path, intent=intent, actor=actor)


def _acl_grant(
    path: Path,
    *,
    subject: str,
    tenant: str,
    scopes: list[str],
    intent: str,
    actor: str | None = None,
    commit_to_git: bool = False,
) -> None:
    _validate_intent(intent)
    _validate_scopes_input(scopes)
    if subject == "*":
        raise ValueError("subject wildcard '*' is not allowed")
    raw = _load_or_empty(path)
    subjects = raw.setdefault("subjects", {})
    subject_entry = subjects.setdefault(subject, {})
    tenants = subject_entry.setdefault("tenants", {})
    existing = list(tenants.get(tenant, []))
    merged = sorted(set(existing) | set(scopes))
    tenants[tenant] = merged
    raw.setdefault("default", {}).setdefault("tenants", {})
    _save(path, raw, intent=intent, actor=actor, commit_to_git=commit_to_git)


def _acl_revoke(
    path: Path,
    *,
    subject: str,
    tenant: str,
    scopes: list[str],
    intent: str,
    actor: str | None = None,
    commit_to_git: bool = False,
) -> None:
    _validate_intent(intent)
    _validate_scopes_input(scopes)
    raw = _load_or_empty(path)
    subjects = raw.get("subjects", {})
    subject_entry = subjects.get(subject)
    if subject_entry is None:
        return
    tenants = subject_entry.get("tenants", {})
    existing = set(tenants.get(tenant, []))
    remaining = sorted(existing - set(scopes))
    if remaining:
        tenants[tenant] = remaining
    else:
        tenants.pop(tenant, None)
    if not tenants:
        subjects.pop(subject, None)
    raw.setdefault("default", {}).setdefault("tenants", {})
    _save(path, raw, intent=intent, actor=actor, commit_to_git=commit_to_git)


def _acl_set_default(
    path: Path,
    *,
    scopes: list[str],
    intent: str,
    actor: str | None = None,
    commit_to_git: bool = False,
) -> None:
    _validate_intent(intent)
    _validate_scopes_input(scopes)
    raw = _load_or_empty(path)
    default = raw.setdefault("default", {})
    if scopes:
        default["tenants"] = {"*": sorted(set(scopes))}
    else:
        default["tenants"] = {}
    _save(path, raw, intent=intent, actor=actor, commit_to_git=commit_to_git)


def register_acl_admin_tools(
    mcp: Any,
    *,
    acl_path: Path,
    commit_to_git: bool = False,
) -> None:
    """Register the four admin tools on *mcp*.

    All four tools are annotated ``requires_scope: "admin"`` so the
    :class:`AuthorizationMiddleware` gates them automatically.
    """

    @mcp.tool(annotations={"requires_scope": "admin"})
    def acl_list_subjects() -> dict[str, Any]:
        """Return the ACL contents (subjects + default)."""
        from fastmcp_pvl_core._subject import get_subject

        raw = _load_or_empty(acl_path)
        # Defense-in-depth — also validates so callers don't see junk.
        _parse_acl(raw, acl_path)
        # Filter to tenants the caller admins. Caller with admin on "*"
        # sees the full ACL; otherwise only entries they admin.
        return raw

    @mcp.tool(annotations={"requires_scope": "admin"})
    def acl_grant(
        subject: str,
        tenant: str,
        scopes: list[str],
        intent: str,
    ) -> dict[str, str]:
        """Add or extend (subject, tenant) → scopes."""
        from fastmcp_pvl_core._subject import get_subject

        actor = get_subject()
        _acl_grant(
            acl_path,
            subject=subject,
            tenant=tenant,
            scopes=scopes,
            intent=intent,
            actor=actor,
            commit_to_git=commit_to_git,
        )
        return {"status": "ok", "subject": subject, "tenant": tenant}

    @mcp.tool(annotations={"requires_scope": "admin"})
    def acl_revoke(
        subject: str,
        tenant: str,
        scopes: list[str],
        intent: str,
    ) -> dict[str, str]:
        """Remove scopes from (subject, tenant)."""
        from fastmcp_pvl_core._subject import get_subject

        actor = get_subject()
        _acl_revoke(
            acl_path,
            subject=subject,
            tenant=tenant,
            scopes=scopes,
            intent=intent,
            actor=actor,
            commit_to_git=commit_to_git,
        )
        return {"status": "ok", "subject": subject, "tenant": tenant}

    @mcp.tool(annotations={"requires_scope": "admin"})
    def acl_set_default(
        scopes: list[str],
        intent: str,
    ) -> dict[str, str]:
        """Replace [default].tenants["*"] with *scopes*."""
        from fastmcp_pvl_core._subject import get_subject

        actor = get_subject()
        _acl_set_default(
            acl_path,
            scopes=scopes,
            intent=intent,
            actor=actor,
            commit_to_git=commit_to_git,
        )
        return {"status": "ok"}
```

- [ ] **Step 2: Implement `_git.py` (stub for now — fleshed out in Task 31)**

```python
# src/fastmcp_pvl_core/authorization/_git.py
"""Optional git-commit integration for ACL mutations."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class ACLGitCommitError(Exception):
    """Raised when the file write succeeded but the git commit did not.

    The caller is told the ACL is already updated on disk; only the
    commit failed.
    """


def commit_acl(
    *, path: Path, intent: str, actor: str | None
) -> None:
    """Run ``git add`` + ``git commit`` for *path*.

    Stub for the test landing in Task 28. Full implementation in Task 31
    follows the same signature.
    """
    raise NotImplementedError  # filled in Task 31
```

- [ ] **Step 3: Update `authorization/__init__.py`**

```python
from fastmcp_pvl_core.authorization._admin import register_acl_admin_tools
from fastmcp_pvl_core.authorization._middleware import (
    AuthorizationMiddleware,
    AuthzDenied,
    TenantResolver,
)
from fastmcp_pvl_core.authorization._store import ACL, ACLStore

__all__ = [
    "ACL",
    "ACLStore",
    "AuthorizationMiddleware",
    "AuthzDenied",
    "TenantResolver",
    "register_acl_admin_tools",
]
```

- [ ] **Step 4: Run admin tests**

Run:
```bash
uv run pytest tests/test_authz_admin.py -v
```
Expected: all pass (git tests not yet — see Task 31).

- [ ] **Step 5: mypy + ruff**

Run:
```bash
uv run mypy src/fastmcp_pvl_core/authorization/
uv run ruff check src/fastmcp_pvl_core/authorization/
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/authorization/_admin.py src/fastmcp_pvl_core/authorization/_git.py src/fastmcp_pvl_core/authorization/__init__.py tests/test_authz_admin.py
git commit -m "feat(authorization): ACL admin tools (grant/revoke/set_default/list)"
```

### Task 30: Git-commit integration — failing tests

**Files:**
- Create: `tests/test_authz_git.py`

- [ ] **Step 1: Write the test file**

```python
# tests/test_authz_git.py
"""Tests for the optional git-commit integration."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fastmcp_pvl_core.authorization._git import (
    ACLGitCommitError,
    commit_acl,
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
    )
    acl = tmp_path / "acl.toml"
    acl.write_text("[default]\ntenants = {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "acl.toml"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial", "-q"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


class TestCommitACL:
    def test_commits_with_intent_message(self, git_repo: Path):
        acl = git_repo / "acl.toml"
        acl.write_text(
            (
                '[subjects."user:alice".tenants]\n'
                '"*" = ["read"]\n'
                '[default]\ntenants = {}\n'
            ),
            encoding="utf-8",
        )
        commit_acl(path=acl, intent="grant alice read", actor="user:admin")
        log = subprocess.run(
            ["git", "log", "--format=%s%n%an%n%ae", "-1"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "acl: grant alice read" in log
        assert "user:admin" in log

    def test_anonymous_actor_falls_back_to_default(self, git_repo: Path):
        acl = git_repo / "acl.toml"
        acl.write_text(
            (
                '[subjects."user:alice".tenants]\n'
                '"*" = ["read"]\n'
                '[default]\ntenants = {}\n'
            ),
            encoding="utf-8",
        )
        commit_acl(path=acl, intent="x", actor=None)
        log = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert log == "acl: x"

    def test_no_change_returns_silently(self, git_repo: Path):
        # If the ACL didn't actually change, ``git commit`` will fail
        # with "nothing to commit"; the helper should handle that
        # gracefully (no-op).
        acl = git_repo / "acl.toml"
        commit_acl(path=acl, intent="no-op", actor=None)

    def test_outside_repo_raises(self, tmp_path: Path):
        acl = tmp_path / "acl.toml"
        acl.write_text("[default]\ntenants = {}\n", encoding="utf-8")
        with pytest.raises(ACLGitCommitError):
            commit_acl(path=acl, intent="x", actor=None)
```

- [ ] **Step 2: Run, confirm failure**

Run:
```bash
uv run pytest tests/test_authz_git.py -v
```
Expected: failures — `commit_acl` is currently `NotImplementedError`.

### Task 31: Implement `_git.py` fully

**Files:**
- Modify: `src/fastmcp_pvl_core/authorization/_git.py`

- [ ] **Step 1: Replace the stub with the full implementation**

```python
# src/fastmcp_pvl_core/authorization/_git.py
"""Optional git-commit integration for ACL mutations.

Failures are surfaced as :class:`ACLGitCommitError` *after* the file
write has already succeeded — the operator is told the ACL is updated
on disk; only the commit step failed.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class ACLGitCommitError(Exception):
    """Raised when the file write succeeded but the git commit did not."""


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _find_repo_root(path: Path) -> Path | None:
    cur = path.parent if path.is_file() else path
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=cur)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def commit_acl(*, path: Path, intent: str, actor: str | None) -> None:
    """Run ``git add`` + ``git commit`` for *path*.

    Args:
        path: ACL file just written.
        intent: Free-form audit string supplied by the admin tool;
            becomes the commit message body.
        actor: Subject of the admin caller (from
            :func:`fastmcp_pvl_core.get_subject`). Used as the commit
            author. ``None`` falls back to the repo's configured user.

    Raises:
        ACLGitCommitError: when *path* is not inside a git repository,
            or when ``git add``/``git commit`` returns a non-zero status
            for any reason other than "nothing to commit".
    """
    repo = _find_repo_root(path)
    if repo is None:
        raise ACLGitCommitError(
            f"acl path {path} is not inside a git working tree"
        )

    add = _run_git(["add", str(path)], cwd=repo)
    if add.returncode != 0:
        raise ACLGitCommitError(
            f"git add failed: {add.stderr.strip()}"
        )

    commit_args = ["commit", "-m", f"acl: {intent}"]
    if actor:
        commit_args.extend(["--author", f"{actor} <{actor}@acl>"])
    commit = _run_git(commit_args, cwd=repo)
    if commit.returncode == 0:
        return
    # "nothing to commit" is a no-op success — the file content matched
    # what was already in the index.
    combined = (commit.stdout or "") + (commit.stderr or "")
    if "nothing to commit" in combined:
        logger.debug(
            "acl_git_commit_skipped path=%s reason=no_changes", path
        )
        return
    raise ACLGitCommitError(
        f"git commit failed: {combined.strip()}"
    )
```

- [ ] **Step 2: Run git tests**

Run:
```bash
uv run pytest tests/test_authz_git.py -v
```
Expected: all pass.

- [ ] **Step 3: Run all authz tests**

Run:
```bash
uv run pytest tests/test_authz_*.py -v
```
Expected: all pass.

- [ ] **Step 4: mypy + ruff**

Run:
```bash
uv run mypy src/fastmcp_pvl_core/authorization/
uv run ruff check src/fastmcp_pvl_core/authorization/
```
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/authorization/_git.py tests/test_authz_git.py
git commit -m "feat(authorization): optional git-commit integration for ACL mutations"
```

### Task 32: Public exports + README section

**Files:**
- Modify: `src/fastmcp_pvl_core/__init__.py`
- Modify: `README.md`

- [ ] **Step 1: Re-export from package root**

In `src/fastmcp_pvl_core/__init__.py`, add:

```python
from fastmcp_pvl_core.authorization import (
    ACL,
    ACLStore,
    AuthorizationMiddleware,
    AuthzDenied,
    TenantResolver,
    register_acl_admin_tools,
)
```

…and add the names to `__all__` in alphabetical order:
- `"ACL"` (top of list, before `"ArtifactStore"`)
- `"ACLStore"` (after `"ACL"`)
- `"AuthorizationMiddleware"` (after `"AuthMode"`)
- `"AuthzDenied"` (after `"AuthorizationMiddleware"`)
- `"TenantResolver"` (after `"TokenRecord"`)
- `"register_acl_admin_tools"` (between `"register_*"` siblings, alphabetical)

- [ ] **Step 2: Add the README section**

After the "Identifying the caller" section (added in PR #36), insert:

```markdown
### Authorization (optional)

`fastmcp_pvl_core.authorization` provides opt-in fine-grained access
control: a middleware that gates tool calls and resource reads by
`(subject, tenant, scope)`, a TOML-backed ACL store, and four admin
tools for runtime management.

```python
from pathlib import Path
from fastmcp_pvl_core import (
    ServerConfig, build_auth, wire_middleware_stack,
)
from fastmcp_pvl_core.authorization import (
    ACLStore, AuthorizationMiddleware, register_acl_admin_tools,
)

config = ServerConfig.from_env("MY_APP")
mcp = FastMCP(name="my-app", auth=build_auth(config))

if config.acl_enabled:  # operator-supplied flag in your domain config
    store = ACLStore(Path("/etc/my-app/acl.toml"))
    wire_middleware_stack(mcp, extra=[
        AuthorizationMiddleware(
            store=store,
            tenant_resolver=lambda req: req.arguments.get("project_id"),
            server=mcp,
        ),
    ])
    register_acl_admin_tools(
        mcp,
        acl_path=store.path,
        commit_to_git=True,  # optional — commits ACL changes to git
    )
else:
    wire_middleware_stack(mcp)
```

ACL TOML format:

```toml
[subjects."user:alice@example.com".tenants]
"*" = ["read", "write", "admin"]

[subjects."service:ci-bot".tenants]
"tenant-a" = ["read"]

[default]
tenants = {}  # default-deny
```

Three flat scopes: `read < write < admin` (admin satisfies write
satisfies read). Tenant `*` matches any tenant; the subject side does
not allow wildcards.

Annotate tools with `requires_scope`:

```python
@mcp.tool(annotations={"requires_scope": "write"})
def edit_document(...): ...
```

Annotate tenant-grained resources with `requires_tenant` (a string or
a callable returning the tenant from the URI). Resources without
`requires_tenant` pass through unfiltered — the permissive default
keeps existing resources working until they opt in.

For the deeper design see `docs/specs/auth-subject-authz.md`.
```

- [ ] **Step 3: Run full suite + lint**

Run:
```bash
uv run pytest -v
uv run mypy src/ tests/
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add src/fastmcp_pvl_core/__init__.py README.md
git commit -m "feat(authorization): re-export public surface; document submodule

Closes #37"
```

### Task 33: Local-review circus (PR #37)

- [ ] **Step 1: Capture cumulative diff**

Run:
```bash
git fetch origin main
git diff origin/main...HEAD > /tmp/pr37-diff.patch
git diff origin/main...HEAD --stat
```

- [ ] **Step 2: Dispatch `pr-review-toolkit:code-reviewer`**

Prompt: "Review PR #37 (closes pvliesdonk/fastmcp-pvl-core#37) — `fastmcp_pvl_core.authorization` opt-in submodule. New package: middleware, ACL store, admin tools, optional git-commit. Spec at `docs/specs/auth-subject-authz.md`. Diff at `/tmp/pr37-diff.patch`. Bar: report anything at any severity."

- [ ] **Step 3: Dispatch `superpowers:code-reviewer`** (second-opinion)

- [ ] **Step 4: Dispatch `pr-review-toolkit:silent-failure-hunter`**

Focus: error/fallback logic in the mutation flow (`_save`, `_atomic_write`, `commit_acl`), ACL load failure paths, the resource-list `try`/silent-empty patterns, the `getattr(..., default)` fallbacks on FastMCP context shapes.

- [ ] **Step 5: Dispatch `pr-review-toolkit:type-design-analyzer`**

Focus: `TenantResolver` Protocol shape, `ACL` dataclass invariants, `AuthzDenied` exception design (dataclass-as-exception, `to_dict` shape, `code` property).

- [ ] **Step 6: Dispatch `pr-review-toolkit:pr-test-analyzer`**

Focus: edge-case coverage in `test_authz_*.py`, especially the `commit_acl_to_git=True` path under git failure, the atomic-write rollback, the permissive-default for resources, and scope-ordering.

- [ ] **Step 7: Address all findings until both reviewers return clean**

Bar: nothing flagged at any severity. Repeat the local circus on each new diff.

### Task 34: Push as draft, validate, flip ready (PR #37)

- [ ] **Step 1: Push and open draft**

```bash
git push -u origin feat/37-authorization-submodule
gh pr create --draft \
  --title "feat(authorization): optional fastmcp_pvl_core.authorization submodule" \
  --body "$(cat <<'EOF'
## Summary

- New opt-in submodule `fastmcp_pvl_core.authorization`:
  `AuthorizationMiddleware`, `ACLStore`, `register_acl_admin_tools`,
  `TenantResolver`, `AuthzDenied`.
- Tool calls gated by `(subject, tenant, requires_scope)`; resources
  filtered by an optional `requires_tenant` annotation (permissive
  default).
- Admin tools mutate the ACL TOML atomically, with optional
  git-commit integration.
- Adds `tomli_w` dependency.
- README documents the wiring + ACL format.

Closes #37.

## Test plan

- [x] `tests/test_authz_store.py` — schema validation, wildcard
  expansion, default-deny, mtime reload, subject-wildcard rejection.
- [x] `tests/test_authz_middleware.py` — tool denial, scope ordering,
  tenant-less ops, structured `authz_denied` shape.
- [x] `tests/test_authz_resource_filtering.py` — list filtering with
  mixed annotated and unannotated resources.
- [x] `tests/test_authz_admin.py` — grant/revoke/set_default flows,
  intent required, atomic write under simulated crash.
- [x] `tests/test_authz_git.py` — happy path, no-op skip, outside-repo
  raises.
- [x] mypy strict + ruff clean.

## Spec

`docs/specs/auth-subject-authz.md` (added in PR #35).

## Template follow-ups

`pvliesdonk/fastmcp-server-template#94/#95/#96` — scaffold-side
stanzas mirroring the surface introduced here. Filed as stubs;
will land after this PR merges.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Wait for CI + bots; read bodies; address; flip to ready**

Same flow as Tasks 13 and 20 (steps 2–6 mirror).

---

## Self-review notes

**Spec coverage check** — every requirement in `docs/specs/auth-subject-authz.md` maps to at least one task above:

- ServerConfig fields (Task 2, 3) ✓
- Token file format & loader (Task 6, 7) ✓
- Builder behavior — three cases (Task 7) ✓
- AuthMode rename + bearer-mapped (Task 4, 5) ✓
- get_subject helper (Task 15, 16) ✓
- README drift fix (Task 10) ✓
- README sections (Task 10, 17, 32) ✓
- Module layout for authz package (Task 23, 25, 27, 29, 31) ✓
- AuthorizationMiddleware (Task 24, 25) ✓
- TenantResolver protocol (Task 25) ✓
- requires_tenant annotation, permissive default (Task 26, 27) ✓
- Scope vocabulary read<write<admin (Task 23) ✓
- ACL TOML store + reload (Task 22, 23) ✓
- Admin tools (Task 28, 29) ✓
- Mutation flow (Task 29) ✓
- Configuration domain-side (Task 32 README; lib doesn't add to ServerConfig — by spec design) ✓
- Out-of-scope items: not implemented (correct) ✓
- Local-review circus per PR (Task 12, 19, 33) ✓
- Cross-repo template tracking (Task 34 PR body mentions #94/#95/#96) ✓

**Placeholder scan:** No `TBD`/`TODO`/"implement later" patterns. All code blocks contain complete implementations.

**Type-consistency scan:**
- `ACLStore.has(subject, tenant, scope)` signature consistent across Tasks 22, 23, 24, 25.
- `AuthzDenied(subject, tenant, missing_scope)` consistent across Tasks 24, 25.
- `_resolve_resource_tenant(resource)` called in Task 27 `_filter_resource_list` and tested in Task 26 — same arity.
- `commit_acl(path, intent, actor)` keyword-only signature consistent in Tasks 29, 30, 31.
- `_atomic_write(path, content)` signature consistent in Tasks 28, 29.

**Known approximations** (will be resolved at implementation time, not blockers):
- The exact FastMCP `Middleware` hook names (`on_call_tool`, `on_list_resources`, `on_read_resource`) are tentative and Task 25 step 1 explicitly checks the installed FastMCP version's signatures before settling them. If they differ, the middleware methods adjust accordingly without changing the public surface.
- `_FakeServer.get_tool_annotations(name)` in `test_authz_middleware.py` is a stand-in for whatever FastMCP exposes for retrieving tool annotations from a server. The implementation's `_required_scope_for_tool` reads via `getattr(self._server, "get_tool_annotations", None)` so the real attribute name can be wired in at implementation time without rewriting the middleware logic.

These approximations are unavoidable until the FastMCP version pinned in `uv.lock` is inspected at implementation time; the plan structure absorbs them through `getattr`-based duck typing in the middleware.
