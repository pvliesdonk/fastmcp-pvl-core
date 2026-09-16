"""The run_http seam — §2 of the root-logging-ownership spec."""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time

import fastmcp
import httpx
import pytest

from fastmcp_pvl_core import ServerConfig, run_http
from fastmcp_pvl_core._serve import _build_uvicorn_config, _run_server


@pytest.fixture
def configured_logging_capture(monkeypatch):
    """Configure logging as a server does, and capture what reaches root."""
    from fastmcp_pvl_core import configure_logging_from_env

    monkeypatch.delenv("TEST_MCP_LOG_LEVEL", raising=False)
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    access = logging.getLogger("uvicorn.access")
    saved_access = (
        access.handlers[:],
        access.level,
        access.propagate,
        access.filters[:],
    )
    fastmcp_logger = logging.getLogger("fastmcp")
    saved_fastmcp = (
        fastmcp_logger.handlers[:],
        fastmcp_logger.level,
        fastmcp_logger.propagate,
        fastmcp_logger.filters[:],
    )
    saved_log_enabled = fastmcp.settings.log_enabled

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
        access.handlers[:], access.level, access.propagate, access.filters[:] = (
            saved_access
        )
        (
            fastmcp_logger.handlers[:],
            fastmcp_logger.level,
            fastmcp_logger.propagate,
            fastmcp_logger.filters[:],
        ) = saved_fastmcp
        fastmcp.settings.log_enabled = saved_log_enabled


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


@contextlib.contextmanager
def _running_server():
    """Run a real ``uvicorn.Server`` for ``_app`` on a free port, in a thread.

    Shared by the two e2e tests below: starts the server on a background
    thread, polls ``server.started`` against a 10s deadline before
    yielding ``(server, port)``, and guarantees shutdown — ``should_exit``
    then a 10s-timeout ``thread.join`` — in a ``finally``, so a test body
    expresses only what it asserts, not the start/stop mechanics.
    """
    import uvicorn

    port = _free_port()
    built = _build_uvicorn_config(_app, host="127.0.0.1", port=port, shutdown_grace_s=1)
    server = uvicorn.Server(built)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "server did not start within 10s"
        yield server, port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_pins_the_settings_pvl_core_owns():
    built = _build_uvicorn_config(_app, host="127.0.0.1", port=8000, shutdown_grace_s=3)
    assert built.log_config is None
    assert built.lifespan == "on"
    assert built.timeout_graceful_shutdown == 3


def test_takes_host_and_port_from_config(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "fastmcp_pvl_core._serve._run_server",
        lambda built: captured.update(built=built),
    )
    run_http(_app, config=ServerConfig(host="0.0.0.0", port=9001, shutdown_grace_s=7))
    built = captured["built"]
    assert (built.host, built.port) == ("0.0.0.0", 9001)
    assert built.timeout_graceful_shutdown == 7


def test_explicit_host_and_port_override_config(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "fastmcp_pvl_core._serve._run_server",
        lambda built: captured.update(built=built),
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
        "fastmcp_pvl_core._serve._run_server",
        lambda built: captured.update(built=built),
    )
    run_http(_app, config=ServerConfig(port=8000), port=0)
    assert captured["built"].port == 0


def test_end_to_end_access_log_policy(configured_logging_capture):
    """A real server, real requests: successes silent, failures logged and redacted."""
    with _running_server() as (_server, port):
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            client.get("/health")
            client.post("/mcp")
            client.get("/nope")
            client.get("/transfer/tok_SECRET123")
        time.sleep(0.3)

    access = [m for m in configured_logging_capture if ' - "' in m]
    assert not any("/health" in m for m in access)
    assert not any("/mcp" in m for m in access)
    assert any("/nope" in m and "404" in m for m in access)
    assert not any("tok_SECRET123" in m for m in access)
    assert any("transfer/<redacted>" in m for m in access)


def test_uvicorn_never_installs_its_own_handlers(configured_logging_capture):
    """log_config=None is what makes PR 1's topology hold at runtime."""
    with _running_server():
        access = logging.getLogger("uvicorn.access")
        assert access.handlers == []
        assert access.propagate is True


class TestRunServerEpilogue:
    """``_run_server`` mirrors the epilogue ``uvicorn.run(...)`` wraps
    ``server.run()`` in — without it, a failed startup exits 0 instead of
    uvicorn's own ``STARTUP_FAILURE``, and a mid-run Ctrl-C propagates as an
    uncaught ``KeyboardInterrupt`` instead of exiting silently."""

    class _FakeServer:
        def __init__(self, *, started: bool, raise_keyboard_interrupt: bool = False):
            self.started = started
            self._raise_keyboard_interrupt = raise_keyboard_interrupt
            self.run_called = False

        def run(self):
            self.run_called = True
            if self._raise_keyboard_interrupt:
                raise KeyboardInterrupt

    def _patch_server(self, monkeypatch, fake):
        import uvicorn

        monkeypatch.setattr(uvicorn, "Server", lambda config: fake)

    def test_exits_with_startup_failure_when_server_never_started(self, monkeypatch):
        fake = self._FakeServer(started=False)
        self._patch_server(monkeypatch, fake)
        built = _build_uvicorn_config(
            _app, host="127.0.0.1", port=8000, shutdown_grace_s=1
        )

        with pytest.raises(SystemExit) as exc_info:
            _run_server(built)

        assert fake.run_called
        assert exc_info.value.code == 3

    def test_keyboard_interrupt_after_startup_exits_silently(self, monkeypatch):
        """Ctrl-C after the server is up: swallowed, no SystemExit, no traceback."""
        fake = self._FakeServer(started=True, raise_keyboard_interrupt=True)
        self._patch_server(monkeypatch, fake)
        built = _build_uvicorn_config(
            _app, host="127.0.0.1", port=8000, shutdown_grace_s=1
        )

        _run_server(built)  # must not raise

        assert fake.run_called

    def test_keyboard_interrupt_before_startup_still_signals_failure(self, monkeypatch):
        """Ctrl-C before the server ever started: still a startup failure."""
        fake = self._FakeServer(started=False, raise_keyboard_interrupt=True)
        self._patch_server(monkeypatch, fake)
        built = _build_uvicorn_config(
            _app, host="127.0.0.1", port=8000, shutdown_grace_s=1
        )

        with pytest.raises(SystemExit) as exc_info:
            _run_server(built)

        assert exc_info.value.code == 3
