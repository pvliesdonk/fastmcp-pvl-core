"""Shared digest + chunk-write primitives for the HTTP-transport data planes.

Both ``_download`` (fetcher: write incoming HTTP body to a temp) and
``_upload`` (route: write incoming PUT body to a temp; sender: write outgoing
hook stream to a temp before hashing) share the same per-chunk
write-and-hash pattern and the same declared-digest verifier semantics.
Centralising the primitives here keeps the contract — every temp-file op
maps to ``transfer-failed`` or is suppressed for cleanup, and an
unsupported digest label fails verification rather than silently skipping
— from being re-derived divergently between the HTTP transports
(matrix row G1, spec §15).

The ``filesystem`` transport (``_filesystem.py``) currently keeps its own
local copies of ``_CHUNK`` and ``_HASHLIB_BY_LABEL`` plus its own digest
verifier; consolidating that consumer is tracked separately and is
intentionally out of scope here.
"""

from __future__ import annotations

import hashlib
from typing import IO

_CHUNK = 1024 * 1024

_HASHLIB_BY_LABEL = {"sha-256": "sha256", "sha-384": "sha384", "sha-512": "sha512"}


def _digest_verifier(
    declared: str | None,
) -> tuple[hashlib._Hash | None, str | None, bool]:
    """Return ``(hasher | None, expected_hex | None, unverifiable)``.

    ``unverifiable`` is True when a digest is declared with an unsupported
    label — verification must then fail (cannot verify), never silently
    skip (§15).
    """
    if declared is None:
        return None, None, False
    label, _, expected_hex = declared.partition(":")
    name = _HASHLIB_BY_LABEL.get(label.lower())
    if name is None:
        return None, expected_hex, True
    return hashlib.new(name), expected_hex.lower(), False


def _write_chunk(tmp: IO[bytes], hasher: hashlib._Hash | None, chunk: bytes) -> None:
    """Write a body chunk to the temp file and fold it into the running hash.

    Both ops run off the event loop in a single ``asyncio.to_thread`` dispatch.
    """
    tmp.write(chunk)
    if hasher is not None:
        hasher.update(chunk)
