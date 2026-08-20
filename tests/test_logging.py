"""Tests for configure_logging_from_env and SecretMaskFilter."""

from __future__ import annotations

import logging

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
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env(verbose=True)
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG


def test_verbose_sets_fastmcp_log_level_env(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env(verbose=True)
    import os

    assert os.environ.get("FASTMCP_LOG_LEVEL") == "DEBUG"


def test_respects_fastmcp_log_level(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "WARNING")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_defaults_to_info_when_nothing_set(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env(verbose=False)
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_lowercase_level_name_handled(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "warning")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_unknown_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "BOGUS")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


_NOISY_LOGGER_NAMES = (
    "uvicorn.access",
    "mcp.server.lowlevel.server",
    "uvicorn.error",
    "docket.worker",
)


@pytest.fixture(autouse=True)
def _restore_noisy_levels():
    """Save and restore the levels of every logger the demotion logic touches.

    Applied module-wide via ``autouse`` because ``configure_logging_from_env``
    mutates these process-global loggers as a side effect — every test that
    calls it (not just the demotion tests) would otherwise leak logger state
    into later test modules.
    """
    saved = {name: logging.getLogger(name).level for name in _NOISY_LOGGER_NAMES}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_noisy_loggers_demoted_to_warning_at_info(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "INFO")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
    assert logging.getLogger("mcp.server.lowlevel.server").level == logging.WARNING


def test_noisy_loggers_notset_at_debug(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger("uvicorn.access").level == logging.NOTSET
    assert logging.getLogger("mcp.server.lowlevel.server").level == logging.NOTSET


def test_noisy_loggers_notset_at_debug_via_verbose(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env(verbose=True)
    assert logging.getLogger("uvicorn.access").level == logging.NOTSET
    assert logging.getLogger("mcp.server.lowlevel.server").level == logging.NOTSET


def test_uvicorn_error_logger_untouched(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "INFO")
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    configure_logging_from_env(verbose=False)
    assert logging.getLogger("uvicorn.error").level == logging.INFO


def test_demotion_idempotent_across_level_flips(monkeypatch):
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


def test_debug_flood_logger_capped_at_info_at_debug(monkeypatch):
    # docket.worker's poll loop emits a DEBUG record per iteration on an
    # idle queue; capping it at INFO keeps DEBUG readable for first-party
    # diagnostics while leaving its lifecycle records in place.
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger("docket.worker").level == logging.INFO


def test_debug_flood_logger_capped_at_info_via_verbose(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env(verbose=True)
    assert logging.getLogger("docket.worker").level == logging.INFO


def test_debug_flood_logger_untouched_below_debug(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "INFO")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger("docket.worker").level == logging.NOTSET


def test_debug_flood_logger_untouched_above_info(monkeypatch):
    # The cap must never *raise* the effective level: at WARNING the logger
    # stays on inheritance so warnings and errors still pass through.
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "WARNING")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger("docket.worker").level == logging.NOTSET


def test_debug_flood_cap_idempotent_across_level_flips(monkeypatch):
    worker = logging.getLogger("docket.worker")

    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env(verbose=False)
    assert worker.level == logging.INFO

    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "INFO")
    configure_logging_from_env(verbose=False)
    assert worker.level == logging.NOTSET

    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env(verbose=False)
    assert worker.level == logging.INFO


def test_debug_flood_logger_filters_poll_records_at_debug(monkeypatch, caplog):
    # Behavioural check, not just a level assertion: at root DEBUG the poll
    # trace is dropped while the worker's INFO records survive.
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env(verbose=False)
    worker = logging.getLogger("docket.worker")

    # at_level() must target the root logger, not docket.worker: naming the
    # logger would reset the very cap under test.
    with caplog.at_level(logging.DEBUG):
        worker.debug("Getting new deliveries")
        worker.info("Starting worker")

    assert [record.getMessage() for record in caplog.records] == ["Starting worker"]


def test_debug_flood_cap_leaves_first_party_debug_intact(monkeypatch, caplog):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    configure_logging_from_env(verbose=False)

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
