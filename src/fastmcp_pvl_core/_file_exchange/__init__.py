"""File-exchange extension implementation (nl.liesdonk.file-exchange v0.1).

Public surface lives at :mod:`fastmcp_pvl_core.file_exchange` (a
namespace re-export). This subpackage is private; downstream code
imports from the namespace module, not from here.

Design: ``docs/superpowers/specs/2026-05-21-file-exchange-139-wire-format-design.md``.
"""

from fastmcp_pvl_core._file_exchange._capability import (
    FileExchangeCapability,
    Role,
    capability_declaration,
)
from fastmcp_pvl_core._file_exchange._spec import (
    HANDLE_TYPE,
    NAMESPACE,
    SPEC_SOURCE_SHA,
    SPEC_VERSION,
    TICKET_TYPE,
    VERSION_PATTERN,
    UnsupportedRequirementError,
    UnsupportedVersionError,
    check_requires,
    check_version_skew,
)
from fastmcp_pvl_core._file_exchange._validation import (
    WireFormatError,
    validate_wire,
)
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    ArtifactMetadata,
    DownloadSource,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    TransferError,
    TransferHandle,
    TransferSink,
    TransferSource,
    UnknownTransportDescriptor,
    UploadSink,
)

__all__ = [
    "ArtifactConstraints",
    "ArtifactMetadata",
    "DownloadSource",
    "FileExchangeCapability",
    "FilesystemSink",
    "FilesystemSource",
    "HANDLE_TYPE",
    "IntakeTicket",
    "NAMESPACE",
    "Role",
    "SPEC_SOURCE_SHA",
    "SPEC_VERSION",
    "TICKET_TYPE",
    "TransferError",
    "TransferHandle",
    "TransferSink",
    "TransferSource",
    "UnknownTransportDescriptor",
    "UnsupportedRequirementError",
    "UnsupportedVersionError",
    "UploadSink",
    "VERSION_PATTERN",
    "WireFormatError",
    "capability_declaration",
    "check_requires",
    "check_version_skew",
    "validate_wire",
]
