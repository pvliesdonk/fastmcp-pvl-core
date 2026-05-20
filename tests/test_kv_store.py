"""Tests for the unified key-value storage factory."""

from __future__ import annotations

import logging
import tempfile

import pytest
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

from fastmcp_pvl_core import ServerConfig, build_kv_store


class TestBuildKvStoreMemoryBackend:
    def test_memory_url_returns_memory_backend(self):
        config = ServerConfig(kv_store_url="memory://")
        store = build_kv_store("MY_APP", config, namespace="ns")
        assert isinstance(store, PrefixCollectionsWrapper)
        assert isinstance(store.key_value, MemoryStore)

    def test_namespace_is_used_as_prefix(self):
        config = ServerConfig(kv_store_url="memory://")
        store = build_kv_store("MY_APP", config, namespace="my-ns")
        assert store.prefix == "my-ns"

    def test_distinct_namespaces_isolate_collections(self):
        # Two factories on the same URL must produce stores whose
        # collection prefixes differ, otherwise the namespace promise
        # is broken.
        cfg = ServerConfig(kv_store_url="memory://")
        a = build_kv_store("MY_APP", cfg, namespace="events")
        b = build_kv_store("MY_APP", cfg, namespace="file-exchange")
        assert a.prefix != b.prefix


class TestBuildKvStoreFileBackend:
    def test_file_url(self):
        with tempfile.TemporaryDirectory() as td:
            config = ServerConfig(kv_store_url=f"file://{td}/state")
            store = build_kv_store("MY_APP", config, namespace="ns")
            assert isinstance(store, PrefixCollectionsWrapper)
            assert isinstance(store.key_value, FileTreeStore)

    def test_default_when_unset(self, tmp_path, monkeypatch):
        """No URL and no legacy override → default file:// path."""
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR",
            str(tmp_path / "default-state"),
        )
        config = ServerConfig()
        store = build_kv_store("MY_APP", config, namespace="ns")
        assert isinstance(store.key_value, FileTreeStore)
        assert (tmp_path / "default-state").exists()


class TestBuildKvStoreUrlPrecedence:
    def test_kv_store_url_wins_over_event_store_url(self):
        config = ServerConfig(
            kv_store_url="memory://",
            event_store_url="file:///should-not-be-used",
        )
        store = build_kv_store("MY_APP", config, namespace="ns")
        assert isinstance(store.key_value, MemoryStore)

    def test_event_store_url_used_when_kv_store_url_unset(self):
        with tempfile.TemporaryDirectory() as td:
            config = ServerConfig(event_store_url=f"file://{td}/legacy")
            store = build_kv_store("MY_APP", config, namespace="ns")
            assert isinstance(store.key_value, FileTreeStore)

    def test_legacy_event_store_url_warns(self, caplog):
        config = ServerConfig(event_store_url="memory://")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._kv_store"):
            build_kv_store("MY_APP", config, namespace="ns")
        assert any("legacy" in record.message.lower() for record in caplog.records)

    def test_kv_store_url_does_not_warn(self, caplog):
        config = ServerConfig(kv_store_url="memory://")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._kv_store"):
            build_kv_store("MY_APP", config, namespace="ns")
        assert not any("legacy" in record.message.lower() for record in caplog.records)


class TestBuildKvStoreOptionalBackends:
    """The redis/dynamodb/mongodb backends are optional extras.

    When the relevant ``py-key-value-aio`` extra is not installed, the
    factory must raise ``ImportError`` with a message that names the
    pvl-core extra to install — not a bare ``ModuleNotFoundError`` that
    leaves the operator guessing.

    These tests mock the import explicitly so they run identically
    whether or not the extra is installed in the test environment
    (CI installs everything via ``uv sync --all-extras``).
    """

    @staticmethod
    def _hide_module(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        """Force ``import <name>`` to raise ``ModuleNotFoundError``."""
        import sys

        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, None)  # type: ignore[arg-type]

    def test_redis_import_error_names_extra(self, monkeypatch: pytest.MonkeyPatch):
        self._hide_module(monkeypatch, "key_value.aio.stores.redis")
        config = ServerConfig(kv_store_url="redis://localhost:6379/0")
        with pytest.raises(ImportError, match=r"fastmcp-pvl-core\[redis\]"):
            build_kv_store("MY_APP", config, namespace="ns")

    def test_dynamodb_import_error_names_extra(self, monkeypatch: pytest.MonkeyPatch):
        self._hide_module(monkeypatch, "key_value.aio.stores.dynamodb")
        config = ServerConfig(kv_store_url="dynamodb://my-table?region=us-east-1")
        with pytest.raises(ImportError, match=r"fastmcp-pvl-core\[dynamodb\]"):
            build_kv_store("MY_APP", config, namespace="ns")

    def test_mongodb_import_error_names_extra(self, monkeypatch: pytest.MonkeyPatch):
        self._hide_module(monkeypatch, "key_value.aio.stores.mongodb")
        config = ServerConfig(kv_store_url="mongodb://localhost:27017/db")
        with pytest.raises(ImportError, match=r"fastmcp-pvl-core\[mongodb\]"):
            build_kv_store("MY_APP", config, namespace="ns")


class TestBuildKvStoreUnknownScheme:
    def test_unknown_scheme_raises(self):
        config = ServerConfig(kv_store_url="postgres://localhost/db")
        with pytest.raises(ValueError, match="Unsupported kv_store URL scheme"):
            build_kv_store("MY_APP", config, namespace="ns")

    def test_dynamodb_requires_table_name(self):
        # ``dynamodb://`` with no host portion is meaningless — bail
        # early rather than constructing a store that points at no
        # table. Skip if the optional extra is not installed (the
        # ImportError would fire first and a separate test covers
        # that case).
        pytest.importorskip("key_value.aio.stores.dynamodb")
        config = ServerConfig(kv_store_url="dynamodb://?region=us-east-1")
        with pytest.raises(ValueError, match="must include a table name"):
            build_kv_store("MY_APP", config, namespace="ns")
