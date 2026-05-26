"""Bind the mechanism-agnostic artifact hooks (#142) to the ``filesystem`` transport.

Free functions for the four roles — provider/fetcher/receiver/sender —
composed over two private byte primitives (:func:`_stage` write,
:func:`_ingest` read), the #141 confinement helpers, and the #142 hooks.
The transport is mechanism-specific *here* on purpose; the hooks it calls
stay mechanism-agnostic. See
``docs/superpowers/specs/2026-05-23-file-exchange-143-filesystem-transport-design.md``.

The two consuming ops (``filesystem_fetcher_consume``,
``filesystem_sender_consume``) map failures to a §13-coded
``FileExchangeTransferError`` for the #148 middleware to render. The two
mint ops (``filesystem_provider_mint``, ``filesystem_receiver_mint``) are
offering-side: per spec §16 the offering roles emit well-formed references
rather than reporting §13 errors, so a hook/IO failure during minting
propagates to the offering tool's own handler unwrapped (``transfer-failed``
denotes a transfer in flight, which minting is not).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import stat
import uuid
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._paths import atomic_write, resolve_filesystem_uri
from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._staging import _CHUNK, _HASHLIB_BY_LABEL
from fastmcp_pvl_core._file_exchange._wire import (
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    TransferHandle,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import BinaryIO

    from _typeshed import SupportsRead

    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
    from fastmcp_pvl_core._file_exchange._paths import VolumeMap
    from fastmcp_pvl_core._file_exchange._wire import (
        ArtifactConstraints,
        ArtifactMetadata,
    )


class _HashingReader:
    """Wrap a readable byte stream, tallying size + a hash as bytes are read.

    ``shutil.copyfileobj`` (inside :func:`atomic_write`) calls ``.read(n)``;
    each chunk updates the digest and byte count before being returned, so
    after the copy ``.size`` and :meth:`hexdigest` describe exactly the bytes
    that passed through. Lets pvl-core compute size+digest in the single pass
    a non-seekable source stream allows (#142).
    """

    def __init__(self, stream: SupportsRead[bytes]) -> None:
        self._stream = stream
        self._hash = hashlib.sha256()
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

logger = logging.getLogger(__name__)


def _open_confined_readonly(path: Path) -> BinaryIO:
    """Open an already-confined path read-only, TOCTOU-guarded.

    ``O_NOFOLLOW`` rejects a final-component symlink swapped in between
    resolution (#141) and this open; ``O_NONBLOCK`` keeps the open from
    blocking on a planted FIFO (a regular file ignores it for reads);
    ``fstat`` rejects any non-regular target. Prefix-component races and full
    per-component ``openat`` traversal are out of scope — see the design
    doc's TOCTOU section. An ``os.open`` failure (including an ``O_NOFOLLOW``
    symlink rejection) or a non-regular target surfaces as ``not-accessible``;
    an unexpected ``os.fdopen`` failure after those checks propagates unwrapped
    (the consuming op maps it to ``transfer-failed``).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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
        stream = os.fdopen(fd, "rb")
        fd = -1  # fdopen took ownership; closing the stream closes the fd
    except BaseException:
        if fd != -1:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    return stream


def _verify_stream(stream: SupportsRead[bytes], artifact: ArtifactMetadata) -> None:
    """Read ``stream`` to EOF, checking size+digest against ``artifact``.

    Verifies only the fields the metadata declares (§7.1: both optional). An
    undecodable/unsupported digest algorithm is a verification failure
    (cannot verify -> ``digest-mismatch``), not a silent skip (§15). ``detail``
    is generic — no raw bytes/paths leak to the wire.
    """
    expected_hex = None
    hashlib_name = None
    if artifact.digest is not None:
        label, _, expected_hex = artifact.digest.partition(":")
        hashlib_name = _HASHLIB_BY_LABEL.get(label.lower())
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
        # Suppress a cleanup-close failure so it can't mask an in-flight
        # transfer error (and can't fail an already-completed stage); the
        # source stream is downstream code, so suppress broadly.
        with contextlib.suppress(Exception):
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


async def _ingest(
    path: Path,
    artifact: ArtifactMetadata,
    sink: ArtifactSink,
    artifact_id: str | None,
) -> None:
    """Read a confined ``path``, verify, then deposit into ``sink``.

    Two passes over one fd: pass 1 verifies size+digest (off-loop) so the
    sink never receives unverified bytes (§15 "validate before use"); pass 2
    rewinds and hands the stream to ``store_artifact`` (the sink reads, does
    not close — #142). pvl-core closes the fd. ``artifact`` is the handle's
    metadata, passed through to the sink. The sink reads the stream on the
    event loop; a sink with heavy I/O is responsible for offloading its own
    reads (pvl-core passes the raw fd, not a thread-bridged wrapper).
    """
    f = await asyncio.to_thread(_open_confined_readonly, path)
    try:
        await asyncio.to_thread(_verify_stream, f, artifact)
        await asyncio.to_thread(f.seek, 0)
        await sink.store_artifact(artifact_id, artifact, f)
    finally:
        # Suppress a cleanup-close failure (broadly, matching _stage) so it
        # can never mask an in-flight transfer error or fail an
        # already-completed ingest — the close runs via asyncio.to_thread, so
        # a non-OSError must not escape the finally either.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(f.close)


async def filesystem_fetcher_consume(
    handle: TransferHandle,
    source: FilesystemSource,
    sink: ArtifactSink,
    *,
    volume_map: VolumeMap,
) -> None:
    """Fetcher role (pull): read the already-selected ``source``.

    Verify against ``handle.artifact`` and deposit into ``sink``.

    Selection (``select_source``) is the caller's step. A descriptor that
    does not resolve/confine is ``not-accessible``; size/digest mismatches
    are ``size-mismatch``/``digest-mismatch``; any other failure (e.g. the
    sink raising) is ``transfer-failed``. The original cause is chained for
    local logs; only generic detail reaches the wire.
    """
    path = resolve_filesystem_uri(source.uri, volume_map=volume_map)
    if path is None:
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport="filesystem",
            detail="source descriptor did not resolve within a configured volume",
        )
    try:
        await _ingest(path, handle.artifact, sink, handle.artifact.id)
    except FileExchangeTransferError:
        raise
    except Exception as exc:
        raise FileExchangeTransferError(
            TransferErrorCode.TRANSFER_FAILED,
            transport="filesystem",
            detail="artifact transfer failed",
        ) from exc


def filesystem_receiver_mint(
    artifact_id: str,
    *,
    volume: str,
    volume_map: VolumeMap,
    expected: ArtifactConstraints | None = None,
) -> IntakeTicket:
    """Receiver role (push): allocate a deposit path and mint an IntakeTicket.

    The ticket's single ``filesystem`` sink points at the allocated path on
    ``volume``. Minting only — no hook is called and no bytes are written. The
    sender deposits later; the receiver's lazy ingest of the deposit into its
    own ``ArtifactSink`` (correlated by ``artifact_id``) is #144/#148, not here.
    """
    # Validate the volume is mapped now (fail loudly on misconfig); the deposit
    # path is built into the URI and resolved lazily at sender/ingest time.
    _require_volume(volume, volume_map)
    relpath = uuid.uuid4().hex
    descriptor = FilesystemSink(
        transport="filesystem", uri=f"exchange://{volume}/{relpath}"
    )
    return IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId=artifact_id,
        expected=expected,
        sinks=[descriptor],
    )


async def filesystem_sender_consume(
    sink: FilesystemSink,
    source: ArtifactSource,
    key: str,
    *,
    volume_map: VolumeMap,
) -> None:
    """Sender role (push): deposit ``key``'s bytes into ``sink``'s path.

    The write is atomic at 0o664. Selection (``select_sink``) is the caller's
    step. A descriptor that does not resolve/confine is ``not-accessible``; any
    other failure (e.g. the source hook raising, or a missing ``file://`` parent
    dir) is ``transfer-failed``. ``expected``-constraint enforcement is the
    receiver's at ingest time (#144/#148), not the sender's.
    """
    path = resolve_filesystem_uri(sink.uri, volume_map=volume_map)
    if path is None:
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport="filesystem",
            detail="sink descriptor did not resolve within a configured volume",
        )
    try:
        await _stage(source, key, path)
    except FileExchangeTransferError:
        raise
    except Exception as exc:
        raise FileExchangeTransferError(
            TransferErrorCode.TRANSFER_FAILED,
            transport="filesystem",
            detail="artifact transfer failed",
        ) from exc


def filesystem_source_readable(
    volume_map: VolumeMap,
) -> Callable[[FilesystemSource], bool]:
    """Build the ``is_accessible`` callback for ``select_source``.

    Returns ``True`` when the resolved location exists and is readable at
    selection time — a best-effort skip signal for §9 selection. The real
    I/O safety gate is :func:`_open_confined_readonly`, which re-checks at
    open time with ``O_NOFOLLOW``; a ``True`` here is not a transfer
    guarantee.
    """

    def _readable(source: FilesystemSource) -> bool:
        path = resolve_filesystem_uri(source.uri, volume_map=volume_map)
        if path is None:
            return False
        if not os.access(path, os.R_OK):
            logger.debug(
                "file-exchange: filesystem source resolved but is not readable; "
                "skipping (volume=%r)",
                urlsplit(source.uri).netloc,
            )
            return False
        return True

    return _readable


def filesystem_sink_writable(
    volume_map: VolumeMap,
) -> Callable[[FilesystemSink], bool]:
    """Build the ``is_accessible`` callback for ``select_sink``.

    Returns ``True`` when the deposit's parent dir exists and is writable at
    selection time (the target file itself need not exist yet) — a best-effort
    skip signal for §9 selection. The real atomicity guarantee is
    :func:`atomic_write`'s temp-then-rename; a ``True`` here is not a write
    guarantee.
    """

    def _writable(sink: FilesystemSink) -> bool:
        path = resolve_filesystem_uri(sink.uri, volume_map=volume_map)
        if path is None:
            return False
        parent = path.parent
        if not (parent.is_dir() and os.access(parent, os.W_OK)):
            logger.debug(
                "file-exchange: filesystem sink resolved but parent is not "
                "writable; skipping (volume=%r)",
                urlsplit(sink.uri).netloc,
            )
            return False
        return True

    return _writable
