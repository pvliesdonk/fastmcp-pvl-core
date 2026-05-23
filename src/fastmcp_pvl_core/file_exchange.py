"""Public namespace for the file-exchange extension.

Downstream code imports from here::

    from fastmcp_pvl_core import file_exchange
    handle = file_exchange.TransferHandle.from_wire(raw_dict)
    cap = file_exchange.capability_declaration(roles={"provider": ["filesystem"]})

The implementation lives in :mod:`fastmcp_pvl_core._file_exchange`;
this module re-exports the public surface explicitly so static
analyzers (Pyright, mypy in strict mode, IDE autocomplete) can
resolve ``file_exchange.SomeName`` without running the import at
type-check time. Wildcard re-export was tried first and silently
broke downstream type checking — explicit imports are the right
default for a public namespace module.
"""

from fastmcp_pvl_core._file_exchange import (
    HANDLE_TYPE,
    KNOWN_CODES,
    NAMESPACE,
    SPEC_SOURCE_SHA,
    SPEC_VERSION,
    TICKET_TYPE,
    VERSION_PATTERN,
    ArtifactConstraints,
    ArtifactMetadata,
    ArtifactSink,
    ArtifactSource,
    DownloadSource,
    FileExchangeCapability,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    Role,
    TransferError,
    TransferErrorCode,
    TransferHandle,
    TransferSink,
    TransferSource,
    UnknownTransportDescriptor,
    UnsupportedRequirementError,
    UnsupportedVersionError,
    UploadSink,
    VolumeMap,
    WireFormatError,
    atomic_write,
    build_file_exchange_error,
    canonicalize_and_confine,
    capability_declaration,
    check_requires,
    check_version_skew,
    load_volume_map,
    resolve_filesystem_uri,
    select_sink,
    select_source,
    validate_wire,
)

__all__ = [
    "ArtifactConstraints",
    "ArtifactMetadata",
    "ArtifactSink",
    "ArtifactSource",
    "DownloadSource",
    "FileExchangeCapability",
    "FilesystemSink",
    "FilesystemSource",
    "HANDLE_TYPE",
    "IntakeTicket",
    "KNOWN_CODES",
    "NAMESPACE",
    "Role",
    "SPEC_SOURCE_SHA",
    "SPEC_VERSION",
    "TICKET_TYPE",
    "TransferError",
    "TransferErrorCode",
    "TransferHandle",
    "TransferSink",
    "TransferSource",
    "UnknownTransportDescriptor",
    "UnsupportedRequirementError",
    "UnsupportedVersionError",
    "UploadSink",
    "VERSION_PATTERN",
    "VolumeMap",
    "WireFormatError",
    "atomic_write",
    "build_file_exchange_error",
    "canonicalize_and_confine",
    "capability_declaration",
    "check_requires",
    "check_version_skew",
    "load_volume_map",
    "resolve_filesystem_uri",
    "select_sink",
    "select_source",
    "validate_wire",
]
