"""In-process transfer/ingest mechanics shared across the ``*-mcp`` family.

Implements ADR 0001 (``docs/adr/0001-transfer-lift.md``): SSRF-hardened URL
fetch, size-capped base64 decode, a one-time capability-link token store, and
the ``/transfer`` route framework. This first module ships the standalone
``fetch_url`` primitive (§11 issue #1); the rest land in sibling issues.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

from .base64 import decode_base64_capped
from .fetch import FetchResult, fetch_url

__all__ = ["FetchResult", "decode_base64_capped", "fetch_url"]
