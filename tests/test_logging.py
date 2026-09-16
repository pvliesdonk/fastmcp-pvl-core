"""Tests for configure_logging_from_env and SecretMaskFilter."""

from __future__ import annotations

import logging
import os

import fastmcp
import pytest

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


def test_noisy_loggers_demoted_to_warning_at_info(monkeypatch):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
    assert logging.getLogger("mcp.server.lowlevel.server").level == logging.WARNING


def test_noisy_loggers_notset_at_debug(monkeypatch):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("uvicorn.access").level == logging.NOTSET
    assert logging.getLogger("mcp.server.lowlevel.server").level == logging.NOTSET


def test_noisy_loggers_notset_at_debug_via_verbose(monkeypatch):
    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    configure_logging_from_env("TEST_MCP", verbose=True)
    assert logging.getLogger("uvicorn.access").level == logging.NOTSET
    assert logging.getLogger("mcp.server.lowlevel.server").level == logging.NOTSET


def test_uvicorn_error_logger_untouched(monkeypatch):
    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    configure_logging_from_env("TEST_MCP")
    assert logging.getLogger("uvicorn.error").level == logging.INFO


def test_demotion_idempotent_across_level_flips(monkeypatch):
    access = logging.getLogger("uvicorn.access")

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    assert access.level == logging.NOTSET

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "INFO")
    configure_logging_from_env("TEST_MCP")
    assert access.level == logging.WARNING

    monkeypatch.setenv("TEST_MCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env("TEST_MCP")
    assert access.level == logging.NOTSET


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
