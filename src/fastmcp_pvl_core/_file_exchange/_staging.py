"""Shared digest + temp-file staging primitives for the file-exchange transports.

The supported-algorithm table (:data:`_HASHLIB_BY_LABEL`) and the
:func:`_digest_verifier` helper are shared across every transport that verifies a
declared ``label:hex`` digest (download, upload, filesystem). The streaming
primitives (:data:`_CHUNK`, :func:`_write_chunk`) are used by the HTTP transports
(download fetcher / upload route + sender) for buffered staging to a transient
temp file with hashing — each HTTP transport keeps its own read loop (download
resumes via ``Range``; upload reads ``request.stream()`` or a hook stream) and
applies the ``OSError -> transfer-failed`` mapping / cleanup-suppression contract
around them.
"""

from __future__ import annotations

import hashlib
from typing import IO

# Streaming chunk size for temp-file staging (1 MiB).
_CHUNK = 1024 * 1024

# Declared-digest label -> hashlib name; an unsupported label fails verification
# (cannot verify -> digest-mismatch), never silently skips (§15).
_HASHLIB_BY_LABEL = {"sha-256": "sha256", "sha-384": "sha384", "sha-512": "sha512"}


def _digest_verifier(
    declared: str | None,
) -> tuple[hashlib._Hash | None, str | None, bool]:
    """Return (hasher | None, expected_hex | None, unverifiable).

    ``unverifiable`` is True when a digest is declared with an unsupported label
    — verification must then fail (cannot verify), never silently skip (§15).
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

    Both ops are synchronous and are designed to be dispatched off the event
    loop by the caller (e.g. via ``asyncio.to_thread``).
    """
    tmp.write(chunk)
    if hasher is not None:
        hasher.update(chunk)
