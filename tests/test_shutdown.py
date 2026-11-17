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

import uvicorn
from fastmcp_pvl_core import ServerConfig, run_http
from fastmcp_pvl_core._serve import _run_server

mode, loop, grace = __import__('sys').argv[1:]
original_server = uvicorn.Server
class Server(original_server):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        if self.started:
            asyncio.create_task(stop(self))
uvicorn.Server = Server

async def stop(server):
    if mode in ('stuck', 'internal', 'eagain', 'finite'):
        ready = threading.Event()
        def work():
            ready.set()
            threading.Event().wait(0.4 if mode == 'finite' else None)
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
    code, _, err = _run_child(tmp_path, mode, grace)
    assert code == expected
    assert "Traceback" not in err


def test_uvloop(tmp_path):
    pytest.importorskip("uvloop")
    assert _run_child(tmp_path, "stuck", loop="uvloop")[0] == 1
    assert _run_child(tmp_path, "healthy", 0, "uvloop")[0] == 0
