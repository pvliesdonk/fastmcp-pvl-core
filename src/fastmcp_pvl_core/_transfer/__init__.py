"""In-process transfer/ingest mechanics shared across the ``*-mcp`` family.

Implements ADR 0001 (``docs/adr/0001-transfer-lift.md``): SSRF-hardened URL
fetch, size-capped base64 decode, a capability-link token store, and the
``/transfer`` route framework. Landed so far: ``fetch_url`` (§11 issue #1),
``decode_base64_capped`` (§11 issue #2), ``TransferStore`` in ``store.py``
(§11 issue #3), and — in ``sink.py`` and ``routes.py`` — the ``TransferSink`` /
``TransferValidator`` domain seam plus ``make_transfer_handler`` (§11 issue #4).
All of these except the two exported primitives stay internal until the
route-registration layer (§11 issue #5) wires them into the public surface.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

from .base64 import decode_base64_capped
from .fetch import FetchResult, fetch_url

__all__ = ["FetchResult", "decode_base64_capped", "fetch_url"]
