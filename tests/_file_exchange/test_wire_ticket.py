"""Tests for IntakeTicket Pydantic model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fastmcp_pvl_core._file_exchange._wire import (
    FilesystemSink,
    IntakeTicket,
    UnknownTransportDescriptor,
)

_GOOD = {
    "type": "nl.liesdonk.file-exchange/intake-ticket",
    "version": "0.1",
    "artifactId": "intake-7f3c2a",
    "sinks": [{"transport": "filesystem", "uri": "exchange://v/in"}],
}


def test_minimal_valid_ticket_parses():
    t = IntakeTicket.model_validate(_GOOD)
    assert t.artifactId == "intake-7f3c2a"
    assert len(t.sinks) == 1


def test_ticket_requires_at_least_one_sink():
    bad = dict(_GOOD)
    bad["sinks"] = []
    with pytest.raises(ValidationError):
        IntakeTicket.model_validate(bad)


def test_ticket_requires_artifact_id():
    bad = dict(_GOOD)
    del bad["artifactId"]
    with pytest.raises(ValidationError):
        IntakeTicket.model_validate(bad)


def test_ticket_type_constant_is_pinned():
    bad = dict(_GOOD)
    bad["type"] = "something/else"
    with pytest.raises(ValidationError):
        IntakeTicket.model_validate(bad)


def test_ticket_with_expected_constraints():
    payload = dict(_GOOD)
    payload["expected"] = {"maxSize": 100, "requireDigest": ["sha-256"]}
    t = IntakeTicket.model_validate(payload)
    assert t.expected is not None
    assert t.expected.maxSize == 100


def test_ticket_v01_must_have_empty_requires():
    bad = dict(_GOOD)
    bad["requires"] = ["some-feature"]
    with pytest.raises(ValidationError):
        IntakeTicket.model_validate(bad)


def test_ticket_is_frozen():
    t = IntakeTicket.model_validate(_GOOD)
    with pytest.raises(ValidationError):
        t.artifactId = "new"  # type: ignore[misc]


def test_ticket_routes_each_sink_to_its_own_branch():
    """Sink-side list-level routing — symmetric with handle test."""
    t = IntakeTicket.model_validate(
        {
            "type": "nl.liesdonk.file-exchange/intake-ticket",
            "version": "0.1",
            "artifactId": "a-2",
            "sinks": [
                {"transport": "filesystem", "uri": "exchange://v/in"},
                {"transport": "s3-multipart-2026", "bucket": "b"},
            ],
        }
    )
    assert isinstance(t.sinks[0], FilesystemSink)
    assert isinstance(t.sinks[1], UnknownTransportDescriptor)
