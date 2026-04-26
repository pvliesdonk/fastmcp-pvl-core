"""MCP File Exchange v0.3 — protocol surface and runtime.

Implements the spec at ``docs/specs/file-exchange.md``:

- **Protocol surface**: :class:`FileRef`, :class:`FileRefPreview`,
  :class:`ExchangeURI` (parser + segment validator),
  :class:`FileExchangeCapability`, and the capability-declaration
  helper :func:`register_file_exchange_capability`.
- **Runtime**: :class:`FileExchange` for env-driven group membership,
  exclusive-create ``.exchange-id`` initialisation, atomic
  write/read of namespaced files, and producer-side TTL+LRU lifecycle
  sweep.

Lifecycle constraints from the spec:

- The *producing server* owns its namespace directory's lifecycle
  exclusively. Only the producer deletes its own files (TTL + LRU
  eviction via :meth:`FileExchange.sweep`).
- The *consuming server* treats the exchange directory as read-only —
  :meth:`FileExchange.read_exchange_uri` never modifies or deletes
  files. Consumers MUST ignore dotfile names.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: Spec version this module implements, advertised as ``version`` in the
#: ``experimental.file_exchange`` capability declaration. ``MAJOR.MINOR``
#: only — patch revisions are internal to the spec.
SPEC_VERSION = "0.3"


class ExchangeURIError(ValueError):
    """Raised when an exchange URI or segment fails spec §6.3 validation."""


# Path separators, ASCII control characters (U+0000..U+001F), and URI
# delimiters (``?`` query, ``#`` fragment) are rejected per spec §6.3.
# Excluding ``?`` and ``#`` from segments closes a parser-bypass where a
# query string or fragment slipped past split('/') and ended up
# concatenated into the file extension. The trailing/leading-whitespace
# and .-or-..-only checks are handled separately so error messages can
# pinpoint the violation.
_FORBIDDEN_SEGMENT_CHARS = re.compile(r"[/\\\x00-\x1f?#]")

#: Matches a percent-hex escape (``%XX``). Used to detect *residual*
#: percent-encoding after one URI-decode pass — its presence indicates
#: the value was double-encoded, which is a known traversal-bypass
#: vector and must be rejected.
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def _check_segment_rules(value: str, *, where: str) -> str:
    """Apply spec §6.3 segment rules to an already-decoded value."""
    if not value:
        raise ExchangeURIError(f"{where} segment is empty")
    if value != value.strip():
        raise ExchangeURIError(
            f"{where} segment has leading/trailing whitespace: {value!r}"
        )
    if value in (".", ".."):
        raise ExchangeURIError(f"{where} segment is path traversal: {value!r}")
    match = _FORBIDDEN_SEGMENT_CHARS.search(value)
    if match:
        char = match.group()
        raise ExchangeURIError(
            f"{where} segment contains forbidden character "
            f"{char!r} (offset {match.start()}): {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class FileRefPreview:
    """Lightweight metadata payload for the LLM (spec §3.2).

    All fields are optional. Producers SHOULD include at least
    :attr:`description` when using the reference-only pattern; in the
    augmented-response pattern the surrounding tool result already
    serves that purpose and ``preview`` may be omitted entirely.
    """

    description: str | None = None
    dimensions: tuple[int, int] | None = None
    thumbnail_base64: str | None = None
    thumbnail_mime_type: str | None = None
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the wire form defined in spec §3.2.

        Optional fields that are ``None`` are omitted entirely so the
        wire payload stays compact and matches the spec's "all preview
        fields are optional" rule.
        """
        out: dict[str, Any] = {}
        if self.description is not None:
            out["description"] = self.description
        if self.dimensions is not None:
            width, height = self.dimensions
            out["dimensions"] = {"width": width, "height": height}
        if self.thumbnail_base64 is not None:
            out["thumbnail_base64"] = self.thumbnail_base64
        if self.thumbnail_mime_type is not None:
            out["thumbnail_mime_type"] = self.thumbnail_mime_type
        if self.metadata is not None:
            out["metadata"] = dict(self.metadata)
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FileRefPreview:
        """Parse from the wire form, tolerating missing optional fields."""
        dims_raw = raw.get("dimensions")
        dimensions: tuple[int, int] | None = None
        if dims_raw is not None:
            if (
                not isinstance(dims_raw, Mapping)
                or dims_raw.get("width") is None
                or dims_raw.get("height") is None
            ):
                raise ValueError(
                    "preview.dimensions must have non-null 'width' and "
                    f"'height': {dims_raw!r}"
                )
            dimensions = (int(dims_raw["width"]), int(dims_raw["height"]))
        metadata_raw = raw.get("metadata")
        metadata: dict[str, Any] | None = None
        if metadata_raw is not None:
            if not isinstance(metadata_raw, Mapping):
                raise ValueError(
                    f"preview.metadata must be a mapping: {metadata_raw!r}"
                )
            metadata = dict(metadata_raw)
        return cls(
            description=raw.get("description"),
            dimensions=dimensions,
            thumbnail_base64=raw.get("thumbnail_base64"),
            thumbnail_mime_type=raw.get("thumbnail_mime_type"),
            metadata=metadata,
        )


# A transfer method block is method-key → method-specific metadata. The
# spec is forward-compatible with new method keys (s3, scp, gdrive, ...)
# so the structure stays an open Mapping rather than a sealed dataclass.
TransferMethods = Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class FileRef:
    """Pass-by-reference handle for a file produced by an MCP server (spec §3.1).

    The interop surface a producer returns to the LLM. ``transfer``
    advertises one or more methods the consumer can use to pick up the
    bytes; ``preview`` (when present) gives the LLM enough context to
    reason about the file without ingesting it.
    """

    origin_server: str
    origin_id: str
    transfer: TransferMethods
    mime_type: str | None = None
    size_bytes: int | None = None
    preview: FileRefPreview | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the wire form defined in spec §3.1."""
        out: dict[str, Any] = {
            "origin_server": self.origin_server,
            "origin_id": self.origin_id,
            "transfer": {k: dict(v) for k, v in self.transfer.items()},
        }
        if self.mime_type is not None:
            out["mime_type"] = self.mime_type
        if self.size_bytes is not None:
            out["size_bytes"] = self.size_bytes
        if self.preview is not None:
            out["preview"] = self.preview.to_dict()
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FileRef:
        """Parse from the wire form. Validates required fields and shape."""
        # ``raw.get(...) is None`` covers both "key absent" and
        # "key present but explicitly null" — the latter would otherwise
        # round-trip to ``str(None) == "None"`` for origin_server /
        # origin_id, which is silent corruption.
        for required in ("origin_server", "origin_id", "transfer"):
            if raw.get(required) is None:
                raise ValueError(
                    f"FileRef missing required field {required!r}: {raw!r}"
                )
        transfer_raw = raw["transfer"]
        if not isinstance(transfer_raw, Mapping) or not transfer_raw:
            raise ValueError(
                f"FileRef.transfer must be a non-empty mapping: {transfer_raw!r}"
            )
        transfer: dict[str, dict[str, Any]] = {}
        for method, meta in transfer_raw.items():
            if not isinstance(meta, Mapping):
                raise ValueError(
                    f"FileRef.transfer[{method!r}] must be a mapping: {meta!r}"
                )
            transfer[str(method)] = dict(meta)

        preview: FileRefPreview | None = None
        if "preview" in raw and raw["preview"] is not None:
            preview_raw = raw["preview"]
            if not isinstance(preview_raw, Mapping):
                raise ValueError(f"FileRef.preview must be a mapping: {preview_raw!r}")
            preview = FileRefPreview.from_dict(preview_raw)

        size_bytes_raw = raw.get("size_bytes")
        return cls(
            origin_server=str(raw["origin_server"]),
            origin_id=str(raw["origin_id"]),
            transfer=transfer,
            mime_type=raw.get("mime_type"),
            # JSON parsers may yield a float (245760.0) for an integer
            # value; coerce so the dataclass holds the type its
            # annotation promises.
            size_bytes=int(size_bytes_raw) if size_bytes_raw is not None else None,
            preview=preview,
        )


@dataclass(frozen=True, slots=True)
class ExchangeURI:
    """Parsed ``exchange://{exchange-id}/{namespace}/{id}.{ext}`` URI (spec §3.6)."""

    exchange_id: str
    namespace: str
    id: str
    ext: str

    @property
    def filename(self) -> str:
        """The on-disk filename: ``{id}.{ext}``."""
        return f"{self.id}.{self.ext}"

    def __str__(self) -> str:
        return f"exchange://{self.exchange_id}/{self.namespace}/{self.filename}"

    @classmethod
    def parse(cls, uri: str) -> ExchangeURI:
        """Parse and validate an exchange URI per spec §3.6 + §6.3.

        Each segment is URI-decoded exactly once and then checked against
        the segment rules. Residual ``%XX`` patterns after one decode are
        rejected as double-encoded (a known traversal-bypass vector).

        Raises:
            ExchangeURIError: If the URI shape is wrong, a segment violates
                the rules, or the value is double-encoded.
        """
        scheme = "exchange://"
        if not uri.startswith(scheme):
            raise ExchangeURIError(
                f"exchange URI must start with 'exchange://': {uri!r}"
            )
        rest = uri[len(scheme) :]
        parts = rest.split("/")
        if len(parts) != 3:
            raise ExchangeURIError(
                "exchange URI must have exactly three path segments "
                f"(group/namespace/file): {uri!r}"
            )
        exchange_id_raw, namespace_raw, filename_raw = parts

        exchange_id = cls.validate_segment(exchange_id_raw, role="uri")
        namespace = cls.validate_segment(namespace_raw, role="uri")
        if namespace.startswith("."):
            raise ExchangeURIError(
                f"namespace MUST NOT start with a dot: {namespace!r}"
            )
        filename = cls.validate_segment(filename_raw, role="uri")

        if "." not in filename:
            raise ExchangeURIError(
                f"filename must be of form 'id.ext', missing dot: {filename!r}"
            )
        file_id, _, ext = filename.rpartition(".")
        if not file_id:
            raise ExchangeURIError(f"filename id is empty (leading dot?): {filename!r}")
        if not ext:
            raise ExchangeURIError(
                f"filename ext is empty (trailing dot?): {filename!r}"
            )

        return cls(
            exchange_id=exchange_id,
            namespace=namespace,
            id=file_id,
            ext=ext,
        )

    @classmethod
    def validate_segment(cls, value: str, *, role: Literal["uri", "json_param"]) -> str:
        """Validate one path segment per spec §6.3.

        Args:
            value: The segment to validate.
            role: ``"uri"`` decodes once before validating (and rejects
                residual ``%XX`` to prevent double-encoded traversal).
                ``"json_param"`` validates the value as-is — JSON-RPC
                parameters such as ``origin_id`` MUST NOT be URI-decoded
                (a literal ``%`` in the value is data, not encoding).

        Returns:
            The decoded (for ``role="uri"``) or raw (for
            ``role="json_param"``) value.

        Raises:
            ExchangeURIError: If the segment violates spec §6.3.
            ValueError: If *role* is not one of ``"uri"`` / ``"json_param"``
                (Literal-typed; runtime check defends against callers
                bypassing the type system).
        """
        if role == "uri":
            decoded = unquote(value)
            if _PERCENT_ESCAPE.search(decoded):
                raise ExchangeURIError(
                    "URI segment contains residual percent-encoding "
                    f"after one decode pass (double-encoded?): {value!r}"
                )
            return _check_segment_rules(decoded, where="uri")
        if role == "json_param":
            return _check_segment_rules(value, where="json_param")
        raise ValueError(f"role must be 'uri' or 'json_param', got {role!r}")


@dataclass(frozen=True, slots=True)
class FileExchangeCapability:
    """Capability declaration payload for ``experimental.file_exchange`` (spec §3.9)."""

    namespace: str
    transfer_methods: TransferMethods
    exchange_id: str | None = None
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    version: str = SPEC_VERSION

    def __post_init__(self) -> None:
        # Coerce list/tuple inputs so callers don't have to remember the
        # tuple constraint just to satisfy the frozen/slots layout.
        object.__setattr__(self, "produces", tuple(self.produces))
        object.__setattr__(self, "consumes", tuple(self.consumes))

        # Validate namespace and exchange_id at construction so a bad
        # value can't propagate silently into capability dicts and
        # ultimately into exchange:// URIs. Spec §3.7 requires the
        # general segment rules; spec §3.8 additionally forbids a
        # leading dot on namespace.
        ExchangeURI.validate_segment(self.namespace, role="json_param")
        if self.namespace.startswith("."):
            raise ExchangeURIError(
                f"namespace MUST NOT start with a dot: {self.namespace!r}"
            )
        if self.exchange_id is not None:
            ExchangeURI.validate_segment(self.exchange_id, role="json_param")

    def to_capability_dict(self) -> dict[str, Any]:
        """Return the dict that lives under ``experimental.file_exchange``."""
        out: dict[str, Any] = {
            "version": self.version,
            "namespace": self.namespace,
            "produces": list(self.produces),
            "consumes": list(self.consumes),
            "transfer_methods": {k: dict(v) for k, v in self.transfer_methods.items()},
        }
        if self.exchange_id is not None:
            out["exchange_id"] = self.exchange_id
        return out


# Sentinels used to detect (and unwrap) a previously-installed wrapper
# so calling ``register_file_exchange_capability`` twice on the same
# server replaces the payload rather than nesting wrappers.
_WRAPPER_FLAG = "_pvl_file_exchange_wrapper"
_WRAPPER_ORIGINAL = "_pvl_file_exchange_original"


def register_file_exchange_capability(
    mcp: FastMCP, capability: FileExchangeCapability
) -> None:
    """Advertise *capability* under ``experimental.file_exchange``.

    FastMCP 3.x does not (yet) expose ``experimental_capabilities`` as a
    constructor argument or setter, and every transport call site
    invokes ``_mcp_server.create_initialization_options()`` with **no**
    arguments — so any state set elsewhere on the FastMCP instance is
    discarded before the capability dict is built. This helper patches
    that method on a per-instance basis to inject the spec's payload.

    Calling this twice on the same ``mcp`` instance replaces the
    previously-registered payload (the wrapper is unwrapped first, so
    repeated calls do not stack).

    See ``docs/specs/file-exchange.md`` § "Capability declaration via
    FastMCP" for the rationale and the long-term path (an upstream
    feature request to expose ``experimental_capabilities`` on
    ``FastMCP.__init__``).

    Args:
        mcp: A FastMCP server instance.
        capability: The :class:`FileExchangeCapability` to advertise.
    """
    payload = capability.to_capability_dict()

    # ``_mcp_server`` is FastMCP 3.x's lowlevel server. Verified against
    # fastmcp >=3,<4 in pyproject.toml; if a future major rev removes
    # the attribute, this ``getattr`` raises a clear AttributeError that
    # surfaces as "FastMCP internal API changed" rather than a silent
    # capability-declaration failure.
    ll = mcp._mcp_server
    original = ll.create_initialization_options
    if getattr(original, _WRAPPER_FLAG, False):
        original = getattr(original, _WRAPPER_ORIGINAL)

    def _patched(
        notification_options: Any = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        merged: dict[str, dict[str, Any]] = dict(experimental_capabilities or {})
        merged["file_exchange"] = payload
        return original(
            notification_options=notification_options,
            experimental_capabilities=merged,
            **kwargs,
        )

    setattr(_patched, _WRAPPER_FLAG, True)
    setattr(_patched, _WRAPPER_ORIGINAL, original)
    # Method-assignment is intentional: this is the documented patching
    # point for FastMCP 3.x's missing experimental_capabilities hook.
    ll.create_initialization_options = _patched  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

#: Default TTL for producer-owned exchange files (spec §7 default).
_DEFAULT_TTL_SECONDS = 3600.0


class FileExchangeConfigError(RuntimeError):
    """File-exchange runtime cannot be configured.

    Raised when ``MCP_EXCHANGE_DIR`` points at an invalid path or a
    required configuration value (e.g. namespace) is missing.
    """


class ExchangeGroupMismatch(ValueError):  # noqa: N818  -- spec-defined name
    """Exchange-group identity disagreement.

    Raised when an explicit ``MCP_EXCHANGE_ID`` conflicts with the
    persisted ``.exchange-id``, or when
    :meth:`FileExchange.read_exchange_uri` is asked for a URI from a
    different exchange group.
    """


def _read_existing_exchange_id(id_path: Path) -> str:
    """Read and validate a persisted ``.exchange-id`` value."""
    existing = id_path.read_text(encoding="utf-8").strip()
    if not existing:
        # Corrupt: O_EXCL succeeded for some prior writer that crashed
        # before populating the file. Surface visibly rather than
        # silently returning an empty group identifier.
        raise FileExchangeConfigError(
            f"{id_path} exists but is empty; remove it manually after "
            "verifying no producer holds writes pending"
        )
    return existing


def _resolve_exchange_id(base_dir: Path, explicit: str | None) -> str:
    """Read or atomically create ``$base_dir/.exchange-id`` (spec §3.5).

    Uses a *link-based* atomicity pattern rather than ``O_CREAT | O_EXCL``
    on the destination directly. The naive ``O_EXCL`` approach has a
    subtle race: ``open(O_EXCL)`` succeeds the moment the empty file
    exists, but the writer hasn't yet written the UUID. A concurrent
    reader that catches ``EEXIST`` and immediately ``read_text``-s sees
    an empty string. The link pattern avoids this by writing the full
    UUID payload to a per-thread temp file, fsyncing, then ``os.link``-ing
    the populated file into place. ``link(2)`` returns ``EEXIST`` if the
    destination already exists and *never* overwrites — same atomicity
    guarantee as ``O_EXCL`` but with the file content already on disk.

    ``rename(2)`` is *not* used: POSIX rename silently overwrites, which
    would quietly corrupt the group identity if two servers initialised
    simultaneously.

    Args:
        base_dir: Resolved ``MCP_EXCHANGE_DIR``.
        explicit: Value of ``MCP_EXCHANGE_ID`` if set; ``None`` means
            generate a fresh UUIDv4 on first call, read existing
            otherwise.

    Returns:
        The exchange-group ID (whitespace stripped per spec).

    Raises:
        ExchangeGroupMismatch: If *explicit* is set and disagrees with
            the value already persisted on disk.
        FileExchangeConfigError: If a persisted ``.exchange-id`` exists
            but is empty (corrupt from a prior crashed init).
    """
    id_path = base_dir / ".exchange-id"

    # Fast path: file already exists with content, no init needed.
    if id_path.exists():
        existing = _read_existing_exchange_id(id_path)
        if explicit is not None and existing != explicit:
            raise ExchangeGroupMismatch(
                f"MCP_EXCHANGE_ID={explicit!r} conflicts with existing "
                f"{id_path.name} value {existing!r}"
            )
        return existing

    candidate = explicit if explicit is not None else str(uuid.uuid4())
    payload = candidate.encode("utf-8") + b"\n"

    # Per-thread tmp name guarantees no collision among concurrent
    # writers in the same process; PID guards against same-thread-id
    # collisions across processes (extremely unlikely but free).
    tmp_path = base_dir / (f".exchange-id.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        # Restrictive 0o600 at create-time keeps CodeQL's
        # overly-permissive-permissions check quiet at the syscall site;
        # the spec-mandated 0o644 is applied via fchmod after the write
        # so consumers running as a different effective UID can read it.
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
            os.fchmod(fd, 0o644)
        finally:
            os.close(fd)

        try:
            os.link(tmp_path, id_path)
        except FileExistsError:
            # We lost the link race. The winner's content is already
            # on disk (they fsynced before linking), so reading is
            # guaranteed to return their populated value.
            existing = _read_existing_exchange_id(id_path)
            if explicit is not None and existing != explicit:
                raise ExchangeGroupMismatch(
                    f"MCP_EXCHANGE_ID={explicit!r} conflicts with existing "
                    f"{id_path.name} value {existing!r}"
                ) from None
            return existing

        logger.debug("exchange_id_created path=%s id=%s", id_path, candidate)
        return candidate
    finally:
        # Always remove the tmp — whether we won, lost the link race,
        # or an exception fired mid-write. Without this we'd leak a
        # ``.exchange-id.tmp.<pid>.<tid>`` per crashed init.
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


class FileExchange:
    """Producer/consumer runtime for the ``exchange://`` transfer method.

    Constructed via :meth:`from_env`; falls back to a sentinel
    "not configured" state when ``MCP_EXCHANGE_DIR`` is unset, so
    callers can always instantiate one and gate exchange-specific
    code paths on :attr:`is_configured`.

    Producer side:
        :meth:`write_atomic` — write content via dotfile-temp + POSIX
        rename, return an ``exchange://`` URI.
        :meth:`sweep` — TTL eviction (and optional LRU eviction by mtime)
        across this server's namespace directory. Producers own their
        namespace's lifecycle; only the producer deletes its own files.

    Consumer side:
        :meth:`read_exchange_uri` — parse the URI, verify it belongs to
        this exchange group, read the bytes. Consumers treat the
        exchange directory as read-only and ignore dotfile names.
    """

    def __init__(
        self,
        base_dir: Path | None,
        exchange_id: str | None,
        namespace: str | None,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        storage_ceiling_bytes: int | None = None,
    ) -> None:
        # Either all three are set (configured) or all three are None
        # (not configured). The two states are exclusive — there is no
        # partial-config valid state.
        configured = (
            base_dir is not None and exchange_id is not None and namespace is not None
        )
        partial = (
            base_dir is not None or exchange_id is not None or namespace is not None
        ) and not configured
        if partial:
            raise FileExchangeConfigError(
                "FileExchange requires base_dir, exchange_id, and namespace "
                "to all be set, or all be None"
            )
        # Direct instantiation bypasses ``from_env``'s validation, so
        # repeat the segment + dot-prefix checks here. ``from_env``
        # already runs them upstream — re-validating spec-compliant
        # values is cheap and the safety net catches bypass paths
        # (test harness, programmatic construction).
        if namespace is not None:
            ExchangeURI.validate_segment(namespace, role="json_param")
            if namespace.startswith("."):
                raise ExchangeURIError(
                    f"namespace MUST NOT start with a dot: {namespace!r}"
                )
        if exchange_id is not None:
            ExchangeURI.validate_segment(exchange_id, role="json_param")
        self._base_dir = base_dir
        self._exchange_id = exchange_id
        self._namespace = namespace
        self._ttl_seconds = float(ttl_seconds)
        self._storage_ceiling_bytes = storage_ceiling_bytes

    @classmethod
    def from_env(
        cls,
        default_namespace: str | None = None,
        *,
        env: Mapping[str, str] | None = None,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        storage_ceiling_bytes: int | None = None,
    ) -> FileExchange:
        """Build a :class:`FileExchange` from environment variables.

        Reads ``MCP_EXCHANGE_DIR`` (required to participate),
        ``MCP_EXCHANGE_ID`` (optional override), and
        ``MCP_EXCHANGE_NAMESPACE`` (optional override). If
        ``MCP_EXCHANGE_DIR`` is unset/empty, returns an unconfigured
        instance — callers gate writes on :attr:`is_configured`.

        Namespace resolution: ``MCP_EXCHANGE_NAMESPACE`` env var wins;
        falls back to ``default_namespace``. The runtime never reaches
        into FastMCP for the server name; pass it explicitly.

        Args:
            default_namespace: Namespace to use when
                ``MCP_EXCHANGE_NAMESPACE`` is unset. Required when the
                env var is not provided.
            env: Override the environment mapping (for tests). Defaults
                to ``os.environ``.
            ttl_seconds: TTL for files written by this producer
                (default 1 hour, per spec §7).
            storage_ceiling_bytes: Optional LRU ceiling for sweep.
                ``None`` disables LRU eviction.

        Returns:
            A configured :class:`FileExchange` if ``MCP_EXCHANGE_DIR``
            is set, otherwise an unconfigured sentinel
            (:attr:`is_configured` is ``False``).

        Raises:
            FileExchangeConfigError: ``MCP_EXCHANGE_DIR`` is set but
                does not point at an existing directory, or no
                namespace can be resolved.
            ExchangeGroupMismatch: ``MCP_EXCHANGE_ID`` is set and
                disagrees with the persisted ``.exchange-id``.
            ExchangeURIError: The resolved namespace fails spec §6.3
                segment validation.
        """
        environ = dict(env) if env is not None else dict(os.environ)
        raw_dir = environ.get("MCP_EXCHANGE_DIR", "").strip()
        if not raw_dir:
            return cls(
                None,
                None,
                None,
                ttl_seconds=ttl_seconds,
                storage_ceiling_bytes=storage_ceiling_bytes,
            )

        base_dir = Path(raw_dir)
        if not base_dir.exists():
            raise FileExchangeConfigError(
                f"MCP_EXCHANGE_DIR does not exist: {base_dir}"
            )
        if not base_dir.is_dir():
            raise FileExchangeConfigError(
                f"MCP_EXCHANGE_DIR is not a directory: {base_dir}"
            )
        base_dir = base_dir.resolve()

        ns_env = environ.get("MCP_EXCHANGE_NAMESPACE", "").strip()
        namespace = ns_env or default_namespace
        if not namespace:
            raise FileExchangeConfigError(
                "namespace is required: set MCP_EXCHANGE_NAMESPACE or pass "
                "default_namespace"
            )
        ExchangeURI.validate_segment(namespace, role="json_param")
        if namespace.startswith("."):
            raise ExchangeURIError(
                f"namespace MUST NOT start with a dot: {namespace!r}"
            )

        explicit_id = environ.get("MCP_EXCHANGE_ID", "").strip() or None
        # Validate ``explicit_id`` BEFORE persisting it. An invalid
        # value reaching ``_resolve_exchange_id`` would link-write
        # bad content into ``.exchange-id`` first and only raise on
        # the post-resolve check, leaving the corrupt file behind —
        # subsequent runs would then read it and disagree with any
        # corrected MCP_EXCHANGE_ID (forcing the operator to manually
        # delete .exchange-id with no diagnostic pointing them to it).
        if explicit_id is not None:
            ExchangeURI.validate_segment(explicit_id, role="json_param")
        exchange_id = _resolve_exchange_id(base_dir, explicit_id)
        # Post-resolve validation handles the corrupt-file recovery
        # case: a pre-fix deployment may have left a malformed value
        # in .exchange-id; surface it here rather than at first write.
        ExchangeURI.validate_segment(exchange_id, role="json_param")

        return cls(
            base_dir,
            exchange_id,
            namespace,
            ttl_seconds=ttl_seconds,
            storage_ceiling_bytes=storage_ceiling_bytes,
        )

    @property
    def is_configured(self) -> bool:
        return self._base_dir is not None

    @property
    def base_dir(self) -> Path:
        if self._base_dir is None:
            raise FileExchangeConfigError("FileExchange is not configured")
        return self._base_dir

    @property
    def exchange_id(self) -> str:
        if self._exchange_id is None:
            raise FileExchangeConfigError("FileExchange is not configured")
        return self._exchange_id

    @property
    def namespace(self) -> str:
        if self._namespace is None:
            raise FileExchangeConfigError("FileExchange is not configured")
        return self._namespace

    def write_atomic(
        self,
        *,
        origin_id: str,
        ext: str,
        content: bytes,
        mime_type: str | None = None,
    ) -> str:
        """Write *content* atomically and return its ``exchange://`` URI.

        Validation:
            ``origin_id`` and ``ext`` are validated as raw JSON params
            (spec §6.3 — no URI decoding).

        Atomicity:
            Content is written to ``$ns/.{origin_id}.{ext}.tmp``
            (dotfile-prefixed so consumers ignore it), fsynced, then
            POSIX-renamed to ``$ns/{origin_id}.{ext}``. A crash
            mid-write leaves only the dotfile behind, which consumers
            silently skip.

        Args:
            origin_id: File identifier within this namespace.
            ext: File extension without leading dot (e.g. ``"png"``).
            content: Raw bytes to persist.
            mime_type: Advisory; logged only. The on-disk file has no
                MIME metadata; the producer's tool result carries it.

        Returns:
            ``exchange://{exchange_id}/{namespace}/{origin_id}.{ext}``

        Raises:
            FileExchangeConfigError: The runtime is not configured.
            ExchangeURIError: ``origin_id`` or ``ext`` violates §6.3.
        """
        if (
            self._base_dir is None
            or self._namespace is None
            or self._exchange_id is None
        ):
            raise FileExchangeConfigError(
                "FileExchange is not configured; cannot write_atomic"
            )
        ExchangeURI.validate_segment(origin_id, role="json_param")
        ExchangeURI.validate_segment(ext, role="json_param")
        # Spec §5: consumers MUST ignore dotfiles, and both
        # read_exchange_uri and sweep filter dotfile names. Writing a
        # dot-prefix filename here would create a file no consumer can
        # ever read AND that sweep silently skips — pure storage leak.
        # Block at the producer rather than letting the asymmetry
        # accumulate dead files on the shared volume.
        if origin_id.startswith("."):
            raise ExchangeURIError(
                f"origin_id MUST NOT start with a dot (would create a "
                f"dotfile invisible to consumers and sweep): {origin_id!r}"
            )
        if ext.startswith("."):
            raise ExchangeURIError(f"ext MUST NOT start with a dot: {ext!r}")

        ns_dir = self._base_dir / self._namespace
        ns_dir.mkdir(mode=0o755, exist_ok=True)
        final_path = ns_dir / f"{origin_id}.{ext}"
        tmp_path = ns_dir / f".{origin_id}.{ext}.tmp"

        # Note: O_TRUNC handles the case where a stale tmp from a prior
        # crash exists at the same name. The dotfile prefix means
        # consumers can never see the partial state; rename is the
        # commit point. Restrictive 0o600 at create + fchmod to 0o644
        # after write keeps CodeQL's overly-permissive-permissions
        # check quiet at the syscall site, while still landing the
        # cross-UID-readable mode the shared-volume topology needs.
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
            os.fsync(fd)
            os.fchmod(fd, 0o644)
        finally:
            os.close(fd)
        os.rename(tmp_path, final_path)

        logger.info(
            "exchange_write namespace=%s origin_id=%s ext=%s size=%d mime=%s",
            self._namespace,
            origin_id,
            ext,
            len(content),
            mime_type or "-",
        )
        return f"exchange://{self._exchange_id}/{self._namespace}/{origin_id}.{ext}"

    def read_exchange_uri(self, uri: str) -> bytes:
        """Resolve an ``exchange://`` URI to bytes (spec §3.6 + §3.7).

        Cross-namespace reads are allowed (a consumer can read any
        namespace under its own ``$MCP_EXCHANGE_DIR``); the only
        gate is the exchange-group identifier match.

        Raises:
            FileExchangeConfigError: The runtime is not configured.
            ExchangeURIError: The URI fails spec §6.3 (path traversal,
                forbidden chars, double-encoded, dotfile name, etc.).
            ExchangeGroupMismatch: ``parsed.exchange_id`` differs from
                this server's exchange-group ID.
            FileNotFoundError: The file is not present on disk (the
                producer hasn't written it, it expired, or the URI
                refers to a never-created file).
        """
        if self._base_dir is None or self._exchange_id is None:
            raise FileExchangeConfigError(
                "FileExchange is not configured; cannot read_exchange_uri"
            )
        parsed = ExchangeURI.parse(uri)
        if parsed.exchange_id != self._exchange_id:
            raise ExchangeGroupMismatch(
                f"exchange group mismatch: local={self._exchange_id!r}, "
                f"uri={parsed.exchange_id!r}"
            )
        # Per spec §5: "Consumers MUST ignore dotfiles." A leading-dot
        # filename id would point at a producer's in-progress write
        # (or a never-meant-to-be-public file); refuse rather than
        # serving partial state.
        if parsed.id.startswith("."):
            raise ExchangeURIError(
                f"filename id starts with dot (dotfiles are not consumer-visible): "
                f"{parsed.id!r}"
            )
        path = self._base_dir / parsed.namespace / parsed.filename
        return path.read_bytes()

    def sweep(self) -> int:
        """Evict expired and over-ceiling files from this server's namespace.

        Producer-only operation: only acts on files under
        ``$base_dir/{self.namespace}/``, never on other namespaces.
        Idempotent — safe to call from a timer or shutdown hook.

        Eviction order:
            1. TTL: any non-dotfile older than ``ttl_seconds``.
            2. LRU: if ``storage_ceiling_bytes`` was set and the
               survivors still exceed it, oldest-by-mtime first
               until below the ceiling.

        Restart-resume: the implementation rebuilds its working set
        from the filesystem on every call rather than relying on an
        in-memory registry, so it works correctly after a process
        restart.

        Returns:
            Number of files evicted.
        """
        if self._base_dir is None or self._namespace is None:
            return 0
        ns_dir = self._base_dir / self._namespace
        if not ns_dir.is_dir():
            return 0

        now = time.time()
        ceiling = self._storage_ceiling_bytes
        evicted = 0
        # Single pass: TTL-evict immediately and collect surviving
        # entries' (path, mtime, size) for the optional LRU pass. Two
        # passes was an extra full scan + stat() per file for no
        # functional gain — gemini-code-assist flagged this as a
        # medium-priority optimisation.
        survivors: list[tuple[Path, float, int]] = []
        for entry in list(ns_dir.iterdir()):
            if entry.name.startswith("."):
                continue  # producer's own in-progress writes
            try:
                stat = entry.stat()
            except FileNotFoundError:
                continue
            if now - stat.st_mtime > self._ttl_seconds:
                try:
                    entry.unlink()
                    evicted += 1
                except FileNotFoundError:
                    pass
            elif ceiling is not None:
                survivors.append((entry, stat.st_mtime, stat.st_size))

        # LRU pass — only when a ceiling is configured.
        if ceiling is not None and survivors:
            total = sum(size for _, _, size in survivors)
            survivors.sort(key=lambda triple: triple[1])  # oldest first
            for path, _, size in survivors:
                if total <= ceiling:
                    break
                try:
                    path.unlink()
                    evicted += 1
                except FileNotFoundError:
                    # External delete beat us to it — the file is gone
                    # so its size shouldn't count toward the running
                    # total either, otherwise we'd over-evict a
                    # surviving file to compensate for a phantom byte.
                    pass
                total -= size

        if evicted:
            logger.info(
                "exchange_sweep namespace=%s evicted=%d", self._namespace, evicted
            )
        return evicted
