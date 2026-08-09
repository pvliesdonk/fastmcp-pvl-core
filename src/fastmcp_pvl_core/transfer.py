"""Public namespace for the transfer capability-link feature (ADR 0001).

Importing from ``fastmcp_pvl_core.transfer`` gathers the whole transfer surface
in one place and reads as an explicit "I am wiring the transfer feature" — most
pointedly :func:`build_transfer_links`, the path-2 seam a downstream uses to
build its own transfer tool. Every name here is also available from the
top-level ``fastmcp_pvl_core`` package; this module is a cohesive re-export, not
a second implementation. The feature's mechanics stay in the internal
``_transfer`` package (relative imports, foldable).
"""

from __future__ import annotations

from ._transfer import (
    TransferBadGatewayError,
    TransferConfig,
    TransferForbiddenError,
    TransferGatewayTimeoutError,
    TransferKind,
    TransferLinks,
    TransferNotFoundError,
    TransferRateLimitedError,
    TransferReadResult,
    TransferResourceGoneError,
    TransferSink,
    TransferSinkError,
    TransferUnavailableError,
    TransferValidator,
    build_transfer_links,
    register_transfer_routes,
)

__all__ = [
    "TransferBadGatewayError",
    "TransferConfig",
    "TransferForbiddenError",
    "TransferGatewayTimeoutError",
    "TransferKind",
    "TransferLinks",
    "TransferNotFoundError",
    "TransferRateLimitedError",
    "TransferReadResult",
    "TransferResourceGoneError",
    "TransferSink",
    "TransferSinkError",
    "TransferUnavailableError",
    "TransferValidator",
    "build_transfer_links",
    "register_transfer_routes",
]
