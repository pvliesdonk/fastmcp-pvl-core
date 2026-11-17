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
this keeps the module importable independently of what the dependency
tree does next, and matches how the downstream CLIs call uvicorn.
See docs/reference/uvicorn-asyncio-shutdown.md for runner teardown behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
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

    * ``log_config=None`` — uvicorn runs no ``dictConfig`` and installs no
      handler of its own, leaving the root chain from :mod:`._logging` in
      charge of its records too.
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
    """Run a server to completion. Seam for tests that must not bind.

    Reproduces the epilogue ``uvicorn.run(...)`` wraps ``server.run()`` in,
    rather than delegating to ``uvicorn.run`` itself, because pvl-core builds
    the ``Config`` here so tests can assert it (see :func:`_build_uvicorn_config`)
    without binding a socket. A main-thread watchdog bounds post-serve
    cleanup and disarms only after executor drainage (or if never armed).
    Without this epilogue a failed startup —
    including FastMCP's own lifespan hooks, which ``lifespan="on"`` pins
    specifically to make fatal — would exit 0 instead of signalling failure.
    """
    import uvicorn
    from uvicorn.config import STARTUP_FAILURE

    server = uvicorn.Server(built)
    # Uvicorn owns signals only on the main thread. An embedded worker-thread
    # server must not gain process-wide termination behavior.
    armed: threading.Event | None = None
    drained: threading.Event | None = None
    guard: threading.Thread | None = None
    if threading.current_thread() is threading.main_thread():
        armed = threading.Event()
        drained = threading.Event()
        armed_event = armed
        drained_event = drained
        budget = max(built.timeout_graceful_shutdown or 0, 1)

        def watchdog() -> None:
            armed_event.wait()
            if not drained_event.wait(budget):
                # No logging or writes: a blocked handler must not block exit.
                os._exit(1)

        guard = threading.Thread(target=watchdog, name="shutdown-watchdog", daemon=True)
        guard.start()
        original_serve = server.serve

        async def serve(*args: Any, **kwargs: Any) -> None:
            try:
                await original_serve(*args, **kwargs)
            finally:
                armed_event.set()
                loop = asyncio.get_running_loop()
                original_shutdown = loop.shutdown_default_executor

                async def shutdown(*_args: Any, **_kwargs: Any) -> None:
                    # Ignore the runner's timeout: returning means a real drain.
                    await original_shutdown()
                    drained_event.set()

                loop.shutdown_default_executor = shutdown  # type: ignore[method-assign]

        server.serve = serve  # type: ignore[method-assign]

    # Ctrl-C is how an operator stops a foreground server, so it is a normal
    # exit rather than a crash — ``uvicorn.run()`` suppresses it for the same
    # reason, and a traceback here would be noise at the end of every session.
    try:
        with contextlib.suppress(KeyboardInterrupt):
            server.run()
    finally:
        if armed is not None and drained is not None and guard is not None:
            if not armed.is_set():
                drained.set()
                armed.set()
            if drained.is_set():
                guard.join()
    if not server.started:
        sys.exit(STARTUP_FAILURE)


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
        app: Domain hook — the ASGI application to serve. pvl-core cannot
            know its shape; the caller builds it.
        config: Operator configuration. ``host``, ``port`` and
            ``shutdown_grace_s`` are read from it. The uvicorn settings
            pvl-core pins on top of these are documented on
            :func:`_build_uvicorn_config`, not overridable here.
        host: Operator configuration override, for a CLI flag that beats the
            environment. ``None`` means "not given", so ``config.host`` is
            used.
        port: Operator configuration override, same precedence. ``0`` is a
            real value — bind any free port — and is *not* treated as unset.

    Raises:
        SystemExit: With uvicorn's own startup-failure code, if the server
            never reaches ``started`` — including a lifespan startup hook
            (OIDC discovery, event-store connect) raising.
    """
    _run_server(
        _build_uvicorn_config(
            app,
            host=config.host if host is None else host,
            port=config.port if port is None else port,
            shutdown_grace_s=config.shutdown_grace_s,
        )
    )
