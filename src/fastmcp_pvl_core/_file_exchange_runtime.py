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

import errno
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fastmcp_pvl_core._env import env
from fastmcp_pvl_core._file_exchange_protocol import ExchangeURI, ExchangeURIError

logger = logging.getLogger(__name__)


_EXCHANGE_ID_FILE: Final = ".exchange-id"
_DEFAULT_TTL_SECONDS: Final = 3600.0


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
        # fchmod ignores umask — without this, a process running with a
        # restrictive umask (e.g. 0o077 in hardened containers) gets
        # 0o600 here and breaks cross-UID readers in the group.
        os.fchmod(fd, 0o644)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return candidate


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

    def write_atomic(self, *, origin_id: str, ext: str, content: bytes) -> ExchangeURI:
        """Atomically write ``content`` to ``$base_dir/{namespace}/{id}.{ext}``.

        The write goes to a dotfile-prefixed temp path first, then
        ``os.rename``-s into place. Both steps live inside the same
        namespace directory so the rename is POSIX-atomic on the same
        filesystem (spec §"Producing server").

        Args:
            origin_id: Producer-chosen file id. Validated as a raw JSON
                parameter (spec §"Security and Path Resolution") — a
                literal ``%`` is preserved.
            ext: File extension (no leading dot). Validated the same
                way.
            content: Bytes to write.

        Returns:
            The resulting :class:`ExchangeURI`.

        Raises:
            ExchangeURIError: ``origin_id`` or ``ext`` violates spec
                segment rules from spec §"Security and Path Resolution",
                or ``origin_id`` starts with a dot
                (which would land in the dotfile name space and be
                hidden from consumers).
        """
        ExchangeURI.validate_segment(origin_id, role="json_param")
        ExchangeURI.validate_segment(ext, role="json_param")
        if origin_id.startswith("."):
            raise ExchangeURIError(
                f"origin_id MUST NOT start with a dot: {origin_id!r}"
            )

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
        final_path = namespace_dir / f"{origin_id}.{ext}"
        tmp_path = namespace_dir / f".{origin_id}.{ext}.tmp"

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
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(str(tmp_path), str(final_path))

        logger.debug(
            "exchange_write namespace=%s id=%s ext=%s size=%d",
            self.namespace,
            origin_id,
            ext,
            len(content),
        )
        return ExchangeURI(
            exchange_id=self.exchange_id,
            namespace=self.namespace,
            id=origin_id,
            ext=ext,
        )

    # ---- consumer ----------------------------------------------------------

    def read_exchange_uri(self, uri: str) -> bytes:
        """Read the bytes at ``uri``.

        Validates per spec §"Security and Path Resolution", refuses
        URIs from a different exchange group, refuses dotfile filenames
        (per spec §"Directory Layout"), and reads the file.

        Args:
            uri: An ``exchange://`` URI.

        Returns:
            The file's bytes.

        Raises:
            ExchangeURIError: URI is malformed or violates segment rules.
            ExchangeGroupMismatch: URI is for a different exchange group.
            FileNotFoundError: No file exists at the resolved path.
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


__all__ = [
    "ExchangeGroupMismatch",
    "FileExchange",
    "FileExchangeConfigError",
]
