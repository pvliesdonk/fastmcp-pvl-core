"""Tests for the four-layer `from_wire` pipeline + JSON-pointer survival."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core._file_exchange._spec import (
    UnsupportedRequirementError,
    UnsupportedVersionError,
)
from fastmcp_pvl_core._file_exchange._validation import WireFormatError
from fastmcp_pvl_core._file_exchange._wire import (
    IntakeTicket,
    TransferError,
    TransferHandle,
)

_GOOD_HANDLE = {
    "type": "nl.liesdonk.file-exchange/transfer-handle",
    "version": "0.1",
    "artifact": {"name": "x.bin"},
    "sources": [{"transport": "filesystem", "uri": "exchange://v/a"}],
}

_GOOD_TICKET = {
    "type": "nl.liesdonk.file-exchange/intake-ticket",
    "version": "0.1",
    "artifactId": "a-1",
    "sinks": [{"transport": "filesystem", "uri": "exchange://v/in"}],
}


# --- TransferHandle ---


def test_handle_from_wire_happy_path():
    h = TransferHandle.from_wire(_GOOD_HANDLE)
    assert h.version == "0.1"


def test_handle_from_wire_raises_wire_format_error_on_schema_failure():
    bad = dict(_GOOD_HANDLE)
    del bad["type"]
    with pytest.raises(WireFormatError):
        TransferHandle.from_wire(bad)


def test_handle_from_wire_json_pointer_survives_re_raise():
    """JSON Pointer must survive the from_wire re-raise unchanged.

    #140's error envelope reads ``exc.json_pointer`` from
    :class:`WireFormatError` raised by :meth:`from_wire`. If the
    re-raise path strips the attribute, the envelope ``detail`` field
    can't carry the field reference.
    """
    bad = dict(_GOOD_HANDLE)
    bad["sources"] = []
    with pytest.raises(WireFormatError) as exc_info:
        TransferHandle.from_wire(bad)
    assert "/sources" in exc_info.value.json_pointer


def test_handle_from_wire_raises_unsupported_version_on_future_major():
    payload = dict(_GOOD_HANDLE)
    payload["version"] = "2.0"
    with pytest.raises(UnsupportedVersionError):
        TransferHandle.from_wire(payload)


def test_handle_from_wire_raises_unsupported_requirement_on_unknown_feature():
    payload = {
        "type": "nl.liesdonk.file-exchange/transfer-handle",
        "version": "0.7",
        "artifact": {"name": "x"},
        "sources": [{"transport": "filesystem", "uri": "exchange://v/a"}],
        "requires": ["future-feat"],
    }
    with pytest.raises(UnsupportedRequirementError):
        TransferHandle.from_wire(payload)


# --- IntakeTicket (symmetric coverage) ---


def test_ticket_from_wire_happy_path():
    t = IntakeTicket.from_wire(_GOOD_TICKET)
    assert t.artifactId == "a-1"


def test_ticket_from_wire_raises_wire_format_error_on_schema_failure():
    bad = dict(_GOOD_TICKET)
    del bad["type"]
    with pytest.raises(WireFormatError):
        IntakeTicket.from_wire(bad)


def test_ticket_from_wire_raises_unsupported_version_on_future_major():
    payload = dict(_GOOD_TICKET)
    payload["version"] = "2.0"
    with pytest.raises(UnsupportedVersionError):
        IntakeTicket.from_wire(payload)


def test_ticket_from_wire_raises_unsupported_requirement():
    payload = {
        "type": "nl.liesdonk.file-exchange/intake-ticket",
        "version": "0.7",
        "artifactId": "a-2",
        "sinks": [{"transport": "filesystem", "uri": "exchange://v/in"}],
        "requires": ["future-feat"],
    }
    with pytest.raises(UnsupportedRequirementError):
        IntakeTicket.from_wire(payload)


# --- TransferError ---


def test_error_from_wire_happy_path():
    e = TransferError.from_wire({"code": "transfer-failed"})
    assert e.code == "transfer-failed"


def test_error_from_wire_unknown_code_accepted():
    # §13: code set is open.
    e = TransferError.from_wire({"code": "future-failure"})
    assert e.code == "future-failure"
