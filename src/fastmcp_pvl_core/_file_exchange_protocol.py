r"""MCP File Exchange — protocol surface (typed envelope, URI, capability).

Implements the data model and capability declaration sections of the
file-exchange spec (see ``docs/specs/file-exchange.md``).
This module is pure data + small helpers; it has no I/O, no env access,
and no filesystem dependencies. The runtime side (env-driven group
membership, atomic writes, lifecycle sweep) lives in
:mod:`fastmcp_pvl_core._file_exchange_runtime`.

The capability-declaration helper :func:`register_file_exchange_capability`
uses fastmcp's documented ``on_initialize`` middleware hook to mutate
``result.capabilities.experimental["file_exchange"]``. If a future
fastmcp release exposes ``experimental_capabilities`` as a first-class
constructor argument or registry method, only :func:`_advertise_experimental`
needs to change.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

#: Spec version this module implements. Advertised as the ``version``
#: field in the ``experimental.file_exchange`` capability declaration
#: (spec §"Capability declaration"). Major.minor only — patch revisions
#: are spec-internal.
SPEC_VERSION = "0.4"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExchangeURIError(ValueError):
    """An ``exchange://`` URI or segment failed validation.

    See spec §"Security and Path Resolution".
    """


# ---------------------------------------------------------------------------
# Segment validation (spec §"Security and Path Resolution")
# ---------------------------------------------------------------------------
#
# The spec defines two contexts in which segment values appear:
# - inside an ``exchange://`` URI (where a single pass of percent-decoding
#   MUST be applied before validation, and double-encoded payloads MUST
#   be rejected); and
# - as raw JSON-RPC parameters such as ``origin_id`` (where decoding
#   MUST NOT be applied — a literal ``%`` in the value is data, not
#   encoding). Both contexts share the rule set below.

_FORBIDDEN_SEGMENT_CHARS = re.compile(r"[/\\\x00-\x1f]")
"""Path separators and ASCII control characters (U+0000–U+001F)."""

_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
"""Matches a residual ``%XX`` escape after one decode pass.

Residual escapes signal that the input was double-encoded — a known
traversal-bypass vector (``%252e%252e%252f`` decodes once to
``%2e%2e%2f`` which on a naive second pass becomes ``../``). The spec
§"Security and Path Resolution" mandates exactly one decode pass and
rejection of residuals.
"""


def _check_segment_rules(value: str, *, where: str) -> str:
    """Apply the spec's segment rules to an already-decoded value.

    See spec §"Security and Path Resolution".
    """
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


# ---------------------------------------------------------------------------
# Preview (spec §"Preview")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileRefPreview:
    """Lightweight LLM-facing metadata for a file (spec §"Preview").

    All fields are optional. Producers SHOULD include at least
    :attr:`description` when using the reference-only pattern. In the
    augmented-response pattern (where the surrounding tool result already
    carries the file's description, dimensions, etc.) ``preview`` may be
    omitted entirely.
    """

    description: str | None = None
    dimensions: tuple[int, int] | None = None
    thumbnail_base64: str | None = None
    thumbnail_mime_type: str | None = None
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the wire form defined in spec §"Preview".

        Optional fields with value ``None`` are omitted entirely so the
        wire payload stays compact.
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
        dimensions: tuple[int, int] | None = None
        dims_raw = raw.get("dimensions")
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


# ---------------------------------------------------------------------------
# File reference (spec §"File Reference")
# ---------------------------------------------------------------------------


# A transfer-methods block is method-key → method-specific metadata. The
# spec is forward-compatible with new method keys (e.g. ``s3``, ``scp``,
# ``gdrive``) so the structure is an open mapping rather than a sealed
# dataclass. Servers that do not recognise a method silently ignore it.
TransferMethods = Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class FileRef:
    """A pass-by-reference handle for a file produced by an MCP server.

    See spec §"File Reference". The interop surface a producer returns to the LLM.
    ``transfer`` advertises one or more methods the consumer can use to
    pick up the bytes; ``preview`` (when present) gives the LLM enough
    context to reason about the file without ingesting it.
    """

    origin_server: str
    origin_id: str
    transfer: TransferMethods
    mime_type: str | None = None
    size_bytes: int | None = None
    preview: FileRefPreview | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the wire form defined in spec §"File Reference"."""
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
        """Parse from the wire form, validating spec §"File Reference" invariants."""
        # ``raw.get(...) is None`` handles both "key absent" and "key
        # present but explicitly null" — a JSON ``null`` for a required
        # field is invalid input, not a silent default.
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
        preview_raw = raw.get("preview")
        if preview_raw is not None:
            if not isinstance(preview_raw, Mapping):
                raise ValueError(f"FileRef.preview must be a mapping: {preview_raw!r}")
            preview = FileRefPreview.from_dict(preview_raw)

        size_bytes_raw = raw.get("size_bytes")
        return cls(
            origin_server=str(raw["origin_server"]),
            origin_id=str(raw["origin_id"]),
            transfer=transfer,
            mime_type=raw.get("mime_type"),
            # JSON parsers may yield ``245760.0`` for an integer field;
            # coerce so the dataclass holds the type its annotation
            # promises.
            size_bytes=int(size_bytes_raw) if size_bytes_raw is not None else None,
            preview=preview,
        )


# ---------------------------------------------------------------------------
# Exchange URI (spec §"Exchange URI")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExchangeURI:
    """Parsed ``exchange://{exchange-id}/{namespace}/{id}.{ext}`` URI.

    See spec §"Exchange URI".
    """

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
        """Parse and validate an exchange URI.

        See spec §"Exchange URI" and §"Security and Path Resolution".
        The URI is split into ``(exchange_id, namespace, filename)``
        components. Each component is URI-decoded exactly once and then
        checked against the segment rules. A residual ``%XX`` pattern
        after one decode is rejected as double-encoded.

        Raises:
            ExchangeURIError: If the URI shape is wrong, if a segment
                violates the rules, or if any component appears to be
                double-encoded.
        """
        # urlsplit handles scheme parsing and surfaces any embedded
        # query/fragment so we can refuse them outright — spec URIs have
        # only path components.
        parts = urlsplit(uri)
        if parts.scheme != "exchange":
            raise ExchangeURIError(
                f"exchange URI must use scheme 'exchange://': {uri!r}"
            )
        if parts.query or parts.fragment:
            raise ExchangeURIError(
                f"exchange URI must not have query or fragment: {uri!r}"
            )
        if not parts.netloc or not parts.path:
            raise ExchangeURIError(
                f"exchange URI must have group, namespace, and filename: {uri!r}"
            )

        exchange_id_raw = parts.netloc
        path_segments = parts.path.lstrip("/").split("/")
        if len(path_segments) != 2:
            raise ExchangeURIError(
                "exchange URI path must be 'namespace/filename' "
                f"(got {len(path_segments)} segments): {uri!r}"
            )
        namespace_raw, filename_raw = path_segments

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
        # The whole filename was already segment-checked above, but the
        # *id* portion after rpartition can still be `.` or `..` when
        # the original filename is `...ext` (decodes from `%2e%2e.ext`).
        # Re-apply the rules to ``id`` and ``ext`` so traversal payloads
        # smuggled through the extension split are still rejected.
        _check_segment_rules(file_id, where="uri")
        _check_segment_rules(ext, where="uri")

        return cls(exchange_id=exchange_id, namespace=namespace, id=file_id, ext=ext)

    @classmethod
    def validate_segment(cls, value: str, *, role: Literal["uri", "json_param"]) -> str:
        """Validate one path segment per spec §"Security and Path Resolution".

        Args:
            value: The segment to validate.
            role: ``"uri"`` decodes the value once before validating
                (and rejects residual ``%XX`` to defeat double-encoded
                traversal). ``"json_param"`` validates the value as-is
                — JSON-RPC parameters such as ``origin_id`` MUST NOT be
                URI-decoded (a literal ``%`` is data, not encoding).

        Returns:
            The decoded (for ``role="uri"``) or raw (for
            ``role="json_param"``) value.

        Raises:
            ExchangeURIError: If the segment violates spec
                §"Security and Path Resolution".
            ValueError: If ``role`` is not ``"uri"`` or ``"json_param"``
                — defends against callers bypassing the Literal type.
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


# ---------------------------------------------------------------------------
# Capability declaration (spec §"Capability declaration")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileExchangeCapability:
    """Payload that lives under ``experimental.file_exchange``.

    See spec §"Capability declaration".

    ``namespace`` and ``exchange_id`` are validated at construction so a
    bad value can't leak into the capability dict and ultimately into
    ``exchange://`` URIs.
    """

    namespace: str
    transfer_methods: TransferMethods
    exchange_id: str | None = None
    produces: tuple[str, ...] = field(default_factory=tuple)
    consumes: tuple[str, ...] = field(default_factory=tuple)
    version: str = SPEC_VERSION

    def __post_init__(self) -> None:
        # Coerce list/tuple inputs so callers don't need to remember the
        # tuple constraint imposed by frozen+slots layout.
        object.__setattr__(self, "produces", tuple(self.produces))
        object.__setattr__(self, "consumes", tuple(self.consumes))
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


# ---------------------------------------------------------------------------
# Capability builder — merges download + upload contributions
# ---------------------------------------------------------------------------


@dataclass
class _FileExchangeCapabilityBuilder:
    """Accumulates per-direction contributions into one capability dict.

    Both ``register_file_exchange`` (download) and
    ``register_file_exchange_upload`` (upload) push their entries into a
    shared per-server instance keyed by the FastMCP instance; the actual
    capability dict is materialised by :meth:`build` once both
    registrars have run.

    The builder is intentionally mutable (not frozen) — it accumulates
    state across multiple registrar calls. The capability it produces
    via :meth:`build` is the immutable :class:`FileExchangeCapability`.
    """

    namespace: str
    exchange_id: str | None = None
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    legacy_capability_shape: bool = False
    _exchange_present: bool = False
    _download_tool: str | None = None
    _upload_tool: str | None = None
    _upload_max_bytes: int | None = None
    _upload_max_ttl_seconds: int | None = None
    _upload_accepts: tuple[str, ...] | None = None

    def set_exchange(self, present: bool = True) -> None:
        """Mark the ``exchange://`` shared-volume method as available."""
        self._exchange_present = present

    def set_download(self, *, tool_name: str) -> None:
        """Record the download-direction tool name."""
        self._download_tool = tool_name

    def set_upload(
        self,
        *,
        tool_name: str,
        max_bytes: int,
        max_ttl_seconds: int,
        accepts: tuple[str, ...] | None = None,
    ) -> None:
        """Record the upload-direction tool name plus per-method knobs."""
        self._upload_tool = tool_name
        self._upload_max_bytes = max_bytes
        self._upload_max_ttl_seconds = max_ttl_seconds
        self._upload_accepts = accepts

    def build(self) -> FileExchangeCapability | None:
        """Materialise the accumulated state into a FileExchangeCapability.

        Returns:
            ``None`` if no transfer method has been set (neither
            exchange nor download nor upload). Otherwise a frozen
            :class:`FileExchangeCapability` with ``version`` ``"0.2"``
            (legacy shape) or ``"0.4"`` (nested-http shape).
        """
        transfer_methods: dict[str, dict[str, Any]] = {}
        if self._exchange_present:
            transfer_methods["exchange"] = {}
        http_block = self._build_http_block()
        if http_block is not None:
            transfer_methods["http"] = http_block
        if not transfer_methods:
            return None
        version = "0.2" if self.legacy_capability_shape else "0.4"
        return FileExchangeCapability(
            namespace=self.namespace,
            exchange_id=self.exchange_id,
            produces=self.produces,
            consumes=self.consumes,
            transfer_methods=transfer_methods,
            version=version,
        )

    def _build_http_block(self) -> dict[str, Any] | None:
        if self.legacy_capability_shape:
            if self._upload_tool is not None:
                logger.warning(
                    "legacy_capability_shape=True drops the upload entry "
                    "(no nested http.upload key in v0.2 spec); upgrade "
                    "clients to v0.4 or unset legacy_capability_shape "
                    "to advertise upload."
                )
            if self._download_tool is None:
                return None
            return {"tool": self._download_tool}

        block: dict[str, Any] = {}
        if self._download_tool is not None:
            block["download"] = {"tool": self._download_tool}
        if self._upload_tool is not None:
            up: dict[str, Any] = {
                "tool": self._upload_tool,
                "max_bytes": self._upload_max_bytes,
                "max_ttl_seconds": self._upload_max_ttl_seconds,
            }
            if self._upload_accepts is not None:
                up["accepts"] = list(self._upload_accepts)
            block["upload"] = up
        return block or None


# ---------------------------------------------------------------------------
# Capability advertisement
# ---------------------------------------------------------------------------
#
# fastmcp does not currently expose a first-class hook for setting
# ``experimental`` capabilities on the MCP ``initialize`` response, but it
# does provide a documented ``Middleware.on_initialize`` lifecycle hook
# that can mutate the response before it returns. We use that — it is the
# supported extension point, not a monkey-patch. If a future fastmcp
# release adds a constructor kwarg or registry method, only
# :func:`_advertise_experimental` needs to change.

_EXPERIMENTAL_KEY = "file_exchange"


def _build_experimental_middleware() -> Any:
    """Build the ``Middleware`` subclass that advertises extra experimental keys.

    Defined inside a function so the import of fastmcp's
    :class:`~fastmcp.server.middleware.middleware.Middleware` base
    happens lazily — keeping module-import time fast and avoiding any
    circular-import surprise during fastmcp's own startup.
    """
    from fastmcp.server.middleware.middleware import Middleware

    class _ExperimentalCapabilityMiddleware(Middleware):
        """Mutates the ``initialize`` response to add experimental keys."""

        def __init__(self) -> None:
            super().__init__()
            self._payloads: dict[str, dict[str, Any]] = {}

        def set(self, key: str, payload: dict[str, Any]) -> None:
            self._payloads[key] = payload

        async def on_initialize(self, context: Any, call_next: Any) -> Any:
            result = await call_next(context)
            if result is None or not self._payloads:
                return result
            # ``capabilities.experimental`` is a free-form dict on the
            # MCP ServerCapabilities model. If absent, install it; if
            # present, merge so we don't trample anyone else's keys.
            caps = getattr(result, "capabilities", None)
            if caps is None:
                logger.error(
                    "experimental capability advertisement skipped: "
                    "initialize result has no .capabilities attribute "
                    "(keys=%s)",
                    list(self._payloads.keys()),
                )
                return result
            existing = getattr(caps, "experimental", None)
            merged: dict[str, dict[str, Any]] = dict(existing) if existing else {}
            for k, v in self._payloads.items():
                merged[k] = v
            try:
                caps.experimental = merged
            except (AttributeError, TypeError) as exc:
                # Direct assignment is forbidden in some pydantic
                # configurations. Fall back to model_copy when
                # available, and surface a hard error if neither path
                # works — silently dropping the advertisement breaks
                # capability-aware clients without leaving any signal.
                model_copy = getattr(result, "model_copy", None)
                if model_copy is None:
                    logger.error(
                        "experimental capability advertisement failed: "
                        "cannot mutate caps.experimental (%s) and "
                        "result has no model_copy fallback (keys=%s)",
                        exc,
                        list(self._payloads.keys()),
                    )
                    return result
                logger.info(
                    "experimental capability: using model_copy fallback (%s)",
                    exc,
                )
                new_caps = caps.model_copy(update={"experimental": merged})
                result = result.model_copy(update={"capabilities": new_caps})
            return result

    return _ExperimentalCapabilityMiddleware()


def _advertise_experimental(mcp: FastMCP, key: str, payload: dict[str, Any]) -> None:
    """Install or update an ``experimental.{key}`` capability advertisement.

    Today this works by attaching a single shared
    :class:`_ExperimentalCapabilityMiddleware` to the FastMCP instance
    and recording the payload on it. If the same ``mcp`` is registered
    against multiple times, the existing middleware's payload is
    updated in place — payloads do not stack.

    When fastmcp gains first-class ``experimental_capabilities``
    support, this function is the single place that needs to change.
    """
    middleware = getattr(mcp, "_pvl_experimental_middleware", None)
    if middleware is None:
        middleware = _build_experimental_middleware()
        # Stash on the instance so subsequent calls update the same one
        # rather than installing parallel middlewares that race.
        mcp._pvl_experimental_middleware = middleware  # type: ignore[attr-defined]
        mcp.add_middleware(middleware)
    middleware.set(key, payload)


def register_file_exchange_capability(
    mcp: FastMCP, capability: FileExchangeCapability
) -> None:
    """Advertise ``experimental.file_exchange`` on the MCP server.

    The capability becomes visible in the ``initialize`` response that
    every MCP client receives during the handshake. Calling this twice
    on the same ``mcp`` replaces the prior payload.

    Args:
        mcp: A FastMCP server instance.
        capability: The :class:`FileExchangeCapability` to advertise.
    """
    _advertise_experimental(mcp, _EXPERIMENTAL_KEY, capability.to_capability_dict())


__all__ = [
    "SPEC_VERSION",
    "ExchangeURI",
    "ExchangeURIError",
    "FileExchangeCapability",
    "FileRef",
    "FileRefPreview",
    "TransferMethods",
    "register_file_exchange_capability",
]
