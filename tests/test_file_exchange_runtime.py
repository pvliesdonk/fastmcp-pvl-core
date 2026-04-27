"""Tests for :mod:`fastmcp_pvl_core._file_exchange_runtime`."""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from fastmcp_pvl_core._file_exchange_protocol import (
    ExchangeURI,
    ExchangeURIError,
)
from fastmcp_pvl_core._file_exchange_runtime import (
    ExchangeGroupMismatch,
    FileExchange,
    FileExchangeConfigError,
)


@pytest.fixture(autouse=True)
def _strip_mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no MCP_EXCHANGE_* env vars set."""
    for var in ("MCP_EXCHANGE_DIR", "MCP_EXCHANGE_ID", "MCP_EXCHANGE_NAMESPACE"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_unset_returns_none(self) -> None:
        assert FileExchange.from_env(default_namespace="image-mcp") is None

    def test_blank_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # set-but-empty is treated as a deployment misconfiguration, not
        # a silent opt-out (use unset for that).
        monkeypatch.setenv("MCP_EXCHANGE_DIR", "   ")
        with pytest.raises(FileExchangeConfigError, match="empty"):
            FileExchange.from_env(default_namespace="image-mcp")

    def test_missing_directory_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path / "absent"))
        with pytest.raises(FileExchangeConfigError, match="does not exist"):
            FileExchange.from_env(default_namespace="image-mcp")

    def test_path_is_a_file_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = tmp_path / "file"
        f.write_text("x")
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(f))
        with pytest.raises(FileExchangeConfigError, match="not a directory"):
            FileExchange.from_env(default_namespace="image-mcp")

    def test_creates_exchange_id_on_first_use(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        fx = FileExchange.from_env(default_namespace="image-mcp")
        assert fx is not None
        assert fx.exchange_id  # non-empty
        # Validate it's a parseable UUID.
        uuid.UUID(fx.exchange_id)
        # File exists with the same value.
        persisted = (tmp_path / ".exchange-id").read_text(encoding="utf-8").strip()
        assert persisted == fx.exchange_id

    def test_reads_existing_exchange_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".exchange-id").write_text(
            "preexisting-id-with-trailing-newline\n", encoding="utf-8"
        )
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        fx = FileExchange.from_env(default_namespace="image-mcp")
        assert fx is not None
        # Trailing newline stripped.
        assert fx.exchange_id == "preexisting-id-with-trailing-newline"

    def test_explicit_id_matches_existing_ok(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".exchange-id").write_text("hades-01\n", encoding="utf-8")
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        monkeypatch.setenv("MCP_EXCHANGE_ID", "hades-01")
        fx = FileExchange.from_env(default_namespace="image-mcp")
        assert fx is not None
        assert fx.exchange_id == "hades-01"

    def test_explicit_id_mismatch_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".exchange-id").write_text("hades-01\n", encoding="utf-8")
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        monkeypatch.setenv("MCP_EXCHANGE_ID", "cloud-02")
        with pytest.raises(ExchangeGroupMismatch, match="hades-01"):
            FileExchange.from_env(default_namespace="image-mcp")

    def test_corrupt_empty_exchange_id_file_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".exchange-id").write_text("", encoding="utf-8")
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        with pytest.raises(FileExchangeConfigError, match="empty"):
            FileExchange.from_env(default_namespace="image-mcp")

    def test_namespace_default_used_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        fx = FileExchange.from_env(default_namespace="image-mcp")
        assert fx is not None
        assert fx.namespace == "image-mcp"

    def test_namespace_env_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        monkeypatch.setenv("MCP_EXCHANGE_NAMESPACE", "image-mcp-2")
        fx = FileExchange.from_env(default_namespace="image-mcp")
        assert fx is not None
        assert fx.namespace == "image-mcp-2"

    def test_invalid_namespace_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        monkeypatch.setenv("MCP_EXCHANGE_NAMESPACE", "bad/name")
        with pytest.raises(FileExchangeConfigError, match="invalid"):
            FileExchange.from_env(default_namespace="image-mcp")

    def test_namespace_starting_with_dot_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        monkeypatch.setenv("MCP_EXCHANGE_NAMESPACE", ".hidden")
        with pytest.raises(FileExchangeConfigError, match="dot"):
            FileExchange.from_env(default_namespace="image-mcp")


# ---------------------------------------------------------------------------
# .exchange-id race safety
# ---------------------------------------------------------------------------


class TestExchangeIdRace:
    def test_concurrent_inits_produce_one_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """16 threads racing on an empty dir — exactly one writer wins."""
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        results: list[str] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(16)

        def init_one() -> None:
            barrier.wait()
            fx = FileExchange.from_env(default_namespace="image-mcp")
            assert fx is not None
            with results_lock:
                results.append(fx.exchange_id)

        threads = [threading.Thread(target=init_one) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads agree on the same id.
        assert len(set(results)) == 1
        # Exactly one .exchange-id file exists.
        files = list(tmp_path.glob(".exchange-id*"))
        assert len(files) == 1


# ---------------------------------------------------------------------------
# write_atomic / read_exchange_uri
# ---------------------------------------------------------------------------


def _make_fx(tmp: Path, namespace: str = "image-mcp") -> FileExchange:
    (tmp / ".exchange-id").write_text("hades-01\n", encoding="utf-8")
    return FileExchange(
        base_dir=tmp,
        exchange_id="hades-01",
        namespace=namespace,
    )


class TestWriteAtomic:
    def test_round_trip(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        uri = fx.write_atomic(origin_id="abc", ext="png", content=b"hello")
        assert isinstance(uri, ExchangeURI)
        assert str(uri) == "exchange://hades-01/image-mcp/abc.png"
        assert (tmp_path / "image-mcp" / "abc.png").read_bytes() == b"hello"

    def test_no_tmp_file_remains_on_success(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        fx.write_atomic(origin_id="abc", ext="png", content=b"x")
        # Only the final file should exist (no leftover dotfile).
        ns_dir = tmp_path / "image-mcp"
        names = sorted(p.name for p in ns_dir.iterdir())
        assert names == ["abc.png"]

    def test_pre_existing_tmp_invisible_to_consumer(self, tmp_path: Path) -> None:
        """A simulated crashed writer leaves a .tmp file consumers ignore."""
        fx = _make_fx(tmp_path)
        ns_dir = tmp_path / "image-mcp"
        ns_dir.mkdir()
        # Writer crashed mid-flight — only the dotfile temp exists.
        (ns_dir / ".abc.png.tmp").write_bytes(b"half-written")
        # The consumer side refuses to read it (file isn't visible by URI).
        with pytest.raises(FileNotFoundError):
            fx.read_exchange_uri("exchange://hades-01/image-mcp/abc.png")

    def test_invalid_origin_id_rejected(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        with pytest.raises(ExchangeURIError):
            fx.write_atomic(origin_id="bad/id", ext="png", content=b"x")

    def test_origin_id_starting_with_dot_rejected(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        with pytest.raises(ExchangeURIError, match="dot"):
            fx.write_atomic(origin_id=".hidden", ext="png", content=b"x")

    def test_overwrite_replaces_atomically(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        fx.write_atomic(origin_id="abc", ext="png", content=b"v1")
        fx.write_atomic(origin_id="abc", ext="png", content=b"v2")
        assert (tmp_path / "image-mcp" / "abc.png").read_bytes() == b"v2"


class TestReadExchangeURI:
    def test_round_trip(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        fx.write_atomic(origin_id="abc", ext="png", content=b"hello")
        data = fx.read_exchange_uri("exchange://hades-01/image-mcp/abc.png")
        assert data == b"hello"

    def test_cross_namespace_read(self, tmp_path: Path) -> None:
        """A consumer (vault-mcp) reads bytes a different namespace produced."""
        producer = _make_fx(tmp_path, namespace="image-mcp")
        producer.write_atomic(origin_id="abc", ext="png", content=b"image")

        consumer = FileExchange(
            base_dir=tmp_path, exchange_id="hades-01", namespace="vault-mcp"
        )
        data = consumer.read_exchange_uri("exchange://hades-01/image-mcp/abc.png")
        assert data == b"image"

    def test_group_mismatch_carries_both_ids(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        with pytest.raises(ExchangeGroupMismatch) as exc_info:
            fx.read_exchange_uri("exchange://other-group/image-mcp/abc.png")
        msg = str(exc_info.value)
        assert "other-group" in msg
        assert "hades-01" in msg

    def test_traversal_uri_rejected(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        with pytest.raises(ExchangeURIError):
            fx.read_exchange_uri(
                "exchange://hades-01/image-mcp/%252e%252e%252fpasswd.png"
            )

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        # Make the namespace dir so only the file is missing.
        (tmp_path / "image-mcp").mkdir()
        with pytest.raises(FileNotFoundError):
            fx.read_exchange_uri("exchange://hades-01/image-mcp/nope.png")


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


class TestSweep:
    def test_no_namespace_dir_yet_is_ok(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        assert fx.sweep() == 0

    def test_ttl_eviction(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        fx = FileExchange(
            base_dir=fx.base_dir,
            exchange_id=fx.exchange_id,
            namespace=fx.namespace,
            ttl_seconds=1.0,
        )
        fx.write_atomic(origin_id="old", ext="bin", content=b"x")
        # Backdate the file's mtime by 5 seconds.
        target = tmp_path / "image-mcp" / "old.bin"
        past = time.time() - 5
        os.utime(target, (past, past))
        # And add a fresh one.
        fx.write_atomic(origin_id="fresh", ext="bin", content=b"y")

        removed = fx.sweep()
        assert removed == 1
        ns = tmp_path / "image-mcp"
        names = sorted(p.name for p in ns.iterdir())
        assert names == ["fresh.bin"]

    def test_dotfiles_skipped(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        fx = FileExchange(
            base_dir=fx.base_dir,
            exchange_id=fx.exchange_id,
            namespace=fx.namespace,
            ttl_seconds=1.0,
        )
        ns = tmp_path / "image-mcp"
        ns.mkdir()
        # Old dotfile (e.g. crashed-writer .tmp) — must not be deleted by sweep.
        tmp_file = ns / ".pending.bin.tmp"
        tmp_file.write_bytes(b"x")
        past = time.time() - 100
        os.utime(tmp_file, (past, past))

        assert fx.sweep() == 0
        assert tmp_file.exists()

    def test_lru_eviction_under_ceiling(self, tmp_path: Path) -> None:
        fx = _make_fx(tmp_path)
        fx = FileExchange(
            base_dir=fx.base_dir,
            exchange_id=fx.exchange_id,
            namespace=fx.namespace,
            # TTL very long so only LRU triggers.
            ttl_seconds=3600.0,
        )
        # Three 100-byte files, oldest first.
        for i, name in enumerate(("a", "b", "c")):
            fx.write_atomic(origin_id=name, ext="bin", content=b"x" * 100)
            # Each file gets a distinct mtime in increasing order.
            target = tmp_path / "image-mcp" / f"{name}.bin"
            mtime = time.time() - (3 - i) * 10
            os.utime(target, (mtime, mtime))

        # Ceiling 150 bytes → must remove 2 of the 3 (oldest first).
        removed = fx.sweep(storage_ceiling_bytes=150)
        assert removed == 2
        names = sorted(p.name for p in (tmp_path / "image-mcp").iterdir())
        assert names == ["c.bin"]
