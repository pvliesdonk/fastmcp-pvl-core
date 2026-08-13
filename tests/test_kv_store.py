"""Tests for the unified key-value storage factory."""

from __future__ import annotations

import logging
import os
import tempfile

import pytest
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

from fastmcp_pvl_core import ServerConfig, build_kv_store

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
"""Root ignores directory permission bits, so the unwritable-directory
cases cannot be exercised as root (CI runs unprivileged)."""

# Note: the autouse fixture that resets ``_legacy_url_warned`` between
# tests lives in ``tests/conftest.py`` so it protects every test file
# that exercises ``build_kv_store`` (not just this one).


class TestBuildKvStoreMemoryBackend:
    def test_memory_url_returns_memory_backend(self):
        config = ServerConfig(kv_store_url="memory://")
        store = build_kv_store(config, namespace="ns")
        assert isinstance(store, PrefixCollectionsWrapper)
        assert isinstance(store.key_value, MemoryStore)

    def test_namespace_is_used_as_prefix(self):
        config = ServerConfig(kv_store_url="memory://")
        store = build_kv_store(config, namespace="my-ns")
        assert store.prefix == "my-ns"

    def test_distinct_namespaces_isolate_collections(self):
        # Two factories on the same URL must produce stores whose
        # collection prefixes differ, otherwise the namespace promise
        # is broken.
        cfg = ServerConfig(kv_store_url="memory://")
        a = build_kv_store(cfg, namespace="events")
        b = build_kv_store(cfg, namespace="file-exchange")
        assert a.prefix != b.prefix

    async def test_namespace_isolation_is_behavioral(self):
        # The .prefix check above proves the configuration; this proves
        # the runtime contract — a write under one namespace is invisible
        # under another, even when both share the same backend. This is
        # the bug class the wrapper exists to prevent (events writing
        # under collection "tokens" and file-exchange writing under
        # collection "tokens" silently colliding).
        cfg = ServerConfig(kv_store_url="memory://")
        a = build_kv_store(cfg, namespace="events")
        b = build_kv_store(cfg, namespace="file-exchange")
        await a.put(collection="tokens", key="k1", value={"who": "events"})
        assert await a.get(collection="tokens", key="k1") == {"who": "events"}
        assert await b.get(collection="tokens", key="k1") is None


class TestBuildKvStoreFileBackend:
    def test_file_url(self):
        with tempfile.TemporaryDirectory() as td:
            config = ServerConfig(kv_store_url=f"file://{td}/state")
            store = build_kv_store(config, namespace="ns")
            assert isinstance(store, PrefixCollectionsWrapper)
            assert isinstance(store.key_value, FileTreeStore)

    def test_default_when_unset(self, tmp_path, monkeypatch):
        """No URL and no legacy override → default file:// path."""
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR",
            str(tmp_path / "default-state"),
        )
        config = ServerConfig()
        store = build_kv_store(config, namespace="ns")
        assert isinstance(store.key_value, FileTreeStore)
        assert (tmp_path / "default-state").exists()

    def test_file_url_with_netloc_rejected(self):
        # `file://relative/path` is a common typo for `file:///abs/path`
        # — netloc absorbs the would-be path leading segment. Reject
        # explicitly rather than silently rewriting to the default.
        config = ServerConfig(kv_store_url="file://var/state")
        with pytest.raises(ValueError, match="host component"):
            build_kv_store(config, namespace="ns")

    def test_file_url_with_empty_path_rejected(self):
        config = ServerConfig(kv_store_url="file://")
        with pytest.raises(ValueError, match="missing a path"):
            build_kv_store(config, namespace="ns")

    def test_file_url_value_error_does_not_leak_credentials(self):
        # A malformed `file://` URL with credentials in userinfo
        # (rare but possible operator misconfiguration) must not echo
        # the raw URL into the ValueError text — that string ends up
        # in process logs / Sentry. Parallel to the legacy-warning
        # credential-redaction promise.
        config = ServerConfig(kv_store_url="file://alice:hunter2@host/p")
        with pytest.raises(ValueError) as exc_info:
            build_kv_store(config, namespace="ns")
        msg = str(exc_info.value)
        assert "hunter2" not in msg
        assert "alice" not in msg


class TestBuildKvStoreDefaultDirectoryUnusable:
    """The unset-URL default degrades instead of crashing construction.

    ``file:///data/state`` is a Docker convention. The same code runs
    unconfigured on hosts that never mount ``/data`` — CI runners, uvx
    installs, the stdio plugin channel — where the eager ``mkdir`` used
    to raise ``PermissionError`` at *server construction* time. There
    the default resolves to ``memory://`` instead.
    """

    def test_missing_parent_falls_back_to_memory(self, tmp_path, monkeypatch):
        # `/data` absent is the CI-runner / uvx case: the host never
        # opted into the volume convention.
        default_dir = tmp_path / "absent-mount" / "state"
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR", str(default_dir)
        )
        store = build_kv_store(ServerConfig(), namespace="ns")
        assert isinstance(store.key_value, MemoryStore)

    def test_missing_parent_is_not_created(self, tmp_path, monkeypatch):
        # Running as root the old mkdir silently "succeeded", leaking a
        # root-level directory nobody mounted. The probe must not create
        # the mount point — only a directory *inside* an existing one.
        default_dir = tmp_path / "absent-mount" / "state"
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR", str(default_dir)
        )
        build_kv_store(ServerConfig(), namespace="ns")
        assert not (tmp_path / "absent-mount").exists()

    def test_fallback_warns_and_names_the_variable(self, tmp_path, monkeypatch, caplog):
        default_dir = tmp_path / "absent-mount" / "state"
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR", str(default_dir)
        )
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._kv_store"):
            build_kv_store(ServerConfig(), namespace="ns")
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "memory://" in messages
        assert "KV_STORE_URL" in messages

    def test_fallback_warning_is_one_shot_per_process(
        self, tmp_path, monkeypatch, caplog
    ):
        # A server builds three namespaced stores (events, transfer,
        # jobs); the operator should see one warning, not three.
        default_dir = tmp_path / "absent-mount" / "state"
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR", str(default_dir)
        )
        config = ServerConfig()
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._kv_store"):
            build_kv_store(config, namespace="events")
            build_kv_store(config, namespace="transfer")
            build_kv_store(config, namespace="jobs")
        fallbacks = [r for r in caplog.records if "memory://" in r.getMessage()]
        assert len(fallbacks) == 1

    @pytest.mark.skipif(_IS_ROOT, reason="root bypasses directory permission checks")
    def test_unwritable_parent_falls_back_to_memory(self, tmp_path, monkeypatch):
        # `/data` present but not writable — the mkdir itself raises.
        mount = tmp_path / "ro-mount"
        mount.mkdir()
        mount.chmod(0o500)
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR", str(mount / "state")
        )
        try:
            store = build_kv_store(ServerConfig(), namespace="ns")
        finally:
            mount.chmod(0o700)
        assert isinstance(store.key_value, MemoryStore)

    @pytest.mark.skipif(_IS_ROOT, reason="root bypasses directory permission checks")
    def test_existing_unwritable_directory_falls_back_to_memory(
        self, tmp_path, monkeypatch
    ):
        # mkdir(exist_ok=True) is a no-op here, so only the explicit
        # access check catches it — otherwise the failure would surface
        # at the first job promotion rather than at construction.
        default_dir = tmp_path / "state"
        default_dir.mkdir()
        default_dir.chmod(0o500)
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR", str(default_dir)
        )
        try:
            store = build_kv_store(ServerConfig(), namespace="ns")
        finally:
            default_dir.chmod(0o700)
        assert isinstance(store.key_value, MemoryStore)

    @pytest.mark.skipif(_IS_ROOT, reason="root bypasses directory permission checks")
    def test_explicit_file_url_is_never_degraded(self, tmp_path):
        # The degradation is a property of the *default*, not of the
        # file backend: an operator who named a directory gets a hard
        # error when it is unusable, never a silent memory store.
        mount = tmp_path / "ro-mount"
        mount.mkdir()
        mount.chmod(0o500)
        config = ServerConfig(kv_store_url=f"file://{mount}/state")
        try:
            with pytest.raises(PermissionError):
                build_kv_store(config, namespace="ns")
        finally:
            mount.chmod(0o700)

    def test_legacy_url_still_takes_precedence_over_the_fallback(
        self, tmp_path, monkeypatch
    ):
        # The default resolves only when *no* URL is configured; the
        # legacy override must not be short-circuited by the probe.
        monkeypatch.setattr(
            "fastmcp_pvl_core._kv_store._DEFAULT_KV_STORE_DIR",
            str(tmp_path / "absent-mount" / "state"),
        )
        config = ServerConfig(event_store_url=f"file://{tmp_path}/legacy")
        store = build_kv_store(config, namespace="ns")
        assert isinstance(store.key_value, FileTreeStore)


class TestBuildKvStoreNamespaceValidation:
    def test_empty_namespace_rejected(self):
        # The whole isolation guarantee is "different subsystems pick
        # different prefixes"; an empty prefix defeats it.
        config = ServerConfig(kv_store_url="memory://")
        with pytest.raises(ValueError, match="non-empty"):
            build_kv_store(config, namespace="")

    def test_whitespace_only_namespace_rejected(self):
        # The guard uses `.strip()` so whitespace-only is also caught;
        # an empty-after-strip prefix is the same isolation defeat.
        config = ServerConfig(kv_store_url="memory://")
        with pytest.raises(ValueError, match="non-empty"):
            build_kv_store(config, namespace="   ")


class TestBuildKvStoreUrlPrecedence:
    def test_kv_store_url_wins_over_event_store_url(self):
        config = ServerConfig(
            kv_store_url="memory://",
            event_store_url="file:///should-not-be-used",
        )
        store = build_kv_store(config, namespace="ns")
        assert isinstance(store.key_value, MemoryStore)

    def test_event_store_url_used_when_kv_store_url_unset(self):
        with tempfile.TemporaryDirectory() as td:
            config = ServerConfig(event_store_url=f"file://{td}/legacy")
            store = build_kv_store(config, namespace="ns")
            assert isinstance(store.key_value, FileTreeStore)

    def test_legacy_event_store_url_warns(self, caplog):
        config = ServerConfig(event_store_url="memory://")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._kv_store"):
            build_kv_store(config, namespace="ns")
        assert any("legacy" in record.message.lower() for record in caplog.records)

    def test_legacy_warning_does_not_leak_credentials(self, caplog):
        # Operator-set URLs may carry secrets in userinfo
        # (redis://user:pass@host/0, mongodb://user:pass@host/db).
        # The legacy-fallback warning must log only the scheme, not
        # the full URL — a regression here would page security.
        config = ServerConfig(event_store_url="redis://carol:hunter2@redis.example/0")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._kv_store"):
            try:
                build_kv_store(config, namespace="ns")
            except ImportError:
                # The redis backend may not be installed in this env;
                # we only care about the log line emitted before
                # backend construction is attempted.
                pass
        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "hunter2" not in messages
        assert "carol" not in messages
        assert "redis.example" not in messages

    def test_kv_store_url_does_not_warn(self, caplog):
        config = ServerConfig(kv_store_url="memory://")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._kv_store"):
            build_kv_store(config, namespace="ns")
        assert not any("legacy" in record.message.lower() for record in caplog.records)

    def test_legacy_warning_is_one_shot_per_process(self, caplog):
        # Multiple subsystems (events, oauth-state, ...) calling
        # build_kv_store on the same legacy-configured config should
        # see exactly one warning — not one per subsystem.
        config = ServerConfig(event_store_url="memory://")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._kv_store"):
            build_kv_store(config, namespace="events")
            build_kv_store(config, namespace="oauth-state")
            build_kv_store(config, namespace="file-exchange")
        legacy_warnings = [r for r in caplog.records if "legacy" in r.message.lower()]
        assert len(legacy_warnings) == 1


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
            build_kv_store(config, namespace="ns")

    def test_dynamodb_import_error_names_extra(self, monkeypatch: pytest.MonkeyPatch):
        self._hide_module(monkeypatch, "key_value.aio.stores.dynamodb")
        config = ServerConfig(kv_store_url="dynamodb://my-table?region=us-east-1")
        with pytest.raises(ImportError, match=r"fastmcp-pvl-core\[dynamodb\]"):
            build_kv_store(config, namespace="ns")

    def test_mongodb_import_error_names_extra(self, monkeypatch: pytest.MonkeyPatch):
        self._hide_module(monkeypatch, "key_value.aio.stores.mongodb")
        config = ServerConfig(kv_store_url="mongodb://localhost:27017/db")
        with pytest.raises(ImportError, match=r"fastmcp-pvl-core\[mongodb\]"):
            build_kv_store(config, namespace="ns")


class TestBuildKvStoreUnknownScheme:
    def test_unknown_scheme_raises(self):
        config = ServerConfig(kv_store_url="postgres://localhost/db")
        with pytest.raises(ValueError, match="Unsupported kv_store URL scheme"):
            build_kv_store(config, namespace="ns")

    def test_dynamodb_requires_table_name(self):
        # ``dynamodb://`` with no host portion is meaningless — bail
        # early rather than constructing a store that points at no
        # table. Skip if the optional extra is not installed (the
        # ImportError would fire first and a separate test covers
        # that case).
        pytest.importorskip("key_value.aio.stores.dynamodb")
        config = ServerConfig(kv_store_url="dynamodb://?region=us-east-1")
        with pytest.raises(ValueError, match="must include a table name"):
            build_kv_store(config, namespace="ns")
