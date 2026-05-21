"""Tests for FileExchangeCapability + capability_declaration helper."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from fastmcp_pvl_core._file_exchange._capability import (
    FileExchangeCapability,
    capability_declaration,
)
from fastmcp_pvl_core._file_exchange._spec import (
    NAMESPACE,
    SPEC_VERSION,
)
from fastmcp_pvl_core._file_exchange._validation import WireFormatError

# --- capability_declaration() ---


def test_minimal_declaration_returns_correct_dict():
    out = capability_declaration(roles={"provider": ["filesystem"]})
    assert out == {
        "version": "0.1",
        "roles": {"provider": ["filesystem"]},
        "digests": ["sha-256"],
    }


def test_declaration_includes_max_artifact_size_when_given():
    out = capability_declaration(
        roles={"provider": ["filesystem"]},
        max_artifact_size=1024,
    )
    assert out["maxArtifactSize"] == 1024


def test_declaration_omits_max_artifact_size_when_none():
    out = capability_declaration(roles={"fetcher": ["filesystem"]})
    assert "maxArtifactSize" not in out


def test_declaration_round_trips_through_capability_model():
    raw = capability_declaration(
        roles={"provider": ["filesystem", "download"]},
        digests=["sha-256", "sha-512"],
        max_artifact_size=1_000_000,
    )
    cap = FileExchangeCapability.from_wire(raw)
    assert cap is not None
    assert cap.version == SPEC_VERSION
    assert cap.maxArtifactSize == 1_000_000


def test_declaration_warns_on_unknown_transport(caplog):
    # §17.5: open set — log a WARNING, don't refuse.
    with caplog.at_level(logging.WARNING):
        out = capability_declaration(roles={"provider": ["dowload"]})
    assert out["roles"]["provider"] == ["dowload"]
    assert any("dowload" in rec.message for rec in caplog.records)


# --- FileExchangeCapability.from_wire ---


def test_capability_from_wire_happy_path():
    raw = {"version": "0.1", "roles": {"provider": ["filesystem"]}}
    cap = FileExchangeCapability.from_wire(raw)
    assert cap is not None
    assert cap.roles == {"provider": ["filesystem"]}


def test_capability_from_wire_returns_none_on_future_major(caplog):
    # §17.3 capability is a SHOULD: from_wire returns None rather than raising.
    raw = {"version": "2.0", "roles": {"provider": ["filesystem"]}}
    with caplog.at_level(logging.WARNING):
        result = FileExchangeCapability.from_wire(raw)
    assert result is None
    # Must surface the skip in logs — operators reading at WARNING
    # level should see why the peer was dropped.
    assert any("2.0" in rec.message for rec in caplog.records)
    assert any(NAMESPACE in rec.message for rec in caplog.records)


def test_capability_from_wire_raises_on_schema_failure():
    with pytest.raises(WireFormatError):
        FileExchangeCapability.from_wire({"version": "0.1"})


def test_capability_accepts_unknown_role_key_for_forward_compat():
    """§17.2 + roles additionalProperties: Pydantic must accept unknown role keys."""
    cap = FileExchangeCapability.from_wire(
        {
            "version": "0.1",
            "roles": {"provider": ["filesystem"], "auditor": ["filesystem"]},
        }
    )
    assert cap is not None
    assert "auditor" in cap.roles


def test_capability_is_frozen():
    cap = FileExchangeCapability(version="0.1", roles={"provider": ["filesystem"]})
    with pytest.raises(ValidationError):
        cap.version = "0.2"  # type: ignore[misc]


def test_capability_max_artifact_size_must_be_non_negative():
    """maxArtifactSize: schema enforces minimum: 0; Pydantic mirrors."""
    with pytest.raises(ValidationError):
        FileExchangeCapability(
            version="0.1", roles={"provider": ["filesystem"]}, maxArtifactSize=-1
        )


def test_declaration_warns_on_unknown_role(caplog):
    """Future-role advertisement: warn about the role, not the transports."""
    with caplog.at_level(logging.WARNING):
        capability_declaration(
            roles={"auditor": ["filesystem"]}  # type: ignore[dict-item]
        )
    # The warning should name the role, not produce a misleading
    # "transport outside set []" for every transport on the role.
    assert any("auditor" in rec.message for rec in caplog.records)


def test_namespace_export_consistency():
    assert NAMESPACE == "nl.liesdonk.file-exchange"
