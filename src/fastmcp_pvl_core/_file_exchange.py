"""MCP File Exchange v0.3 — protocol surface.

Implements the typed envelope (:class:`FileRef`, :class:`FileRefPreview`),
the URI parser (:class:`ExchangeURI`), and the capability-declaration
helper (:func:`register_file_exchange_capability`) for the spec at
``docs/specs/file-exchange.md``.

The runtime side (env detection, atomic writes, exchange-group lifecycle,
consumer URI resolution) lives in a separate module and is not yet
implemented — see issue #21.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
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
