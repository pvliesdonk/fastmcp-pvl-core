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


def test_error_codes_exposed():
    from fastmcp_pvl_core import file_exchange

    assert hasattr(file_exchange, "TransferErrorCode")
    assert hasattr(file_exchange, "KNOWN_CODES")
    # Spot-check one member end-to-end (full coverage is in test_codes.py).
    assert file_exchange.TransferErrorCode.NO_SUPPORTED_TRANSPORT == (
        "no-supported-transport"
    )
    assert "transfer-failed" in file_exchange.KNOWN_CODES


def test_selection_helpers_exposed():
    from fastmcp_pvl_core import file_exchange

    assert callable(file_exchange.select_source)
    assert callable(file_exchange.select_sink)


def test_error_envelope_helper_exposed():
    from fastmcp_pvl_core import file_exchange

    assert callable(file_exchange.build_file_exchange_error)
    # End-to-end smoke through the namespace import path.
    result = file_exchange.build_file_exchange_error(
        file_exchange.TransferErrorCode.NO_SUPPORTED_TRANSPORT,
    )
    assert result.isError is True
    inner = result.meta["nl.liesdonk.file-exchange/error"]
    assert inner == {"code": "no-supported-transport"}


def test_path_helpers_exposed():
    from fastmcp_pvl_core import file_exchange

    assert callable(file_exchange.canonicalize_and_confine)
    assert callable(file_exchange.resolve_filesystem_uri)
    assert callable(file_exchange.load_volume_map)
    assert hasattr(file_exchange, "VolumeMap")


def test_hook_helpers_exposed():
    from fastmcp_pvl_core import file_exchange

    # Protocols are classes; atomic_write is callable.
    assert isinstance(file_exchange.ArtifactSource, type)
    assert isinstance(file_exchange.ArtifactSink, type)
    assert callable(file_exchange.atomic_write)
    for name in ("ArtifactSource", "ArtifactSink", "atomic_write"):
        assert name in file_exchange.__all__


def test_filesystem_transport_names_reexported():
    from fastmcp_pvl_core import file_exchange

    for name in (
        "filesystem_provider_mint",
        "filesystem_fetcher_consume",
        "filesystem_receiver_mint",
        "filesystem_sender_consume",
        "filesystem_source_readable",
        "filesystem_sink_writable",
        "FileExchangeTransferError",
    ):
        assert hasattr(file_exchange, name), name
        assert name in file_exchange.__all__, name


def test_capability_token_names_reexported():
    from fastmcp_pvl_core import file_exchange

    for name in (
        "CapabilityTokenStore",
        "MintedToken",
        "TokenRecord",
        "build_capability_token_store",
        "capability_url",
    ):
        assert hasattr(file_exchange, name), name
        assert name in file_exchange.__all__, name


def test_download_data_plane_names_reexported():
    from fastmcp_pvl_core import file_exchange

    for name in (
        "download_provider_mint",
        "download_fetcher_consume",
        "register_file_exchange_routes",
    ):
        assert hasattr(file_exchange, name), name
        assert name in file_exchange.__all__, name
    # DOWNLOAD_PREFIX is internal route shape, not part of the public surface.
    assert not hasattr(file_exchange, "DOWNLOAD_PREFIX")
