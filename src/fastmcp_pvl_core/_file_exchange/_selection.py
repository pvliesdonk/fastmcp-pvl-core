"""§9 descriptor selection: pick the first survivable descriptor.

Two typed entry points, parallel in shape:

- :func:`select_source` returns the chosen :class:`TransferSource` from
  a :class:`TransferHandle`'s ``sources`` array, or ``None`` if none
  survive.
- :func:`select_sink` does the same for a :class:`TransferSink` on
  ``ticket.sinks``.

The §17.4 must-understand check is NOT re-run here — it has already
run inside :meth:`TransferHandle.from_wire` /
:meth:`IntakeTicket.from_wire`. Selection assumes the reference came
from one of those. Direct in-process construction enforces v0.1's
``requires``-must-be-empty rule at the Pydantic layer instead.

When selection returns ``None`` the caller is responsible for building
the §13 ``no-supported-transport`` error envelope via
:func:`fastmcp_pvl_core.file_exchange.build_file_exchange_error`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastmcp_pvl_core._file_exchange._wire import (
    DownloadSource,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    TransferHandle,
    TransferSink,
    TransferSource,
    UploadSink,
)

# §9 says ``a small tolerance (for example, 30 seconds)``. pvl-core
# picks 30s. Not a kwarg — shape decision per the framing principle.
# If a real operational need emerges, lift to an env var (operator
# config), never a per-call argument.
_EXPIRY_TOLERANCE = timedelta(seconds=30)


def select_source(
    handle: TransferHandle,
    *,
    is_accessible: Callable[[FilesystemSource], bool] | None = None,
    now: datetime | None = None,
) -> TransferSource | None:
    """Pick a source descriptor per §9.

    Args:
        handle: The :class:`TransferHandle` whose ``sources`` array
            will be searched in order.
        is_accessible: Callback invoked for each
            :class:`FilesystemSource` to confirm the resolved location
            is readable. ``None`` means the party does not support
            filesystem at all — every filesystem source is skipped.
            For HTTPS sources the callback is not consulted (URL
            reachability is checked at transfer time, not selection
            time).
        now: Reference time for expiry checks. Defaults to the wall
            clock when ``None``; pass an explicit value only from
            tests.

    Returns:
        The first descriptor that survives the §9 checks, or ``None``
        if none did. ``None`` is normal control flow — caller renders
        a ``no-supported-transport`` error envelope.
    """
    reference_time = now if now is not None else datetime.now(timezone.utc)
    for src in handle.sources:
        if isinstance(src, FilesystemSource):
            if is_accessible is None:
                continue
            if not is_accessible(src):
                continue
            return src
        if isinstance(src, DownloadSource):
            if src.expiresAt < reference_time - _EXPIRY_TOLERANCE:
                continue
            return src
        # UnknownTransportDescriptor or anything else not in the known
        # source union: forward-compat fallthrough — skip.
        continue
    return None


def select_sink(
    ticket: IntakeTicket,
    *,
    is_accessible: Callable[[FilesystemSink], bool] | None = None,
    now: datetime | None = None,
) -> TransferSink | None:
    """Pick a sink descriptor per §9.

    Mirrors :func:`select_source` for the write direction. The
    callback signature differs (``FilesystemSink`` instead of
    ``FilesystemSource``) because read vs write accessibility is a
    different check on the downstream's filesystem.

    Args:
        ticket: The :class:`IntakeTicket` whose ``sinks`` array will
            be searched in order.
        is_accessible: Callback invoked for each
            :class:`FilesystemSink` to confirm the resolved location
            is writable. ``None`` means the party does not support
            filesystem at all.
        now: Reference time for expiry checks. Defaults to the wall
            clock when ``None``.

    Returns:
        The first descriptor that survives the §9 checks, or ``None``.
    """
    reference_time = now if now is not None else datetime.now(timezone.utc)
    for sink in ticket.sinks:
        if isinstance(sink, FilesystemSink):
            if is_accessible is None:
                continue
            if not is_accessible(sink):
                continue
            return sink
        if isinstance(sink, UploadSink):
            if sink.expiresAt < reference_time - _EXPIRY_TOLERANCE:
                continue
            return sink
        continue
    return None
