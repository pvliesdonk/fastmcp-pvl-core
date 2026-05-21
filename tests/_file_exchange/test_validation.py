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


def test_wire_format_error_root_pointer_is_empty_string():
    """RFC 6901 §5: the whole-document pointer is ``""``, not ``"/"``."""
    fake = JSEValidationError("synthetic", path=[])
    err = WireFormatError.from_jsonschema(fake)
    assert err.json_pointer == ""


def test_root_level_required_failure_yields_empty_pointer():
    """Realistic root-level failure path: missing top-level ``type`` field."""
    bad = dict(_VALID_HANDLE)
    del bad["type"]
    with pytest.raises(WireFormatError) as exc_info:
        validate_wire(bad, kind="handle")
    assert exc_info.value.json_pointer == ""


# --- format-checker (date-time / uri) ---


def test_invalid_expires_at_format_raises_wire_format_error():
    """Enforce ``format: date-time`` on ``DownloadSource.expiresAt`` at schema layer.

    Without an active ``format_checker`` jsonschema treats the format
    keyword as advisory and lets the value through to Pydantic — defeating
    the layered-validation design. The pointer stops at the offending
    source's index rather than drilling into ``/expiresAt`` because the
    ``sources`` array uses a ``oneOf`` discriminator and we don't yet
    consume ``jsonschema.exceptions.best_match`` — that's the depth
    work deferred to #140's error envelope.
    """
    bad = dict(_VALID_HANDLE)
    bad["sources"] = [
        {
            "transport": "download",
            "url": "https://example.invalid/x.bin",
            "expiresAt": "tomorrow",
        }
    ]
    with pytest.raises(WireFormatError) as exc_info:
        validate_wire(bad, kind="handle")
    assert exc_info.value.json_pointer == "/sources/0"


def test_invalid_download_url_format_raises_wire_format_error():
    """``format: uri`` on ``DownloadSource.url`` is enforced at the schema layer."""
    bad = dict(_VALID_HANDLE)
    bad["sources"] = [
        {
            "transport": "download",
            "url": "not a uri",
            "expiresAt": "2026-12-31T00:00:00Z",
        }
    ]
    with pytest.raises(WireFormatError) as exc_info:
        validate_wire(bad, kind="handle")
    # Pointer stops at /sources/0 for the same oneOf reason — see the
    # docstring on test_invalid_expires_at_format_raises_wire_format_error.
    assert exc_info.value.json_pointer == "/sources/0"
