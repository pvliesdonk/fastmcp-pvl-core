"""CLI helpers for FastMCP servers.

Narrow surface: ``normalise_http_path`` + ``make_serve_parser``.
Each project builds its own ``main()`` by adding domain subparsers.
"""

from __future__ import annotations

import argparse


def normalise_http_path(path: str | None, *, default: str = "/mcp") -> str:
    """Normalize an HTTP mount path.

    Ensures a leading ``/`` and strips a trailing ``/`` (except for root
    ``/``). ``None``, empty, or whitespace-only values fall back to
    ``default``.

    Args:
        path: Raw path from env var or CLI flag (may be ``None``).
        default: Value to return when ``path`` is empty. Defaults to
            ``/mcp``.

    Returns:
        A normalised mount path suitable for FastMCP streamable HTTP
        transport.
    """
    if path is None:
        return default
    normalised = path.strip()
    if not normalised:
        return default
    if not normalised.startswith("/"):
        normalised = f"/{normalised}"
    if len(normalised) > 1:
        normalised = normalised.rstrip("/")
    return normalised


def make_serve_parser(*, prog: str, description: str = "") -> argparse.ArgumentParser:
    """Build the common ``serve`` argparse parser.

    Returns a parser pre-populated with the generic flags shared across
    every FastMCP-based server: ``-v/--verbose``, ``--transport``,
    ``--host``, ``--port``, and ``--http-path``. Projects compose
    domain-specific subparsers on top::

        parser = make_serve_parser(prog="myapp")
        subs = parser.add_subparsers(dest="cmd")
        subs.add_parser("index", ...)

    Args:
        prog: Program name (shown in ``--help`` output).
        description: Optional parser description.

    Returns:
        A configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging (sets FASTMCP_LOG_LEVEL=DEBUG)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for http/sse (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for http/sse (default: 8000)",
    )
    parser.add_argument(
        "--http-path",
        default=None,
        help="HTTP mount path (default: /mcp)",
    )
    return parser
