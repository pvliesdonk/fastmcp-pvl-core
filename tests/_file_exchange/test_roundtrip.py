"""Outbound roundtrip + jsonschema/Pydantic agreement tests.

The agreement parametrization is the load-bearing safety net: every
vendored ``valid/<kind>/*.json`` fixture must be accepted by both
layers. Any divergence is a pvl-core model bug, caught here rather
than at the wire boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastmcp_pvl_core._file_exchange._capability import (
    FileExchangeCapability,
    capability_declaration,
)
from fastmcp_pvl_core._file_exchange._validation import validate_wire
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactMetadata,
    DownloadSource,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    TransferError,
    TransferHandle,
    UploadSink,
)

from .conftest import discover_fixtures, fixture_ids

# --- outbound roundtrip — all four kinds ---


def test_outbound_handle_roundtrips_through_validate_wire():
    h = TransferHandle.model_validate(
        {
            "type": "nl.liesdonk.file-exchange/transfer-handle",
            "version": "0.1",
            "artifact": {"name": "x.bin", "size": 10},
            "sources": [{"transport": "filesystem", "uri": "exchange://v/a"}],
        }
    )
    dumped = h.model_dump(mode="json", exclude_none=True)
    validate_wire(dumped, kind="handle")


def test_outbound_handle_with_download_source_roundtrips():
    """Datetime on DownloadSource must serialize to a schema-valid ISO 8601 string."""
    h = TransferHandle(
        type="nl.liesdonk.file-exchange/transfer-handle",
        version="0.1",
        artifact=ArtifactMetadata(name="x", size=10),
        sources=[
            FilesystemSource(transport="filesystem", uri="exchange://v/a"),
            DownloadSource(
                transport="download",
                url="https://example/d",
                expiresAt="2026-05-18T12:30:00Z",
                singleUse=False,
            ),
        ],
    )
    validate_wire(h.model_dump(mode="json", exclude_none=True), kind="handle")


def test_outbound_ticket_roundtrips_through_validate_wire():
    t = IntakeTicket.model_validate(
        {
            "type": "nl.liesdonk.file-exchange/intake-ticket",
            "version": "0.1",
            "artifactId": "a-1",
            "sinks": [{"transport": "filesystem", "uri": "exchange://v/in"}],
        }
    )
    validate_wire(t.model_dump(mode="json", exclude_none=True), kind="ticket")


def test_outbound_ticket_with_upload_sink_roundtrips():
    """UploadSink with explicit method=POST + datetime must roundtrip."""
    t = IntakeTicket(
        type="nl.liesdonk.file-exchange/intake-ticket",
        version="0.1",
        artifactId="a-2",
        sinks=[
            FilesystemSink(transport="filesystem", uri="exchange://v/in"),
            UploadSink(
                transport="upload",
                url="https://intake/u",
                method="POST",
                expiresAt="2026-05-18T12:30:00Z",
            ),
        ],
    )
    validate_wire(t.model_dump(mode="json", exclude_none=True), kind="ticket")


def test_outbound_capability_roundtrips_through_validate_wire():
    raw = capability_declaration(roles={"provider": ["filesystem"]})
    validate_wire(raw, kind="capability")


def test_outbound_error_roundtrips_through_validate_wire():
    e = TransferError(code="transfer-failed", detail="example")
    validate_wire(e.model_dump(mode="json", exclude_none=True), kind="error")


# --- jsonschema/Pydantic agreement on every vendored valid fixture ---


def _agreement_params(kind: str):
    paths = discover_fixtures(f"valid/{kind}")
    return pytest.mark.parametrize("path", paths, ids=fixture_ids(paths))


@_agreement_params("handle")
def test_handle_jsonschema_implies_pydantic(path: Path):
    raw = json.loads(path.read_text())
    validate_wire(raw, kind="handle")
    TransferHandle.model_validate(raw)


@_agreement_params("ticket")
def test_ticket_jsonschema_implies_pydantic(path: Path):
    raw = json.loads(path.read_text())
    validate_wire(raw, kind="ticket")
    IntakeTicket.model_validate(raw)


@_agreement_params("capability")
def test_capability_jsonschema_implies_pydantic(path: Path):
    raw = json.loads(path.read_text())
    validate_wire(raw, kind="capability")
    FileExchangeCapability.model_validate(raw)


@_agreement_params("error")
def test_error_jsonschema_implies_pydantic(path: Path):
    raw = json.loads(path.read_text())
    validate_wire(raw, kind="error")
    TransferError.model_validate(raw)
