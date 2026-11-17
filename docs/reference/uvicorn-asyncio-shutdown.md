---
type: Reference
title: Uvicorn and asyncio shutdown
description: Shutdown ordering and default executor teardown.
subject_version: "CPython 3.10.22 and 3.14.8; uvicorn 0.54.0; uvloop 0.22.1"
valid_for: "CPython 3.10–3.14, uvicorn 0.54, uvloop 0.22"
generated:
  by: process:researching-references
  at: 2026-11-16
stale_after: 2027-05-16
status: stable
verified:
  - by: process:researching-references
    at: 2026-11-16
sources:
  - id: py310
    title: CPython 3.10 asyncio event loop
    resource: https://github.com/python/cpython/blob/v3.10.22/Lib/asyncio/base_events.py
    accessed: 2026-11-16
  - id: py314
    title: CPython 3.14 asyncio runner and event loop
    resource: https://github.com/python/cpython/blob/v3.14.8/Lib/asyncio/runners.py
    accessed: 2026-11-16
  - id: base314
    title: CPython 3.14 executor shutdown
    resource: https://github.com/python/cpython/blob/v3.14.8/Lib/asyncio/base_events.py
    accessed: 2026-11-16
  - id: threads
    title: CPython thread executor
    resource: https://github.com/python/cpython/blob/v3.14.8/Lib/concurrent/futures/thread.py
    accessed: 2026-11-16
  - id: uvicorn
    title: Uvicorn server shutdown
    resource: https://github.com/Kludex/uvicorn/blob/0.54.0/uvicorn/server.py
    accessed: 2026-11-16
  - id: compat
    title: Uvicorn asyncio compatibility runner
    resource: https://github.com/Kludex/uvicorn/blob/0.54.0/uvicorn/_compat.py
    accessed: 2026-11-16
  - id: uvloop
    title: uvloop loop and runner
    resource: https://github.com/MagicStack/uvloop/blob/v0.22.1/uvloop/loop.pyx
    accessed: 2026-11-16
  - id: uvloop-class
    title: uvloop public Loop class
    resource: https://github.com/MagicStack/uvloop/blob/v0.22.1/uvloop/__init__.py
    accessed: 2026-11-16
---

# Uvicorn and asyncio shutdown

This reference covers executor shutdown after HTTP serving, as used by `_serve.py`; it does not cover general ASGI lifecycle semantics.

## Claims

- Uvicorn runs `serve` inside an asyncio runner; its graceful request timeout precedes lifespan shutdown and does not bound runner teardown. [source: uvicorn] [source: compat] [pins: tests/test_shutdown.py::test_stuck_executor]
- The runner cancels tasks, shuts down async generators, and then shuts down the default executor. Python 3.10 calls it without a timeout; Python 3.14 passes 300 seconds. [source: compat] [source: py314] [pins: tests/test_shutdown.py::test_stuck_executor]
- Executor shutdown starts a helper thread; Python 3.10 waits indefinitely, while 3.14 warns on timeout and calls `shutdown(wait=False)`. Omitting the 3.14 timeout waits indefinitely. [source: py310] [source: base314] [pins: tests/test_shutdown.py::test_start_failure]
- At interpreter exit, the thread executor joins its workers without a timeout even after `shutdown(wait=False)`. [source: threads] [pins: tests/test_shutdown.py::test_stuck_executor]
- Uvicorn restores signal handlers and re-raises captured signals after serving; signal handlers are installed only on the main thread. [source: uvicorn]
- uvloop exposes a Python `Loop` subclass, and its executor shutdown accepts a timeout defaulting to `None`; version 0.22.1 awaits the shutdown future before its timed thread join, so that timeout does not bound a stuck worker. [source: uvloop-class] [source: uvloop] [pins: tests/test_shutdown.py::test_uvloop, tests/test_shutdown.py::test_application_executor_timeout_is_preserved]

## Where pvl-core departs

On the main thread pvl-core supplies one post-serve deadline shared by runner task cancellation, async-generator close and default-executor drainage; ASGI lifespan shutdown precedes it. A failed drain keeps the watchdog armed. [pins: tests/test_shutdown.py::test_start_failure]

## Not covered

The reported OpenBLAS trigger and container PID 1 signal behavior are unverified; neither is needed for the fixture-only reproduction.
