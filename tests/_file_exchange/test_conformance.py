"""Conformance harness: vendored upstream fixtures vs jsonschema validator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastmcp_pvl_core._file_exchange._validation import (
    WireFormatError,
    validate_wire,
)

from .conftest import discover_fixtures, fixture_ids

_KINDS = ("capability", "error", "handle", "ticket")


def _params_valid(kind: str):
    paths = discover_fixtures(f"valid/{kind}")
    return pytest.mark.parametrize("path", paths, ids=fixture_ids(paths))


def _params_invalid(kind: str):
    paths = discover_fixtures(f"invalid/{kind}")
    return pytest.mark.parametrize("path", paths, ids=fixture_ids(paths))


# Sanity: every kind must have at least one fixture in each bucket,
# otherwise the harness is silently a no-op.
@pytest.mark.parametrize("kind", _KINDS)
def test_each_kind_has_fixtures(kind: str):
    assert discover_fixtures(f"valid/{kind}"), f"no valid/{kind} fixtures"
    assert discover_fixtures(f"invalid/{kind}"), f"no invalid/{kind} fixtures"


@_params_valid("capability")
def test_valid_capability(path: Path):
    validate_wire(json.loads(path.read_text()), kind="capability")


@_params_invalid("capability")
def test_invalid_capability(path: Path):
    with pytest.raises(WireFormatError):
        validate_wire(json.loads(path.read_text()), kind="capability")


@_params_valid("error")
def test_valid_error(path: Path):
    validate_wire(json.loads(path.read_text()), kind="error")


@_params_invalid("error")
def test_invalid_error(path: Path):
    with pytest.raises(WireFormatError):
        validate_wire(json.loads(path.read_text()), kind="error")


@_params_valid("handle")
def test_valid_handle(path: Path):
    validate_wire(json.loads(path.read_text()), kind="handle")


@_params_invalid("handle")
def test_invalid_handle(path: Path):
    with pytest.raises(WireFormatError):
        validate_wire(json.loads(path.read_text()), kind="handle")


@_params_valid("ticket")
def test_valid_ticket(path: Path):
    validate_wire(json.loads(path.read_text()), kind="ticket")


@_params_invalid("ticket")
def test_invalid_ticket(path: Path):
    with pytest.raises(WireFormatError):
        validate_wire(json.loads(path.read_text()), kind="ticket")
