"""Tests for server-factory helpers."""

from __future__ import annotations

import tempfile

import pytest

from fastmcp_pvl_core import (
    ServerConfig,
    build_event_store,
    build_instructions,
    compute_app_domain,
)


class TestBuildInstructions:
    def test_read_only_line(self):
        text = build_instructions(
            read_only=True,
            env_prefix="MY_APP",
            domain_line="A widget service.",
        )
        assert "READ-ONLY" in text
        assert "A widget service." in text
        assert "MY_APP_INSTRUCTIONS" in text

    def test_read_write_line(self):
        text = build_instructions(
            read_only=False,
            env_prefix="MY_APP",
            domain_line="A widget service.",
        )
        assert "READ-WRITE" in text
        assert "READ-ONLY" not in text

    def test_env_prefix_trailing_underscore_stripped(self):
        """Callers passing 'MY_APP_' or 'MY_APP' should get the same result."""
        a = build_instructions(read_only=True, env_prefix="MY_APP", domain_line="x.")
        b = build_instructions(read_only=True, env_prefix="MY_APP_", domain_line="x.")
        assert a == b
        assert "MY_APP_INSTRUCTIONS" in a
        # Should NOT double the underscore.
        assert "MY_APP__INSTRUCTIONS" not in a


class TestBuildEventStore:
    def test_memory_url(self):
        config = ServerConfig(event_store_url="memory://")
        store = build_event_store("MY_APP", config)
        assert store is not None

    def test_file_url(self):
        with tempfile.TemporaryDirectory() as td:
            config = ServerConfig(event_store_url=f"file://{td}/events")
            store = build_event_store("MY_APP", config)
            assert store is not None

    def test_default_when_unset(self, tmp_path):
        config = ServerConfig(event_store_url=f"file://{tmp_path}/default-events")
        store = build_event_store("MY_APP", config)
        assert store is not None

    def test_unknown_scheme_raises(self):
        config = ServerConfig(event_store_url="redis://localhost:6379/0")
        with pytest.raises(ValueError, match="Unsupported"):
            build_event_store("MY_APP", config)

    def test_none_url_uses_default_path(self, tmp_path, monkeypatch):
        """When event_store_url is None, falls back to a file-backed default."""
        # Redirect the default path so the test doesn't touch /data/state/events.
        monkeypatch.setattr(
            "fastmcp_pvl_core._factory._DEFAULT_EVENT_STORE_DIR",
            str(tmp_path / "events-default"),
        )
        config = ServerConfig(event_store_url=None)
        store = build_event_store("MY_APP", config)
        assert store is not None


class TestComputeAppDomain:
    def test_override_wins(self):
        config = ServerConfig(
            base_url="https://x.example",
            app_domain="override.example",
        )
        assert compute_app_domain(config) == "override.example"

    def test_derives_from_base_url(self):
        config = ServerConfig(base_url="https://mcp.example.com")
        assert compute_app_domain(config) == "mcp.example.com"

    def test_derives_from_base_url_with_port(self):
        config = ServerConfig(base_url="https://mcp.example.com:8443")
        assert compute_app_domain(config) == "mcp.example.com:8443"

    def test_none_when_no_base_url(self):
        assert compute_app_domain(ServerConfig()) is None

    def test_none_for_bare_url_without_scheme(self):
        """urlparse('example.com') yields empty netloc → None."""
        config = ServerConfig(base_url="example.com")
        assert compute_app_domain(config) is None
