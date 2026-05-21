"""Pydantic wire models for the file-exchange extension (v0.1).

Mirrors the vendored wire schema in ``_schema/file-exchange.json`` —
the schema is the authority; these models provide typed runtime access
on top.

Two base configurations encode the §17 shape rules structurally:

* :class:`_WireBase` — ``extra="allow"`` per §17.2 (tolerant reading
  of unknown fields on non-descriptor objects).
* :class:`_DescriptorBase` — ``extra="forbid"`` per §17.5 (descriptor
  shapes are closed; an unknown field signals malformation).

Both are frozen to match pvl-core's ``dataclass(frozen=True)`` house
style. ``populate_by_name=True`` is set on both for forward-compat —
no field currently declares an alias, but the setting keeps alias
population working if a future minor renames a wire field.

The discriminated unions :data:`TransferSource` and :data:`TransferSink`
use a callable Pydantic v2 ``Discriminator`` so transport names outside
the known set route to :class:`UnknownTransportDescriptor`; a malformed
*known* transport (e.g. ``filesystem`` with an extra field) still fails
its closed branch — selection (#140) skips the fallthrough.

References (:class:`TransferHandle`, :class:`IntakeTicket`) have
``from_wire(raw)`` classmethods that run the four-layer pipeline:
jsonschema → Pydantic → version-skew (§17.3) → must-understand (§17.4).
Direct ``ClassName(...)`` construction enforces only Pydantic-level
invariants; the version-skew + must-understand checks live in
``from_wire`` because Pydantic v2 wraps validator exceptions into
:class:`pydantic.ValidationError`, which would erase the typed
:class:`UnsupportedVersionError` / :class:`UnsupportedRequirementError`
that #140's error envelope needs to dispatch on.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, cast

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    model_validator,
)

from fastmcp_pvl_core._file_exchange._spec import (
    SPEC_VERSION,
    VERSION_PATTERN,
    check_requires,
    check_version_skew,
)
from fastmcp_pvl_core._file_exchange._validation import validate_wire


class _WireBase(BaseModel):
    """Base for non-descriptor wire objects (open to unknown fields per §17.2)."""

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        populate_by_name=True,
    )


class _DescriptorBase(BaseModel):
    """Base for transport descriptors (closed shape per §17.5)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


# Patterns mirrored verbatim from schema/file-exchange.json. The schema
# is the authority; these constants exist so Pydantic field-level
# validation rejects malformed values before they reach the jsonschema
# layer. A future spec bump that loosens any of these must update both
# the vendored schema and these constants — the sync script's --check
# job catches schema drift, and the conformance fixtures exercise both
# sides via the test_roundtrip agreement parametrization.
_DIGEST_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]*:[0-9a-f]+$"
_FS_URI_PATTERN = r"^(exchange://[^/]+/.+|file:///[^/].*)$"
_HTTPS_URL_PATTERN = r"^https://"


class ArtifactMetadata(_WireBase):
    """Metadata describing an artifact, independent of transport (§7.1)."""

    id: str | None = None
    name: str | None = None
    mimeType: str | None = None  # noqa: N815  # wire-format name
    size: int | None = Field(default=None, ge=0)
    digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    description: str | None = None
    # ISO 8601 string deliberately kept as `str` — descriptor timestamps
    # (`expiresAt`) are parsed to a tz-aware `datetime` because selection
    # (§9) does arithmetic on them; `createdAt` is informational metadata,
    # kept byte-stable to avoid timezone-normalisation drift on round-trip.
    createdAt: str | None = None  # noqa: N815  # wire-format name

    @model_validator(mode="after")
    def _at_least_one_field(self) -> ArtifactMetadata:
        # §7.1 / schema `minProperties: 1`: a metadata object with no
        # properties at all is invalid. `model_dump(exclude_none=True)`
        # includes `extra="allow"` extras, so a payload whose only
        # property is an unknown forward-compat field still satisfies
        # the invariant — matches §17.2 tolerant reading.
        if not self.model_dump(exclude_none=True):
            raise ValueError("ArtifactMetadata MUST contain at least one field")
        return self


class ArtifactConstraints(_WireBase):
    """Constraints a receiver imposes on an artifact (§7.4 `expected` field)."""

    maxSize: int | None = Field(default=None, ge=0)  # noqa: N815
    acceptMimeTypes: list[str] | None = None  # noqa: N815
    requireDigest: list[str] | None = Field(default=None, min_length=1)  # noqa: N815


class FilesystemSource(_DescriptorBase):
    """Source descriptor: read an artifact from a shared filesystem (§7.2.1)."""

    transport: Literal["filesystem"]
    uri: str = Field(pattern=_FS_URI_PATTERN)


class DownloadSource(_DescriptorBase):
    """Source descriptor: read an artifact via an HTTPS download URL (§7.2.2)."""

    transport: Literal["download"]
    url: str = Field(pattern=_HTTPS_URL_PATTERN)
    # Parsed to a tz-aware datetime — §9 selection compares against
    # ``datetime.now(timezone.utc)``. ``AwareDatetime`` is the Pydantic
    # v2 type that rejects naive datetimes at validation time; the
    # schema layer's ``format: date-time`` enforces this on the wire
    # path (every ISO 8601 ``date-time`` literal carries an offset),
    # but ``AwareDatetime`` also blocks direct Python construction with
    # a naive value (``DownloadSource(expiresAt=datetime.now())``).
    # Without that guard, selection's comparison would raise
    # ``TypeError: can't compare offset-naive and offset-aware
    # datetimes`` at runtime in a future selection PR.
    expiresAt: AwareDatetime  # noqa: N815
    singleUse: bool = True  # noqa: N815


class FilesystemSink(_DescriptorBase):
    """Sink descriptor: write an artifact to a shared filesystem (§7.2.3)."""

    transport: Literal["filesystem"]
    uri: str = Field(pattern=_FS_URI_PATTERN)


class UploadSink(_DescriptorBase):
    """Sink descriptor: write an artifact via an HTTPS upload URL (§7.2.4)."""

    transport: Literal["upload"]
    url: str = Field(pattern=_HTTPS_URL_PATTERN)
    method: Literal["PUT", "POST"] = "PUT"
    # See :class:`DownloadSource.expiresAt` for the ``AwareDatetime``
    # rationale — selection (§9) compares against tz-aware ``now``.
    expiresAt: AwareDatetime  # noqa: N815


# Known transport sets per direction. _ALL_KNOWN_DESCRIPTOR_TRANSPORTS
# is the union because the schema's single UnknownTransportDescriptor
# is shared by both unions — a `download` value in a sink position
# still gets refused (it would otherwise fail FilesystemSink/UploadSink
# branches AND match the fallthrough, which the schema's `not.enum`
# explicitly prevents).
_KNOWN_SOURCE_TRANSPORTS = frozenset({"filesystem", "download"})
_KNOWN_SINK_TRANSPORTS = frozenset({"filesystem", "upload"})
_ALL_KNOWN_DESCRIPTOR_TRANSPORTS = _KNOWN_SOURCE_TRANSPORTS | _KNOWN_SINK_TRANSPORTS


class UnknownTransportDescriptor(BaseModel):
    """Fallthrough branch: a transport this implementation doesn't recognize.

    Per §17.5 the transport set is open. An unknown descriptor is
    parsed permissively (``extra="allow"``) — selection (#140) skips
    it. Known transport names route to their closed branches via the
    discriminator, so a malformed instance of a known descriptor (e.g.
    ``filesystem`` with an extra field) fails its closed branch
    rather than silently matching the fallthrough.

    Deliberately does NOT inherit from :class:`_DescriptorBase` —
    that base is ``extra="forbid"`` (closed shape), while the
    fallthrough is by design open to extras so a future-version
    descriptor's unknown fields don't break decode.
    """

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)
    transport: str

    @model_validator(mode="after")
    def _refuse_known_transport(self) -> UnknownTransportDescriptor:
        # Schema-side parity: the schema's UnknownTransportDescriptor
        # has ``not.enum: ["filesystem", "download", "upload"]`` so a
        # malformed known descriptor cannot match the fallthrough.
        # Direct Python construction has to honour the same invariant;
        # otherwise a future contributor building this class directly
        # silently violates §17.5.
        if self.transport in _ALL_KNOWN_DESCRIPTOR_TRANSPORTS:
            raise ValueError(
                f"UnknownTransportDescriptor refuses known transport "
                f"{self.transport!r}; route to its closed branch instead"
            )
        return self


def _source_discriminator(value: Any) -> str:
    t = (
        value.get("transport")
        if isinstance(value, dict)
        else getattr(value, "transport", None)
    )
    if t in _KNOWN_SOURCE_TRANSPORTS:
        return cast(str, t)
    return "unknown"


def _sink_discriminator(value: Any) -> str:
    t = (
        value.get("transport")
        if isinstance(value, dict)
        else getattr(value, "transport", None)
    )
    if t in _KNOWN_SINK_TRANSPORTS:
        return cast(str, t)
    return "unknown"


TransferSource = Annotated[
    (
        Annotated[FilesystemSource, Tag("filesystem")]
        | Annotated[DownloadSource, Tag("download")]
        | Annotated[UnknownTransportDescriptor, Tag("unknown")]
    ),
    Discriminator(_source_discriminator),
]
"""Discriminated union over `transport` for source descriptors (§9 selection input)."""


TransferSink = Annotated[
    (
        Annotated[FilesystemSink, Tag("filesystem")]
        | Annotated[UploadSink, Tag("upload")]
        | Annotated[UnknownTransportDescriptor, Tag("unknown")]
    ),
    Discriminator(_sink_discriminator),
]
"""Discriminated union over `transport` for sink descriptors (§9 selection input)."""


def _check_handle_or_ticket_requires(version: str, requires: list[str]) -> None:
    """Per-construction invariants on ``requires`` (§17.4 schema rules)."""
    if any(not r for r in requires):
        raise ValueError("`requires` entries MUST be non-empty strings")
    if len(set(requires)) != len(requires):
        raise ValueError("`requires` MUST NOT contain duplicate entries")
    if version == SPEC_VERSION and requires:
        raise ValueError(
            f"v{SPEC_VERSION} reference MUST omit or empty `requires` (§17.4)"
        )


class TransferHandle(_WireBase):
    """A pull token: artifact metadata + one or more source descriptors (§7.3).

    Construct from wire payloads via :meth:`from_wire` — it runs the
    full four-layer pipeline (jsonschema → Pydantic → §17.3
    version-skew → §17.4 must-understand). Direct construction
    (``TransferHandle(...)``) enforces only Pydantic-level invariants
    (type/shape/length/uniqueness/v0.1-empty-requires); version-skew
    and must-understand checks live in :meth:`from_wire` because
    Pydantic v2 would wrap their typed exceptions into a generic
    ``ValidationError`` if they ran inside a ``model_validator``,
    erasing the :class:`UnsupportedVersionError` /
    :class:`UnsupportedRequirementError` that #140's error envelope
    dispatches on.
    """

    type: Literal["nl.liesdonk.file-exchange/transfer-handle"]
    version: str = Field(pattern=VERSION_PATTERN)
    artifact: ArtifactMetadata
    sources: list[TransferSource] = Field(min_length=1)
    requires: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_requires_invariant(self) -> TransferHandle:
        _check_handle_or_ticket_requires(self.version, self.requires)
        return self

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> TransferHandle:
        """Validate a raw payload and construct a TransferHandle.

        Four layers in order: jsonschema → Pydantic → §17.3 version-skew
        → §17.4 must-understand. Raises :class:`WireFormatError`,
        :class:`pydantic.ValidationError`,
        :class:`UnsupportedVersionError`, or
        :class:`UnsupportedRequirementError` from the failing layer.
        """
        validate_wire(raw, kind="handle")
        handle = cls.model_validate(raw)
        check_version_skew(handle.version, kind="reference")
        check_requires(handle.requires)
        return handle


class IntakeTicket(_WireBase):
    """A push token: an artifactId + one or more sink descriptors (§7.4).

    Construction-vs-:meth:`from_wire` invariant split mirrors
    :class:`TransferHandle` — see its docstring for the rationale.
    """

    type: Literal["nl.liesdonk.file-exchange/intake-ticket"]
    version: str = Field(pattern=VERSION_PATTERN)
    artifactId: str  # noqa: N815
    expected: ArtifactConstraints | None = None
    sinks: list[TransferSink] = Field(min_length=1)
    requires: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_requires_invariant(self) -> IntakeTicket:
        _check_handle_or_ticket_requires(self.version, self.requires)
        return self

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> IntakeTicket:
        """Validate a raw payload and construct an IntakeTicket.

        Four-layer pipeline; see :meth:`TransferHandle.from_wire`.
        """
        validate_wire(raw, kind="ticket")
        ticket = cls.model_validate(raw)
        check_version_skew(ticket.version, kind="reference")
        check_requires(ticket.requires)
        return ticket


class TransferError(_WireBase):
    """Error payload placed under ``_meta["nl.liesdonk.file-exchange/error"]`` (§13)."""

    # Open code set per §13 — consumers SHOULD treat unknown codes as
    # generic failures; we don't constrain to a Literal.
    code: str
    transport: str | None = None
    detail: str | None = None

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> TransferError:
        """Validate and construct a TransferError envelope.

        Two layers: jsonschema → Pydantic. Error envelopes have no
        ``version`` and no ``requires``, so the §17.3/§17.4 layers
        of the reference pipeline don't apply.
        """
        validate_wire(raw, kind="error")
        return cls.model_validate(raw)
