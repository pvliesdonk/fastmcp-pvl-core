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
from fastmcp_pvl_core._file_exchange._codes import (
    KNOWN_CODES,
    TransferErrorCode,
)
from fastmcp_pvl_core._file_exchange._errors import (
    FileExchangeTransferError,
    build_file_exchange_error,
)
from fastmcp_pvl_core._file_exchange._filesystem import (
    filesystem_fetcher_consume,
    filesystem_provider_mint,
    filesystem_receiver_mint,
    filesystem_sender_consume,
    filesystem_sink_writable,
    filesystem_source_readable,
)
from fastmcp_pvl_core._file_exchange._hooks import (
    ArtifactSink,
    ArtifactSource,
)
from fastmcp_pvl_core._file_exchange._paths import (
    VolumeMap,
    atomic_write,
    canonicalize_and_confine,
    load_volume_map,
    resolve_filesystem_uri,
)
from fastmcp_pvl_core._file_exchange._selection import (
    select_sink,
    select_source,
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
from fastmcp_pvl_core._file_exchange._tokens import (
    CapabilityTokenStore,
    MintedToken,
    TokenRecord,
    build_capability_token_store,
    capability_url,
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
    "ArtifactSink",
    "ArtifactSource",
    "CapabilityTokenStore",
    "DownloadSource",
    "FileExchangeCapability",
    "FileExchangeTransferError",
    "FilesystemSink",
    "FilesystemSource",
    "HANDLE_TYPE",
    "IntakeTicket",
    "KNOWN_CODES",
    "MintedToken",
    "NAMESPACE",
    "Role",
    "SPEC_SOURCE_SHA",
    "SPEC_VERSION",
    "TICKET_TYPE",
    "TransferError",
    "TransferErrorCode",
    "TransferHandle",
    "TransferSink",
    "TokenRecord",
    "TransferSource",
    "UnknownTransportDescriptor",
    "UnsupportedRequirementError",
    "UnsupportedVersionError",
    "UploadSink",
    "VERSION_PATTERN",
    "VolumeMap",
    "WireFormatError",
    "atomic_write",
    "build_capability_token_store",
    "build_file_exchange_error",
    "canonicalize_and_confine",
    "capability_declaration",
    "capability_url",
    "check_requires",
    "check_version_skew",
    "filesystem_fetcher_consume",
    "filesystem_provider_mint",
    "filesystem_receiver_mint",
    "filesystem_sender_consume",
    "filesystem_sink_writable",
    "filesystem_source_readable",
    "load_volume_map",
    "resolve_filesystem_uri",
    "select_sink",
    "select_source",
    "validate_wire",
]
