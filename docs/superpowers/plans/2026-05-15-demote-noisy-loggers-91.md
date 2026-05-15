# Demote Noisy Third-Party Loggers (#91) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Demote `uvicorn.access` and `mcp.server.lowlevel.server` below `INFO` in `configure_logging_from_env`, so their per-request chatter only appears when the operator opts into `DEBUG`.

**Architecture:** Add a module constant listing the noisy loggers. In `configure_logging_from_env`, after the root level is resolved, set each noisy logger to `WARNING` (root above DEBUG) or `NOTSET` (root at DEBUG, restoring inheritance). `uvicorn.error` is deliberately excluded.

**Tech Stack:** Python `logging`, pytest, ruff, mypy.

**Issue:** [#91](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/91). **Spec:** `docs/superpowers/specs/2026-05-15-logging-conformance-90-91-design.md`.

This is PR 1 of 2. It is independent of #90 and lands first.

---

### Task 1: Demote noisy loggers in `configure_logging_from_env`

**Files:**
- Modify: `src/fastmcp_pvl_core/_logging.py`
- Test: `tests/test_logging.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_logging.py` (after the existing `test_unknown_level_falls_back_to_info`, before `class TestSecretMaskFilter`). Add `import pytest` to the imports at the top of the file if it is not already present:

```python
_NOISY_LOGGER_NAMES = (
    "uvicorn.access",
    "mcp.server.lowlevel.server",
    "uvicorn.error",
)


@pytest.fixture
def _restore_noisy_levels():
    """Save and restore the levels of every logger the demotion logic touches."""
    saved = {name: logging.getLogger(name).level for name in _NOISY_LOGGER_NAMES}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_noisy_loggers_demoted_to_warning_at_info(monkeypatch, _restore_noisy_levels):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "INFO")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
    assert logging.getLogger("mcp.server.lowlevel.server").level == logging.WARNING


def test_noisy_loggers_notset_at_debug(monkeypatch, _restore_noisy_levels):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger("uvicorn.access").level == logging.NOTSET
    assert logging.getLogger("mcp.server.lowlevel.server").level == logging.NOTSET


def test_uvicorn_error_logger_untouched(monkeypatch, _restore_noisy_levels):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "INFO")
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    configure_logging_from_env(verbose=False)
    assert logging.getLogger("uvicorn.error").level == logging.INFO


def test_demotion_idempotent_across_level_flips(monkeypatch, _restore_noisy_levels):
    access = logging.getLogger("uvicorn.access")

    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env(verbose=False)
    assert access.level == logging.NOTSET

    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "INFO")
    configure_logging_from_env(verbose=False)
    assert access.level == logging.WARNING

    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env(verbose=False)
    assert access.level == logging.NOTSET
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_logging.py -k "noisy or demotion or uvicorn_error" -v`
Expected: 4 tests FAIL — `uvicorn.access` / `mcp.server.lowlevel.server` levels are `NOTSET` (0) after `configure_logging_from_env`, not `WARNING`, because the demotion logic does not exist yet.

- [ ] **Step 3: Add the module constant**

In `src/fastmcp_pvl_core/_logging.py`, immediately after the `_VALID_LEVELS` line (currently line 19), add:

```python

# Third-party transport / SDK loggers that emit non-conforming INFO-level
# chatter — one or two lines per request. Demoted to WARNING unless the
# operator opts into DEBUG. ``uvicorn.error`` is deliberately excluded: it
# carries genuine bind / startup failures.
_NOISY_THIRD_PARTY_LOGGERS = ("uvicorn.access", "mcp.server.lowlevel.server")
```

- [ ] **Step 4: Add the demotion logic**

In `src/fastmcp_pvl_core/_logging.py`, in `configure_logging_from_env`, immediately after the existing `configure_logging(level)` line (currently line 52), add:

```python

    noisy_level = logging.NOTSET if level == logging.DEBUG else logging.WARNING
    for name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)
```

- [ ] **Step 5: Update the `configure_logging_from_env` docstring**

In `src/fastmcp_pvl_core/_logging.py`, in the `configure_logging_from_env` docstring, immediately before the `Args:` line, add this paragraph:

```
    Two noisy third-party loggers — ``uvicorn.access`` (the HTTP access
    log) and ``mcp.server.lowlevel.server`` (the MCP SDK request line) —
    are demoted to ``WARNING`` whenever the resolved level is above
    ``DEBUG``, so their per-request chatter stays out of the default
    ``INFO`` stream. At ``DEBUG`` they are reset to ``NOTSET`` and
    reappear. ``uvicorn.error`` is never demoted.

```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_logging.py -v`
Expected: all tests PASS, including the 4 new ones and the pre-existing `configure_logging_from_env` / `SecretMaskFilter` tests.

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_logging.py tests/test_logging.py
git commit -m "$(cat <<'EOF'
feat(logging): demote uvicorn/MCP-SDK access loggers below INFO

Closes #91

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Document the demotion in the README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a `### Logging` section**

In `README.md`, insert the following block immediately after the closing ` ``` ` of the `## Usage` code example (the line containing `wire_middleware_stack(mcp)` is inside that block) and immediately before the `### Per-user subject mapping (bearer auth)` heading:

````markdown
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

````

- [ ] **Step 2: Verify the README renders cleanly**

Run: `grep -n "### Logging" README.md`
Expected: one match; the heading sits between the `## Usage` example and `### Per-user subject mapping`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(logging): document noisy-logger demotion in README

Refs #91

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Local checks and open the PR

**Files:** none (verification + PR).

- [ ] **Step 1: Run the full local check suite**

Run each, expecting success:

```bash
uv sync --all-extras
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: pytest all green; ruff format reports no changes; ruff check passes; mypy reports no issues. Fix anything that fails and re-run before proceeding.

- [ ] **Step 2: Run the pre-flight review circus**

Invoke the `preflight-circus` skill on the cumulative diff `BASE..HEAD` (where `BASE = $(git merge-base HEAD origin/main)`). Address every finding at confidence >= 80 locally — do not push until the skill's status is `clean`.

- [ ] **Step 3: Push and open the PR as a draft**

```bash
git push -u origin HEAD
gh pr create --draft --title "feat(logging): demote uvicorn/MCP-SDK access loggers below INFO" --body "$(cat <<'EOF'
## Summary

- Demote `uvicorn.access` and `mcp.server.lowlevel.server` to `WARNING` whenever the resolved log level is above `DEBUG`, so their per-request chatter stays out of the default `INFO` stream.
- Both loggers reappear at `DEBUG`; `uvicorn.error` is never demoted.
- Document the behaviour in a new README `### Logging` section.

Closes #91

## Test plan

- [ ] `uv run pytest tests/test_logging.py` — new demotion + idempotency tests pass.
- [ ] At `INFO`, `uvicorn.access` / `Processing request of type` lines are absent.
- [ ] At `DEBUG`, both reappear.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Verify bot reviews and CI**

After the push, wait for `claude-review` and CI. Read the `claude-review` body (not just the check status). If a bot finds anything, re-invoke `preflight-circus` on the new diff before pushing a fix. Once local review was clean, bot bodies say LGTM, and CI is green, flip the PR ready with `gh pr ready <N>`. Merging is human-only.

---

## Self-Review

- **Spec coverage:** the spec's "Issue #91" section — module constant, demotion logic, behaviour table, docstring, README, acceptance checklist — is covered by Task 1 (constant + logic + docstring), Task 2 (README), Task 3 (verification). All four acceptance boxes map to tests in Task 1 Step 1.
- **Placeholder scan:** no TBD / TODO / vague steps; all code shown in full.
- **Type consistency:** `_NOISY_THIRD_PARTY_LOGGERS` (source constant) and `_NOISY_LOGGER_NAMES` (test fixture tuple, deliberately distinct — it additionally includes `uvicorn.error` for save/restore) are used consistently within their files.
