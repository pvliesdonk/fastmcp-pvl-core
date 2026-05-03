"""Optional remote-debugger entrypoint helper.

Containerised consumers (image-generation-mcp, markdown-vault-mcp, ...)
need a uniform way to attach a remote Python debugger without each one
hand-rolling the ``debugpy`` bootstrap. Call :func:`maybe_start_debugpy`
early in ``main()`` (after logging is configured, before argument
parsing): it is a no-op unless ``DEBUG_PORT`` is set, so it is safe to
ship in default scaffolds.

Environment contract:

* ``DEBUG_PORT`` — TCP port to listen on. Unset, blank, or any value
  that parses to ``0`` disables the helper silently. Non-numeric or
  out-of-``1..65535`` values log a ``WARNING`` and the helper returns
  without raising. Surrounding whitespace is ignored.
* ``DEBUG_WAIT`` — when truthy (``1``/``true``/``yes``/``on``,
  case-insensitive — see ``parse_bool`` in ``_env.py``), block startup
  until the IDE attaches. Default is non-blocking so missing-attach
  doesn't deadlock production containers that were accidentally built
  with ``DEBUG=true``.

The ``debugpy`` package is imported lazily — install it via
``pip install 'fastmcp-pvl-core[debug]'`` (quote the brackets in zsh)
or ``uv add debugpy`` only on images that actually need the listener.
A failed import logs a ``WARNING`` and continues, so the helper is safe
to call unconditionally in the default scaffold.

.. warning::

   The listener binds ``0.0.0.0`` so the debugger is reachable from
   outside the container — this is intentional for the developer
   workflow, but **debugpy's DAP protocol is unauthenticated**: any
   peer that can reach the port has arbitrary code execution as the
   server process. Only enable ``DEBUG_PORT`` in environments where
   the port is reachable solely from a trusted developer workstation
   (e.g. ``kubectl port-forward``, ``docker run -p 127.0.0.1:5678:5678``,
   an SSH tunnel). Never publish the debug port on a public network.
"""

from __future__ import annotations

import logging
import os

from fastmcp_pvl_core._env import parse_bool

logger = logging.getLogger(__name__)

# Module-level latch — debugpy.listen() raises if called twice on the
# same process, so the helper is idempotent across repeated invocations
# (e.g. typer subcommands sharing a root callback). Not thread-safe;
# the helper is meant to be called from main() before any worker
# threads spawn. A racing second caller would hit debugpy's own
# "already listening" error, which the broad except below absorbs into
# a WARNING — benign, but worth knowing.
_started = False


def maybe_start_debugpy() -> None:
    """Start a debugpy listener if ``DEBUG_PORT`` is set.

    Reads ``DEBUG_PORT`` from the environment. Unset, blank, or any
    value that parses to ``0`` is a silent no-op. Non-numeric or
    out-of-``1..65535`` values log a ``WARNING`` so misconfiguration
    is visible. Otherwise imports :mod:`debugpy` lazily and binds
    ``("0.0.0.0", port)``.

    A failed import logs a ``WARNING`` pointing at the ``debug`` extra
    and returns. A failure inside :func:`debugpy.listen` (port in use,
    permission denied, debugpy-internal error) likewise logs a
    ``WARNING`` and returns — a debug-port problem must never crash
    the server.

    When ``DEBUG_WAIT`` is truthy the helper blocks until the IDE
    attaches (``debugpy.wait_for_client()``); otherwise startup
    continues immediately. A failure inside ``wait_for_client`` (e.g.
    debugpy-internal error, transport hiccup) likewise logs a
    ``WARNING`` and returns — the listener is still up, so the IDE can
    still attach manually. ``KeyboardInterrupt`` propagates so an
    operator-initiated Ctrl-C still aborts the process.

    Idempotent across repeated calls in the same process. Not
    thread-safe — call from ``main()`` before spawning workers. After
    a ``wait_for_client`` failure a re-call short-circuits silently:
    the listener is up, there is nothing more to do.

    Security note: the listener binds ``0.0.0.0`` and debugpy's DAP
    protocol is unauthenticated. Only expose the port to a trusted
    developer workstation (port-forward, loopback bind, SSH tunnel).
    See the module docstring for details.
    """
    global _started
    if _started:
        return

    raw = os.environ.get("DEBUG_PORT", "").strip()
    if not raw:
        return

    try:
        port = int(raw)
    except ValueError:
        logger.warning(
            "DEBUG_PORT=%r is not a valid integer; debugpy listener not started.",
            raw,
        )
        return

    # All forms of zero (``0``, ``00``, ``-0``, ``+0``, surrounded by
    # whitespace) are the documented disable form — silent no-op.
    if port == 0:
        return

    if not 1 <= port <= 65535:
        logger.warning(
            "DEBUG_PORT=%d is outside 1..65535; debugpy listener not started.",
            port,
        )
        return

    try:
        # ``import-not-found`` covers environments without the optional
        # ``[debug]`` extra; ``unused-ignore`` covers CI / dev installs
        # that *do* have debugpy and would otherwise flag the comment.
        import debugpy  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        logger.warning(
            "DEBUG_PORT=%d set but debugpy is not installed. "
            "Install with `pip install 'fastmcp-pvl-core[debug]'` "
            "or `uv add debugpy`.",
            port,
        )
        return

    try:
        debugpy.listen(("0.0.0.0", port))
    except Exception as exc:  # noqa: BLE001 — listener bring-up must not crash startup
        logger.warning(
            "debugpy.listen on port %d failed: %s; continuing without remote debugger.",
            port,
            exc,
        )
        return

    _started = True
    logger.info("debugpy listening on 0.0.0.0:%d", port)

    if parse_bool(os.environ.get("DEBUG_WAIT", "")):
        logger.info("DEBUG_WAIT=true — blocking until debugger attaches...")
        try:
            debugpy.wait_for_client()
        except KeyboardInterrupt:
            # Operator hit Ctrl-C while waiting — propagate so the
            # process exits as the user expects. The latch stays set
            # because listen() already succeeded; a re-caller would
            # short-circuit silently (listener is up, nothing to do).
            raise
        except Exception as exc:  # noqa: BLE001 — debugger bring-up must not crash startup
            logger.warning(
                "debugpy.wait_for_client failed: %s; continuing startup. "
                "The listener on 0.0.0.0:%d is still up; "
                "the IDE can still attach manually.",
                exc,
                port,
            )
            return
        logger.info("debugger attached; continuing startup.")
