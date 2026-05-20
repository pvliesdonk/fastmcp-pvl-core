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
        config = ServerConfig(kv_store_url="memory://")
        store = build_event_store("MY_APP", config)
        assert store is not None

    def test_file_url(self):
        with tempfile.TemporaryDirectory() as td:
            config = ServerConfig(kv_store_url=f"file://{td}/state")
            store = build_event_store("MY_APP", config)
            assert store is not None

    def test_unknown_scheme_raises(self):
        # The unified factory raises with its own message; the
        # event-store wrapper does not add a second one.
        config = ServerConfig(kv_store_url="postgres://localhost/db")
        with pytest.raises(ValueError, match="Unsupported kv_store URL scheme"):
            build_event_store("MY_APP", config)

    def test_none_url_uses_default_path(self, tmp_path, monkeypatch):
        """When kv_store_url is None, falls back to a file-backed default."""
        # Redirect the default path so the test doesn't touch /data/state.
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR",
            str(tmp_path / "state-default"),
        )
        config = ServerConfig(kv_store_url=None)
        store = build_event_store("MY_APP", config)
        assert store is not None

    def test_legacy_event_store_url_still_works(self):
        # Existing deployments that set EVENT_STORE_URL must continue
        # to work when KV_STORE_URL is unset — that is the load-bearing
        # backwards-compatibility contract.
        with tempfile.TemporaryDirectory() as td:
            config = ServerConfig(event_store_url=f"file://{td}/legacy-events")
            store = build_event_store("MY_APP", config)
            assert store is not None

    def test_uses_events_namespace(self):
        # The "events" namespace is the load-bearing contract that
        # prevents collision with future subsystems (oauth-state,
        # file-exchange) sharing the same backend. Pin the runtime
        # contract — read the wrapper prefix off the constructed
        # EventStore, not the kwarg of a mocked call — so a future
        # refactor that inlines storage construction (bypassing
        # build_kv_store) still gets caught here.
        config = ServerConfig(kv_store_url="memory://")
        event_store = build_event_store("MY_APP", config)
        # ``_storage`` is fastmcp.server.event_store.EventStore's
        # private slot for the wrapped backend; reading it here is a
        # deliberate cross-package introspection to pin the namespace.
        assert event_store._storage.prefix == "events"  # noqa: SLF001


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
