"""Tests for the background-task backend wiring (``configure_task_backend``)."""

from __future__ import annotations

import logging

import fastmcp
import pytest

from fastmcp_pvl_core import ConfigurationError, ServerConfig, configure_task_backend

_AVAILABLE = "fastmcp.server.dependencies.is_docket_available"


@pytest.fixture(autouse=True)
def _neutral_docket_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``fastmcp.settings.docket`` and its env vars to a known baseline.

    ``configure_task_backend`` mutates the process-global settings object;
    ``monkeypatch.setattr`` restores the original values on teardown so
    tests cannot leak state into each other (or into unrelated suites).
    The native env vars are cleared so the "operator set FASTMCP_DOCKET_*"
    scenarios are opt-in per test.
    """
    monkeypatch.setattr(fastmcp.settings.docket, "url", "memory://")
    monkeypatch.setattr(fastmcp.settings.docket, "name", "fastmcp")
    monkeypatch.delenv("FASTMCP_DOCKET_URL", raising=False)
    monkeypatch.delenv("FASTMCP_DOCKET_NAME", raising=False)


@pytest.fixture
def docket_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``is_docket_available()`` to True.

    pydocket ships with pvl-core's base dependencies, so this normally
    matches reality; pinning keeps the wiring tests deterministic in a
    stripped environment (and symmetric with the tests that pin False).
    """
    monkeypatch.setattr(_AVAILABLE, lambda: True)


class TestExplicitTasksUrl:
    def test_redis_url_written_to_settings(self, docket_installed):
        config = ServerConfig(tasks_url="redis://queue-host:6379/0")
        configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "redis://queue-host:6379/0"

    def test_memory_url_accepted(self, docket_installed):
        config = ServerConfig(tasks_url="memory://")
        configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "memory://"

    @pytest.mark.parametrize("bad", ["file:///data/q", "postgres://h/db", "redis"])
    def test_unsupported_scheme_raises_naming_var(self, docket_installed, bad):
        config = ServerConfig(tasks_url=bad)
        with pytest.raises(ConfigurationError, match="MY_APP_TASKS_URL"):
            configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "memory://"

    def test_invalid_scheme_raises_even_without_pydocket(self, monkeypatch):
        """The strict validation must not be short-circuited by the no-op.

        An operator typo in an explicitly-set var fails fast regardless of
        whether the extra is installed (the ``PORT`` precedent).
        """
        monkeypatch.setattr(_AVAILABLE, lambda: False)
        config = ServerConfig(tasks_url="file:///data/q")
        with pytest.raises(ConfigurationError, match="MY_APP_TASKS_URL"):
            configure_task_backend("MY_APP", config)

    def test_error_names_scheme_not_url(self, docket_installed):
        """Redaction: the raw URL (possible credentials) never appears."""
        config = ServerConfig(tasks_url="amqp://user:hunter2@host/vq")
        with pytest.raises(ConfigurationError) as exc_info:
            configure_task_backend("MY_APP", config)
        assert "hunter2" not in str(exc_info.value)
        assert "'amqp'" in str(exc_info.value)

    def test_wins_over_redis_kv_store_url(self, docket_installed):
        config = ServerConfig(
            tasks_url="redis://tasks-host:6379/1",
            kv_store_url="redis://kv-host:6379/0",
        )
        configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "redis://tasks-host:6379/1"


class TestKvDerivation:
    def test_redis_kv_store_url_reused(self, docket_installed):
        config = ServerConfig(kv_store_url="redis://kv-host:6379/0")
        configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "redis://kv-host:6379/0"

    def test_legacy_event_store_url_reused(self, docket_installed):
        config = ServerConfig(event_store_url="redis://legacy-host:6379/0")
        configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "redis://legacy-host:6379/0"

    @pytest.mark.parametrize(
        "kv_url", ["memory://", "file:///data/state", "mongodb://h:27017/db", None]
    )
    def test_non_redis_kv_leaves_url_untouched(self, docket_installed, kv_url):
        config = ServerConfig(kv_store_url=kv_url)
        configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "memory://"


class TestNativeEscapeHatch:
    def test_native_url_beats_kv_derivation(self, docket_installed, monkeypatch):
        """An explicit FASTMCP_DOCKET_URL outranks the derived default."""
        monkeypatch.setenv("FASTMCP_DOCKET_URL", "redis://native-host:6379/2")
        monkeypatch.setattr(
            fastmcp.settings.docket, "url", "redis://native-host:6379/2"
        )
        config = ServerConfig(kv_store_url="redis://kv-host:6379/0")
        configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "redis://native-host:6379/2"

    def test_explicit_tasks_url_beats_native_with_warning(
        self, docket_installed, monkeypatch, caplog
    ):
        monkeypatch.setenv("FASTMCP_DOCKET_URL", "redis://native-host:6379/2")
        monkeypatch.setattr(
            fastmcp.settings.docket, "url", "redis://native-host:6379/2"
        )
        config = ServerConfig(tasks_url="redis://tasks-host:6379/1")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._tasks"):
            configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "redis://tasks-host:6379/1"
        warning = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        )
        assert "MY_APP_TASKS_URL" in warning
        assert "FASTMCP_DOCKET_URL" in warning

    def test_agreeing_explicit_values_do_not_warn(
        self, docket_installed, monkeypatch, caplog
    ):
        monkeypatch.setenv("FASTMCP_DOCKET_URL", "redis://same-host:6379/0")
        config = ServerConfig(tasks_url="redis://same-host:6379/0")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._tasks"):
            configure_task_backend("MY_APP", config)
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_native_name_respected(self, docket_installed, monkeypatch):
        monkeypatch.setenv("FASTMCP_DOCKET_NAME", "operator-queue")
        monkeypatch.setattr(fastmcp.settings.docket, "name", "operator-queue")
        configure_task_backend("MY_APP", ServerConfig())
        assert fastmcp.settings.docket.name == "operator-queue"


class TestQueueName:
    def test_derived_from_prefix(self, docket_installed):
        configure_task_backend("MY_APP", ServerConfig())
        assert fastmcp.settings.docket.name == "my-app"

    def test_trailing_underscore_normalised(self, docket_installed):
        configure_task_backend("MY_APP_", ServerConfig())
        assert fastmcp.settings.docket.name == "my-app"

    def test_derived_even_when_url_untouched(self, docket_installed):
        """Queue identity is set on every path, not only when a URL resolves.

        Two family servers pointed at one Redis via native vars must still
        get distinct queues.
        """
        config = ServerConfig(kv_store_url="file:///data/state")
        configure_task_backend("SCHOLAR_MCP", config)
        assert fastmcp.settings.docket.name == "scholar-mcp"


class TestNoPydocket:
    def test_noop_without_pydocket(self, monkeypatch):
        monkeypatch.setattr(_AVAILABLE, lambda: False)
        config = ServerConfig(kv_store_url="redis://kv-host:6379/0")
        configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "memory://"
        assert fastmcp.settings.docket.name == "fastmcp"

    def test_explicit_tasks_url_warns_when_dropped(self, monkeypatch, caplog):
        monkeypatch.setattr(_AVAILABLE, lambda: False)
        config = ServerConfig(tasks_url="redis://tasks-host:6379/1")
        with caplog.at_level(logging.WARNING, logger="fastmcp_pvl_core._tasks"):
            configure_task_backend("MY_APP", config)
        assert fastmcp.settings.docket.url == "memory://"
        warning = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        )
        assert "MY_APP_TASKS_URL" in warning
        assert "pydocket" in warning


class TestMemoryBackendSignal:
    def test_memory_plus_http_logs_process_local_note(self, docket_installed, caplog):
        config = ServerConfig(transport="http")
        with caplog.at_level(logging.INFO, logger="fastmcp_pvl_core._tasks"):
            configure_task_backend("MY_APP", config)
        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "backend=memory" in messages
        assert "process_local=true" in messages

    def test_memory_plus_stdio_logs_plain_line(self, docket_installed, caplog):
        config = ServerConfig(transport="stdio")
        with caplog.at_level(logging.INFO, logger="fastmcp_pvl_core._tasks"):
            configure_task_backend("MY_APP", config)
        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "process_local" not in messages

    def test_redis_logs_scheme_and_queue_only(self, docket_installed, caplog):
        """Redaction: host/credentials never reach the log line."""
        config = ServerConfig(
            transport="http", tasks_url="redis://user:hunter2@tasks-host:6379/1"
        )
        with caplog.at_level(logging.INFO, logger="fastmcp_pvl_core._tasks"):
            configure_task_backend("MY_APP", config)
        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "backend=redis" in messages
        assert "queue=my-app" in messages
        assert "hunter2" not in messages


class TestConfigSurface:
    def test_from_env_reads_tasks_url(self, monkeypatch):
        monkeypatch.setenv("PVLCORE_TEST_TASKS_URL", "redis://h:6379/0")
        config = ServerConfig.from_env("PVLCORE_TEST")
        assert config.tasks_url == "redis://h:6379/0"

    def test_unset_is_none(self, clean_env):
        config = ServerConfig.from_env("PVLCORE_TEST")
        assert config.tasks_url is None
