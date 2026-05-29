"""Umbrella tool-registration helpers for the file-exchange extension (#148).

Provides one setup call (``register_file_exchange``) that wires the
cross-cutting infrastructure once, plus four per-role helpers
(``register_file_exchange_provider`` / ``_receiver`` / ``_fetcher``
/ ``_sender``). Provider and receiver are decorators on
downstream-owned tool bodies; fetcher and sender are fully-generated
tool registrations.

See ``docs/superpowers/specs/2026-05-28-file-exchange-148-umbrella-helpers-design.md``
for the architecture and the per-role contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastmcp_pvl_core._file_exchange._routes import register_file_exchange_routes
from fastmcp_pvl_core._file_exchange._tokens import (
    CapabilityTokenStore,
    build_capability_token_store,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource


@dataclass(frozen=True)
class FileExchangeContext:
    """Shared state produced by :func:`register_file_exchange`.

    Consumed by the four per-role helpers.

    Frozen so a downstream that holds it cannot accidentally swap in a
    different token store or hook out from under in-flight registrations.
    """

    token_store: CapabilityTokenStore
    base_url: str
    config: ServerConfig
    source: ArtifactSource | None
    sink: ArtifactSink | None


def register_file_exchange(
    mcp: FastMCP,
    *,
    config: ServerConfig,
    base_url: str,
    source: ArtifactSource | None = None,
    sink: ArtifactSink | None = None,
) -> FileExchangeContext:
    """One-shot file-exchange setup.

    Builds the token store, mounts the routes, and (in a later task)
    declares the Tasks capability. Returns the context the per-tool
    helpers consume.

    Kwargs (per CLAUDE.md classification):

    - ``config`` (**config**): operator-side ``ServerConfig``.
    - ``base_url`` (**config**): origin URL the capability URLs encode.
    - ``source`` (**hook**): downstream's :class:`ArtifactSource` — required
      if any provider or sender helper will be registered later.
    - ``sink`` (**hook**): downstream's :class:`ArtifactSink` — required if
      any receiver or fetcher helper will be registered later.

    Mounting validation (``source``-or-``sink``, ``sink``-needs-``config``)
    is delegated to :func:`register_file_exchange_routes`; the per-tool
    helpers further raise ``ValueError`` at *their* registration time if
    they need a hook the context lacks.
    """
    token_store = build_capability_token_store(config)
    register_file_exchange_routes(
        mcp,
        token_store=token_store,
        source=source,
        sink=sink,
        config=config,
    )
    # §14: declare the server-level Tasks capability so peers know this
    # server accepts tools/call as a task submission. Direct dict mutation
    # on ``experimental_capabilities`` — FastMCP merges this into the
    # wire capability advertisement at request time. We deliberately do
    # NOT set ``_support_tasks_by_default`` because that flips per-tool
    # ``task=True`` defaults that require the ``fastmcp[tasks]`` extra
    # (docket scheduler); the helpers here register tools without
    # ``task=True`` and only need the *capability declaration* per §14.
    mcp.experimental_capabilities["tasks"] = {"requests": {"tools": {"call": True}}}
    return FileExchangeContext(
        token_store=token_store,
        base_url=base_url,
        config=config,
        source=source,
        sink=sink,
    )
