"""MCP File Exchange — runtime for the ``exchange`` transfer method.

Implements the spec sections covering exchange-group membership,
deployer setup, directory layout, and producer/consumer requirements
(see ``docs/specs/file-exchange.md``). Consumes the protocol primitives
(``ExchangeURI``) from :mod:`fastmcp_pvl_core._file_exchange_protocol`.

Lifecycle constraints from the spec:

- The producer owns its namespace directory exclusively. Only the
  producer writes to or deletes its own files (TTL + LRU eviction via
  :meth:`FileExchange.sweep`).
- The consumer treats the exchange directory as read-only.
  :meth:`FileExchange.read_exchange_uri` never modifies or deletes
  files. Consumers MUST ignore dotfile names (in-progress writes).
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import secrets
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Final, cast

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fastmcp_pvl_core._env import env
from fastmcp_pvl_core._file_exchange_protocol import ExchangeURI, ExchangeURIError

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_pvl_core._token_store import UploadRecord, UploadStore

logger = logging.getLogger(__name__)


_EXCHANGE_ID_FILE: Final = ".exchange-id"
_DEFAULT_TTL_SECONDS: Final = 3600.0
_STREAM_CHUNK_SIZE: Final = 64 * 1024


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FileExchangeConfigError(RuntimeError):
    """File-exchange runtime cannot be configured.

    Raised when ``MCP_EXCHANGE_DIR`` points at an invalid path, when
    the persisted ``.exchange-id`` is corrupt (empty), or when the
    resolved namespace is itself invalid.
    """


class ExchangeGroupMismatch(ValueError):  # noqa: N818  -- spec-defined name
    """An ``exchange://`` URI references a different exchange group.

    Raised by :meth:`FileExchange.read_exchange_uri` when the URI's
    group id does not match this server's, or by
    :meth:`FileExchange.from_env` when an explicit
    ``MCP_EXCHANGE_ID`` conflicts with the persisted value.
    """


# ---------------------------------------------------------------------------
# .exchange-id resolution (spec §"Exchange Group" / "Deployer Setup")
# ---------------------------------------------------------------------------


_EMPTY_FILE_RETRY_ATTEMPTS = 10
_EMPTY_FILE_RETRY_INTERVAL_SECONDS = 0.01


def _read_exchange_id_file(path: Path) -> str:
    """Read and validate a persisted ``.exchange-id`` value.

    The spec mandates a UTF-8 plaintext UUID; consumers strip trailing
    whitespace before comparison.

    The naive ``O_CREAT | O_EXCL`` pattern has a narrow race: a winner
    creates the file, but a concurrent reader can ``open`` and ``read``
    after the file exists but before the winner has written the UUID
    bytes and fsynced. We treat an empty read as transient and retry a
    few times with short sleeps before declaring corruption — the
    winner's write-then-fsync completes in microseconds in practice, so
    the retry loop almost never runs more than once.

    A truly empty file (winner crashed mid-init) eventually surfaces
    as :class:`FileExchangeConfigError` so the deployer can clean up.
    """
    for attempt in range(_EMPTY_FILE_RETRY_ATTEMPTS):
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            if attempt > 0:
                logger.warning(
                    "exchange_id read succeeded after %d empty retries — "
                    "investigate if this recurs (path=%s)",
                    attempt,
                    path,
                )
            return raw
        time.sleep(_EMPTY_FILE_RETRY_INTERVAL_SECONDS)
    raise FileExchangeConfigError(
        f"{path} exists but is empty after retries; remove it manually "
        "after verifying no producer holds writes pending"
    )


def _resolve_exchange_id(base_dir: Path, explicit: str | None) -> str:
    """Read or atomically create ``$base_dir/.exchange-id``.

    See spec §"Deployer Setup". Uses ``O_CREAT | O_EXCL | O_WRONLY`` for
    the create attempt. The spec explicitly forbids ``rename(2)`` here
    because POSIX rename silently overwrites — which would split-brain
    on a race.

    Args:
        base_dir: Resolved ``MCP_EXCHANGE_DIR``.
        explicit: Value of ``MCP_EXCHANGE_ID`` if the deployer pinned it,
            otherwise ``None``.

    Returns:
        The exchange-group id (whitespace stripped).

    Raises:
        ExchangeGroupMismatch: ``explicit`` was set and disagrees with
            the value already persisted on disk.
        FileExchangeConfigError: A persisted ``.exchange-id`` exists
            but is empty.
    """
    id_path = base_dir / _EXCHANGE_ID_FILE

    # Fast path: file already exists with content, no init needed.
    if id_path.exists():
        existing = _read_exchange_id_file(id_path)
        if explicit is not None and existing != explicit:
            raise ExchangeGroupMismatch(
                f"MCP_EXCHANGE_ID={explicit!r} conflicts with existing "
                f"{id_path.name} value {existing!r}"
            )
        return existing

    candidate = explicit if explicit is not None else str(uuid.uuid4())
    payload = candidate.encode("utf-8") + b"\n"

    try:
        # Open restrictively (0o600) to satisfy CodeQL's
        # overly-permissive-open check at the syscall site. The
        # spec-mandated 0o644 is applied via fchmod below — fchmod
        # ignores umask so cross-UID containers in the group can read
        # this regardless of the producer's process umask.
        fd = os.open(
            str(id_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        # Another writer won the race. Read theirs.
        existing = _read_exchange_id_file(id_path)
        if explicit is not None and existing != explicit:
            raise ExchangeGroupMismatch(
                f"MCP_EXCHANGE_ID={explicit!r} conflicts with existing "
                f"{id_path.name} value {existing!r}"
            ) from None
        return existing

    try:
        try:
            # fchmod ignores umask — without this, a process running
            # with a restrictive umask (e.g. 0o077 in hardened
            # containers) gets 0o600 here and breaks cross-UID readers
            # in the group.
            os.fchmod(fd, 0o644)
            # ``os.write`` may perform a partial write under load.
            # Wrapping the fd in a buffered file object delegates the
            # loop-until-complete behaviour to Python's stdlib.
            with os.fdopen(fd, "wb", closefd=False) as f:
                f.write(payload)
                f.flush()
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        # Open succeeded but write/sync failed: leaving an empty file
        # would block every other server's init forever (the
        # _read_exchange_id_file retry loop eventually surfaces it as
        # a corruption error). Unlink so the next start can try again.
        try:
            os.unlink(id_path)
        except OSError:
            pass
        raise
    return candidate


def _exchange_segment(origin_id: str) -> str:
    """Derive the ``exchange://`` URI ``{id}`` segment from an ``origin_id``.

    The segment is a literal path component and filename, so it must
    satisfy the spec segment rules whatever the ``origin_id`` contains.
    A SHA-256 hex digest is unconditionally segment-safe, fixed-length,
    and deterministic — repeated writes for the same ``origin_id``
    resolve to the same exchange file. One-way by design: the
    ``file_ref`` carries the real ``origin_id``; nothing reverses this.
    """
    return hashlib.sha256(origin_id.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# FileExchange — env-driven runtime
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileExchange:
    """Configured exchange-volume runtime for one MCP server.

    Construct via :meth:`from_env`; that classmethod returns ``None``
    when the deployer has not enabled the exchange method (no
    ``MCP_EXCHANGE_DIR`` set), so callers should pattern-match::

        fx = FileExchange.from_env(default_namespace="vault-mcp")
        if fx is not None:
            uri = fx.write_atomic(origin_id="abc", ext="png", content=b"...")

    Attributes:
        base_dir: Resolved ``MCP_EXCHANGE_DIR`` (existing, is-a-directory).
        exchange_id: Group identifier from ``.exchange-id``.
        namespace: This server's namespace within the group; the
            sub-directory under ``base_dir`` it owns exclusively.
        ttl_seconds: Default TTL for produced files.
    """

    base_dir: Path
    exchange_id: str
    namespace: str
    ttl_seconds: float = _DEFAULT_TTL_SECONDS

    # ---- construction ------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        default_namespace: str,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> FileExchange | None:
        """Build a :class:`FileExchange` from the deployer's env.

        Returns ``None`` when ``MCP_EXCHANGE_DIR`` is unset — the
        server is simply not participating in the ``exchange`` method,
        which is a normal mode of operation per spec §"Exchange Group".
        A *set-but-empty* ``MCP_EXCHANGE_DIR`` is treated as a
        deployment misconfiguration and raises
        :class:`FileExchangeConfigError` rather than silently
        degrading to no-exchange mode.

        Args:
            default_namespace: Fallback namespace when
                ``MCP_EXCHANGE_NAMESPACE`` is unset. Typically the MCP
                server's logical name.
            ttl_seconds: Default lifetime for produced files.

        Raises:
            FileExchangeConfigError: ``MCP_EXCHANGE_DIR`` is set to an
                empty value, points at a non-existent path, or points
                at a non-directory; persisted ``.exchange-id`` is
                corrupt; resolved namespace is invalid.
            ExchangeGroupMismatch: An explicit ``MCP_EXCHANGE_ID``
                disagrees with the persisted value.
        """
        raw_present = os.environ.get("MCP_EXCHANGE_DIR")
        if raw_present is None:
            return None
        raw_dir = raw_present.strip()
        if not raw_dir:
            raise FileExchangeConfigError(
                "MCP_EXCHANGE_DIR is set but empty — unset it to disable "
                "the exchange method, or set a valid directory path"
            )
        base_dir = Path(raw_dir)
        if not base_dir.exists():
            raise FileExchangeConfigError(
                f"MCP_EXCHANGE_DIR={raw_dir!r} does not exist"
            )
        if not base_dir.is_dir():
            raise FileExchangeConfigError(
                f"MCP_EXCHANGE_DIR={raw_dir!r} is not a directory"
            )

        explicit_id = env("MCP", "EXCHANGE_ID")
        exchange_id = _resolve_exchange_id(base_dir, explicit_id)

        namespace_raw = env("MCP", "EXCHANGE_NAMESPACE") or default_namespace
        try:
            ExchangeURI.validate_segment(namespace_raw, role="json_param")
        except ExchangeURIError as exc:
            raise FileExchangeConfigError(
                f"resolved namespace {namespace_raw!r} is invalid: {exc}"
            ) from exc
        if namespace_raw.startswith("."):
            raise FileExchangeConfigError(
                f"namespace MUST NOT start with a dot: {namespace_raw!r}"
            )

        return cls(
            base_dir=base_dir,
            exchange_id=exchange_id,
            namespace=namespace_raw,
            ttl_seconds=float(ttl_seconds),
        )

    # ---- producer ----------------------------------------------------------

    def write_atomic(
        self, *, origin_id: str, ext: str, content: bytes | Path
    ) -> ExchangeURI:
        """Atomically write ``content`` to ``$base_dir/{namespace}/{id}.{ext}``.

        The write goes to a dotfile-prefixed temp path first, then
        ``os.rename``-s into place. Both steps live inside the same
        namespace directory so the rename is POSIX-atomic on the same
        filesystem (spec §"Producing server").

        Args:
            origin_id: Producer-chosen opaque reference. Never used
                verbatim as a path component — the ``exchange://`` URI
                ``{id}`` segment is derived from it via
                :func:`_exchange_segment` (spec v0.5 §"Security and
                Path Resolution"). Used here only for the debug log and
                as the derivation input.
            ext: File extension (no leading dot). Validated as a literal
                URI segment.
            content: Bytes to write, OR a :class:`pathlib.Path` to a
                file to copy. The Path form is stream-copied chunk by
                chunk and never materialises the full file in memory —
                use it for large producer files that would otherwise
                push the publisher near the 256 MiB cap.

        Returns:
            The resulting :class:`ExchangeURI`.

        Raises:
            ExchangeURIError: ``ext`` violates the spec segment rules
                from spec §"Security and Path Resolution".
        """
        ExchangeURI.validate_segment(ext, role="json_param")
        seg = _exchange_segment(origin_id)

        namespace_dir = self.base_dir / self.namespace
        namespace_dir.mkdir(mode=0o755, exist_ok=True)
        # mkdir() honours the umask; force 0o755 so consumers running as
        # different UIDs can list and read this directory.
        try:
            namespace_dir.chmod(0o755)
        except OSError as exc:
            logger.debug(
                "exchange_chmod_namespace_dir_failed dir=%s err=%s",
                namespace_dir,
                exc,
            )
        final_path = namespace_dir / f"{seg}.{ext}"
        # Include a random suffix in the temp name so two concurrent
        # writes for the same origin_id don't collide on the same temp
        # path (one writer's truncate would otherwise corrupt the
        # other's in-flight bytes). Atomic rename still gives last-
        # writer-wins on the final filename, which is the documented
        # contract — but each writer's stream is now isolated.
        unique = secrets.token_hex(8)
        tmp_path = namespace_dir / f".{seg}.{unique}.{ext}.tmp"

        try:
            # Open restrictively (0o600) to satisfy CodeQL's
            # overly-permissive-open check; fchmod sets the spec-mandated
            # 0o644 below.
            fd = os.open(
                str(tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            try:
                # fchmod ignores umask — needed for the same cross-UID
                # readability reason as the .exchange-id write above.
                os.fchmod(fd, 0o644)
                # Buffered write loops until the full payload lands;
                # raw os.write can do a partial write on large buffers.
                with os.fdopen(fd, "wb", closefd=False) as f:
                    if isinstance(content, bytes):
                        f.write(content)
                        bytes_written = len(content)
                    else:
                        # Stream from disk in 64 KiB chunks so a 256 MiB
                        # source file never lands in memory in one piece.
                        bytes_written = 0
                        with content.open("rb") as src:
                            while chunk := src.read(_STREAM_CHUNK_SIZE):
                                f.write(chunk)
                                bytes_written += len(chunk)
                    f.flush()
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(str(tmp_path), str(final_path))
        except BaseException:
            # Any failure between open and rename leaves the dotfile
            # temp on disk. Sweep skips dotfiles to stay race-safe with
            # in-flight writes, so an orphan would accrue silently —
            # clean it up before propagating.
            _try_unlink(tmp_path)
            raise

        logger.debug(
            "exchange_write namespace=%s id=%s ext=%s size=%d",
            self.namespace,
            origin_id,
            ext,
            bytes_written,
        )
        return ExchangeURI(
            exchange_id=self.exchange_id,
            namespace=self.namespace,
            id=seg,
            ext=ext,
        )

    # ---- consumer ----------------------------------------------------------

    def read_exchange_uri(self, uri: str, *, max_bytes: int | None = None) -> bytes:
        """Read the bytes at ``uri``.

        Validates per spec §"Security and Path Resolution", refuses
        URIs from a different exchange group, refuses dotfile filenames
        (per spec §"Directory Layout"), and reads the file.

        Args:
            uri: An ``exchange://`` URI.
            max_bytes: Optional ceiling on the file size, in bytes. The
                size is checked via ``stat`` before any bytes are read,
                so an oversized file never lands in memory. ``None``
                (default) skips the check; the consumer ``fetch_file``
                tool always passes its own cap so this default applies
                only to direct callers of the runtime.

        Returns:
            The file's bytes.

        Raises:
            ExchangeURIError: URI is malformed or violates segment rules.
            ExchangeGroupMismatch: URI is for a different exchange group.
            FileNotFoundError: No file exists at the resolved path.
            OSError: ``max_bytes`` is set and the file's size exceeds it.
        """
        parsed = ExchangeURI.parse(uri)
        if parsed.exchange_id != self.exchange_id:
            raise ExchangeGroupMismatch(
                "exchange URI is for group "
                f"{parsed.exchange_id!r} but this server is in group "
                f"{self.exchange_id!r}"
            )
        if parsed.id.startswith(".") or parsed.namespace.startswith("."):
            # Defence in depth: ExchangeURI.parse already rejects these,
            # but explicit re-check protects against future parser
            # changes that loosen the rules.
            raise ExchangeURIError(
                f"refusing dotfile filename (spec §'Directory Layout'): {uri!r}"
            )
        file_path = self.base_dir / parsed.namespace / parsed.filename
        if max_bytes is not None:
            size = file_path.stat().st_size
            if size > max_bytes:
                raise OSError(
                    f"exchange file {uri!r} exceeds max_bytes ({size} > {max_bytes})"
                )
        return file_path.read_bytes()

    # ---- lifecycle ---------------------------------------------------------

    def sweep(self, *, storage_ceiling_bytes: int | None = None) -> int:
        """Producer-owned TTL + LRU sweep of this server's namespace.

        Restart-resume safe: walks the on-disk namespace directory each
        call rather than relying on an in-process registry. Skips
        dotfiles unconditionally so it never races a producer's
        in-progress write.

        Args:
            storage_ceiling_bytes: Optional LRU eviction threshold.
                When set, after TTL eviction, the oldest (by mtime)
                non-dotfiles are deleted until the total namespace size
                fits under the ceiling.

        Returns:
            Number of files deleted.
        """
        namespace_dir = self.base_dir / self.namespace
        if not namespace_dir.exists():
            return 0
        now = time.time()
        cutoff = now - self.ttl_seconds
        removed = 0
        survivors: list[tuple[Path, os.stat_result]] = []

        # Iterate the iterdir() generator directly so we don't
        # materialise a list of every entry on namespace dirs holding
        # tens of thousands of files. The try/except catches OSError
        # from both the initial iterdir() call and from any
        # __next__() raise during traversal (some filesystems surface
        # permission flips at iteration time, others at open time).
        try:
            for entry in namespace_dir.iterdir():
                if entry.name.startswith("."):
                    continue
                try:
                    stat = entry.stat()
                except FileNotFoundError:
                    continue
                if not entry.is_file():
                    continue
                if stat.st_mtime < cutoff:
                    if _try_unlink(entry):
                        removed += 1
                else:
                    survivors.append((entry, stat))
        except OSError as exc:
            logger.warning(
                "exchange_sweep_iterdir_failed namespace=%s err=%s",
                self.namespace,
                exc,
            )
            return removed

        if storage_ceiling_bytes is not None and survivors:
            survivors.sort(key=lambda pair: pair[1].st_mtime)
            total = sum(s.st_size for _, s in survivors)
            for entry, stat in survivors:
                if total <= storage_ceiling_bytes:
                    break
                if _try_unlink(entry):
                    total -= stat.st_size
                    removed += 1

        if removed:
            logger.debug(
                "exchange_sweep namespace=%s removed=%d",
                self.namespace,
                removed,
            )
        return removed


def _try_unlink(path: Path) -> bool:
    """Best-effort delete; ``True`` if the file is gone afterwards."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        # Lost a race with another sweeper or the producer itself.
        return True
    except OSError as exc:
        if exc.errno == errno.EISDIR:
            # The is_file() check above filters dirs out; reaching here
            # means the entry's type changed between stat and unlink —
            # an external mutation worth surfacing.
            logger.warning(
                "exchange_sweep encountered a directory in namespace "
                "%s: %s — external mutation suspected",
                path.parent.name,
                path,
            )
            return False
        logger.warning("exchange_sweep_unlink_failed path=%s err=%s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Upload route (POST /<ns>/uploads/{token}) — spec §"Inbound HTTP transfer"
# ---------------------------------------------------------------------------


# pvl-core's file_exchange facade adapts the public ``SinkHook`` to
# this internal shape; the route itself only sees ``(record, file-like)``.
RouteSink = Callable[
    ["UploadRecord", BinaryIO],
    "Awaitable[Mapping[str, Any]]",
]

# Inbound POST body spool: keep small uploads in memory, spill larger
# ones to disk so a receive never holds the whole body in RAM at once.
_UPLOAD_SPOOL_MAX_BYTES: Final = 1024 * 1024


class _UploadOversizeError(BaseException):  # noqa: N818  -- internal sentinel
    """Internal sentinel for mid-stream oversize aborts.

    Raised by the bounded chunk generator when the request body exceeds
    ``record.max_bytes`` mid-stream. Caught inside the upload handler and
    translated to a ``413`` response. Not part of the sink contract —
    never propagates to user code.

    Derives from :class:`BaseException` (not :class:`Exception`) so the
    handler's broad ``except Exception`` (which maps a sink failure to a
    ``500``) cannot accidentally swallow the oversize abort raised while
    spooling the body and complete on a truncated upload. The handler
    catches it explicitly via
    ``except _UploadOversizeError``; ``BaseException`` subclasses remain
    catchable by name. Same pattern Python uses for
    :class:`asyncio.CancelledError` (Python 3.8+).
    """


def _accepts_match(content_type: str, accepts: tuple[str, ...]) -> bool:
    if "*/*" in accepts:
        return True
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    for entry in accepts:
        e = entry.strip().lower()
        if e == ct:
            return True
        if e.endswith("/*") and ct.startswith(e[:-1]):
            return True
    return False


def register_upload_route(
    mcp: FastMCP,
    *,
    store: UploadStore,
    namespace: str,
    sink: RouteSink,
    accepts: tuple[str, ...] = ("*/*",),
) -> None:
    """Mount ``POST /<namespace>/uploads/{token}`` on ``mcp``.

    The received body is spooled into a :class:`tempfile.SpooledTemporaryFile`
    — bounded in memory, spilling to disk past a threshold — and handed
    to ``sink`` as a sync file-like. The route returns:

    - ``200`` with the sink's mapping serialised as JSON on success.
    - ``404 Not Found`` if the token is unknown, expired, or already
      consumed (indistinguishable, to avoid leaking token state to a
      probing caller).
    - ``413 Payload Too Large`` if ``Content-Length`` exceeds
      ``record.max_bytes`` OR the body's running total exceeds the cap
      mid-stream (defense in depth against lying clients).
    - ``415 Unsupported Media Type`` if ``Content-Type`` does not match
      any entry in ``accepts`` (with ``*/*`` semantics as below).
    - ``400 Bad Request`` if the sink raises ``ValueError`` — the
      exception's ``str()`` is returned as the response body, so sinks
      SHOULD frame ``ValueError`` messages as caller-facing diagnostics
      ("invalid path", "extension not allowed").
    - ``409 Conflict`` if the sink raises ``FileExistsError`` — same
      body convention as 400.
    - ``500 Internal Server Error`` if the sink raises any other
      exception. The full traceback is written at ERROR; the response
      body is a generic ``"Internal Server Error"`` (does NOT echo
      ``str(exc)`` to avoid leaking internal detail).

    All non-success responses (4xx, 5xx) consume the token; clients
    MUST re-call ``create_upload_link`` to retry. This is intentional —
    the runtime consumes the token at the lookup step for anti-replay
    safety, before the precondition gates (size, content-type) and
    the sink run. A naive retry on the same URL after a 413 / 415
    therefore returns 404, not the original error code.

    The body is read through a bounded async generator wrapping
    ``request.stream()``; the running-total cap fires mid-iteration if
    the body exceeds ``record.max_bytes``, aborting before the spool is
    fully written. The runtime uses a :class:`BaseException` subclass
    internally to signal that oversize abort, so a sink's normal
    ``except Exception`` cannot accidentally swallow it.

    ``accepts`` is enforced before any body read — a request whose
    ``Content-Type`` does not match any entry returns 415. Use ``"*/*"``
    (the default) to disable the gate. Glob patterns like ``"image/*"``
    match any subtype. A request with no ``Content-Type`` header is
    treated as not matching any non-wildcard entry — explicit
    accept-lists therefore demand the header.
    """
    path = f"/{namespace}/uploads/{{token}}"

    @mcp.custom_route(path, methods=["POST"])
    async def _upload_handler(request: Request) -> Response:
        # Note: the request body is intentionally not consumed on
        # early-return error paths (404/413/415). ASGI servers
        # (uvicorn, hypercorn) handle the connection-state reset; we
        # do NOT need to call await request.body() to drain.
        token = request.path_params.get("token", "")
        record = store.consume(token)
        if record is None:
            # 404 covers all three token-unusable conditions — unknown,
            # expired, already-consumed — indistinguishably (spec
            # §"http_upload / POST contract": anti-leak, no 410).
            logger.debug("upload_handler_miss token_prefix=%s", (token or "")[:8])
            return Response(status_code=404)

        # Reject unsupported media types BEFORE any size check or body
        # read. ``"*/*"`` in ``accepts`` (the default) disables this gate.
        ct_header = request.headers.get("content-type", "")
        if not _accepts_match(ct_header, accepts):
            logger.info(
                "upload_handler_unsupported_media_type token_prefix=%s ct=%r",
                token[:8],
                ct_header,
            )
            return Response(
                content="Unsupported Media Type",
                status_code=415,
            )

        # Content-Length precheck — reject before reading any body bytes.
        cl = request.headers.get("content-length")
        if cl is not None:
            # Tolerate malformed/negative Content-Length: fall through to
            # the chunk-reader cap below. Rejecting here with 400 would
            # over-reject conformant clients sending unusual transfer
            # encodings; the real cap is enforced regardless of header.
            try:
                cl_int = int(cl)
            except ValueError:
                cl_int = -1
            if cl_int > record.max_bytes:
                logger.info(
                    "upload_handler_oversize_cl token_prefix=%s declared=%d max=%d",
                    token[:8],
                    cl_int,
                    record.max_bytes,
                )
                return Response(content="Payload Too Large", status_code=413)

        # Defense in depth — the client may lie about Content-Length. A
        # bounded async-generator wraps ``request.stream()`` with a
        # running-total cap; the handler spools its output into a
        # SpooledTemporaryFile. The cap fires mid-iteration, aborting
        # before the spool is fully written.
        async def _bounded_chunks() -> AsyncIterator[bytes]:
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > record.max_bytes:
                    logger.info(
                        "upload_handler_oversize_chunk token_prefix=%s total=%d max=%d",
                        token[:8],
                        total,
                        record.max_bytes,
                    )
                    raise _UploadOversizeError()
                yield chunk

        # Spool the bounded body into a sync file-like; the sink reads
        # it back. SpooledTemporaryFile keeps small bodies in memory and
        # spills larger ones to disk, so memory stays bounded regardless
        # of ``record.max_bytes``.
        spool: tempfile.SpooledTemporaryFile[bytes] = tempfile.SpooledTemporaryFile(
            max_size=_UPLOAD_SPOOL_MAX_BYTES
        )
        try:
            async for chunk in _bounded_chunks():
                # spool.write is a blocking disk write once the spool
                # spills past its in-memory threshold — keep it off the
                # event loop.
                await asyncio.to_thread(spool.write, chunk)
            spool.seek(0)
            result = await sink(record, cast("BinaryIO", spool))
        except _UploadOversizeError:
            return Response(content="Payload Too Large", status_code=413)
        except ValueError as exc:
            logger.info(
                "upload_sink_value_error token_prefix=%s: %s",
                token[:8],
                exc,
            )
            return Response(content=str(exc), status_code=400)
        except FileExistsError as exc:
            logger.info(
                "upload_sink_conflict token_prefix=%s: %s",
                token[:8],
                exc,
            )
            return Response(content=str(exc), status_code=409)
        except Exception as exc:
            # logger.exception attaches the full traceback (exc_info) at
            # ERROR level. We also include str(exc) in the formatted
            # message so the failure cause is visible in log scrapers
            # that only render the message field — getMessage() does
            # not include exc_info.
            logger.exception(
                "upload_sink_failure token_prefix=%s: %s",
                token[:8],
                exc,
            )
            return Response(
                content="Internal Server Error",
                status_code=500,
            )
        finally:
            spool.close()

        return JSONResponse(dict(result), status_code=200)


__all__ = [
    "ExchangeGroupMismatch",
    "FileExchange",
    "FileExchangeConfigError",
    "register_upload_route",
]
