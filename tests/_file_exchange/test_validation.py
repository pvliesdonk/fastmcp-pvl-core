"""Tests for the jsonschema validation layer."""

from __future__ import annotations

import pytest
from jsonschema.exceptions import ValidationError as JSEValidationError

from fastmcp_pvl_core._file_exchange._validation import (
    WireFormatError,
    validate_wire,
)

_VALID_HANDLE: dict = {
    "type": "nl.liesdonk.file-exchange/transfer-handle",
    "version": "0.1",
    "artifact": {"name": "x.bin"},
    "sources": [{"transport": "filesystem", "uri": "exchange://v/a"}],
}

_VALID_TICKET: dict = {
    "type": "nl.liesdonk.file-exchange/intake-ticket",
    "version": "0.1",
    "artifactId": "a-1",
    "sinks": [{"transport": "filesystem", "uri": "exchange://v/in"}],
}

_VALID_CAPABILITY: dict = {
    "version": "0.1",
    "roles": {"provider": ["filesystem"]},
}

_VALID_ERROR: dict = {"code": "transfer-failed"}


def test_valid_handle_passes():
    validate_wire(_VALID_HANDLE, kind="handle")


def test_valid_ticket_passes():
    validate_wire(_VALID_TICKET, kind="ticket")


def test_valid_capability_passes():
    validate_wire(_VALID_CAPABILITY, kind="capability")


def test_valid_error_passes():
    validate_wire(_VALID_ERROR, kind="error")


def test_missing_required_field_raises_wire_format_error():
    bad = dict(_VALID_HANDLE)
    del bad["type"]
    with pytest.raises(WireFormatError):
        validate_wire(bad, kind="handle")


def test_empty_sources_raises():
    bad = dict(_VALID_HANDLE)
    bad["sources"] = []
    with pytest.raises(WireFormatError):
        validate_wire(bad, kind="handle")


def test_unknown_kind_raises_value_error():
    with pytest.raises(ValueError, match="kind"):
        validate_wire(_VALID_HANDLE, kind="not-a-kind")  # type: ignore[arg-type]


def test_wire_format_error_carries_json_pointer_path():
    bad = dict(_VALID_HANDLE)
    bad["sources"] = []
    with pytest.raises(WireFormatError) as exc_info:
        validate_wire(bad, kind="handle")
    assert "/sources" in exc_info.value.json_pointer


# --- RFC 6901 escape order ---


def test_wire_format_error_escapes_tilde_in_json_pointer():
    """RFC 6901: ``~`` in a path segment must become ``~0``."""
    fake = JSEValidationError("synthetic", path=["weird~field"])
    err = WireFormatError.from_jsonschema(fake)
    assert err.json_pointer == "/weird~0field"


def test_wire_format_error_escapes_slash_in_json_pointer():
    """RFC 6901: ``/`` in a path segment must become ``~1``."""
    fake = JSEValidationError("synthetic", path=["a/b"])
    err = WireFormatError.from_jsonschema(fake)
    assert err.json_pointer == "/a~1b"


def test_wire_format_error_escapes_both_in_correct_order():
    """RFC 6901: ``~`` escaped first so a literal ``/`` doesn't round-trip."""
    fake = JSEValidationError("synthetic", path=["a~/b"])
    err = WireFormatError.from_jsonschema(fake)
    # `~/` → `~0` first → `~0/` → then `/` → `~1` → `~0~1`
    assert err.json_pointer == "/a~0~1b"
