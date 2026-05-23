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
import os
import stat
import uuid
from typing import TYPE_CHECKING

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._paths import atomic_write
from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
from fastmcp_pvl_core._file_exchange._wire import FilesystemSource, TransferHandle

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO

    from _typeshed import SupportsRead

    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSource
    from fastmcp_pvl_core._file_exchange._paths import VolumeMap
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

# Verifier maps a declared `<label>:` to a hashlib name; an unsupported label
# fails verification (cannot verify -> digest-mismatch), never silently skips.
_HASHLIB_BY_LABEL = {"sha-256": "sha256", "sha-384": "sha384", "sha-512": "sha512"}

_CHUNK = 1024 * 1024


def _open_confined_readonly(path: Path) -> BinaryIO:
    """Open an already-confined path read-only, TOCTOU-guarded.

    ``O_NOFOLLOW`` rejects a final-component symlink swapped in between
    resolution (#141) and this open; ``fstat`` rejects a non-regular target.
    Prefix-component races and full per-component ``openat`` traversal are out
    of scope — see the design doc's TOCTOU section. Any failure surfaces as
    ``not-accessible``.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport="filesystem",
            detail="source could not be opened read-only",
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FileExchangeTransferError(
                TransferErrorCode.NOT_ACCESSIBLE,
                transport="filesystem",
                detail="source is not a regular file",
            )
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _verify_stream(stream: SupportsRead[bytes], artifact: ArtifactMetadata) -> None:
    """Read ``stream`` to EOF, checking size+digest against ``artifact``.

    Verifies only the fields the metadata declares (§7.1: both optional). An
    undecodable/unsupported digest algorithm is a verification failure
    (cannot verify -> ``digest-mismatch``), not a silent skip (§15). ``detail``
    is generic — no raw bytes/paths leak to the wire.
    """
    label = expected_hex = None
    hashlib_name = None
    if artifact.digest is not None:
        label, _, expected_hex = artifact.digest.partition(":")
        hashlib_name = _HASHLIB_BY_LABEL.get(label)
    hasher = hashlib.new(hashlib_name) if hashlib_name is not None else None

    size = 0
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if hasher is not None:
            hasher.update(chunk)

    if artifact.size is not None and size != artifact.size:
        raise FileExchangeTransferError(
            TransferErrorCode.SIZE_MISMATCH,
            transport="filesystem",
            detail="transferred byte count did not match declared size",
        )
    if artifact.digest is not None:
        if hasher is None or hasher.hexdigest() != (expected_hex or "").lower():
            raise FileExchangeTransferError(
                TransferErrorCode.DIGEST_MISMATCH,
                transport="filesystem",
                detail="transferred bytes did not match declared digest",
            )


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


def _require_volume(volume: str, volume_map: VolumeMap) -> Path:
    """Return the mount root for ``volume`` or fail loudly.

    A mint op naming a volume the server has no mapping for is a caller/config
    mistake (not a per-transfer §13 failure), so it raises
    :class:`ConfigurationError` rather than ``FileExchangeTransferError``.
    """
    root = volume_map.get(volume)
    if root is None:
        raise ConfigurationError(
            f"file-exchange: mint volume {volume!r} is not in the volume map"
        )
    return root


async def filesystem_provider_mint(
    source: ArtifactSource,
    key: str,
    *,
    volume: str,
    volume_map: VolumeMap,
) -> TransferHandle:
    """Provider role (pull): stage ``key``'s bytes onto ``volume`` and mint a handle.

    The returned :class:`TransferHandle` has a single ``filesystem`` source
    pointing at the staged file, with computed ``size`` + ``digest`` folded into
    the metadata. ``volume`` names which mapped volume to stage into; how a
    server picks it is #148's concern. The staged file's lifecycle/cleanup is
    the provider's (§10.1.3) and out of scope here.
    """
    root = _require_volume(volume, volume_map)
    relpath = uuid.uuid4().hex
    size, digest, meta = await _stage(source, key, root / relpath)
    artifact = meta.model_copy(update={"size": size, "digest": digest})
    descriptor = FilesystemSource(
        transport="filesystem", uri=f"exchange://{volume}/{relpath}"
    )
    return TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=artifact,
        sources=[descriptor],
    )
