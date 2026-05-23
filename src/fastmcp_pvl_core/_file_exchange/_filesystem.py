"""Bind the mechanism-agnostic artifact hooks (#142) to the ``filesystem`` transport.

Free functions for the four roles — provider/fetcher/receiver/sender —
composed over two private byte primitives (:func:`_stage` write,
:func:`_ingest` read), the #141 confinement helpers, and the #142 hooks.
The transport is mechanism-specific *here* on purpose; the hooks it calls
stay mechanism-agnostic. See
``docs/superpowers/specs/2026-05-23-file-exchange-143-filesystem-transport-design.md``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _typeshed import SupportsRead


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
