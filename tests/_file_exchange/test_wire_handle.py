"""Tests for TransferHandle + TransferError Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fastmcp_pvl_core._file_exchange._wire import (
    FilesystemSource,
    TransferError,
    TransferHandle,
    UnknownTransportDescriptor,
)

_GOOD = {
    "type": "nl.liesdonk.file-exchange/transfer-handle",
    "version": "0.1",
    "artifact": {"name": "x.bin"},
    "sources": [{"transport": "filesystem", "uri": "exchange://v/a"}],
}


def test_minimal_valid_handle_parses():
    h = TransferHandle.model_validate(_GOOD)
    assert h.version == "0.1"
    assert len(h.sources) == 1


def test_handle_requires_at_least_one_source():
    bad = dict(_GOOD)
    bad["sources"] = []
    with pytest.raises(ValidationError):
        TransferHandle.model_validate(bad)


def test_handle_type_constant_is_pinned():
    bad = dict(_GOOD)
    bad["type"] = "something/else"
    with pytest.raises(ValidationError):
        TransferHandle.model_validate(bad)


def test_handle_v01_must_have_empty_requires():
    bad = dict(_GOOD)
    bad["requires"] = ["some-feature"]
    with pytest.raises(ValidationError):
        TransferHandle.model_validate(bad)


def test_handle_requires_must_be_unique():
    bad = {
        "type": "nl.liesdonk.file-exchange/transfer-handle",
        "version": "1.0",
        "artifact": {"name": "x"},
        "sources": [{"transport": "filesystem", "uri": "exchange://v/a"}],
        "requires": ["feat-a", "feat-a"],
    }
    with pytest.raises(ValidationError):
        TransferHandle.model_validate(bad)


def test_handle_requires_rejects_empty_string_entry():
    bad = {
        "type": "nl.liesdonk.file-exchange/transfer-handle",
        "version": "1.0",
        "artifact": {"name": "x"},
        "sources": [{"transport": "filesystem", "uri": "exchange://v/a"}],
        "requires": [""],
    }
    with pytest.raises(ValidationError):
        TransferHandle.model_validate(bad)


def test_handle_allows_meta_via_extra():
    payload = dict(_GOOD)
    payload["_meta"] = {"trace": "abc"}
    h = TransferHandle.model_validate(payload)
    assert h.model_extra is not None
    assert h.model_extra.get("_meta") == {"trace": "abc"}


def test_handle_is_frozen():
    h = TransferHandle.model_validate(_GOOD)
    with pytest.raises(ValidationError):
        h.version = "0.2"  # type: ignore[misc]


def test_handle_routes_each_source_to_its_own_branch():
    """List-level routing: per-element discrimination under TransferHandle.sources."""
    h = TransferHandle.model_validate(
        {
            "type": "nl.liesdonk.file-exchange/transfer-handle",
            "version": "0.1",
            "artifact": {"name": "x"},
            "sources": [
                {"transport": "filesystem", "uri": "exchange://v/a"},
                {"transport": "quic-2026", "foo": "bar"},
            ],
        }
    )
    assert isinstance(h.sources[0], FilesystemSource)
    assert isinstance(h.sources[1], UnknownTransportDescriptor)


# --- TransferError ---


def test_error_minimal_valid():
    e = TransferError(code="transfer-failed")
    assert e.code == "transfer-failed"


def test_error_accepts_unknown_code_for_forward_compat():
    # §13: code set is open.
    e = TransferError.model_validate({"code": "future-failure-mode"})
    assert e.code == "future-failure-mode"


def test_error_extra_fields_allowed():
    e = TransferError.model_validate({"code": "transfer-failed", "futureField": 1})
    assert e.code == "transfer-failed"


def test_error_is_frozen():
    e = TransferError(code="transfer-failed")
    with pytest.raises(ValidationError):
        e.code = "size-mismatch"  # type: ignore[misc]
