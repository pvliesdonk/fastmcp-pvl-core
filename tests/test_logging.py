"""Tests for configure_logging_from_env and SecretMaskFilter."""

from __future__ import annotations

import io
import logging
import os

import fastmcp
import pytest
import uvicorn
import uvicorn.config
from rich.logging import RichHandler

from fastmcp_pvl_core import SecretMaskFilter, configure_logging_from_env


def _record(msg: str, args: tuple[object, ...] | None = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="x",
        level=logging.DEBUG,
        pathname="_",
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_sets_debug_when_verbose_true(monkeypatch):
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env("TEST_MCP", verbose=True)
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG


def test_respects_prefixed_level(monkeypatch):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "WARNING")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_defaults_to_info_when_nothing_set(monkeypatch):
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_lowercase_level_name_handled(monkeypatch):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "warning")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_unknown_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "BOGUS")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def _bridge_warnings(caplog) -> list[logging.LogRecord]:
    """The deprecation records naming the legacy variable.

    Shared because five tests ask the same question of ``caplog`` — how many
    times, and at what severity, an operator was told the old variable is
    going away.
    """
    return [r for r in caplog.records if "FASTMCP_LOG_LEVEL" in r.getMessage()]


def test_bridges_fastmcp_log_level_with_one_warning(monkeypatch, caplog):
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "WARNING")
    with caplog.at_level(logging.WARNING):
        configure_logging_from_env("TEST_MCP")
        assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    (warning,) = _bridge_warnings(caplog)
    assert "TEST_MCP_LOG_LEVEL" in warning.getMessage()


def test_bridges_fastmcp_log_level_at_error_severity(monkeypatch, caplog):
    # Regression: a flat logger.warning(...) is silently dropped by an
    # operator's own ERROR/CRITICAL level (WARNING < ERROR), so the
    # deprecation notice never reaches them. It must be logged at whatever
    # severity is at least as high as the level the operator chose.
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "ERROR")
    with caplog.at_level(logging.ERROR):
        configure_logging_from_env("TEST_MCP")
        assert logging.getLogger().getEffectiveLevel() == logging.ERROR
    (warning,) = _bridge_warnings(caplog)
    assert warning.levelno == logging.ERROR


def test_empty_legacy_log_level_is_not_bridged(monkeypatch, caplog):
    # env() treats an empty prefixed value as unset; the legacy variable
    # must be read the same way, or FASTMCP_LOG_LEVEL="" fires a spurious
    # deprecation warning for a variable that carries no value.
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "")
    with caplog.at_level(logging.WARNING):
        configure_logging_from_env("TEST_MCP")
        assert logging.getLogger().getEffectiveLevel() == logging.INFO
    assert _bridge_warnings(caplog) == []


def test_prefixed_level_wins_and_is_silent(monkeypatch, caplog):
    # The effective-level assertion must live inside the `with` block:
    # caplog.at_level() restores the root logger's *pre-with* level on
    # exit, which would otherwise mask the level configure_logging_from_env
    # actually installed. caplog.records is unaffected by that restore, so
    # the warning-absence check is fine outside.
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    with caplog.at_level(logging.WARNING):
        configure_logging_from_env("TEST_MCP")
        assert logging.getLogger().getEffectiveLevel() == logging.ERROR
    assert _bridge_warnings(caplog) == []


def test_verbose_overrides_both_and_is_silent(monkeypatch, caplog):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "ERROR")
    with caplog.at_level(logging.WARNING):
        configure_logging_from_env("TEST_MCP", verbose=True)
        assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    assert _bridge_warnings(caplog) == []


def test_verbose_no_longer_writes_the_fastmcp_env_var(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env("TEST_MCP", verbose=True)
    assert os.environ.get("FASTMCP_LOG_LEVEL") is None


def test_env_prefix_is_required():
    with pytest.raises(TypeError):
        configure_logging_from_env()  # type: ignore[call-arg]


_MANAGED_LOGGERS = (
    "fastmcp",
    "uvicorn",
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


def test_uvicorn_error_logger_untouched(monkeypatch):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("uvicorn.error").level == logging.INFO


def test_demotion_idempotent_across_level_flips(monkeypatch):
    # uvicorn.access is left at NOTSET and no longer demoted (it gets the
    # failures-only filter instead — see test_access_filter_* below), so
    # this idempotency check exercises a logger that is still demoted.
    demoted = logging.getLogger("mcp.server.lowlevel.server")

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    assert demoted.level == logging.NOTSET

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
    configure_logging_from_env("TEST_MCP")
    assert demoted.level == logging.WARNING

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    assert demoted.level == logging.NOTSET


def test_debug_flood_logger_capped_at_info_at_debug(monkeypatch):
    # docket.worker's poll loop emits a DEBUG record per iteration on an
    # idle queue; capping it at INFO keeps DEBUG readable for first-party
    # diagnostics while leaving its lifecycle records in place.
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("docket.worker").level == logging.INFO


def test_debug_flood_logger_capped_at_info_via_verbose(monkeypatch):
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    configure_logging_from_env("TEST_MCP", verbose=True)
    assert logging.getLogger("docket.worker").level == logging.INFO


def test_debug_flood_logger_untouched_below_debug(monkeypatch):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("docket.worker").level == logging.NOTSET


def test_debug_flood_logger_untouched_above_info(monkeypatch):
    # The cap must never *raise* the effective level: at WARNING the logger
    # stays on inheritance so warnings and errors still pass through.
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "WARNING")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("docket.worker").level == logging.NOTSET


def test_debug_flood_cap_idempotent_across_level_flips(monkeypatch):
    worker = logging.getLogger("docket.worker")

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    assert worker.level == logging.INFO

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
    configure_logging_from_env("TEST_MCP")
    assert worker.level == logging.NOTSET

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    assert worker.level == logging.INFO


def test_debug_flood_logger_filters_poll_records_at_debug(monkeypatch, caplog):
    # Behavioural check, not just a level assertion: at root DEBUG the poll
    # trace is dropped while the worker's INFO records survive.
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    worker = logging.getLogger("docket.worker")

    # at_level() must target the root logger, not docket.worker: naming the
    # logger would reset the very cap under test.
    with caplog.at_level(logging.DEBUG):
        worker.debug("Getting new deliveries")
        worker.info("Starting worker")

    assert [record.getMessage() for record in caplog.records] == ["Starting worker"]


def test_debug_flood_cap_leaves_first_party_debug_intact(monkeypatch, caplog):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("fastmcp_pvl_core._auth").debug("token_validated")

    assert [record.getMessage() for record in caplog.records] == ["token_validated"]


class TestSecretMaskFilter:
    def test_masks_token_header(self):
        record = _record("Authorization: Token abcdef1234567890")

        SecretMaskFilter().filter(record)

        assert "abcdef1234567890" not in record.getMessage()
        assert "Token ***" in record.getMessage()

    def test_masks_bearer_header(self):
        record = _record("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload")

        SecretMaskFilter().filter(record)

        assert "eyJ" not in record.getMessage()
        assert "Bearer ***" in record.getMessage()

    def test_masks_basic_header(self):
        # Basic auth carries base64-encoded user:password — same exposure
        # risk as Bearer/Token, so it gets the same redaction.
        record = _record("Authorization: Basic dXNlcjpwYXNzd29yZA==")

        SecretMaskFilter().filter(record)

        assert "dXNlcjpwYXNzd29yZA" not in record.getMessage()
        assert "Basic ***" in record.getMessage()

    def test_masks_dict_repr_token(self):
        record = _record("headers={'Authorization': 'Token abcdef1234567890'}")

        SecretMaskFilter().filter(record)

        assert "abcdef" not in record.getMessage()
        assert "Token ***" in record.getMessage()
        # Surrounding dict structure is preserved.
        assert record.getMessage().endswith("'}")

    def test_masks_dict_repr_bearer(self):
        record = _record('headers={"Authorization": "Bearer eyJhbGciOi"}')

        SecretMaskFilter().filter(record)

        assert "eyJhbGciOi" not in record.getMessage()
        assert "Bearer ***" in record.getMessage()

    def test_case_insensitive(self):
        record = _record("authorization: bearer eyJhbGciOi")

        SecretMaskFilter().filter(record)

        assert "eyJhbGciOi" not in record.getMessage()
        # Original casing of the scheme name is preserved by the substitution.
        assert "bearer ***" in record.getMessage()

    def test_passes_unrelated_messages_through(self):
        record = _record("plain debug message with no secrets")

        SecretMaskFilter().filter(record)

        assert record.getMessage() == "plain debug message with no secrets"

    def test_returns_true_for_unrelated(self):
        # A logging filter that returns False suppresses the record. This
        # filter is a redactor, not a gatekeeper — must always return True.
        record = _record("plain message")

        assert SecretMaskFilter().filter(record) is True

    def test_returns_true_when_masking(self):
        record = _record("Authorization: Token abc")

        assert SecretMaskFilter().filter(record) is True

    def test_handles_format_args(self):
        # When the record uses %-formatting, the secret only appears after
        # getMessage() expands args. The filter must mask the formatted form
        # and clear args so subsequent getMessage() calls return the masked
        # text rather than re-expanding the original args.
        record = _record(
            "request headers=%s",
            ({"Authorization": "Token abcdef1234567890"},),
        )

        SecretMaskFilter().filter(record)

        msg = record.getMessage()
        assert "abcdef" not in msg
        assert "Token ***" in msg
        # Calling getMessage() again must not re-introduce the secret.
        assert "abcdef" not in record.getMessage()

    def test_masks_multiple_occurrences(self):
        record = _record(
            "in=Authorization: Token aaa111 / out=Authorization: Bearer bbb222"
        )

        SecretMaskFilter().filter(record)

        msg = record.getMessage()
        assert "aaa111" not in msg
        assert "bbb222" not in msg
        assert msg.count("***") == 2

    def test_does_not_match_other_header_keys(self):
        # Only the Authorization keyword triggers masking; tokens that
        # happen to live in other headers (e.g. API keys with their own key
        # names) are out of scope for this filter.
        record = _record("X-Api-Key: Token abcdef1234567890")

        SecretMaskFilter().filter(record)

        assert "abcdef1234567890" in record.getMessage()

    def test_does_not_match_bare_scheme_word(self):
        # "Bearer" or "Token" without an Authorization prefix is just a
        # word — do not mutate it.
        record = _record("Token holder is logged in")

        SecretMaskFilter().filter(record)

        assert record.getMessage() == "Token holder is logged in"

    def test_masks_equals_separator(self):
        # The regex permits "=" as a key/value separator alongside ":";
        # exercise that path so the alternation isn't dead code.
        record = _record("Authorization=Token abcdef1234567890")

        SecretMaskFilter().filter(record)

        assert "abcdef1234567890" not in record.getMessage()
        assert "Token ***" in record.getMessage()

    def test_handles_broken_format_string(self):
        # If the producer logged a format string with mismatched args,
        # getMessage() raises TypeError during %-formatting. The filter
        # must catch that and still return True — a broken log line is
        # preferable to silencing the entire log stream.
        record = _record("only one placeholder %s", ("a", "b", "c"))

        assert SecretMaskFilter().filter(record) is True


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


def test_replaces_a_pre_existing_foreign_rich_handler():
    # Exercises the console.file fallback in _is_console_handler: a foreign
    # RichHandler() (not one of ours, so unmarked by _OWNED_ATTR) still
    # writes to a console stream via console.file, not .stream — the type
    # check alone would miss it and leave two Rich chains at root.
    root = logging.getLogger()
    foreign = RichHandler()  # defaults to Console() over sys.stdout
    root.addHandler(foreign)
    configure_logging_from_env("TEST_MCP")
    assert foreign not in root.handlers


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
    # Must fail in both directions: stdout must stay empty, and the record
    # must actually reach stderr — a handler pair that silently dropped
    # every record would satisfy the stdout-only half of this check too.
    configure_logging_from_env("TEST_MCP")
    logging.getLogger("some.domain.module").warning("stderr only")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stderr only" in captured.err


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


def _access_filter(monkeypatch):
    # Mirrors the delenv sibling tests do: without it, a developer with
    # TEST_MCP_LOG_LEVEL=DEBUG exported gets no filter installed and every
    # `(log_filter,) = _access_filter(...)` unpack fails with a confusing
    # ValueError instead of a clear assertion.
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env("TEST_MCP")
    access = logging.getLogger("uvicorn.access")
    return [f for f in access.filters if type(f).__name__ == "_AccessLogFilter"]


@pytest.mark.parametrize(
    ("status", "kept"),
    [
        (200, False),
        (204, False),
        (302, False),
        (400, True),
        (401, True),
        (404, True),
        (413, True),
        (500, True),
        (503, True),
    ],
)
def test_access_filter_keeps_only_failures(status, kept, monkeypatch):
    (log_filter,) = _access_filter(monkeypatch)
    assert log_filter.filter(_access_record("GET", "/mcp", status)) is kept


def test_access_filter_strips_the_query_string(monkeypatch):
    (log_filter,) = _access_filter(monkeypatch)
    record = _access_record("GET", "/authorize?code=SECRET&state=xyz", 401)
    assert log_filter.filter(record) is True
    assert "SECRET" not in record.getMessage()
    assert "/authorize" in record.getMessage()


def test_access_filter_redacts_the_transfer_token(monkeypatch):
    (log_filter,) = _access_filter(monkeypatch)
    record = _access_record("GET", "/transfer/tok_abc123", 404)
    assert log_filter.filter(record) is True
    assert "tok_abc123" not in record.getMessage()
    assert "transfer/<redacted>" in record.getMessage()


def test_access_filter_redacts_the_transfer_token_case_insensitively(monkeypatch):
    # Starlette routes case-sensitively, so "/TRANSFER/..." 404s — a status
    # this filter always keeps — and without a case-insensitive match the
    # token would reach the log unredacted precisely because the request
    # failed to route.
    (log_filter,) = _access_filter(monkeypatch)
    record = _access_record("GET", "/TRANSFER/tok_SECRET", 404)
    assert log_filter.filter(record) is True
    assert "tok_SECRET" not in record.getMessage()
    assert "transfer/<redacted>" in record.getMessage()


def test_access_filter_passes_records_of_another_shape(monkeypatch):
    (log_filter,) = _access_filter(monkeypatch)
    other = logging.LogRecord(
        "uvicorn.access", logging.INFO, "_", 0, "startup", None, None
    )
    assert log_filter.filter(other) is True


def test_access_filter_present_and_keeps_success_at_debug():
    # Redaction must never be a side effect of verbosity: the filter stays
    # installed at DEBUG, it just stops dropping 2xx/3xx records.
    configure_logging_from_env("TEST_MCP", verbose=True)
    access = logging.getLogger("uvicorn.access")
    (log_filter,) = [
        f for f in access.filters if type(f).__name__ == "_AccessLogFilter"
    ]
    assert log_filter.filter(_access_record("GET", "/mcp", 200)) is True


def test_access_lines_redacted_at_debug():
    # Live handler attached at root, not the filter's return value: proves
    # the secrets are actually gone from what a handler receives, for the
    # two cases that only exist at DEBUG — a *live* transfer link (2xx,
    # only visible once successes stop being dropped) and an OAuth
    # authorize redirect.
    configure_logging_from_env("TEST_MCP", verbose=True)
    access = logging.getLogger("uvicorn.access")
    handler = logging.Handler()
    received: list[logging.LogRecord] = []
    handler.emit = received.append  # type: ignore[method-assign]
    logging.getLogger().addHandler(handler)
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:5678",
            "GET",
            "/transfer/tok_SECRET123",
            "1.1",
            200,
        )
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:5678",
            "GET",
            "/authorize?code=AUTHCODE&state=x",
            "1.1",
            302,
        )
        assert len(received) == 2
        messages = [r.getMessage() for r in received]
        assert not any("SECRET" in m or "AUTHCODE" in m for m in messages)
        assert "transfer/<redacted>" in messages[0]
        assert "/authorize" in messages[1] and "?" not in messages[1]
    finally:
        logging.getLogger().removeHandler(handler)


def test_access_filter_rule_tracks_the_latest_call(monkeypatch):
    # Idempotence must not go stale on a flip: the surviving instance after
    # DEBUG must apply DEBUG's rule (keep 200s), and after flipping back to
    # INFO it must apply INFO's rule (drop 200s) — never a rule left over
    # from before the flip.
    access = logging.getLogger("uvicorn.access")

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
    configure_logging_from_env("TEST_MCP")
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    filters = [f for f in access.filters if type(f).__name__ == "_AccessLogFilter"]
    assert len(filters) == 1
    assert filters[0].filter(_access_record("GET", "/mcp", 200)) is True

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
    configure_logging_from_env("TEST_MCP")
    filters = [f for f in access.filters if type(f).__name__ == "_AccessLogFilter"]
    assert len(filters) == 1
    assert filters[0].filter(_access_record("GET", "/mcp", 200)) is False


def test_repeated_calls_leave_one_access_filter(monkeypatch):
    configure_logging_from_env("TEST_MCP")
    configure_logging_from_env("TEST_MCP")
    assert len(_access_filter(monkeypatch)) == 1


def test_debug_then_info_leaves_no_residue(monkeypatch):
    configure_logging_from_env("TEST_MCP", verbose=True)
    configure_logging_from_env("TEST_MCP")
    assert len(_access_filter(monkeypatch)) == 1


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


@pytest.mark.parametrize("name", ["mcp.server.lowlevel.server", "httpx", "httpcore"])
def test_noisy_loggers_never_louder_than_operator_level(name, monkeypatch):
    # Regression: a flat WARNING demotion *raises* the effective level for
    # these loggers above ERROR, so they print WARNING records an operator
    # who chose ERROR explicitly asked not to see — louder than the
    # server's own first-party code, which correctly stays silent.
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "ERROR")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger(name).level == logging.ERROR


def test_uvicorn_error_is_never_demoted():
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("uvicorn.error").level == logging.NOTSET


def test_access_logger_level_is_notset():
    """Left at NOTSET rather than pinned: the filter decides which requests
    are worth a line, the level decides whether the operator wants request
    lines at all, and the level answers its own question by inheritance."""
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("uvicorn.access").level == logging.NOTSET


def test_docket_worker_capped_at_debug():
    configure_logging_from_env("TEST_MCP", verbose=True)
    assert logging.getLogger("docket.worker").level == logging.INFO


def test_access_line_dropped_by_level_before_the_filter_sees_it(monkeypatch):
    # End-to-end check with a real handler attached at root, not caplog, and
    # a real logging call (access.info(...)) rather than a hand-built record
    # pushed through .handle() — .handle() skips the isEnabledFor() level
    # check entirely, which is exactly the mechanism under test. uvicorn.access
    # is left at NOTSET so it inherits the root level: at WARNING that check
    # must drop even a kept-shape 404 before the failures-only filter ever
    # runs. The mirror at INFO confirms the same call does reach the handler.
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "WARNING")
    configure_logging_from_env("TEST_MCP")
    access = logging.getLogger("uvicorn.access")
    handler = logging.Handler()
    received: list[logging.LogRecord] = []
    handler.emit = received.append  # type: ignore[method-assign]
    logging.getLogger().addHandler(handler)
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5678", "GET", "/mcp", "1.1", 404
        )
        assert received == []

        monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
        configure_logging_from_env("TEST_MCP")
        access.info(
            '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5678", "GET", "/mcp", "1.1", 404
        )
        assert len(received) == 1
    finally:
        logging.getLogger().removeHandler(handler)


def test_uvicorn_dictconfig_still_runs_but_filter_and_redaction_survive():
    """The PR-1 gap: closed by PR 2's ``run_http(log_config=None)`` seam.

    uvicorn's own ``Config(...).configure_logging()`` still runs at HTTP
    server start in this release. It reinstalls uvicorn's own handler on
    ``uvicorn.access`` (a plain ``StreamHandler`` on **stdout**, level
    ``INFO``, ``propagate=False``) regardless of ``{PREFIX}_LOG_LEVEL`` —
    so "pvl-core owns the console" and "the level governs access lines" do
    not hold yet under HTTP transport. What survives is proven here:
    ``dictConfig`` replaces handlers, not filters, so ``_AccessLogFilter``
    is still attached to ``uvicorn.access`` afterwards and still redacts
    whatever uvicorn logs through its own reinstalled handler.
    """
    configure_logging_from_env("TEST_MCP")
    uvicorn.Config(None, log_config=uvicorn.config.LOGGING_CONFIG).configure_logging()

    access = logging.getLogger("uvicorn.access")

    # The gap: uvicorn reinstalled its own chain, not pvl-core's.
    assert access.propagate is False
    assert access.level == logging.INFO
    assert len(access.handlers) == 1
    assert getattr(access.handlers[0], "_pvl_core_owned", False) is False

    # What survives: our filter is still attached to the logger.
    filters = [f for f in access.filters if type(f).__name__ == "_AccessLogFilter"]
    assert len(filters) == 1

    # And it still redacts whatever reaches uvicorn's own handler.
    handler = access.handlers[0]
    original_stream = handler.stream
    buf = io.StringIO()
    handler.stream = buf
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:5678",
            "GET",
            "/transfer/tok_SECRET123",
            "1.1",
            404,
        )
    finally:
        handler.stream = original_stream
    line = buf.getvalue()
    assert "tok_SECRET123" not in line
    assert "transfer/<redacted>" in line
