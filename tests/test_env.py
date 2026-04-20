"""Tests for env var reading helpers."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core import env, parse_bool, parse_list, parse_scopes


class TestEnv:
    def test_returns_default_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MYAPP_FOO", raising=False)
        assert env("MYAPP", "FOO", default="bar") == "bar"

    def test_returns_value_when_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_FOO", "hello")
        assert env("MYAPP", "FOO") == "hello"

    def test_empty_string_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_FOO", "")
        assert env("MYAPP", "FOO", default="fallback") == "fallback"

    def test_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_FOO", "  value  ")
        assert env("MYAPP", "FOO") == "value"

    def test_prefix_can_have_trailing_underscore(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MYAPP_FOO", "x")
        assert env("MYAPP_", "FOO") == "x"
        assert env("MYAPP", "FOO") == "x"


class TestParseBool:
    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
    def test_truthy(self, value: str):
        assert parse_bool(value) is True

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", ""])
    def test_falsy(self, value: str):
        assert parse_bool(value) is False


class TestParseList:
    def test_empty(self):
        assert parse_list("") == []

    def test_comma_separated(self):
        assert parse_list("a,b,c") == ["a", "b", "c"]

    def test_strips_whitespace(self):
        assert parse_list(" a , b , c ") == ["a", "b", "c"]

    def test_drops_empty_items(self):
        assert parse_list("a,,b,") == ["a", "b"]


class TestParseScopes:
    def test_none_returns_none(self):
        assert parse_scopes(None) is None

    def test_empty_returns_empty_list(self):
        assert parse_scopes("") == []

    def test_space_separated(self):
        assert parse_scopes("read write") == ["read", "write"]

    def test_comma_separated(self):
        assert parse_scopes("read,write") == ["read", "write"]

    def test_mixed(self):
        assert parse_scopes("read, write profile") == ["read", "write", "profile"]
