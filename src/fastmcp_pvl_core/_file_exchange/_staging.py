"""Shared temp-file staging + digest primitives for the HTTP transports.

The download fetcher (#145) and the upload route/sender (#146) both buffer a
byte stream to a transient temp file with hashing and a size bound, and verify
a declared digest. These primitives factor out the common parts.

**Universal items** (used by both HTTP transports and by ``_filesystem.py``):

- ``_CHUNK`` — streaming chunk size (1 MiB), the I/O unit for all temp-staging
  loops.
- ``_HASHLIB_BY_LABEL`` — the supported ``sha-256``/``sha-384``/``sha-512``
  label set; an unsupported label fails verification, never silently skips (§15).

**Download + upload transport items** (HTTP-specific):

- ``_digest_verifier`` — used by the download fetcher to parse
  ``ArtifactMetadata.digest`` and by the upload route to hash received bytes;
  also called by ``_filesystem.py``'s verification path.
- ``_write_chunk`` — used by both HTTP-transport staging loops (single
  ``asyncio.to_thread`` dispatch to write+hash a chunk); NOT used by the
  filesystem transport (which goes through ``shutil.copyfileobj`` in
  ``atomic_write``).

Each transport keeps its own read loop (download resumes via ``Range``; upload
reads ``request.stream()`` or a hook stream) and applies the
``OSError -> transfer-failed`` mapping / cleanup-suppression contract around
these shared primitives.
"""

from __future__ import annotations

import hashlib
from typing import IO

# Streaming chunk size for temp-file staging (1 MiB). Universal — applies to
# all transports that buffer bytes in a staging loop.
_CHUNK = 1024 * 1024

# Declared-digest label -> hashlib name; an unsupported label fails verification
# (cannot verify -> digest-mismatch), never silently skips (§15). Mirrors the
# same set in _filesystem.py; both must be kept in sync with the wire spec's
# supported-digest section.
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

    Both ops run off the event loop in a single ``asyncio.to_thread`` dispatch.
    HTTP-transport only — the filesystem transport uses ``shutil.copyfileobj``
    via ``atomic_write``.
    """
    tmp.write(chunk)
    if hasher is not None:
        hasher.update(chunk)
