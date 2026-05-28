"""Tests for ArtifactMetadata + ArtifactConstraints Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    ArtifactMetadata,
)

# --- ArtifactMetadata ---


def test_metadata_accepts_single_field():
    m = ArtifactMetadata(name="x")
    assert m.name == "x"


def test_metadata_rejects_empty_object():
    with pytest.raises(ValidationError):
        ArtifactMetadata()


def test_metadata_digest_pattern():
    ArtifactMetadata(digest="sha-256:abc123")
    with pytest.raises(ValidationError):
        ArtifactMetadata(digest="not a digest")


def test_metadata_size_non_negative():
    ArtifactMetadata(size=0)
    with pytest.raises(ValidationError):
        ArtifactMetadata(size=-1)


def test_metadata_is_frozen():
    m = ArtifactMetadata(name="x")
    with pytest.raises(ValidationError):
        m.name = "y"  # type: ignore[misc]


def test_metadata_allows_extra_fields_for_forward_compat():
    # §17.2 tolerant reading: unknown fields on non-descriptor object accepted.
    m = ArtifactMetadata.model_validate({"name": "x", "unknownField": 42})
    assert m.name == "x"


# --- ArtifactConstraints ---


def test_constraints_all_optional():
    ArtifactConstraints()


def test_constraints_require_digest_must_be_non_empty():
    with pytest.raises(ValidationError):
        ArtifactConstraints(requireDigest=[])


def test_constraints_require_digest_elements_must_be_non_empty():
    """An empty-string algorithm name would create a token that 400s every
    upload (the route's ``preferred_set={""}`` matches no real algorithm).
    Reject at validation time."""
    with pytest.raises(ValidationError):
        ArtifactConstraints(requireDigest=[""])


def test_constraints_max_size_non_negative():
    ArtifactConstraints(maxSize=0)
    with pytest.raises(ValidationError):
        ArtifactConstraints(maxSize=-1)


def test_constraints_is_frozen():
    c = ArtifactConstraints(maxSize=100)
    with pytest.raises(ValidationError):
        c.maxSize = 200  # type: ignore[misc]
