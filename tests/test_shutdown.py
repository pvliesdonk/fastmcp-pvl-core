"""Process-level tests for the HTTP runner's executor shutdown."""

import os
import subprocess
import sys
import textwrap

import pytest

_CHILD = r"""
import asyncio
import os
import signal
import threading
import time

import uvicorn
from fastmcp_pvl_core import ServerConfig, run_http
from fastmcp_pvl_core._serve import _run_server

mode, loop, grace = __import__('sys').argv[1:]
release = threading.Event()
finished = threading.Event()
original_server = uvicorn.Server
class Server(original_server):
    async def serve(self, sockets=None):
        try:
            await super().serve(sockets)
        finally:
            if mode == 'finite':
                print('SERVE_DONE', flush=True)
                release.set()

    async def startup(self, sockets=None):
        await super().startup(sockets)
        if self.started:
            asyncio.create_task(stop(self))
uvicorn.Server = Server

async def stop(server):
    if mode in ('stuck', 'internal', 'eagain', 'finite', 'app_timeout'):
        ready = threading.Event()
        def work():
            ready.set()
            if mode == 'finite':
                release.wait()
                time.sleep(0.2)
                finished.set()
                print('WORK_DONE', flush=True)
            elif mode == 'app_timeout':
                release.wait(1.5)
            else:
                threading.Event().wait()
        asyncio.get_running_loop().run_in_executor(None, work)
        while not ready.is_set():
            await asyncio.sleep(0.01)
    print('READY', flush=True)
    await asyncio.sleep(0.5)
    if mode == 'internal':
        server.should_exit = True
    else:
        os.kill(os.getpid(), signal.SIGINT)

async def app(scope, receive, send):
    assert scope['type'] == 'lifespan'
    while True:
        message = await receive()
        if message['type'] == 'lifespan.startup':
            if mode == 'startup':
                await send({'type': 'lifespan.startup.failed', 'message': 'fixture'})
                return
            await send({'type': 'lifespan.startup.complete'})
        else:
            if mode == 'app_timeout':
                running_loop = asyncio.get_running_loop()
                method = running_loop.shutdown_default_executor
                print('NATIVE', getattr(method, '__self__', None) is running_loop,
                      flush=True)
                started = time.monotonic()
                await method(timeout=0.05)
                print('TIMEOUT_ELAPSED', time.monotonic() - started, flush=True)
                release.set()
            if mode == 'finite':
                print('PENDING', not finished.is_set(), flush=True)
            if mode == 'slow_lifespan':
                await asyncio.sleep(1.3)
            if mode == 'eagain':
                def fail(self):
                    raise RuntimeError('fixture EAGAIN')
                threading.Thread.start = fail
            await send({'type': 'lifespan.shutdown.complete'})
            print('SHUTDOWN', flush=True)
            return

config = ServerConfig(host='127.0.0.1', port=0, shutdown_grace_s=int(grace))
if loop == 'auto':
    run_http(app, config=config)
else:
    _run_server(uvicorn.Config(app, host='127.0.0.1', port=0, loop=loop,
                               log_config=None, lifespan='on',
                               timeout_graceful_shutdown=int(grace)))
"""


def _run_child(tmp_path, mode, grace=1, loop="auto"):
    env = os.environ.copy()
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        env.pop(key, None)
    env.update(
        HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path)
    )
    child = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_CHILD), mode, loop, str(grace)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        try:
            out, err = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("child did not exit within 10 seconds")
        return child.returncode, out, err
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


@pytest.mark.parametrize("mode", ["stuck", "internal"])
def test_stuck_executor(tmp_path, mode):
    code, out, _ = _run_child(tmp_path, mode)
    assert "READY" in out and "SHUTDOWN" in out
    assert code == 1


def test_start_failure(tmp_path):
    code, out, _ = _run_child(tmp_path, "eagain")
    assert "SHUTDOWN" in out
    assert code == 1


@pytest.mark.parametrize(
    "mode,grace,expected",
    [
        ("healthy", 0, 0),
        ("healthy", 1, 0),
        ("finite", 1, 0),
        ("startup", 1, 3),
        ("slow_lifespan", 1, 0),
    ],
)
def test_healthy_shutdown(tmp_path, mode, grace, expected):
    code, out, err = _run_child(tmp_path, mode, grace)
    if mode == "finite":
        assert "PENDING True" in out
        assert out.index("SHUTDOWN") < out.index("SERVE_DONE") < out.index("WORK_DONE")
    assert code == expected
    assert "Traceback" not in err


def test_uvloop(tmp_path):
    pytest.importorskip("uvloop")
    assert _run_child(tmp_path, "stuck", loop="uvloop")[0] == 1
    assert _run_child(tmp_path, "healthy", 0, "uvloop")[0] == 0


@pytest.mark.skipif(sys.version_info < (3, 12), reason="executor timeout added in 3.12")
@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
def test_application_executor_timeout_is_preserved(tmp_path, loop):
    if loop == "uvloop":
        pytest.importorskip("uvloop")
    code, out, _ = _run_child(tmp_path, "app_timeout", loop=loop)
    assert code == 0
    assert "NATIVE True" in out
    if loop == "asyncio":
        elapsed = float(out.split("TIMEOUT_ELAPSED ")[1].splitlines()[0])
        assert elapsed < 1
