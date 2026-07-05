"""In-process transfer/ingest mechanics shared across the ``*-mcp`` family.

Implements ADR 0001 (``docs/adr/0001-transfer-lift.md``): SSRF-hardened URL
fetch (§11 #1), size-capped base64 decode (§11 #2), a capability-link token
store (§11 #3), the ``TransferSink`` / ``TransferValidator`` domain seam plus
``make_transfer_handler`` (§11 #4), and ``register_transfer_routes`` — the entry
point that wires the ``/transfer`` route and the two link tools (§11 #5).

The public surface is the two standalone primitives (``fetch_url`` with its
``FetchResult``, and ``decode_base64_capped``) plus the transfer feature's entry
point and its two domain hooks: ``register_transfer_routes``, ``TransferConfig``,
``TransferSink``, ``TransferValidator``, ``TransferReadResult``, ``TransferKind``.
The store, handler, and route mechanics stay internal — pvl-core owns their shape.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

from .base64 import decode_base64_capped
from .config import TransferConfig
from .fetch import FetchResult, fetch_url
from .register import register_transfer_routes
from .sink import TransferKind, TransferReadResult, TransferSink, TransferValidator

__all__ = [
    "FetchResult",
    "TransferConfig",
    "TransferKind",
    "TransferReadResult",
    "TransferSink",
    "TransferValidator",
    "decode_base64_capped",
    "fetch_url",
    "register_transfer_routes",
]
