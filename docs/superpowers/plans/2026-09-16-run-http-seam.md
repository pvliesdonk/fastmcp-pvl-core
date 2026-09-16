# The `run_http` seam — implementation plan (PR 2 of #327)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give pvl-core ownership of the uvicorn invocation, so `log_config=None` reaches it and the topology PR 1 built actually holds at runtime.

**Architecture:** One new export, `run_http`, which every downstream calls instead of `uvicorn.run(...)`. It pins the settings that are pvl-core's to decide (`log_config=None`, `lifespan="on"`) and takes the deployment-dependent ones from `ServerConfig`. A new `ServerConfig.shutdown_grace_s` resolves a divergence two downstreams already shipped. Nothing about rendering changes; that is PR 4.

**Tech Stack:** Python 3.10+, uvicorn 0.44.0, fastmcp 4.0.0; pytest; ruff; mypy.

**Spec:** `docs/superpowers/specs/2026-09-11-root-logging-ownership-design.md` — §2 is what this plan implements, and §1/§4 are the behaviour it completes. Read §2 before Task 1.

**Issue:** child 3 of [#327](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/327), labelled `ships-atomically` — merges to `main` but must not be released until children 4 and 5 land.

## Global Constraints

- **Breaking**, because `ServerConfig` grows a field and downstreams must stop calling `uvicorn.run` themselves. The commit adding the config field and the export uses `feat!:` with a `BREAKING CHANGE:` footer.
- **Relative intra-package imports** inside `src/` (`from ._x import y`); never `from fastmcp_pvl_core...`.
- **No self-name lookups**; **Python 3.10 floor**; CI runs 3.10–3.13.
- **uvicorn stays an optional import.** pvl-core supports stdio-only servers, and `uvicorn` must not be imported at package import time. Import it inside the function, as the downstream CLIs do today.
- **No `**uvicorn_kwargs` passthrough.** It would hand `log_config` back to downstream and silently restore the three-chain behaviour, with no failure signal until someone reads container logs. A future need (TLS) gets a named, classified parameter.
- **No rendering work** — no JSON, no `LOG_FORMAT`, no TTY detection. PR 4.
- **Local checks** before pushing: `uv sync --all-extras`, `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`, **and** `get_change_risk` with `introduced == 0` and `worsened == 0`.

## File Structure

| File | Responsibility |
|---|---|
| `src/fastmcp_pvl_core/_serve.py` (create) | `run_http` and the private `_build_uvicorn_config` it delegates to. New module rather than an addition to `_cli.py`, whose docstring declares a narrow surface of argparse helpers. |
| `src/fastmcp_pvl_core/_config.py` (modify) | `ServerConfig.shutdown_grace_s`, its `from_env` read, and the env-suffix set. |
| `src/fastmcp_pvl_core/__init__.py` (modify) | Export `run_http`. |
| `tests/test_serve.py` (create) | Config assembly, precedence, and one real end-to-end server. |
| `tests/test_config.py` (modify) | The new field's parsing and defaults. |
| `README.md` (modify) | Document `run_http`; un-scope the PR 1 caveats that said uvicorn still reinstalls its handlers. |

## Verified mechanics

Probed on this branch's stack (uvicorn 0.44.0, fastmcp 4.0.0) before this plan was written. Do not re-derive:

- A real server built with `uvicorn.Config(app, log_config=None, lifespan="on", timeout_graceful_shutdown=3)` leaves `uvicorn.access` at `handlers=[] propagate=True`, so records reach pvl-core's root chain and the PR 1 policy governs them.
- End to end against that server: `GET /health` → **not logged**, `POST /mcp` → **not logged**, `GET /nope` → logged 404, `GET /transfer/tok_SECRET123` → logged as `/transfer/<redacted>`. uvicorn's own lifecycle lines ("Shutting down", "Application shutdown complete") render through pvl-core's handler too.
- `uvicorn.Server` exposes `started` (poll it to know when to send requests) and `should_exit` (set it to stop the thread) — that is how the end-to-end test starts and stops a server without a subprocess.

## Not in scope, and tracked

- **Rendering** — PR 4 of #327.
- **The template and downstream cutover** — one PR after the whole epic releases; downstream issues for markdown-vault-mcp and scholar-mcp already exist on the epic.

---

### Task 1: `ServerConfig.shutdown_grace_s`

**Files:**
- Modify: `src/fastmcp_pvl_core/_config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ServerConfig.shutdown_grace_s: int` (default `3`), read from `{PREFIX}_SHUTDOWN_GRACE_S`. Task 2 passes it to uvicorn.

**Why 3 and why a field at all.** The template and scholar-mcp hardcode `timeout_graceful_shutdown=0`; markdown-vault-mcp hardcodes `3`, with a comment that SIGTERM should drain in-flight requests so containers stop cleanly. Two downstreams disagreeing means pvl-core picks one — and `0` drops in-flight requests on every container stop, so MVM's value wins. It is *operator* config rather than shape, because it has to agree with the orchestrator's own termination grace period.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_shutdown_grace_defaults_to_three(monkeypatch):
    monkeypatch.delenv("MYAPP_SHUTDOWN_GRACE_S", raising=False)
    assert ServerConfig.from_env("MYAPP").shutdown_grace_s == 3


def test_shutdown_grace_read_from_env(monkeypatch):
    monkeypatch.setenv("MYAPP_SHUTDOWN_GRACE_S", "30")
    assert ServerConfig.from_env("MYAPP").shutdown_grace_s == 30


def test_shutdown_grace_zero_is_allowed(monkeypatch):
    """0 means "do not drain" — a deliberate choice, not a misconfiguration."""
    monkeypatch.setenv("MYAPP_SHUTDOWN_GRACE_S", "0")
    assert ServerConfig.from_env("MYAPP").shutdown_grace_s == 0


@pytest.mark.parametrize("value", ["-1", "not-a-number", "3.5"])
def test_shutdown_grace_rejects_invalid(monkeypatch, value):
    monkeypatch.setenv("MYAPP_SHUTDOWN_GRACE_S", value)
    with pytest.raises(ConfigurationError, match="MYAPP_SHUTDOWN_GRACE_S"):
        ServerConfig.from_env("MYAPP")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -q -k shutdown_grace`
Expected: `AttributeError` / `TypeError` — the field does not exist.

- [ ] **Step 3: Add the field**

In `ServerConfig`, beside `port`:

```python
    shutdown_grace_s: int = field(
        default=3,
        metadata={
            "help": (
                "Seconds SIGTERM may spend draining in-flight requests "
                "before the HTTP server exits. Set it no higher than the "
                "orchestrator's own termination grace period. ``0`` drops "
                "in-flight requests immediately."
            ),
            "tags": ("server",),
            "wizard": {"group": "Server", "when": "server"},
        },
    )
```

In `from_env`, beside the `PORT` read, using the same strict parse:

```python
        shutdown_grace_s = env_int(
            env_prefix, "SHUTDOWN_GRACE_S", 3, strict=True, minimum=0
        )
```

Add `"SHUTDOWN_GRACE_S"` to `_SERVER_CONFIG_ENV_SUFFIXES`, and pass the value into the `ServerConfig(...)` constructor call at the end of `from_env`. Extend `from_env`'s `Raises:` section to name the new variable alongside `PORT`.

Note: `tests/test_config.py` already asserts `server_config_env_suffixes() == _suffixes_read_by_from_env()`, so a missing suffix fails the suite rather than drifting silently. Do not weaken that test.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS, including the pre-existing drift guard.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_config.py tests/test_config.py
git commit -m "feat(config): add shutdown_grace_s for SIGTERM draining

Refs #327"
```

---

### Task 2: `run_http`

**Files:**
- Create: `src/fastmcp_pvl_core/_serve.py`
- Modify: `src/fastmcp_pvl_core/__init__.py`
- Test: `tests/test_serve.py`

**Interfaces:**
- Consumes: `ServerConfig.shutdown_grace_s` from Task 1.
- Produces: `run_http(app, *, config: ServerConfig, host: str | None = None, port: int | None = None) -> None`, and the private `_build_uvicorn_config(app, *, host, port, shutdown_grace_s) -> uvicorn.Config`.

**Why the split.** `_build_uvicorn_config` exists so the assembly can be asserted without binding a socket, and so the end-to-end test drives the same code path the caller does rather than a copy of it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_serve.py`:

```python
"""The run_http seam — §2 of the root-logging-ownership spec."""

from __future__ import annotations

import logging
import socket
import threading
import time

import httpx
import pytest

from fastmcp_pvl_core import ServerConfig, run_http
from fastmcp_pvl_core._serve import _build_uvicorn_config


async def _app(scope, receive, send):
    """A minimal ASGI app: 200 on /health and /mcp, 404 elsewhere."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    status = 200 if scope["path"] in ("/health", "/mcp") else 404
    await send({"type": "http.response.start", "status": status, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_pins_the_settings_pvl_core_owns():
    built = _build_uvicorn_config(_app, host="127.0.0.1", port=8000, shutdown_grace_s=3)
    assert built.log_config is None
    assert built.lifespan == "on"
    assert built.timeout_graceful_shutdown == 3


def test_takes_host_and_port_from_config(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "fastmcp_pvl_core._serve._run_server", lambda built: captured.update(built=built)
    )
    run_http(_app, config=ServerConfig(host="0.0.0.0", port=9001, shutdown_grace_s=7))
    built = captured["built"]
    assert (built.host, built.port) == ("0.0.0.0", 9001)
    assert built.timeout_graceful_shutdown == 7


def test_explicit_host_and_port_override_config(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "fastmcp_pvl_core._serve._run_server", lambda built: captured.update(built=built)
    )
    run_http(
        _app,
        config=ServerConfig(host="127.0.0.1", port=8000),
        host="0.0.0.0",
        port=9999,
    )
    built = captured["built"]
    assert (built.host, built.port) == ("0.0.0.0", 9999)


def test_zero_port_is_an_override_not_a_fallback(monkeypatch):
    """0 is a real request (bind any free port), not "unset"."""
    captured = {}
    monkeypatch.setattr(
        "fastmcp_pvl_core._serve._run_server", lambda built: captured.update(built=built)
    )
    run_http(_app, config=ServerConfig(port=8000), port=0)
    assert captured["built"].port == 0


def test_end_to_end_access_log_policy(configured_logging_capture):
    """A real server, real requests: successes silent, failures logged and redacted."""
    port = _free_port()
    import uvicorn

    built = _build_uvicorn_config(_app, host="127.0.0.1", port=port, shutdown_grace_s=1)
    server = uvicorn.Server(built)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "server did not start within 10s"

        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            client.get("/health")
            client.post("/mcp")
            client.get("/nope")
            client.get("/transfer/tok_SECRET123")
        time.sleep(0.3)
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    access = [m for m in configured_logging_capture if ' - "' in m]
    assert not any("/health" in m for m in access)
    assert not any("/mcp" in m for m in access)
    assert any("/nope" in m and "404" in m for m in access)
    assert not any("tok_SECRET123" in m for m in access)
    assert any("transfer/<redacted>" in m for m in access)


def test_uvicorn_never_installs_its_own_handlers(configured_logging_capture):
    """log_config=None is what makes PR 1's topology hold at runtime."""
    port = _free_port()
    import uvicorn

    built = _build_uvicorn_config(_app, host="127.0.0.1", port=port, shutdown_grace_s=1)
    server = uvicorn.Server(built)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        access = logging.getLogger("uvicorn.access")
        assert access.handlers == []
        assert access.propagate is True
    finally:
        server.should_exit = True
        thread.join(timeout=10)
```

Add the fixture at the top of the same file — it configures logging the way a server does and captures what reaches root, restoring the topology afterwards:

```python
@pytest.fixture
def configured_logging_capture(monkeypatch):
    """Configure logging as a server does, and capture what reaches root."""
    from fastmcp_pvl_core import configure_logging_from_env

    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    access = logging.getLogger("uvicorn.access")
    saved_access = (access.handlers[:], access.level, access.propagate, access.filters[:])

    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    configure_logging_from_env("TEST_MCP")
    root.addHandler(_Capture())
    try:
        yield messages
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        access.handlers[:], access.level, access.propagate, access.filters[:] = saved_access
```

No `pytest.mark.integration` decorators: this repo registers no custom markers (`pyproject.toml` has `addopts = "-ra"` and `testpaths` only), and inventing one for two tests would add a convention the suite does not otherwise have. The two socket-binding tests live in the same file as the rest and are named so their cost is obvious.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_serve.py -q`
Expected: collection error — `fastmcp_pvl_core._serve` does not exist.

- [ ] **Step 3: Write the implementation**

Create `src/fastmcp_pvl_core/_serve.py`:

```python
"""Run a FastMCP server over HTTP, with uvicorn configured by pvl-core.

Downstream used to call ``uvicorn.run(...)`` itself, which left three
settings for each server to get right independently — and one of them,
``log_config``, decides whether pvl-core's logging topology survives at all.
uvicorn's default config reinstalls its own handlers on ``uvicorn.*`` at
server start, with ``propagate = False`` and the access log on **stdout**,
which undoes the single handler chain :mod:`._logging` installs at root.

So the invocation moves here. What pvl-core decides, it decides for every
server; what depends on the deployment comes from :class:`.ServerConfig`.

``uvicorn`` is imported inside the functions rather than at module import:
pvl-core supports stdio-only servers, which have no reason to pay for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import uvicorn

    from ._config import ServerConfig


def _build_uvicorn_config(
    app: Any,
    *,
    host: str,
    port: int,
    shutdown_grace_s: int,
) -> uvicorn.Config:
    """Assemble the uvicorn configuration pvl-core runs servers with.

    Separate from :func:`run_http` so the assembly can be asserted without
    binding a socket, and so an end-to-end test drives the same code path a
    caller does rather than a copy of it.

    Three settings are pvl-core's to decide and are not overridable:

    * ``log_config=None`` — uvicorn touches logging not at all, leaving the
      root chain from :mod:`._logging` in charge of its records too.
    * ``lifespan="on"`` — FastMCP's startup and shutdown hooks run through
      the ASGI lifespan protocol; a server with this off is broken rather
      than configured.
    * ``access_log`` is left at its default: whether an access record is
      worth printing is decided by the filter in :mod:`._logging`, which can
      express "failures only" where a boolean cannot.
    """
    import uvicorn

    return uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        lifespan="on",
        timeout_graceful_shutdown=shutdown_grace_s,
    )


def _run_server(built: uvicorn.Config) -> None:
    """Run a server to completion. Seam for tests that must not bind."""
    import uvicorn

    uvicorn.Server(built).run()


def run_http(
    app: Any,
    *,
    config: ServerConfig,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Serve *app* over HTTP with pvl-core's uvicorn settings.

    Call this instead of ``uvicorn.run(...)``. The caller builds the ASGI
    app — typically ``server.http_app(path=..., event_store=...)`` — and
    pvl-core owns the invocation.

    Args:
        app: The ASGI application to serve.
        config: Operator configuration. ``host``, ``port`` and
            ``shutdown_grace_s`` are read from it.
        host: Optional override, for a CLI flag that beats the environment.
            ``None`` means "not given", so ``config.host`` is used.
        port: Optional override, same precedence. ``0`` is a real value —
            bind any free port — and is *not* treated as unset.
    """
    _run_server(
        _build_uvicorn_config(
            app,
            host=config.host if host is None else host,
            port=config.port if port is None else port,
            shutdown_grace_s=config.shutdown_grace_s,
        )
    )
```

In `src/fastmcp_pvl_core/__init__.py`, add `from ._serve import run_http` beside the other relative imports, and `"run_http"` to `__all__` in alphabetical order — between `"resolve_auth_mode"` and `"server_config_env_suffixes"`. Verify that position against the real file rather than trusting this line.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_serve.py -q`
Expected: PASS, including both integration tests.

If `httpx` is not already a test dependency, check `pyproject.toml` before adding it — pvl-core declares `httpx` directly, so it should already be importable.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_serve.py src/fastmcp_pvl_core/__init__.py tests/test_serve.py
git commit -m "feat(serve)!: own the uvicorn invocation through run_http

Downstream called uvicorn.run itself, so log_config was each server's to
get right — and uvicorn's default reinstalls its own handlers on uvicorn.*
at server start, undoing the root-logger topology. run_http pins
log_config=None and lifespan=on, and takes host, port and the graceful
shutdown window from ServerConfig.

BREAKING CHANGE: downstream servers must call run_http(app, config=...)
instead of uvicorn.run(...).

Refs #327"
```

---

### Task 3: Documentation

**Files:**
- Modify: `README.md`
- Modify: `src/fastmcp_pvl_core/_logging.py` (the PR 1 caveats only)
- Modify: `tests/test_logging.py` (one test's framing)

**Interfaces:** consumes Tasks 1-2; produces no code.

- [ ] **Step 1: Document `run_http` in the README**

Add a section covering: what it replaces (`uvicorn.run`), the three settings pvl-core pins and why each, `{PREFIX}_SHUTDOWN_GRACE_S` with its default and its relationship to the orchestrator's grace period, and the host/port precedence including that `0` is a real port. Show the call in context — the caller still builds the ASGI app.

- [ ] **Step 2: Un-scope PR 1's caveats**

PR 1 deliberately qualified several claims because uvicorn still reinstalled its handlers. Those qualifications are now false in the other direction. Find them in `README.md`'s `### Logging` section and in `_logging.py`'s module docstring — they mention `run_http`, `log_config=None`, or "until … lands" — and rewrite each to state the behaviour plainly. Grep for `run_http` and `log_config` to find them all rather than relying on memory.

- [ ] **Step 3: Re-frame the PR 1 gap test**

`tests/test_logging.py` has `test_uvicorn_dictconfig_still_runs_but_filter_and_redaction_survive`, whose name asserts a state of the world this PR ends. Keep the test — it still documents something true and useful, that the filter survives a `dictConfig` run by *anything* — but rename and re-comment it so it reads as defence-in-depth for a downstream that calls uvicorn directly, not as a description of pvl-core's own path. Do not weaken its assertions.

- [ ] **Step 4: Verify every claim**

Read the finished prose against the code and run any example. The previous PR shipped a doc that told contributors to restore the behaviour it had just removed; the sweep that caught it is the standard here.

- [ ] **Step 5: Commit**

```bash
git add README.md src/fastmcp_pvl_core/_logging.py tests/test_logging.py
git commit -m "docs(serve): document run_http and drop the PR 1 caveats

Refs #327"
```

---

### Task 4: Full local gate

**Files:** none unless a check fails.

- [ ] **Step 1:** `uv sync --all-extras`
- [ ] **Step 2:** `uv run pytest` — the whole suite. It was 1233 passing at the start of this branch.
- [ ] **Step 3:** `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`.
- [ ] **Step 4:** Both ends of the matrix:
```bash
uv run --python 3.10 pytest tests/test_serve.py tests/test_config.py tests/test_logging.py -q
uv run --python 3.13 pytest tests/test_serve.py tests/test_config.py tests/test_logging.py -q
```
Threads plus an event loop plus a socket is exactly the shape that behaves differently across interpreters, so do not skip this.
- [ ] **Step 5: Run the end-to-end probe by hand** and paste the output into the report:
```bash
uv run python -c "
import logging, socket, threading, time, httpx, uvicorn
from fastmcp_pvl_core import configure_logging_from_env
from fastmcp_pvl_core._serve import _build_uvicorn_config
" 2>&1 | tail -5
```
Then run the fuller probe from the task report of Task 2 — a live server, four requests, and the resulting access lines.
- [ ] **Step 6: The health gate, BEFORE pushing.** Ask the controller to run `get_change_risk` over `main..HEAD` and confirm `introduced == 0` **and** `worsened == 0`. It is an MCP tool, not a CLI. In a linked worktree it needs explicit SHAs.
- [ ] **Step 7:** Commit any fixes (`chore: satisfy the health gate`), or note that the tree was clean.

---

## Self-review notes

- **Spec coverage.** §2's parameter table maps to Task 2's `_build_uvicorn_config` (the three pinned settings) and `run_http`'s signature (operator config). §2's `shutdown_grace_s` paragraph is Task 1. The "no passthrough" rule is in the Global Constraints and enforced by the signature having no `**kwargs`.
- **A cell the spec asserts and this plan tests:** §2 says the access log flows into the unified tree because `access_log` is left alone and `hasHandlers()` is true through propagation. `test_uvicorn_never_installs_its_own_handlers` is that assertion.
- **Type consistency.** `_build_uvicorn_config(..., shutdown_grace_s: int)` matches `ServerConfig.shutdown_grace_s: int` from Task 1 and uvicorn's `timeout_graceful_shutdown: int | None`. `_run_server(built: uvicorn.Config)` is the name the Task 2 tests monkeypatch, spelled identically in both places.
- **Known risk.** The two integration tests bind a real socket and run a background thread. If they prove flaky in CI, the fix is a longer start deadline, not deleting them — they are the only place the whole topology is proven against a real server.
