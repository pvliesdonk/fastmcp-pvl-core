"""Tests for the ``fastmcp_pvl_core.file_exchange`` public namespace."""

from __future__ import annotations


def test_namespace_module_imports():
    from fastmcp_pvl_core import file_exchange  # noqa: F401


def test_top_level_attribute_exists():
    import fastmcp_pvl_core

    assert hasattr(fastmcp_pvl_core, "file_exchange")


def test_constants_exposed():
    from fastmcp_pvl_core import file_exchange

    assert file_exchange.SPEC_VERSION == "0.1"
    assert file_exchange.NAMESPACE == "nl.liesdonk.file-exchange"
    assert file_exchange.HANDLE_TYPE == "nl.liesdonk.file-exchange/transfer-handle"
    assert file_exchange.TICKET_TYPE == "nl.liesdonk.file-exchange/intake-ticket"
    assert isinstance(file_exchange.SPEC_SOURCE_SHA, str)


def test_wire_models_exposed():
    from fastmcp_pvl_core import file_exchange

    names = [
        "ArtifactMetadata",
        "ArtifactConstraints",
        "FilesystemSource",
        "DownloadSource",
        "FilesystemSink",
        "UploadSink",
        "UnknownTransportDescriptor",
        "TransferSource",
        "TransferSink",
        "TransferHandle",
        "IntakeTicket",
        "FileExchangeCapability",
        "TransferError",
    ]
    for n in names:
        assert hasattr(file_exchange, n), f"missing {n}"


def test_exceptions_exposed():
    from fastmcp_pvl_core import file_exchange

    for n in (
        "WireFormatError",
        "UnsupportedVersionError",
        "UnsupportedRequirementError",
    ):
        assert hasattr(file_exchange, n), f"missing {n}"


def test_helpers_exposed():
    from fastmcp_pvl_core import file_exchange

    assert callable(file_exchange.capability_declaration)
    assert callable(file_exchange.validate_wire)


def test_handle_from_wire_via_namespace():
    from fastmcp_pvl_core import file_exchange

    raw = {
        "type": "nl.liesdonk.file-exchange/transfer-handle",
        "version": "0.1",
        "artifact": {"name": "x"},
        "sources": [{"transport": "filesystem", "uri": "exchange://v/a"}],
    }
    handle = file_exchange.TransferHandle.from_wire(raw)
    assert handle.version == "0.1"
