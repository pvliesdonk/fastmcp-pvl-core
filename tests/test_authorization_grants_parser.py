"""Tests for parse_claim_grants (inline-JSON claim-value->scopes loader)."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core._authorization import parse_claim_grants
from fastmcp_pvl_core._errors import ConfigurationError


def test_happy_path() -> None:
    result = parse_claim_grants('{"writers": ["read", "write"], "admins": ["*"]}')
    assert result == {
        "writers": frozenset({"read", "write"}),
        "admins": frozenset({"*"}),
    }


def test_empty_object_permitted() -> None:
    assert parse_claim_grants("{}") == {}


def test_scopes_stripped() -> None:
    assert parse_claim_grants('{"g": [" write "]}') == {"g": frozenset({"write"})}


def test_invalid_json() -> None:
    with pytest.raises(ConfigurationError, match="could not be parsed"):
        parse_claim_grants("{not json")


def test_top_level_not_object() -> None:
    with pytest.raises(ConfigurationError, match="must be a JSON object"):
        parse_claim_grants('["a", "b"]')


def test_blank_key() -> None:
    with pytest.raises(ConfigurationError, match="empty or whitespace"):
        parse_claim_grants('{"  ": ["read"]}')


def test_star_key_rejected() -> None:
    with pytest.raises(ConfigurationError, match="not allowed"):
        parse_claim_grants('{"*": ["read"]}')


def test_value_not_array() -> None:
    with pytest.raises(ConfigurationError, match="must be an array"):
        parse_claim_grants('{"g": "read"}')


def test_non_string_scope() -> None:
    with pytest.raises(ConfigurationError, match="must be a string"):
        parse_claim_grants('{"g": ["read", 5]}')


def test_blank_scope() -> None:
    with pytest.raises(ConfigurationError, match="empty or whitespace"):
        parse_claim_grants('{"g": ["read", "  "]}')
