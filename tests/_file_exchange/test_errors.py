"""Tests for build_file_exchange_error — the §13 envelope builder."""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from fastmcp_pvl_core._file_exchange._codes import (
    KNOWN_CODES,
    TransferErrorCode,
)
from fastmcp_pvl_core._file_exchange._errors import (
    _DEFAULT_TEXT,
    build_file_exchange_error,
)

_NAMESPACE_KEY = "nl.liesdonk.file-exchange/error"


def test_returns_call_tool_result_with_is_error_true():
    result = build_file_exchange_error(TransferErrorCode.NO_SUPPORTED_TRANSPORT)
    assert isinstance(result, CallToolResult)
    assert result.isError is True


def test_meta_carries_namespaced_envelope_with_code():
    result = build_file_exchange_error(TransferErrorCode.NO_SUPPORTED_TRANSPORT)
    assert result.meta == {
        _NAMESPACE_KEY: {"code": "no-supported-transport"},
    }


def test_meta_omits_transport_and_detail_when_none():
    """Absent fields are absent — no JSON nulls in the envelope."""
    result = build_file_exchange_error(TransferErrorCode.TRANSFER_FAILED)
    inner = result.meta[_NAMESPACE_KEY]
    assert "transport" not in inner
    assert "detail" not in inner


def test_content_is_single_text_block_with_default_text():
    result = build_file_exchange_error(TransferErrorCode.NO_SUPPORTED_TRANSPORT)
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.type == "text"
    assert block.text == _DEFAULT_TEXT[TransferErrorCode.NO_SUPPORTED_TRANSPORT]


def test_transport_kwarg_populates_meta_and_appends_to_text():
    """When transport is supplied, default text gets a ``(transport: X)`` suffix."""
    result = build_file_exchange_error(
        TransferErrorCode.NOT_ACCESSIBLE,
        transport="download",
    )
    assert result.meta[_NAMESPACE_KEY]["transport"] == "download"
    expected_text = (
        _DEFAULT_TEXT[TransferErrorCode.NOT_ACCESSIBLE] + " (transport: download)"
    )
    assert result.content[0].text == expected_text


def test_detail_goes_into_meta_but_not_into_text():
    """Log-leak guard: ``detail`` is structured data, never auto-rendered."""
    detail_str = "expected sha-256:9f..., got sha-256:1b..."
    result = build_file_exchange_error(
        TransferErrorCode.DIGEST_MISMATCH,
        detail=detail_str,
    )
    assert result.meta[_NAMESPACE_KEY]["detail"] == detail_str
    assert detail_str not in result.content[0].text


def test_explicit_text_overrides_default_and_skips_transport_suffix():
    """When ``text`` is given, ``_DEFAULT_TEXT`` is bypassed entirely."""
    result = build_file_exchange_error(
        TransferErrorCode.NOT_ACCESSIBLE,
        transport="filesystem",
        text="Custom operator-friendly message.",
    )
    assert result.content[0].text == "Custom operator-friendly message."
    # transport still goes into meta though
    assert result.meta[_NAMESPACE_KEY]["transport"] == "filesystem"


def test_empty_text_falls_back_to_default():
    """``text=""`` is treated as no-override — an error result is never empty.

    Guards the ``if text:`` check (vs ``if text is not None``): an
    explicitly-empty string must not produce an empty TextContent block.
    """
    result = build_file_exchange_error(
        TransferErrorCode.NO_SUPPORTED_TRANSPORT, text=""
    )
    assert (
        result.content[0].text
        == _DEFAULT_TEXT[TransferErrorCode.NO_SUPPORTED_TRANSPORT]
    )


def test_unknown_code_renders_generic_text_and_passes_through():
    """§13: consumers SHOULD treat unrecognized codes as generic failures."""
    result = build_file_exchange_error("future-spec-code")
    assert result.meta[_NAMESPACE_KEY]["code"] == "future-spec-code"
    assert result.content[0].text == "File transfer failed: future-spec-code"


def test_known_code_missing_default_text_degrades_gracefully(monkeypatch):
    """A known code absent from ``_DEFAULT_TEXT`` falls back, not ``KeyError``.

    ``KNOWN_CODES`` is auto-derived from the enum while ``_DEFAULT_TEXT``
    is hand-maintained, so a future enum member could pass the
    ``in KNOWN_CODES`` gate while missing its text entry. The lookup
    uses ``.get`` so that degrades to the generic fallback at runtime
    rather than raising. (``test_every_known_code_has_a_default_text_mapping``
    still guards the real invariant; this pins the defensive path.)
    """
    from fastmcp_pvl_core._file_exchange import _errors

    patched = dict(_DEFAULT_TEXT)
    del patched[TransferErrorCode.DIGEST_MISMATCH]
    monkeypatch.setattr(_errors, "_DEFAULT_TEXT", patched)

    result = build_file_exchange_error(TransferErrorCode.DIGEST_MISMATCH)
    assert result.isError is True
    assert result.meta[_NAMESPACE_KEY]["code"] == "digest-mismatch"
    assert result.content[0].text == "File transfer failed: digest-mismatch"


def test_every_known_code_has_a_default_text_mapping():
    """Drift guard: adding a TransferErrorCode without updating _DEFAULT_TEXT fails."""
    for member in TransferErrorCode:
        assert member in _DEFAULT_TEXT, f"missing default text for {member}"


def test_code_accepts_enum_member_or_raw_string():
    """The signature is ``code: str | TransferErrorCode``."""
    from_enum = build_file_exchange_error(TransferErrorCode.DIGEST_MISMATCH)
    from_str = build_file_exchange_error("digest-mismatch")
    assert from_enum.meta[_NAMESPACE_KEY]["code"] == "digest-mismatch"
    assert from_str.meta[_NAMESPACE_KEY]["code"] == "digest-mismatch"


def test_known_codes_dont_render_generic_fallback_text():
    """Sanity: known codes never produce the ``File transfer failed: ...`` fallback."""
    for code in KNOWN_CODES:
        result = build_file_exchange_error(code)
        assert not result.content[0].text.startswith("File transfer failed: ")


def test_unknown_code_with_transport_omits_suffix_keeps_meta():
    """Unknown code: transport lands in ``_meta`` but no text suffix.

    Pins the documented asymmetry — the generic ``"File transfer
    failed: <code>"`` fallback is intentionally terse and does NOT get
    the ``(transport: X)`` suffix, while ``transport`` still appears in
    the structured envelope.
    """
    result = build_file_exchange_error("future-spec-code", transport="download")
    assert result.meta[_NAMESPACE_KEY]["transport"] == "download"
    assert result.content[0].text == "File transfer failed: future-spec-code"


def test_meta_serialises_under_underscore_meta_alias_on_wire():
    """§13 mandates the envelope under the JSON key ``_meta`` (alias).

    Pins the *wire* shape, not just the Python attribute: ``meta`` has
    JSON alias ``_meta``, so a refactor that emitted a plain ``meta``
    key (or dropped the alias) would break the §13 contract while the
    in-memory ``result.meta`` assertions still passed.
    """
    result = build_file_exchange_error(
        TransferErrorCode.DIGEST_MISMATCH,
        transport="download",
        detail="expected sha-256:9f..., got sha-256:1b...",
    )
    dumped = result.model_dump(by_alias=True, exclude_none=True)
    assert "_meta" in dumped
    assert "meta" not in dumped
    assert dumped["_meta"][_NAMESPACE_KEY] == {
        "code": "digest-mismatch",
        "transport": "download",
        "detail": "expected sha-256:9f..., got sha-256:1b...",
    }
    assert dumped["isError"] is True


def test_transfer_error_carries_code_transport_detail():
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError

    exc = FileExchangeTransferError(
        TransferErrorCode.DIGEST_MISMATCH,
        transport="filesystem",
        detail="bytes did not match declared digest",
    )
    assert exc.code is TransferErrorCode.DIGEST_MISMATCH
    assert exc.transport == "filesystem"
    assert exc.detail == "bytes did not match declared digest"
    assert "digest-mismatch" in str(exc)
    assert isinstance(exc, Exception)


def test_transfer_error_message_is_bare_code_when_no_detail():
    from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
    from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError

    exc = FileExchangeTransferError(TransferErrorCode.NOT_ACCESSIBLE)
    assert str(exc) == "not-accessible"
    assert exc.detail is None
    assert exc.transport is None
