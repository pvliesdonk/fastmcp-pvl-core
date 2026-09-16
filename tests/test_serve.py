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
