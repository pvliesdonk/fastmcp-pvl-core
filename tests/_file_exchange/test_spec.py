"""Tests for file-exchange spec constants + skew/must-understand helpers."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core._file_exchange import _spec
from fastmcp_pvl_core._file_exchange._spec import (
    UnsupportedRequirementError,
    UnsupportedVersionError,
    check_requires,
    check_version_skew,
)

# --- constants ---


def test_spec_version_is_v01():
    assert _spec.SPEC_VERSION == "0.1"


def test_namespace_is_reverse_dns():
    assert _spec.NAMESPACE == "nl.liesdonk.file-exchange"


def test_type_constants_compose_namespace_and_suffix():
    assert _spec.HANDLE_TYPE == "nl.liesdonk.file-exchange/transfer-handle"
    assert _spec.TICKET_TYPE == "nl.liesdonk.file-exchange/intake-ticket"


def test_spec_source_sha_is_pinned_full_sha():
    # Format-only check: a full 40-char lowercase hex SHA. The actual
    # value is enforced by the ``file-exchange-spec-sync`` CI job (it
    # diffs the pinned constant's vendored artifacts against upstream
    # at that SHA), so pinning the value here too would add friction
    # to legitimate ``--bump`` PRs without strengthening the guard.
    assert len(_spec.SPEC_SOURCE_SHA) == 40
    assert all(c in "0123456789abcdef" for c in _spec.SPEC_SOURCE_SHA)


def test_implemented_major_is_zero():
    assert _spec._IMPLEMENTED_MAJOR == 0


def test_known_requires_is_empty_frozenset_in_v01():
    assert _spec._KNOWN_REQUIRES == frozenset()
    assert isinstance(_spec._KNOWN_REQUIRES, frozenset)


# --- check_version_skew ---


def test_reference_same_major_passes_regardless_of_minor():
    assert check_version_skew("0.1", kind="reference") is True
    assert check_version_skew("0.7", kind="reference") is True


def test_reference_different_major_raises():
    with pytest.raises(UnsupportedVersionError) as exc_info:
        check_version_skew("1.0", kind="reference")
    assert "1.0" in str(exc_info.value)


def test_capability_same_major_returns_true():
    assert check_version_skew("0.1", kind="capability") is True
    assert check_version_skew("0.99", kind="capability") is True


def test_capability_different_major_returns_false_no_raise():
    # §17.3 SHOULD: survivable; treat peer as non-participant.
    assert check_version_skew("1.0", kind="capability") is False
    assert check_version_skew("2.7", kind="capability") is False


def test_check_version_skew_rejects_malformed_version():
    """Direct callers must get UnsupportedVersionError, not int() ValueError."""
    for bad in ("garbage", "1", "1.2.3", "", "0.x"):
        with pytest.raises(UnsupportedVersionError):
            check_version_skew(bad, kind="capability")
        with pytest.raises(UnsupportedVersionError):
            check_version_skew(bad, kind="reference")


# --- check_requires ---


def test_check_requires_empty_passes():
    check_requires([])


def test_check_requires_unknown_feature_raises():
    with pytest.raises(UnsupportedRequirementError) as exc_info:
        check_requires(["some-future-feature"])
    assert exc_info.value.unknown_features == frozenset({"some-future-feature"})


def test_check_requires_carries_all_unknown_features():
    with pytest.raises(UnsupportedRequirementError) as exc_info:
        check_requires(["a", "b"])
    assert exc_info.value.unknown_features == frozenset({"a", "b"})
