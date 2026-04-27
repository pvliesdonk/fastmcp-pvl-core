"""Tests for :mod:`fastmcp_pvl_core.file_exchange` (the public facade)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    FetchContext,
    FetchResult,
    FileExchangeHandle,
    FileRef,
    FileRefPreview,
    register_file_exchange,
    set_artifact_store,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with no MCP_EXCHANGE_* / TEST_FE_* env vars."""
    for var in (
        "MCP_EXCHANGE_DIR",
        "MCP_EXCHANGE_ID",
        "MCP_EXCHANGE_NAMESPACE",
        "TEST_FE_FILE_EXCHANGE_ENABLED",
        "TEST_FE_FILE_EXCHANGE_PRODUCE",
        "TEST_FE_FILE_EXCHANGE_CONSUME",
        "TEST_FE_FILE_EXCHANGE_TTL",
        "TEST_FE_BASE_URL",
        "TEST_FE_TRANSPORT",
        "FASTMCP_TRANSPORT",
    ):
        monkeypatch.delenv(var, raising=False)
    set_artifact_store(None)
    yield
    set_artifact_store(None)


def _new_mcp() -> FastMCP:
    return FastMCP("test-fe")


async def _async_tool_names(mcp: FastMCP) -> set[str]:
    return {t.name for t in await mcp.list_tools()}


def _tool_names(mcp: FastMCP) -> set[str]:
    """Return the names of all tools currently registered on ``mcp``."""
    import asyncio

    return asyncio.run(_async_tool_names(mcp))


# ---------------------------------------------------------------------------
# enable / transport gating
# ---------------------------------------------------------------------------


class TestEnableGating:
    def test_stdio_default_disables(self) -> None:
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp, namespace="test-mcp", env_prefix="TEST_FE", transport="stdio"
        )
        assert h.enabled is False
        assert "create_download_link" not in _tool_names(mcp)
        assert "fetch_file" not in _tool_names(mcp)

    def test_http_default_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp, namespace="test-mcp", env_prefix="TEST_FE", transport="http"
        )
        assert h.enabled is True
        assert "create_download_link" in _tool_names(mcp)

    def test_explicit_env_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_FILE_EXCHANGE_ENABLED", "true")
        mcp = _new_mcp()
        # stdio transport but env says enable.
        h = register_file_exchange(
            mcp, namespace="test-mcp", env_prefix="TEST_FE", transport="stdio"
        )
        assert h.enabled is True


# ---------------------------------------------------------------------------
# Producer-only mode
# ---------------------------------------------------------------------------


class TestProducerOnly:
    def test_registers_create_download_link_not_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="test-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            transport="http",
        )
        names = _tool_names(mcp)
        assert "create_download_link" in names
        assert "fetch_file" not in names

    def test_capability_advertises_http_only_without_exchange(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="test-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            transport="http",
        )
        assert h.capability is not None
        cap = h.capability.to_capability_dict()
        assert "http" in cap["transfer_methods"]
        assert "exchange" not in cap["transfer_methods"]
        assert cap["produces"] == ["image/png"]
        assert cap["consumes"] == []
        assert "exchange_id" not in cap


# ---------------------------------------------------------------------------
# Consumer-only mode
# ---------------------------------------------------------------------------


async def _identity_sink(data: bytes, ctx: FetchContext) -> FetchResult:
    return FetchResult(stored_at="memory", bytes_written=len(data))


class TestConsumerOnly:
    def test_registers_fetch_file_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No BASE_URL → producer side (http) is not active.
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumes=("image/png", "application/pdf"),
            consumer_sink=_identity_sink,
            transport="http",
        )
        names = _tool_names(mcp)
        assert "fetch_file" in names
        assert "create_download_link" not in names


# ---------------------------------------------------------------------------
# Full-mode capability shape
# ---------------------------------------------------------------------------


class TestFullModeCapability:
    def test_advertises_both_methods(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            consumes=("image/png",),
            consumer_sink=_identity_sink,
            transport="http",
        )
        assert h.capability is not None
        cap = h.capability.to_capability_dict()
        assert "http" in cap["transfer_methods"]
        assert "exchange" in cap["transfer_methods"]
        assert cap["exchange_id"]


# ---------------------------------------------------------------------------
# publish() — origin_id, lazy, eager, exchange interaction
# ---------------------------------------------------------------------------


def _make_handle_http(
    monkeypatch: pytest.MonkeyPatch, *, base_url: str = "http://test.example"
) -> tuple[FastMCP, FileExchangeHandle]:
    monkeypatch.setenv("TEST_FE_BASE_URL", base_url)
    mcp = _new_mcp()
    h = register_file_exchange(
        mcp,
        namespace="image-mcp",
        env_prefix="TEST_FE",
        produces=("image/png",),
        transport="http",
    )
    return mcp, h


class TestPublish:
    async def test_default_origin_id_is_uuid_hex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, h = _make_handle_http(monkeypatch)
        ref = await h.publish(source=b"x", mime_type="image/png")
        assert len(ref.origin_id) == 32
        # Hex-only.
        int(ref.origin_id, 16)

    async def test_explicit_origin_id_round_trips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, h = _make_handle_http(monkeypatch)
        ref = await h.publish(
            source=b"x", mime_type="image/png", origin_id="my-stable-id"
        )
        assert ref.origin_id == "my-stable-id"

    async def test_publish_bytes_advertises_http_transfer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, h = _make_handle_http(monkeypatch)
        ref = await h.publish(source=b"x", mime_type="image/png")
        assert "http" in ref.transfer
        assert ref.transfer["http"]["tool"] == "create_download_link"

    async def test_publish_path_writes_through_to_http(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _, h = _make_handle_http(monkeypatch)
        f = tmp_path / "img.png"
        f.write_bytes(b"PNG-bytes")
        ref = await h.publish(source=f, mime_type="image/png")
        assert ref.size_bytes == len(b"PNG-bytes")
        # Record present in registry; bytes not yet read (Path not opened
        # until create_download_link is called).
        record = h.publish_registry[ref.origin_id]
        assert record.eager_path == f
        assert record.eager_bytes is None

    async def test_publish_lazy_not_invoked_at_publish_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, h = _make_handle_http(monkeypatch)
        invocations = 0

        def render() -> bytes:
            nonlocal invocations
            invocations += 1
            return b"rendered"

        ref = await h.publish(lazy=render, mime_type="image/png")
        assert invocations == 0
        # The record stores the callable; bytes still un-materialised.
        record = h.publish_registry[ref.origin_id]
        assert record.lazy is render
        assert record.eager_bytes is None

    async def test_publish_lazy_with_exchange_materialises_eagerly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            transport="http",
        )

        invocations = 0

        async def render() -> bytes:
            nonlocal invocations
            invocations += 1
            return b"rendered"

        with caplog.at_level("WARNING"):
            ref = await h.publish(lazy=render, mime_type="image/png")
        assert invocations == 1, (
            "lazy callable must be invoked at publish time when exchange is on"
        )
        assert any("materialising eagerly" in r.message for r in caplog.records)
        # Bytes are now on the exchange volume.
        assert "exchange" in ref.transfer
        uri = ref.transfer["exchange"]["uri"]
        assert uri.startswith("exchange://")

    async def test_publish_neither_source_nor_lazy_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, h = _make_handle_http(monkeypatch)
        with pytest.raises(ValueError, match="source= or lazy="):
            await h.publish(mime_type="image/png")

    async def test_publish_both_source_and_lazy_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, h = _make_handle_http(monkeypatch)
        with pytest.raises(ValueError, match="source= or lazy="):
            await h.publish(
                source=b"x",
                lazy=lambda: b"y",
                mime_type="image/png",
            )

    async def test_publish_disabled_handle_raises(self) -> None:
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp, namespace="x", env_prefix="TEST_FE", transport="stdio"
        )
        with pytest.raises(RuntimeError, match="disabled"):
            await h.publish(source=b"x", mime_type="image/png")


# ---------------------------------------------------------------------------
# create_download_link — end-to-end through the registered tool
# ---------------------------------------------------------------------------


async def _get_tool(mcp: FastMCP, name: str) -> Any:
    return await mcp.get_tool(name)


async def _call_tool(mcp: FastMCP, name: str, **kwargs: Any) -> dict[str, Any]:
    tool = await _get_tool(mcp, name)
    result = await tool.run(kwargs)
    # FastMCP ToolResult exposes ``structured_content`` when the body
    # returned a dict; otherwise we coerce from text content.
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    text_blocks = [b.text for b in result.content if hasattr(b, "text")]
    return json.loads(text_blocks[0]) if text_blocks else {}


class TestCreateDownloadLinkTool:
    async def test_round_trip_eager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp, h = _make_handle_http(monkeypatch)
        ref = await h.publish(
            source=b"hello",
            mime_type="text/plain",
            ext="txt",
            origin_id="abc",
        )
        out = await _call_tool(mcp, "create_download_link", origin_id="abc")
        assert "url" in out
        assert out["url"].startswith("http://test.example/artifacts/")
        assert out["mime_type"] == "text/plain"
        assert out["ttl_seconds"] > 0
        # FileRef advertises only http here (no MCP_EXCHANGE_DIR).
        assert set(ref.transfer) == {"http"}

    async def test_round_trip_lazy_invoked_per_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, h = _make_handle_http(monkeypatch)
        invocations = 0

        def render() -> bytes:
            nonlocal invocations
            invocations += 1
            return f"v{invocations}".encode()

        await h.publish(lazy=render, mime_type="image/png", origin_id="abc")
        await _call_tool(mcp, "create_download_link", origin_id="abc")
        await _call_tool(mcp, "create_download_link", origin_id="abc")
        assert invocations == 2, (
            "lazy must be re-invoked on each create_download_link call"
        )

    async def test_unknown_origin_id_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, _ = _make_handle_http(monkeypatch)
        out = await _call_tool(mcp, "create_download_link", origin_id="never-published")
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"

    async def test_invalid_origin_id_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, _ = _make_handle_http(monkeypatch)
        out = await _call_tool(mcp, "create_download_link", origin_id="bad/with-slash")
        assert out["error"] == "transfer_failed"

    async def test_ttl_clamped_to_handle_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, h = _make_handle_http(monkeypatch)
        await h.publish(source=b"x", mime_type="image/png", origin_id="abc")
        out = await _call_tool(
            mcp,
            "create_download_link",
            origin_id="abc",
            ttl_seconds=1_000_000,
        )
        assert out["ttl_seconds"] == h.ttl_seconds


# ---------------------------------------------------------------------------
# fetch_file — end-to-end through the registered tool
# ---------------------------------------------------------------------------


class TestFetchFileTool:
    async def test_exchange_url_via_consumer_sink(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        # Producer writes a file to the volume…
        from fastmcp_pvl_core import FileExchange

        producer = FileExchange.from_env(default_namespace="image-mcp")
        assert producer is not None
        uri = str(producer.write_atomic(origin_id="abc", ext="png", content=b"img"))

        # …consumer wires fetch_file with a sink and reads it.
        captured: dict[str, Any] = {}

        async def vault_sink(data: bytes, ctx: FetchContext) -> FetchResult:
            captured["data"] = data
            captured["url"] = ctx.url
            return FetchResult(
                stored_at="vault://generated/test.png",
                bytes_written=len(data),
                extra={"saved_path": "generated/test.png"},
            )

        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumes=("image/png",),
            consumer_sink=vault_sink,
            transport="http",
        )
        out = await _call_tool(mcp, "fetch_file", url=uri)
        assert out["method"] == "exchange"
        assert out["bytes_written"] == 3
        assert out["saved_path"] == "generated/test.png"
        assert captured["data"] == b"img"

    async def test_invalid_input_neither_or_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumer_sink=_identity_sink,
            transport="http",
        )
        # Neither
        out = await _call_tool(mcp, "fetch_file")
        assert out["error"] == "invalid_input"
        # Both
        ref = FileRef(
            origin_server="x", origin_id="y", transfer={"http": {"tool": "t"}}
        ).to_dict()
        out2 = await _call_tool(mcp, "fetch_file", file_ref=ref, url="http://x/y")
        assert out2["error"] == "invalid_input"

    async def test_unsupported_scheme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumer_sink=_identity_sink,
            transport="http",
        )
        out = await _call_tool(mcp, "fetch_file", url="ftp://nope/x")
        assert out["error"] == "invalid_input"

    async def test_file_ref_exchange_then_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Consumer is in group "vault-group" but file_ref is from "image-group".
        # Exchange resolution fails with ExchangeGroupMismatch → tool reports
        # transfer_failed with remaining_transfer.
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        # Pre-create .exchange-id so the consumer attaches to "vault-group".
        (tmp_path / ".exchange-id").write_text("vault-group\n")
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumer_sink=_identity_sink,
            transport="http",
        )
        ref = FileRef(
            origin_server="image-mcp",
            origin_id="abc",
            transfer={
                "exchange": {"uri": "exchange://image-group/image-mcp/abc.png"},
                "http": {"tool": "create_download_link"},
            },
        ).to_dict()
        out = await _call_tool(mcp, "fetch_file", file_ref=ref)
        assert out["error"] == "transfer_failed"
        assert out["method"] == "exchange"
        assert "remaining_transfer" in out
        assert "http" in out["remaining_transfer"]

    async def test_ssrf_guard_rejects_private_ip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumer_sink=_identity_sink,
            transport="http",
        )
        # Bare-URL SSRF refusals come back as a structured transfer_failed
        # envelope (the same shape as a file_ref-supplied http failure).
        out = await _call_tool(mcp, "fetch_file", url="http://127.0.0.1/secret")
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert "private/loopback" in out["message"]


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class TestMisc:
    def test_handle_exposes_status_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://t")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            produces=("image/png",),
            transport="http",
        )
        assert h.http_enabled is True
        assert h.exchange_enabled is False  # no MCP_EXCHANGE_DIR

    def test_preview_round_trips_via_publish(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://t")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            produces=("image/png",),
            transport="http",
        )
        preview = FileRefPreview(description="Test", dimensions=(10, 20))
        import asyncio

        ref = asyncio.run(
            h.publish(source=b"x", mime_type="image/png", preview=preview)
        )
        assert ref.preview == preview
        assert ref.to_dict()["preview"] == {
            "description": "Test",
            "dimensions": {"width": 10, "height": 20},
        }
