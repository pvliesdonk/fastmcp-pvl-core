"""Tests for ``maybe_start_debugpy``."""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pytest

from fastmcp_pvl_core import _debug as debug_mod
from fastmcp_pvl_core import maybe_start_debugpy


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the helper's idempotency latch reset."""
    monkeypatch.setattr(debug_mod, "_started", False)
    monkeypatch.delenv("DEBUG_PORT", raising=False)
    monkeypatch.delenv("DEBUG_WAIT", raising=False)


def _install_fake_debugpy(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Inject a stub ``debugpy`` module recording listen / wait_for_client calls."""
    calls: list[tuple[str, Any]] = []

    def listen(addr: tuple[str, int]) -> tuple[str, int]:
        calls.append(("listen", addr))
        return addr

    def wait_for_client() -> None:
        calls.append(("wait", None))

    fake = types.ModuleType("debugpy")
    fake.listen = listen  # type: ignore[attr-defined]
    fake.wait_for_client = wait_for_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "debugpy", fake)
    return types.SimpleNamespace(calls=calls)


def test_no_op_when_debug_port_unset() -> None:
    # No env, no fake debugpy installed — must not raise.
    maybe_start_debugpy()


@pytest.mark.parametrize("raw", ["0", "00", "+0", "-0", " 0 "])
def test_no_op_when_debug_port_parses_to_zero(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # All forms that parse to 0 are the documented "disable" form and
    # must be silent — not the out-of-range WARNING that "70000" triggers.
    monkeypatch.setenv("DEBUG_PORT", raw)
    fake = _install_fake_debugpy(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger="fastmcp_pvl_core._debug"):
        maybe_start_debugpy()

    assert fake.calls == []
    assert caplog.records == [], (
        f"DEBUG_PORT={raw!r} should be a silent no-op, but logged: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_no_op_when_debug_port_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG_PORT", "   ")
    fake = _install_fake_debugpy(monkeypatch)

    maybe_start_debugpy()

    assert fake.calls == []


def test_invalid_port_logs_warning_and_no_ops(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DEBUG_PORT", "not-a-number")
    fake = _install_fake_debugpy(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._debug"):
        maybe_start_debugpy()

    assert fake.calls == []
    assert any(
        "DEBUG_PORT" in rec.message and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_out_of_range_port_logs_warning_and_no_ops(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DEBUG_PORT", "70000")
    fake = _install_fake_debugpy(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._debug"):
        maybe_start_debugpy()

    assert fake.calls == []
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_negative_port_logs_warning_and_no_ops(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DEBUG_PORT", "-1")
    fake = _install_fake_debugpy(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._debug"):
        maybe_start_debugpy()

    assert fake.calls == []
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_debugpy_missing_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DEBUG_PORT", "5678")
    # Force ImportError when the helper tries to import debugpy.
    monkeypatch.setitem(sys.modules, "debugpy", None)

    with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._debug"):
        maybe_start_debugpy()

    msgs = " ".join(rec.message for rec in caplog.records)
    assert "debugpy" in msgs
    # The hint should point at the install path so the operator can act on it.
    assert "extra" in msgs.lower() or "install" in msgs.lower()


def test_happy_path_calls_listen(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DEBUG_PORT", "5678")
    fake = _install_fake_debugpy(monkeypatch)

    with caplog.at_level(logging.INFO, logger="fastmcp_pvl_core._debug"):
        maybe_start_debugpy()

    assert fake.calls == [("listen", ("0.0.0.0", 5678))]
    assert any(
        rec.levelno == logging.INFO and "5678" in rec.message for rec in caplog.records
    )


def test_debug_wait_triggers_wait_for_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEBUG_PORT", "5678")
    monkeypatch.setenv("DEBUG_WAIT", "true")
    fake = _install_fake_debugpy(monkeypatch)

    maybe_start_debugpy()

    assert ("listen", ("0.0.0.0", 5678)) in fake.calls
    assert ("wait", None) in fake.calls
    # listen must come before wait_for_client.
    assert fake.calls.index(("listen", ("0.0.0.0", 5678))) < fake.calls.index(
        ("wait", None)
    )


def test_debug_wait_false_skips_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG_PORT", "5678")
    monkeypatch.setenv("DEBUG_WAIT", "false")
    fake = _install_fake_debugpy(monkeypatch)

    maybe_start_debugpy()

    assert ("listen", ("0.0.0.0", 5678)) in fake.calls
    assert ("wait", None) not in fake.calls


def test_idempotent_on_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG_PORT", "5678")
    fake = _install_fake_debugpy(monkeypatch)

    maybe_start_debugpy()
    maybe_start_debugpy()

    # listen runs exactly once even though the helper was called twice.
    assert [c for c in fake.calls if c[0] == "listen"] == [
        ("listen", ("0.0.0.0", 5678))
    ]


@pytest.mark.parametrize(
    "exc",
    [
        OSError("address in use"),
        PermissionError("port requires CAP_NET_BIND_SERVICE"),
        # The broad except is documented as deliberate — non-OSError
        # failures (e.g. debugpy-internal RuntimeError on partial
        # installs) must also be absorbed rather than crashing startup.
        RuntimeError("debugpy internal error"),
    ],
)
def test_listen_failure_logs_warning_and_continues(
    exc: Exception,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DEBUG_PORT", "5678")

    def boom(_: tuple[str, int]) -> None:
        raise exc

    fake = types.ModuleType("debugpy")
    fake.listen = boom  # type: ignore[attr-defined]
    fake.wait_for_client = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "debugpy", fake)

    with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._debug"):
        maybe_start_debugpy()  # must not raise

    assert any(
        rec.levelno == logging.WARNING and str(exc) in rec.message
        for rec in caplog.records
    )


def test_failed_listen_does_not_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the first listen() fails (port in use, etc.) the idempotency
    # latch must NOT be set — a subsequent call (e.g. after the operator
    # frees the port and re-runs) is allowed to retry. Regression guard
    # for the "set _started after success" ordering in _debug.py.
    monkeypatch.setenv("DEBUG_PORT", "5678")
    attempts: list[tuple[str, int]] = []
    raise_once = {"left": True}

    def listen(addr: tuple[str, int]) -> None:
        attempts.append(addr)
        if raise_once["left"]:
            raise_once["left"] = False
            raise OSError("address in use")

    fake = types.ModuleType("debugpy")
    fake.listen = listen  # type: ignore[attr-defined]
    fake.wait_for_client = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "debugpy", fake)

    maybe_start_debugpy()  # first attempt fails
    maybe_start_debugpy()  # retry must reach listen() again

    assert attempts == [("0.0.0.0", 5678), ("0.0.0.0", 5678)]
    assert debug_mod._started is True  # latched only after the successful attempt
