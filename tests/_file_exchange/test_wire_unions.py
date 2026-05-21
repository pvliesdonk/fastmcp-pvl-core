"""Tests for the discriminated TransferSource / TransferSink unions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from fastmcp_pvl_core._file_exchange._wire import (
    _ALL_KNOWN_DESCRIPTOR_TRANSPORTS,
    DownloadSource,
    FilesystemSink,
    FilesystemSource,
    TransferSink,
    TransferSource,
    UnknownTransportDescriptor,
    UploadSink,
)

_source_adapter: TypeAdapter[TransferSource] = TypeAdapter(TransferSource)
_sink_adapter: TypeAdapter[TransferSink] = TypeAdapter(TransferSink)


# --- routing ---


def test_source_union_filesystem_branch():
    obj = _source_adapter.validate_python(
        {"transport": "filesystem", "uri": "exchange://v/a"}
    )
    assert isinstance(obj, FilesystemSource)


def test_source_union_download_branch():
    obj = _source_adapter.validate_python(
        {
            "transport": "download",
            "url": "https://example/d",
            "expiresAt": "2026-05-18T12:30:00Z",
        }
    )
    assert isinstance(obj, DownloadSource)


def test_source_union_unknown_transport_routes_to_fallthrough():
    obj = _source_adapter.validate_python(
        {"transport": "quic-direct-2026", "foo": "bar"}
    )
    assert isinstance(obj, UnknownTransportDescriptor)
    assert obj.transport == "quic-direct-2026"
    assert obj.model_extra == {"foo": "bar"}


def test_source_union_known_transport_with_extra_field_fails_closed_branch():
    # §17.5: malformed known descriptor fails its closed branch, not fallthrough.
    with pytest.raises(ValidationError):
        _source_adapter.validate_python(
            {"transport": "filesystem", "uri": "exchange://v/a", "extra": 1}
        )


def test_sink_union_filesystem_branch():
    obj = _sink_adapter.validate_python(
        {"transport": "filesystem", "uri": "exchange://v/in"}
    )
    assert isinstance(obj, FilesystemSink)


def test_sink_union_upload_branch():
    obj = _sink_adapter.validate_python(
        {
            "transport": "upload",
            "url": "https://intake/u",
            "expiresAt": "2026-05-18T12:30:00Z",
        }
    )
    assert isinstance(obj, UploadSink)


def test_sink_union_unknown_transport_routes_to_fallthrough():
    obj = _sink_adapter.validate_python({"transport": "s3-multipart", "bucket": "b"})
    assert isinstance(obj, UnknownTransportDescriptor)


def test_sink_union_known_transport_with_extra_field_fails_closed_branch():
    """Sink-side parallel to the source-side known-but-malformed test."""
    with pytest.raises(ValidationError):
        _sink_adapter.validate_python(
            {"transport": "filesystem", "uri": "exchange://v/in", "extra": 1}
        )
    with pytest.raises(ValidationError):
        _sink_adapter.validate_python(
            {
                "transport": "upload",
                "url": "https://intake/u",
                "expiresAt": "2026-05-18T12:30:00Z",
                "secret": "leak",
            }
        )


# --- UnknownTransportDescriptor invariants ---


def test_unknown_transport_descriptor_requires_transport_field():
    with pytest.raises(ValidationError):
        UnknownTransportDescriptor.model_validate({})


def test_unknown_transport_descriptor_refuses_known_transport_directly():
    """Direct Python construction must honor the schema's not.enum exclusion."""
    for known in ("filesystem", "download", "upload"):
        with pytest.raises(ValidationError):
            UnknownTransportDescriptor.model_validate({"transport": known})


def test_unknown_transport_descriptor_is_frozen():
    u = UnknownTransportDescriptor.model_validate({"transport": "quic-2026"})
    with pytest.raises(ValidationError):
        u.transport = "stream"  # type: ignore[misc]


# --- schema↔Pydantic drift detection ---


def test_schema_not_enum_matches_pydantic_known_transports():
    """Schema's not.enum exclusion and the Pydantic known-set must match.

    A future spec amendment adding a transport must update both sites —
    this test fails loudly if only one is touched.
    """
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "src/fastmcp_pvl_core/_file_exchange/_schema/file-exchange.json"
    )
    schema = json.loads(schema_path.read_text())
    not_enum = schema["$defs"]["UnknownTransportDescriptor"]["properties"]["transport"][
        "not"
    ]["enum"]
    assert frozenset(not_enum) == _ALL_KNOWN_DESCRIPTOR_TRANSPORTS, (
        f"schema not.enum {sorted(not_enum)} drifted from "
        f"_ALL_KNOWN_DESCRIPTOR_TRANSPORTS "
        f"{sorted(_ALL_KNOWN_DESCRIPTOR_TRANSPORTS)}"
    )
