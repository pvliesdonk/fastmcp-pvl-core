"""In-process transfer/ingest mechanics shared across the ``*-mcp`` family.

Implements ADR 0001 (``docs/adr/0001-transfer-lift.md``): SSRF-hardened URL
fetch (§11 #1), size-capped base64 decode (§11 #2), a capability-link token
store (§11 #3), the ``TransferSink`` / ``TransferValidator`` domain seam plus
``make_transfer_handler`` (§11 #4), and ``register_transfer_routes`` — the entry
point that wires the ``/transfer`` route and the two link tools (§11 #5).

The public surface is the two standalone primitives (``fetch_url`` with its
``FetchResult``, and ``decode_base64_capped``) plus the transfer feature's two
entry points — ``register_transfer_routes`` (path 1: generic tools) and
``build_transfer_links`` (path 2: the ``TransferLinks`` minter, no tools) —
its config (``TransferConfig``), its two domain hooks (``TransferSink``,
``TransferValidator``), and their supporting types (``TransferReadResult``,
``TransferKind``). The store, handler, and route mechanics stay internal —
pvl-core owns their shape.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

from .base64 import decode_base64_capped
from .config import TransferConfig
from .fetch import FetchResult, fetch_url
from .register import TransferLinks, build_transfer_links, register_transfer_routes
from .sink import TransferKind, TransferReadResult, TransferSink, TransferValidator

__all__ = [
    "FetchResult",
    "TransferConfig",
    "TransferKind",
    "TransferLinks",
    "TransferReadResult",
    "TransferSink",
    "TransferValidator",
    "build_transfer_links",
    "decode_base64_capped",
    "fetch_url",
    "register_transfer_routes",
]
