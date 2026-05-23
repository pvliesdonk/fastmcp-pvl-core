"""Bind the mechanism-agnostic artifact hooks (#142) to the ``filesystem`` transport.

Free functions for the four roles — provider/fetcher/receiver/sender —
composed over two private byte primitives (:func:`_stage` write,
:func:`_ingest` read), the #141 confinement helpers, and the #142 hooks.
The transport is mechanism-specific *here* on purpose; the hooks it calls
stay mechanism-agnostic. See
``docs/superpowers/specs/2026-05-23-file-exchange-143-filesystem-transport-design.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

from fastmcp_pvl_core._file_exchange._paths import atomic_write

if TYPE_CHECKING:
    from pathlib import Path

    from _typeshed import SupportsRead

    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSource
    from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _HashingReader:
    """Wrap a readable byte stream, tallying size + a hash as bytes are read.

    ``shutil.copyfileobj`` (inside :func:`atomic_write`) calls ``.read(n)``;
    each chunk updates the digest and byte count before being returned, so
    after the copy ``.size`` and :meth:`hexdigest` describe exactly the bytes
    that passed through. Lets pvl-core compute size+digest in the single pass
    a non-seekable source stream allows (#142).
    """

    def __init__(
        self, stream: SupportsRead[bytes], *, algorithm: str = "sha256"
    ) -> None:
        self._stream = stream
        self._hash = hashlib.new(algorithm)
        self.size = 0

    def read(self, size: int = -1, /) -> bytes:
        chunk = self._stream.read(size)
        self._hash.update(chunk)
        self.size += len(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


# pvl-core emits sha-256 (the §7.1 example) — its shape.
_DIGEST_LABEL = "sha-256"

# Fixed deposit/staged-file mode (#155): owner rw, group rw, other r — so a
# different-uid party on a shared volume can read what pvl-core writes.
_DEPOSIT_MODE = 0o664


def _write_hashed(stream: SupportsRead[bytes], target: Path) -> tuple[int, str]:
    """Sync: copy ``stream`` to ``target`` atomically at 0o664, hashing as it goes.

    Returns ``(size, "sha-256:<hex>")``.
    """
    reader = _HashingReader(stream)
    atomic_write(target, reader, mode=_DEPOSIT_MODE)
    return reader.size, f"{_DIGEST_LABEL}:{reader.hexdigest()}"


async def _stage(
    source: ArtifactSource, key: str, target: Path
) -> tuple[int, str, ArtifactMetadata]:
    """Pull ``source``'s bytes for ``key`` and stage them at ``target``.

    ``open_artifact`` is awaited on the loop; the blocking copy/hash/atomic
    rename runs in a worker thread. pvl-core owns the returned stream and
    closes it (per #142). Returns ``(size, digest, metadata)`` — the caller
    folds size+digest into the reference it builds.
    """
    stream, meta = await source.open_artifact(key)
    try:
        size, digest = await asyncio.to_thread(_write_hashed, stream, target)
    finally:
        stream.close()
    return size, digest, meta
