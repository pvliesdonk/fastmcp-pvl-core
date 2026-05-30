"""Tests for MCP Apps helpers."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
from fastmcp.apps.config import UI_EXTENSION_ID
from fastmcp.server.providers.addressing import (
    hash_tool,
    hashed_backend_name,
    parse_hashed_backend_name,
)

from fastmcp_pvl_core import app_tool_address, app_tool_meta, client_supports_apps


class TestAppToolMeta:
    def test_returns_correct_structure(self):
        result = app_tool_meta("vault", "vault_list")
        assert isinstance(result, dict)
        assert "fastmcp" in result
        inner = result["fastmcp"]
        assert "app" in inner
        assert "_tool_hash" in inner

    def test_app_name_stored(self):
        result = app_tool_meta("vault", "vault_list")
        assert result["fastmcp"]["app"] == "vault"

    def test_tool_hash_matches_hash_tool(self):
        result = app_tool_meta("vault", "vault_list")
        assert result["fastmcp"]["_tool_hash"] == hash_tool("vault", "vault_list")

    def test_different_inputs_produce_different_hashes(self):
        a = app_tool_meta("vault", "vault_list")
        b = app_tool_meta("vault", "vault_search")
        assert a["fastmcp"]["_tool_hash"] != b["fastmcp"]["_tool_hash"]

    def test_app_name_varies_independently(self):
        a = app_tool_meta("vault", "vault_list")
        b = app_tool_meta("other", "vault_list")
        assert a["fastmcp"]["app"] != b["fastmcp"]["app"]
        assert a["fastmcp"]["_tool_hash"] != b["fastmcp"]["_tool_hash"]


class TestAppToolAddress:
    def test_matches_hashed_backend_name(self):
        result = app_tool_address("vault", "vault_list")
        assert result == hashed_backend_name("vault", "vault_list")

    def test_parses_back_to_tool_name(self):
        result = app_tool_address("vault", "vault_list")
        _, tool_name = parse_hashed_backend_name(result)
        assert tool_name == "vault_list"

    def test_different_pairs_produce_different_addresses(self):
        a = app_tool_address("vault", "vault_list")
        b = app_tool_address("vault", "vault_search")
        assert a != b

    def test_address_contains_tool_name_suffix(self):
        result = app_tool_address("vault", "vault_list")
        assert result.endswith("_vault_list")


class TestClientSupportsApps:
    def test_returns_true_when_extension_supported(self):
        ctx = MagicMock()
        ctx.client_supports_extension.return_value = True
        assert client_supports_apps(ctx) is True

    def test_returns_false_when_extension_not_supported(self):
        ctx = MagicMock()
        ctx.client_supports_extension.return_value = False
        assert client_supports_apps(ctx) is False

    def test_passes_ui_extension_id(self):
        ctx = MagicMock()
        ctx.client_supports_extension.return_value = False
        client_supports_apps(ctx)
        ctx.client_supports_extension.assert_called_once_with(UI_EXTENSION_ID)


class TestImportErrorGuard:
    def _suppress_and_reimport(self, suppress_key: str) -> None:
        import importlib

        apps_module_key = "fastmcp_pvl_core._apps"
        saved_module = sys.modules.get(suppress_key)
        saved_apps_module = sys.modules.pop(apps_module_key, None)

        try:
            sys.modules[suppress_key] = None  # type: ignore[assignment]
            with pytest.raises(ImportError, match="3.3.1"):
                importlib.import_module(apps_module_key)
        finally:
            if saved_module is not None:
                sys.modules[suppress_key] = saved_module
            else:
                sys.modules.pop(suppress_key, None)
            if saved_apps_module is not None:
                sys.modules[apps_module_key] = saved_apps_module

    def test_addressing_absent_raises_with_version(self):
        self._suppress_and_reimport("fastmcp.server.providers.addressing")

    def test_apps_config_absent_raises_with_version(self):
        self._suppress_and_reimport("fastmcp.apps.config")
