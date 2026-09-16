# Root logger topology — implementation plan (PR 1 of #327)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make pvl-core the sole owner of the root logger, so every namespace in the process — `fastmcp.*`, `uvicorn.*`, the MCP SDK, domain code — renders through one handler chain.

**Architecture:** `configure_logging_from_env` installs the process's only console handler pair at root and turns both libraries off at their own documented switches: `fastmcp.settings.log_enabled = False` (which also disarms `temporary_log_level`) and, in PR 2, `log_config=None` for uvicorn. Level policy, noise policy and the access-log filter then apply once, on one tree. Rendering stays Rich; the JSON formatter is PR 4.

**Tech Stack:** Python 3.10+, stdlib `logging`, `rich`, fastmcp 4.0.0, uvicorn 0.44.0; pytest; ruff; mypy.

**Spec:** `docs/superpowers/specs/2026-09-11-root-logging-ownership-design.md` — §1 (ownership), §4 (level and filter policy) and §6 (environment contract) are what this plan implements. Read them before Task 1. §3 (rendering) and §2 (`run_http`) are **not** this PR.

**Issue:** child of [#327](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/327), which is labelled `ships-atomically` — this PR and the three after it release together as one major.

## Global Constraints

- **Breaking.** `configure_logging_from_env` gains a required positional `env_prefix`. Commit the signature change with `feat!:` and a `BREAKING CHANGE:` footer; the operator surface changes too (`FASTMCP_LOG_LEVEL` → `<PREFIX>_LOG_LEVEL`).
- **Relative intra-package imports** inside `src/` (`from ._x import y`). Never `from fastmcp_pvl_core...` inside `src/`.
- **No self-name lookups** — no `importlib.metadata.version(...)` or `importlib.resources.files(...)` of this package.
- **Python 3.10 floor**; CI runs 3.10–3.13.
- **stderr only.** Nothing this PR installs may write to stdout — stdout is the protocol channel under stdio transport.
- **Public surface unchanged.** No new names in `__all__`. `SecretMaskFilter` keeps its current behaviour and export.
- **Rich only.** Do not add a JSON formatter, a `LOG_FORMAT` variable, or TTY detection. That is PR 4 and adding it here puts this PR over the size where these reviews converge.
- **Local checks** before pushing: `uv sync --all-extras`, `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`, **and** `get_change_risk(revspec=...)` with `health_delta.introduced == 0`.

## File Structure

| File | Responsibility |
|---|---|
| `src/fastmcp_pvl_core/_logging.py` (modify) | Everything: root ownership, fastmcp neutralisation, env contract, noise policy, the access filter. It is ~130 lines today and will roughly double; that is still one coherent responsibility, so it stays one module. |
| `src/fastmcp_pvl_core/_cli.py:64` (modify) | One help string that currently promises `FASTMCP_LOG_LEVEL=DEBUG`. |
| `tests/test_logging.py` (modify) | Existing tests move to the new signature; the fixture grows to snapshot the whole topology. |
| `README.md` (modify) | The `### Logging` section describes behaviour this PR replaces. |

## Verified mechanics

Every mechanism below was probed on this repo's stack (fastmcp 4.0.0, uvicorn 0.44.0) before this plan was written. Do not re-derive them; do not "improve" them without a failing test.

- `fastmcp.settings.log_enabled = False` makes `configure_logging` return early, so `temporary_log_level` can no longer reattach handlers or reset `propagate`.
- The `fastmcp` logger must also be reset to `NOTSET`: its import-time `INFO` otherwise blocks every `fastmcp.*` DEBUG record before it reaches root.
- `RichHandler` is **not** a `StreamHandler` and has no `.stream`; its stream is `handler.console.file`.
- pytest's `LogCaptureHandler` **is** a `StreamHandler`, over a `StringIO` — so console detection must compare the stream against `sys.stdout`/`sys.stderr` (and their `__dunder__` originals) by identity, never by handler type, or `caplog` dies across the suite.
- uvicorn access records are `'%s - "%s %s HTTP/%s" %d'` with args `(client, method, path_with_query, http_version, status)`; status is `args[4]`.

## Not in scope, and tracked

- **`run_http` and `log_config=None`** — PR 2. Until it lands, uvicorn still installs its own handlers on `uvicorn.*` at server start. This PR still improves the stream, because filters survive `dictConfig` and the access filter is installed on the logger.
- **JSON rendering, `<PREFIX>_LOG_FORMAT`, TTY detection** — PR 4.
- **pvl-core's own non-conforming log calls** — [#328](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/328).

---

### Task 1: Own the root logger

**Files:**
- Modify: `src/fastmcp_pvl_core/_logging.py`
- Test: `tests/test_logging.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `_OWNED_ATTR` (the marker attribute name), `_install_root_handlers(level: int) -> None`, `_neutralise_fastmcp() -> None`, and the behaviour that `configure_logging_from_env` installs exactly one handler pair at root. Tasks 2 and 3 call into the same function.

- [ ] **Step 1: Replace the test fixture**

In `tests/test_logging.py`, replace the existing `_NOISY_LOGGER_NAMES` / autouse fixture with one that snapshots the whole topology. The existing fixture restores only noisy-logger levels, so root-handler tests would leak into each other and into the rest of the suite:

```python
import fastmcp

_MANAGED_LOGGERS = (
    "fastmcp",
    "uvicorn.access",
    "uvicorn.error",
    "mcp.server.lowlevel.server",
    "httpx",
    "httpcore",
    "docket.worker",
)


@pytest.fixture(autouse=True)
def _restore_logging_topology():
    """Snapshot and restore every logger this module touches.

    Root handlers included: these tests install and remove handlers at
    root, and without this the first one to run would leave the rest of
    the suite — and pytest's own ``caplog`` — on a tree it did not expect.
    """
    root = logging.getLogger()
    saved_root = (root.handlers[:], root.level)
    saved = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).filters[:],
        )
        for name in _MANAGED_LOGGERS
    }
    saved_log_enabled = fastmcp.settings.log_enabled
    try:
        yield
    finally:
        root.handlers[:] = saved_root[0]
        root.setLevel(saved_root[1])
        for name, (handlers, level, propagate, filters) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.filters[:] = filters
        fastmcp.settings.log_enabled = saved_log_enabled
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_logging.py`:

```python
class _Capture(logging.Handler):
    """A non-console handler, standing in for an OTLP or file handler."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_installs_one_handler_pair_at_root():
    configure_logging_from_env("TEST_MCP")
    root = logging.getLogger()
    owned = [h for h in root.handlers if getattr(h, "_pvl_core_owned", False)]
    assert len(owned) == 2


def test_repeated_calls_do_not_stack_handlers():
    configure_logging_from_env("TEST_MCP")
    first = len(logging.getLogger().handlers)
    configure_logging_from_env("TEST_MCP")
    configure_logging_from_env("TEST_MCP")
    assert len(logging.getLogger().handlers) == first


def test_fastmcp_logger_is_neutralised():
    configure_logging_from_env("TEST_MCP")
    fastmcp_logger = logging.getLogger("fastmcp")
    assert fastmcp_logger.handlers == []
    assert fastmcp_logger.propagate is True
    assert fastmcp_logger.level == logging.NOTSET


def test_temporary_log_level_cannot_revert_the_topology():
    """The regression that parked #323: fastmcp re-running its own config."""
    from fastmcp.utilities.logging import configure_logging, temporary_log_level

    configure_logging_from_env("TEST_MCP")
    fastmcp_logger = logging.getLogger("fastmcp")
    with temporary_log_level("DEBUG"):
        assert fastmcp_logger.handlers == []
        assert fastmcp_logger.propagate is True
    configure_logging("INFO")
    assert fastmcp_logger.handlers == []
    assert fastmcp_logger.propagate is True


def test_fastmcp_debug_records_reach_root():
    configure_logging_from_env("TEST_MCP", verbose=True)
    capture = _Capture()
    logging.getLogger().addHandler(capture)
    logging.getLogger("fastmcp.middleware.requests").debug("probe")
    assert "probe" in capture.messages


def test_replaces_a_pre_existing_console_handler():
    root = logging.getLogger()
    console = logging.StreamHandler()  # defaults to sys.stderr
    root.addHandler(console)
    configure_logging_from_env("TEST_MCP")
    assert console not in root.handlers


def test_leaves_non_console_handlers_alone():
    """The #323 fix: an operator's OTLP handler at root must survive."""
    root = logging.getLogger()
    capture = _Capture()
    root.addHandler(capture)
    configure_logging_from_env("TEST_MCP")
    assert capture in root.handlers
    logging.getLogger("fastmcp.middleware.requests").warning("exported")
    logging.getLogger("some.domain.module").warning("also exported")
    assert capture.messages == ["exported", "also exported"]


def test_caplog_survives(caplog):
    """pytest's handler is a StreamHandler over StringIO — type-based
    console detection would detach it across the whole suite."""
    configure_logging_from_env("TEST_MCP")
    with caplog.at_level(logging.WARNING):
        logging.getLogger("some.domain.module").warning("captured")
    assert "captured" in caplog.text


def test_writes_nothing_to_stdout(capsys):
    configure_logging_from_env("TEST_MCP")
    logging.getLogger("some.domain.module").warning("stderr only")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_exception_records_go_to_the_traceback_handler_only():
    configure_logging_from_env("TEST_MCP")
    root = logging.getLogger()
    owned = [h for h in root.handlers if getattr(h, "_pvl_core_owned", False)]
    plain = logging.LogRecord("x", logging.ERROR, "_", 0, "no traceback", None, None)
    try:
        raise ValueError("boom")
    except ValueError:
        import sys as _sys

        with_exc = logging.LogRecord(
            "x", logging.ERROR, "_", 0, "traceback", None, _sys.exc_info()
        )
    accepting_plain = [h for h in owned if h.filter(plain)]
    accepting_exc = [h for h in owned if h.filter(with_exc)]
    assert len(accepting_plain) == 1
    assert len(accepting_exc) == 1
    assert accepting_plain != accepting_exc
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_logging.py -q`
Expected: the new tests fail — `configure_logging_from_env()` does not accept a positional argument yet, and no handler carries `_pvl_core_owned`.

- [ ] **Step 4: Implement root ownership**

In `src/fastmcp_pvl_core/_logging.py`, add the imports (`sys`, `Console`, `RichHandler`, `fastmcp`) and this machinery. Keep the existing level-resolution code working for now — Task 2 replaces it. For this task, give `configure_logging_from_env` the new first parameter with a temporary default (`env_prefix: str = ""`); Task 2 makes it required.

```python
_OWNED_ATTR = "_pvl_core_owned"
"""Marks the handlers this module installed.

Idempotence needs it: a ``RichHandler`` is not a ``StreamHandler`` and
exposes no ``.stream``, so the console rule below cannot recognise our own
Rich pair on a second call. Marking is what makes repeated configuration —
and a mode change in a later PR — leave exactly one chain at root.
"""


def _console_stream_ids() -> set[int]:
    """Identity of every stream that means "the operator's console"."""
    streams = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    return {id(stream) for stream in streams if stream is not None}


def _is_console_handler(handler: logging.Handler) -> bool:
    """Whether *handler* writes to the console.

    Keys on stream identity, never on handler type. pytest's
    ``LogCaptureHandler`` is a ``StreamHandler`` subclass over a
    ``StringIO``; a type-based rule would detach ``caplog`` from every test
    in the suite. ``RichHandler`` is the mirror case — not a
    ``StreamHandler`` at all, with its stream at ``console.file``.
    """
    stream = getattr(handler, "stream", None)
    if stream is None:
        stream = getattr(getattr(handler, "console", None), "file", None)
    return stream is not None and id(stream) in _console_stream_ids()


def _neutralise_fastmcp() -> None:
    """Stop FastMCP owning its own logger tree.

    ``log_enabled = False`` makes its ``configure_logging`` return before it
    touches handlers or ``propagate`` — including the call inside
    ``temporary_log_level``, which is what silently reverted the first
    attempt at this (issue #323). Turning the library off is durable where
    fighting it was not.

    The level reset is load-bearing rather than tidiness: FastMCP pins the
    ``fastmcp`` logger to ``INFO`` at import time, and left in place that
    blocks every ``fastmcp.*`` DEBUG record — the request middleware's
    included — before it can reach root.
    """
    fastmcp.settings.log_enabled = False
    logger = logging.getLogger("fastmcp")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


def _install_root_handlers(level: int) -> None:
    """Make pvl-core the sole owner of the root logger's console output.

    Removes our own handlers and any pre-existing console handler — the
    double-render source when ``opentelemetry-instrument`` has installed
    one — and leaves every other handler untouched, so an operator's OTLP,
    file or syslog handler survives. That tolerance is what closes #323:
    with ``fastmcp.*`` propagating again, a handler attached at root now
    receives every record in the process.

    Two handlers, not one, reproducing the pair FastMCP installs: tracebacks
    render compressed (no path or level column, framework frames suppressed,
    three frames) so a stack trace does not drown the line that caused it.
    """
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if getattr(handler, _OWNED_ATTR, False) or _is_console_handler(handler):
            root.removeHandler(handler)

    console = Console(stderr=True)
    formatter = logging.Formatter("%(message)s")

    main = RichHandler(console=console)
    main.setFormatter(formatter)
    main.addFilter(lambda record: record.exc_info is None)

    tracebacks = RichHandler(
        console=console,
        show_path=False,
        show_level=False,
        rich_tracebacks=True,
        tracebacks_max_frames=3,
    )
    tracebacks.setFormatter(formatter)
    tracebacks.addFilter(lambda record: record.exc_info is not None)

    for handler in (main, tracebacks):
        setattr(handler, _OWNED_ATTR, True)
        root.addHandler(handler)
    root.setLevel(level)
```

Then, in `configure_logging_from_env`, replace the `configure_logging(level)` call with:

```python
    _neutralise_fastmcp()
    _install_root_handlers(level)
```

and delete the `from fastmcp.utilities.logging import configure_logging` import, which is now unused.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_logging.py -q`
Expected: PASS. If `test_caplog_survives` fails, the console rule is keying on type rather than stream identity.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_logging.py tests/test_logging.py
git commit -m "feat(logging): make pvl-core the sole owner of the root logger

Refs #327"
```

---

### Task 2: The environment contract

**Files:**
- Modify: `src/fastmcp_pvl_core/_logging.py`
- Modify: `src/fastmcp_pvl_core/_cli.py:64`
- Test: `tests/test_logging.py`

**Interfaces:**
- Consumes: `_neutralise_fastmcp`, `_install_root_handlers` from Task 1.
- Produces: `configure_logging_from_env(env_prefix: str, *, verbose: bool = False) -> None` — `env_prefix` required and positional. Task 3 adds policy inside the same function.

- [ ] **Step 1: Update the existing tests to the new signature**

Every existing test in `tests/test_logging.py` calls `configure_logging_from_env(verbose=...)` or `configure_logging_from_env()` and sets `FASTMCP_LOG_LEVEL`. Rewrite them to pass a prefix and set `TEST_MCP_LOG_LEVEL`. Delete `test_verbose_sets_fastmcp_log_level_env` outright — the environment write it asserts is removed in this task, and the assertion cannot be rescued. For example:

```python
def test_sets_debug_when_verbose_true(monkeypatch):
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env("TEST_MCP", verbose=True)
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG


def test_respects_prefixed_level(monkeypatch):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "WARNING")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
```

Apply the same treatment to the lowercase, unknown-level and default cases.

- [ ] **Step 2: Write the failing tests for the bridge**

```python
def test_bridges_fastmcp_log_level_with_one_warning(monkeypatch, caplog):
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "WARNING")
    with caplog.at_level(logging.WARNING):
        configure_logging_from_env("TEST_MCP")
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    warnings = [r for r in caplog.records if "FASTMCP_LOG_LEVEL" in r.getMessage()]
    assert len(warnings) == 1
    assert "TEST_MCP_LOG_LEVEL" in warnings[0].getMessage()


def test_prefixed_level_wins_and_is_silent(monkeypatch, caplog):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    with caplog.at_level(logging.WARNING):
        configure_logging_from_env("TEST_MCP")
    assert logging.getLogger().getEffectiveLevel() == logging.ERROR
    assert [r for r in caplog.records if "FASTMCP_LOG_LEVEL" in r.getMessage()] == []


def test_verbose_overrides_both_and_is_silent(monkeypatch, caplog):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "ERROR")
    with caplog.at_level(logging.WARNING):
        configure_logging_from_env("TEST_MCP", verbose=True)
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    assert [r for r in caplog.records if "FASTMCP_LOG_LEVEL" in r.getMessage()] == []


def test_verbose_no_longer_writes_the_fastmcp_env_var(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env("TEST_MCP", verbose=True)
    assert os.environ.get("FASTMCP_LOG_LEVEL") is None


def test_env_prefix_is_required():
    with pytest.raises(TypeError):
        configure_logging_from_env()  # type: ignore[call-arg]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_logging.py -q`
Expected: the bridge tests fail — no warning is emitted and `FASTMCP_LOG_LEVEL` is still read unconditionally.

- [ ] **Step 4: Implement the contract**

Replace the level-resolution block of `configure_logging_from_env` with this, and make `env_prefix` a required positional parameter (drop the temporary default from Task 1):

```python
def _resolve_level(env_prefix: str, *, verbose: bool) -> tuple[int, bool]:
    """Resolve the level, and say whether the legacy variable supplied it.

    Order: ``verbose`` wins, then ``{PREFIX}_LOG_LEVEL``, then the legacy
    ``FASTMCP_LOG_LEVEL``, then ``INFO``. An unrecognised name falls back to
    ``INFO`` rather than raising — a typo in a log level should not stop a
    server from starting.
    """
    if verbose:
        return logging.DEBUG, False

    raw = env(env_prefix, "LOG_LEVEL")
    legacy = os.environ.get("FASTMCP_LOG_LEVEL")
    bridged = raw is None and legacy is not None
    name = (raw if raw is not None else legacy or "INFO").strip().upper()
    if name not in _VALID_LEVELS:
        name = "INFO"
    return getattr(logging, name, logging.INFO), bridged
```

`env(prefix, name)` is this repo's own helper (`._env`): it builds `{PREFIX}_{NAME}`, tolerates a trailing underscore on the prefix, strips whitespace, and returns `None` for an unset *or* empty value. Import it with `from ._env import env`. The legacy variable is read directly because it is not part of the server's prefixed surface.

Then in `configure_logging_from_env`:

```python
    level, bridged = _resolve_level(env_prefix, verbose=verbose)
    _neutralise_fastmcp()
    _install_root_handlers(level)
    # ... Task 3's policy goes here ...
    if bridged:
        # Emitted last, deliberately: the handlers that carry it are
        # installed above. A deprecation notice nobody can see is worse
        # than none, because it reads as if the migration were silent.
        logger.warning(
            "log_level_env_deprecated old=FASTMCP_LOG_LEVEL new=%s_LOG_LEVEL",
            env_prefix.rstrip("_"),
        )
```

Delete the `os.environ["FASTMCP_LOG_LEVEL"] = "DEBUG"` write on the verbose path — it existed only to align FastMCP's own loggers, which no longer render anything.

In `src/fastmcp_pvl_core/_cli.py:64`, change the help text to:

```python
        help="Enable DEBUG logging (overrides <PREFIX>_LOG_LEVEL)",
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_logging.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_logging.py src/fastmcp_pvl_core/_cli.py tests/test_logging.py
git commit -m "feat(logging)!: read the level from <PREFIX>_LOG_LEVEL

configure_logging_from_env now takes the server's env prefix as a required
first argument, and reads <PREFIX>_LOG_LEVEL. FASTMCP_LOG_LEVEL still works
for one major release and logs one deprecation warning naming the new
variable; an operator's compose file does not migrate via copier update.

BREAKING CHANGE: configure_logging_from_env requires an env_prefix
argument, and FASTMCP_LOG_LEVEL is superseded by <PREFIX>_LOG_LEVEL.

Refs #327"
```

---

### Task 3: Level and filter policy on the unified tree

**Files:**
- Modify: `src/fastmcp_pvl_core/_logging.py`
- Test: `tests/test_logging.py`

**Interfaces:**
- Consumes: `configure_logging_from_env` from Tasks 1-2.
- Produces: `_AccessLogFilter` (a `logging.Filter`), and the policy applied inside `configure_logging_from_env`.

- [ ] **Step 1: Write the failing tests**

```python
def _access_record(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="_",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:5678", method, path, "1.1", status),
        exc_info=None,
    )


def _access_filter():
    configure_logging_from_env("TEST_MCP")
    access = logging.getLogger("uvicorn.access")
    return [f for f in access.filters if type(f).__name__ == "_AccessLogFilter"]


@pytest.mark.parametrize(
    ("status", "kept"),
    [(200, False), (204, False), (302, False), (400, True), (401, True),
     (404, True), (413, True), (500, True), (503, True)],
)
def test_access_filter_keeps_only_failures(status, kept):
    (log_filter,) = _access_filter()
    assert log_filter.filter(_access_record("GET", "/mcp", status)) is kept


def test_access_filter_strips_the_query_string():
    (log_filter,) = _access_filter()
    record = _access_record("GET", "/authorize?code=SECRET&state=xyz", 401)
    assert log_filter.filter(record) is True
    assert "SECRET" not in record.getMessage()
    assert "/authorize" in record.getMessage()


def test_access_filter_redacts_the_transfer_token():
    (log_filter,) = _access_filter()
    record = _access_record("GET", "/transfer/tok_abc123", 404)
    assert log_filter.filter(record) is True
    assert "tok_abc123" not in record.getMessage()
    assert "transfer/<redacted>" in record.getMessage()


def test_access_filter_passes_records_of_another_shape():
    (log_filter,) = _access_filter()
    other = logging.LogRecord("uvicorn.access", logging.INFO, "_", 0, "startup", None, None)
    assert log_filter.filter(other) is True


def test_access_filter_is_absent_at_debug():
    configure_logging_from_env("TEST_MCP", verbose=True)
    access = logging.getLogger("uvicorn.access")
    assert [f for f in access.filters if type(f).__name__ == "_AccessLogFilter"] == []


def test_repeated_calls_leave_one_access_filter():
    configure_logging_from_env("TEST_MCP")
    configure_logging_from_env("TEST_MCP")
    assert len(_access_filter()) == 1


def test_debug_then_info_leaves_no_residue():
    configure_logging_from_env("TEST_MCP", verbose=True)
    configure_logging_from_env("TEST_MCP")
    assert len(_access_filter()) == 1


@pytest.mark.parametrize("name", ["mcp.server.lowlevel.server", "httpx", "httpcore"])
def test_noisy_loggers_demoted_at_info(name, monkeypatch):
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger(name).level == logging.WARNING


@pytest.mark.parametrize("name", ["mcp.server.lowlevel.server", "httpx", "httpcore"])
def test_noisy_loggers_restored_at_debug(name):
    configure_logging_from_env("TEST_MCP", verbose=True)
    assert logging.getLogger(name).level == logging.NOTSET


def test_uvicorn_error_is_never_demoted():
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("uvicorn.error").level == logging.NOTSET


def test_access_logger_level_is_pinned_to_info():
    """Pinned rather than demoted, so the outcome is the same whether or
    not uvicorn's own dictConfig has run."""
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("uvicorn.access").level == logging.INFO


def test_docket_worker_capped_at_debug():
    configure_logging_from_env("TEST_MCP", verbose=True)
    assert logging.getLogger("docket.worker").level == logging.INFO
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_logging.py -q`
Expected: failures — there is no `_AccessLogFilter`, and `httpx`/`httpcore` are untouched.

- [ ] **Step 3: Implement the policy**

Replace `_NOISY_THIRD_PARTY_LOGGERS` with the split constants and add the filter:

```python
_NOISY_THIRD_PARTY_LOGGERS = (
    "mcp.server.lowlevel.server",
    "httpx",
    "httpcore",
)
"""Loggers that are loud at ``INFO`` and quiet at ``WARNING``.

``httpx``/``httpcore`` are here because pvl-core pulls them for the whole
family through fastmcp, exactly like ``docket.worker`` below — the same
position, so the same owner. Three downstream servers had each quieted them
locally, two of them by contradictory rules that fought inside one process.

``uvicorn.access`` is deliberately absent: a level cannot express "failures
only", so it gets a filter instead.
"""

_ACCESS_LOGGER = "uvicorn.access"

_QUERY_RE = re.compile(r"[?#]")
_TRANSFER_TOKEN_RE = re.compile(r"(^|/)transfer/[^/]+")

_ACCESS_ARG_COUNT = 5
_ACCESS_STATUS_INDEX = 4
_ACCESS_PATH_INDEX = 2


class _AccessLogFilter(logging.Filter):
    """Keep failing requests only, and never log a credential.

    uvicorn emits every access record at ``INFO`` regardless of status, so
    at the default level the successful ones are pure duplication: the
    request-logging middleware already reports each MCP call with its method
    and duration. What the middleware cannot report is what never reached it
    — a 401 refused by auth, a 404, a 413 — so those stay, and a readiness
    probe becomes visible exactly when it starts failing.

    Both redactions are about credentials in the request line, which uvicorn
    logs as ``path?query``:

    * the query string goes entirely. No route in this family carries
      diagnostic query parameters; the ones that carry anything are the
      OAuth routes, where it is an authorization code or PKCE material.
    * the segment after ``/transfer/`` is masked, because the transfer token
      is in the *path* — an expired link produces exactly the 4xx this
      filter keeps.

    A record of any other shape passes untouched: this filter judges
    uvicorn's access line, and anything else on that logger is not its
    business.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not (
            isinstance(args, tuple)
            and len(args) == _ACCESS_ARG_COUNT
            and isinstance(args[_ACCESS_STATUS_INDEX], int)
        ):
            return True
        if args[_ACCESS_STATUS_INDEX] < 400:
            return False
        path = _QUERY_RE.split(str(args[_ACCESS_PATH_INDEX]), maxsplit=1)[0]
        path = _TRANSFER_TOKEN_RE.sub(r"\1transfer/<redacted>", path)
        record.args = args[:_ACCESS_PATH_INDEX] + (path,) + args[_ACCESS_PATH_INDEX + 1 :]
        return True


def _apply_access_policy(level: int) -> None:
    """Install or remove the access filter, leaving exactly one either way."""
    access = logging.getLogger(_ACCESS_LOGGER)
    for existing in [f for f in access.filters if isinstance(f, _AccessLogFilter)]:
        access.removeFilter(existing)
    # Pinned, not demoted: uvicorn's own dictConfig sets this logger to INFO
    # at server start, so anything else here would mean the policy depended
    # on whether that had run yet.
    access.setLevel(logging.INFO)
    if level != logging.DEBUG:
        access.addFilter(_AccessLogFilter())
```

Call `_apply_access_policy(level)` from `configure_logging_from_env` alongside the existing noisy/flood loops, before the bridge warning.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_logging.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_logging.py tests/test_logging.py
git commit -m "feat(logging): keep only failing access lines, and redact them

Refs #327"
```

---

### Task 4: Documentation

**Files:**
- Modify: `README.md` (the `### Logging` section, from line 253)
- Modify: `src/fastmcp_pvl_core/_logging.py` (module and function docstrings)

**Interfaces:**
- Consumes: the behaviour of Tasks 1-3.
- Produces: no code.

- [ ] **Step 1: Rewrite the README's `### Logging` section**

It currently describes `FASTMCP_LOG_LEVEL` and a demotion of `uvicorn.access` to `WARNING` — both untrue after this PR. Rewrite it to cover, in this order: that pvl-core owns the root logger and every namespace propagates into it; `<PREFIX>_LOG_LEVEL` with the `FASTMCP_LOG_LEVEL` bridge and its one-release lifetime; which loggers are quieted and which are never touched; the access-log policy including both redactions; and that an OTLP or file handler attached at root survives and now receives `fastmcp.*` too.

Keep the existing `docket.worker` paragraph — it is still accurate.

Do **not** describe JSON output, `<PREFIX>_LOG_FORMAT`, or TTY detection. They do not exist until PR 4, and documenting them early is how a README starts lying.

- [ ] **Step 2: Update the module docstring**

`_logging.py`'s docstring still says the module "delegates to FastMCP's `configure_logging`". It now does the opposite — it turns that function off. Rewrite it to state: pvl-core owns root; both libraries are disabled at their own switches; what the level order is; and that rendering is Rich until PR 4.

- [ ] **Step 3: Update `configure_logging_from_env`'s docstring**

Cover the new signature, the level order including the bridge, the three handler-ownership invariants from §1 of the spec, and the policy table. Classify `env_prefix` and `verbose` in the `Args:` section per CLAUDE.md's rule that each kwarg's category is documented.

- [ ] **Step 4: Verify the docs against the code**

Read the finished README section and docstrings beside the implementation and check each claim. Run any example you write. An inaccurate doc claim in this file is worse than a missing one, because the previous version's inaccuracy is exactly what this PR exists to fix.

- [ ] **Step 5: Commit**

```bash
git add README.md src/fastmcp_pvl_core/_logging.py
git commit -m "docs(logging): describe root ownership and the new env contract

Refs #327"
```

---

### Task 5: Full local gate

**Files:** none modified unless a check fails.

- [ ] **Step 1: Match CI's dependency state**

Run: `uv sync --all-extras`

- [ ] **Step 2: Run the whole suite**

Run: `uv run pytest`
Expected: PASS. Any failure outside `tests/test_logging.py` means this PR changed behaviour another module depended on — report it rather than patching the other module.

- [ ] **Step 3: Format, lint, types**

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

- [ ] **Step 4: Both ends of the CI matrix**

```bash
uv run --python 3.10 pytest tests/test_logging.py -q
uv run --python 3.13 pytest tests/test_logging.py -q
```

- [ ] **Step 5: Prove the topology end to end**

```bash
uv run python -c "
import logging
from fastmcp_pvl_core import configure_logging_from_env
configure_logging_from_env('DEMO_MCP')
logging.getLogger('demo.domain').warning('domain_line key=%s', 'v')
logging.getLogger('fastmcp.middleware.requests').warning('middleware_line key=%s', 'v')
logging.getLogger('uvicorn.error').warning('uvicorn_line key=%s', 'v')
" 2>&1
```
Expected: three lines, one format, all on stderr. Before this PR they would have arrived in three different formats from three different chains.

- [ ] **Step 6: The health gate, BEFORE pushing**

This repo's Repowise check fails a fully-AI PR on any net introduced finding. Ask the controller to run `get_change_risk` over `main..HEAD` and confirm `health_delta.introduced == 0`. It is an MCP tool, not a CLI — do not try to run `repowise review`. In a linked worktree it needs explicit SHAs, or it compares a commit against itself.

If findings appear, fix them before the push, preferring extraction of named helpers over inlining.

- [ ] **Step 7: Commit any fixes**

```bash
git add -u && git commit -m "chore: satisfy the health gate

Refs #327"
```
(Skip if the tree is clean.)

---

## Self-review notes

- **Spec coverage.** §1 → Task 1 (all three invariants, plus the traceback pair). §6 → Task 2 (prefix, bridge, deletions). §4 → Task 3 (the four policy rows, both redactions). §3 and §2 are out of scope by the spec's own staging and appear in no task.
- **Deliberate omission.** The spec's §4 table lists `uvicorn.access` with a filter and the rest with levels; Task 3 pins the access logger to `INFO` rather than demoting it, which §4's prose requires and the table implies.
- **Type consistency.** `_resolve_level` returns `tuple[int, bool]` in Task 2 and is unpacked as `level, bridged` there. `_install_root_handlers(level: int)` and `_apply_access_policy(level: int)` take the same resolved `int`. `_OWNED_ATTR`'s value `"_pvl_core_owned"` is the literal the Task 1 tests assert with `getattr`.
- **Fixed during self-review.** Task 2's `_resolve_level` originally showed two ways of building the env key and would not have compiled as written; it now uses the repo's `env()` helper alone.
