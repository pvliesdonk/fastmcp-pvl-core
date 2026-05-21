"""Tests for the TransferErrorCode enum + KNOWN_CODES frozenset."""

from __future__ import annotations

from fastmcp_pvl_core._file_exchange._codes import (
    KNOWN_CODES,
    TransferErrorCode,
)


def test_member_values_match_spec_table():
    """§13 defines exactly these strings; the enum mirrors them verbatim."""
    assert TransferErrorCode.NO_SUPPORTED_TRANSPORT.value == "no-supported-transport"
    assert TransferErrorCode.DESCRIPTOR_EXPIRED.value == "descriptor-expired"
    assert TransferErrorCode.NOT_ACCESSIBLE.value == "not-accessible"
    assert TransferErrorCode.DIGEST_MISMATCH.value == "digest-mismatch"
    assert TransferErrorCode.SIZE_MISMATCH.value == "size-mismatch"
    assert TransferErrorCode.TOO_LARGE.value == "too-large"
    assert TransferErrorCode.MIME_TYPE_REJECTED.value == "mime-type-rejected"
    assert TransferErrorCode.UNSUPPORTED_REQUIREMENT.value == "unsupported-requirement"
    assert TransferErrorCode.TRANSFER_FAILED.value == "transfer-failed"


def test_known_codes_is_exactly_nine_spec_strings():
    """Drift guard: KNOWN_CODES must equal the spec's 9 strings, no more no less."""
    expected = frozenset(
        {
            "no-supported-transport",
            "descriptor-expired",
            "not-accessible",
            "digest-mismatch",
            "size-mismatch",
            "too-large",
            "mime-type-rejected",
            "unsupported-requirement",
            "transfer-failed",
        }
    )
    assert KNOWN_CODES == expected


def test_known_codes_covers_every_enum_member():
    """Every TransferErrorCode member must be in KNOWN_CODES (no enum/set drift)."""
    for member in TransferErrorCode:
        assert member.value in KNOWN_CODES, f"missing {member.value}"


def test_str_mixin_equality():
    """``(str, Enum)`` mixin: enum member compares equal to its str value."""
    assert TransferErrorCode.DIGEST_MISMATCH == "digest-mismatch"


def test_membership_test_works_for_known_and_rejects_typo():
    assert TransferErrorCode.DIGEST_MISMATCH in KNOWN_CODES
    assert "digestmismatch" not in KNOWN_CODES  # typo


def test_known_codes_is_frozenset():
    """Module-level constant must be immutable — frozenset not set."""
    assert isinstance(KNOWN_CODES, frozenset)
