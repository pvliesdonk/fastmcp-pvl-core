"""Tests for :mod:`fastmcp_pvl_core.file_exchange` (the public facade)."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    FileExchangeHandle,
    FileRef,
    FileRefPreview,
    ResolvedSource,
    SinkContext,
    register_file_exchange,
    set_artifact_store,
)
from fastmcp_pvl_core._token_store import ArtifactStore
from fastmcp_pvl_core.file_exchange import _set_artifact_store_for_test


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
# Downstream hook helpers
# ---------------------------------------------------------------------------
#
# The file-exchange surface is two hooks: a SourceHook
# ``(origin_id) -> ResolvedSource`` and a SinkHook
# ``(file-like, SinkContext) -> mapping``. The helpers below build the
# minimal in-memory hooks the tests need.


def _src(
    payload: bytes,
    *,
    content_type: str | None = None,
    origin_id: str | None = None,
) -> Any:
    """Return a sync :data:`SourceHook` resolving to ``payload``.

    When ``origin_id`` is given, the hook rejects any other id with a
    ``ValueError`` (the contract for an unknown id). Otherwise it
    resolves every id to the same bytes.
    """

    def hook(requested: str) -> ResolvedSource:
        if origin_id is not None and requested != origin_id:
            raise ValueError(f"unknown origin_id: {requested!r}")
        return ResolvedSource(stream=io.BytesIO(payload), content_type=content_type)

    return hook


def _async_src(payload: bytes, *, content_type: str | None = None) -> Any:
    """Return an async :data:`SourceHook` resolving to ``payload``."""

    async def hook(requested: str) -> ResolvedSource:
        return ResolvedSource(stream=io.BytesIO(payload), content_type=content_type)

    return hook


def _sink(stream: BinaryIO, ctx: SinkContext) -> Mapping[str, Any]:
    """A sync :data:`SinkHook` that records the bytes it received."""
    data = stream.read()
    return {"stored_at": "memory", "bytes_written": len(data)}


async def _async_sink(stream: BinaryIO, ctx: SinkContext) -> Mapping[str, Any]:
    """An async :data:`SinkHook` that records the bytes it received."""
    data = stream.read()
    return {"stored_at": "memory", "bytes_written": len(data)}


# ---------------------------------------------------------------------------
# enable / transport gating
# ---------------------------------------------------------------------------


class TestEnableGating:
    def test_stdio_default_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FE_TRANSPORT", "stdio")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="test-mcp",
            env_prefix="TEST_FE",
            source=_src(b"x"),
            sink=_sink,
        )
        assert h.enabled is False
        assert "create_download_link" not in _tool_names(mcp)
        assert "fetch_file" not in _tool_names(mcp)

    def test_http_default_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="test-mcp",
            env_prefix="TEST_FE",
            source=_src(b"x"),
        )
        assert h.enabled is True
        assert "create_download_link" in _tool_names(mcp)

    def test_explicit_env_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_FILE_EXCHANGE_ENABLED", "true")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "stdio")
        mcp = _new_mcp()
        # stdio transport but env says enable.
        h = register_file_exchange(mcp, namespace="test-mcp", env_prefix="TEST_FE")
        assert h.enabled is True


# ---------------------------------------------------------------------------
# Producer-only mode
# ---------------------------------------------------------------------------


class TestProducerOnly:
    def test_registers_create_download_link_not_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="test-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            source=_src(b"x"),
        )
        names = _tool_names(mcp)
        assert "create_download_link" in names
        assert "fetch_file" not in names
        assert h.produce is True
        assert h.consume is False

    def test_no_source_hook_skips_producer_side(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Producer side is gated on a source hook, not just env.

        Even on http transport with BASE_URL set, a registration with
        no ``source=`` does not register ``create_download_link`` and
        does not advertise the producer capability.
        """
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="test-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
        )
        assert h.produce is False
        assert "create_download_link" not in _tool_names(mcp)
        # With neither role active, no file-exchange capability is
        # advertised at all.
        assert h.capability is None

    def test_capability_advertises_http_only_without_exchange(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="test-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            source=_src(b"x"),
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


class TestConsumerOnly:
    def test_registers_fetch_file_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No source hook → producer side (http) is not active.
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumes=("image/png", "application/pdf"),
            sink=_sink,
        )
        names = _tool_names(mcp)
        assert "fetch_file" in names
        assert "create_download_link" not in names
        assert h.consume is True
        assert h.produce is False

    def test_no_sink_hook_skips_consumer_side(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Consumer side is gated on a sink hook, not just env."""
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumes=("image/png",),
        )
        assert h.consume is False
        assert "fetch_file" not in _tool_names(mcp)


# ---------------------------------------------------------------------------
# Full-mode capability shape
# ---------------------------------------------------------------------------


class TestFullModeCapability:
    def test_advertises_both_methods(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            consumes=("image/png",),
            source=_src(b"x"),
            sink=_sink,
        )
        assert h.capability is not None
        cap = h.capability.to_capability_dict()
        assert "http" in cap["transfer_methods"]
        assert "exchange" in cap["transfer_methods"]
        assert cap["exchange_id"]


# ---------------------------------------------------------------------------
# make_file_ref — origin_id, http, exchange interaction
# ---------------------------------------------------------------------------


def _make_handle_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str = "http://test.example",
    source: Any | None = None,
) -> tuple[FastMCP, FileExchangeHandle]:
    """Register an http-only producing handle and return ``(mcp, handle)``.

    ``source`` defaults to a hook resolving every id to ``b"x"``.
    """
    monkeypatch.setenv("TEST_FE_BASE_URL", base_url)
    monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
    mcp = _new_mcp()
    h = register_file_exchange(
        mcp,
        namespace="image-mcp",
        env_prefix="TEST_FE",
        produces=("image/png",),
        source=source if source is not None else _src(b"x"),
    )
    return mcp, h


class TestMakeFileRef:
    async def test_origin_id_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The downstream owns the origin_id; make_file_ref echoes it.
        _, h = _make_handle_http(monkeypatch)
        ref = await h.make_file_ref("my-stable-id", mime_type="image/png")
        assert ref.origin_id == "my-stable-id"

    async def test_advertises_http_transfer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, h = _make_handle_http(monkeypatch)
        ref = await h.make_file_ref("abc", mime_type="image/png")
        assert "http" in ref.transfer
        assert ref.transfer["http"]["tool"] == "create_download_link"

    async def test_http_only_does_not_touch_source_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """For an http-only server make_file_ref does not invoke the hook.

        The ``transfer["http"]`` block just names the download tool;
        the source hook is not resolved until ``create_download_link``
        is actually called.
        """
        invocations = 0

        def render(requested: str) -> ResolvedSource:
            nonlocal invocations
            invocations += 1
            return ResolvedSource(stream=io.BytesIO(b"rendered"))

        _, h = _make_handle_http(monkeypatch, source=render)
        ref = await h.make_file_ref("abc", mime_type="image/png")
        assert invocations == 0
        assert set(ref.transfer) == {"http"}

    async def test_exchange_materialises_via_source_hook(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """With the exchange volume on, make_file_ref resolves the hook now.

        The shared volume has no pull trigger, so pvl-core invokes the
        source hook and writes the bytes into the volume eagerly.
        """
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()

        invocations = 0

        async def render(requested: str) -> ResolvedSource:
            nonlocal invocations
            invocations += 1
            return ResolvedSource(stream=io.BytesIO(b"rendered"))

        h = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            source=render,
        )
        ref = await h.make_file_ref("abc", mime_type="image/png")
        assert invocations == 1, (
            "source hook must be invoked when the exchange volume is on"
        )
        # Bytes are now on the exchange volume.
        assert "exchange" in ref.transfer
        uri = ref.transfer["exchange"]["uri"]
        assert uri.startswith("exchange://")
        assert ref.size_bytes == len(b"rendered")

    async def test_exchange_segment_decoupled_from_origin_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The ``exchange://`` URI segment is a SHA-256 of the origin_id.

        v0.5: a path-shaped ``origin_id`` is never embedded verbatim in
        the URI, path, or filename — pvl-core derives a segment-safe
        digest. The ``file_ref`` still carries the real ``origin_id``.
        """
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            source=_src(b"rendered"),
        )
        origin_id = "folder/to/note.md"
        ref = await h.make_file_ref(origin_id, mime_type="image/png")

        uri = ref.transfer["exchange"]["uri"]
        sha256hex = hashlib.sha256(b"folder/to/note.md").hexdigest()
        # The raw origin_id never appears in the URI…
        assert origin_id not in uri
        # …and the URI ends with the derived segment + ext.
        assert uri.endswith(f"/{sha256hex}.png")
        # The file is on disk under the derived segment.
        assert (tmp_path / "image-mcp" / f"{sha256hex}.png").exists()
        # The file_ref still carries the real, opaque origin_id.
        assert ref.origin_id == origin_id

    async def test_dual_transport_ref_carries_both_keys(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """With both the exchange volume AND a BASE_URL, the ref is dual-transport.

        ``MCP_EXCHANGE_DIR`` enables the ``exchange`` method and
        ``{PREFIX}_BASE_URL`` enables the ``http`` method; a producing
        ``make_file_ref`` then mints a ref whose ``transfer`` dict
        carries *both* keys so a consumer can pick either path.
        """
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            source=_src(b"rendered"),
        )
        ref = await h.make_file_ref("abc", mime_type="image/png")
        assert set(ref.transfer) == {"exchange", "http"}
        assert ref.transfer["http"]["tool"] == "create_download_link"
        assert ref.transfer["exchange"]["uri"].startswith("exchange://")

    async def test_disabled_handle_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_TRANSPORT", "stdio")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp, namespace="x", env_prefix="TEST_FE", source=_src(b"x")
        )
        with pytest.raises(RuntimeError, match="disabled"):
            await h.make_file_ref("abc", mime_type="image/png")

    async def test_preview_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, h = _make_handle_http(monkeypatch)
        preview = FileRefPreview(description="Test", dimensions=(10, 20))
        ref = await h.make_file_ref("abc", mime_type="image/png", preview=preview)
        assert ref.preview == preview
        assert ref.to_dict()["preview"] == {
            "description": "Test",
            "dimensions": {"width": 10, "height": 20},
        }


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
    async def test_round_trip_resolves_source_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, h = _make_handle_http(
            monkeypatch,
            source=_src(b"hello", content_type="text/plain", origin_id="abc"),
        )
        out = await _call_tool(mcp, "create_download_link", origin_id="abc")
        assert "url" in out
        assert out["url"].startswith("http://test.example/artifacts/")
        assert out["mime_type"] == "text/plain"
        assert out["ttl_seconds"] > 0

    async def test_async_source_hook_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, _ = _make_handle_http(
            monkeypatch, source=_async_src(b"async-bytes", content_type="image/png")
        )
        out = await _call_tool(mcp, "create_download_link", origin_id="abc")
        assert out["url"].startswith("http://test.example/artifacts/")
        assert out["mime_type"] == "image/png"

    async def test_source_hook_re_invoked_per_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        invocations = 0

        def render(requested: str) -> ResolvedSource:
            nonlocal invocations
            invocations += 1
            return ResolvedSource(stream=io.BytesIO(f"v{invocations}".encode()))

        mcp, _ = _make_handle_http(monkeypatch, source=render)
        await _call_tool(mcp, "create_download_link", origin_id="abc")
        await _call_tool(mcp, "create_download_link", origin_id="abc")
        assert invocations == 2, (
            "source hook must be re-invoked on each create_download_link call"
        )

    async def test_source_value_error_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The source hook rejects any id other than "abc" with ValueError.
        mcp, _ = _make_handle_http(monkeypatch, source=_src(b"x", origin_id="abc"))
        out = await _call_tool(mcp, "create_download_link", origin_id="never-published")
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert out["origin_id"] == "never-published"
        assert out["origin_server"] == "image-mcp"

    async def test_opaque_origin_id_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # v0.5: origin_id is an opaque domain reference — path-shaped and
        # URI-shaped values are passed through to the source hook, not
        # rejected by pvl-core.
        for origin_id in ("folder/to/note.md", "image://job-1/0", "../weird"):
            mcp, _ = _make_handle_http(
                monkeypatch, source=_src(b"x", origin_id=origin_id)
            )
            out = await _call_tool(mcp, "create_download_link", origin_id=origin_id)
            assert "url" in out, out
            assert "error" not in out

    @pytest.mark.parametrize(
        "bad",
        ["", " leading", "trailing ", "with\x00null", "with\x01ctrl"],
    )
    async def test_floor_violation_origin_id_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        # The minimal-safety floor is still enforced: empty, whitespace
        # edges, null bytes, control characters.
        mcp, _ = _make_handle_http(monkeypatch)
        out = await _call_tool(mcp, "create_download_link", origin_id=bad)
        assert out["error"] == "transfer_failed"

    async def test_resource_uri_origin_id_round_trips_to_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # v0.5: a resource-URI-shaped origin_id reaches the source hook
        # verbatim and a download URL comes back.
        seen: list[str] = []

        def render(requested: str) -> ResolvedSource:
            seen.append(requested)
            if requested != "image://job-1/view":
                raise ValueError(f"unknown origin_id: {requested!r}")
            return ResolvedSource(
                stream=io.BytesIO(b"png-bytes"), content_type="image/png"
            )

        mcp, _ = _make_handle_http(monkeypatch, source=render)
        out = await _call_tool(
            mcp, "create_download_link", origin_id="image://job-1/view"
        )
        assert seen == ["image://job-1/view"]
        assert out["url"].startswith("http://test.example/artifacts/")

    async def test_source_non_value_error_is_reraised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-ValueError from the source hook is a server bug — re-raised.

        Only ``ValueError`` maps to a ``transfer_failed`` envelope;
        anything else is logged and propagates so the failure is loud.
        """

        def boom(requested: str) -> ResolvedSource:
            raise RuntimeError("source hook is broken")

        mcp, _ = _make_handle_http(monkeypatch, source=boom)
        with pytest.raises(RuntimeError, match="source hook is broken"):
            await _call_tool(mcp, "create_download_link", origin_id="abc")

    async def test_ttl_clamped_to_handle_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp, h = _make_handle_http(monkeypatch)
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
    async def test_exchange_url_via_sink(
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

        async def vault_sink(stream: BinaryIO, ctx: SinkContext) -> Mapping[str, Any]:
            data = stream.read()
            captured["data"] = data
            captured["origin_id"] = ctx.origin_id
            return {
                "stored_at": "vault://generated/test.png",
                "bytes_written": len(data),
                "saved_path": "generated/test.png",
            }

        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumes=("image/png",),
            sink=vault_sink,
        )
        out = await _call_tool(mcp, "fetch_file", url=uri)
        assert out["method"] == "exchange"
        assert out["bytes_written"] == 3
        assert out["saved_path"] == "generated/test.png"
        assert captured["data"] == b"img"

    async def test_sync_sink_works(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A sync sink hook is supported alongside the async form."""
        monkeypatch.setenv("MCP_EXCHANGE_DIR", str(tmp_path))
        from fastmcp_pvl_core import FileExchange

        producer = FileExchange.from_env(default_namespace="image-mcp")
        assert producer is not None
        uri = str(producer.write_atomic(origin_id="abc", ext="png", content=b"img"))

        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            consumes=("image/png",),
            sink=_sink,
        )
        out = await _call_tool(mcp, "fetch_file", url=uri)
        assert out["method"] == "exchange"
        assert out["bytes_written"] == 3
        assert out["stored_at"] == "memory"

    async def test_invalid_input_neither_or_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            sink=_sink,
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
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            sink=_sink,
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
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        # Pre-create .exchange-id so the consumer attaches to "vault-group".
        (tmp_path / ".exchange-id").write_text("vault-group\n")
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            sink=_sink,
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
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        register_file_exchange(
            mcp,
            namespace="vault-mcp",
            env_prefix="TEST_FE",
            sink=_sink,
        )
        # Bare-URL SSRF refusals come back as a structured transfer_failed
        # envelope (the same shape as a file_ref-supplied http failure).
        out = await _call_tool(mcp, "fetch_file", url="http://127.0.0.1/secret")
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert "private/loopback" in out["message"]


# ---------------------------------------------------------------------------
# _disposition_filename — header-safe filename derivation
# ---------------------------------------------------------------------------


class TestDispositionFilename:
    @pytest.mark.parametrize(
        ("origin_id", "ext", "expected"),
        [
            # origin_id is opaque — pvl-core does not parse it; the whole
            # reference is folded to a safe charset, never split.
            ("folder/note.md", "png", "folder_note.md.png"),
            ("image://job-1/0", "webp", "image_job-1_0.webp"),
            ("a1b2c3", "png", "a1b2c3.png"),
            # Runs of unsafe characters collapse to one "_"; an all-unsafe
            # id falls back to "download".
            ("///", "png", "download.png"),
            # Slash and backslash are folded identically — neither is a
            # path separator inside an opaque reference.
            ("dir\\file.txt", "png", "dir_file.txt.png"),
            # A "." is kept, so an extension-suffixed id plus the appended
            # ext yields a (harmless) double extension.
            ("image://job-1/0.png", "png", "image_job-1_0.png.png"),
        ],
    )
    def test_derives_header_safe_filename(
        self, origin_id: str, ext: str, expected: str
    ) -> None:
        from fastmcp_pvl_core.file_exchange import _disposition_filename

        assert _disposition_filename(origin_id, ext) == expected


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class TestMisc:
    def test_handle_exposes_status_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://t")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            produces=("image/png",),
            source=_src(b"x"),
        )
        assert h.http_enabled is True
        assert h.exchange_enabled is False  # no MCP_EXCHANGE_DIR

    def test_internal_artifact_store_seam_injects_fake(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reset_artifact_store_test_seam: None,
    ) -> None:
        """``_set_artifact_store_for_test`` swaps in a fake store for
        the next register_file_exchange call.  Private API; tests only."""
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://example.test")
        fake = ArtifactStore(ttl_seconds=10.0, base_url="http://fake.test")
        _set_artifact_store_for_test(fake)
        mcp = _new_mcp()
        h = register_file_exchange(
            mcp,
            namespace="x",
            env_prefix="TEST_FE",
            produces=("application/pdf",),
            source=_src(b"x"),
        )
        assert h.artifact_store is fake

    def test_register_file_exchange_rejects_artifact_store_kwarg(self) -> None:
        """artifact_store is no longer accepted — pvl-core builds the
        store from env vars; test injection goes through the private
        _set_artifact_store_for_test seam."""
        mcp = _new_mcp()
        with pytest.raises(TypeError, match="artifact_store"):
            register_file_exchange(
                mcp,
                namespace="x",
                env_prefix="TEST_FE",
                artifact_store=object(),  # type: ignore[call-arg]
            )

    def test_register_file_exchange_rejects_transport_kwarg(self) -> None:
        """transport is no longer accepted — pvl-core resolves transport
        from {PREFIX}_TRANSPORT (fallback FASTMCP_TRANSPORT)."""
        mcp = _new_mcp()
        with pytest.raises(TypeError, match="transport"):
            register_file_exchange(
                mcp,
                namespace="x",
                env_prefix="TEST_FE",
                transport="http",  # type: ignore[call-arg]
            )

    def test_register_file_exchange_rejects_download_tool_name_kwarg(self) -> None:
        """Tool name is pvl-core's shape decision; not overridable."""
        mcp = _new_mcp()
        with pytest.raises(TypeError, match="download_tool_name"):
            register_file_exchange(
                mcp,
                namespace="x",
                env_prefix="TEST_FE",
                download_tool_name="not_allowed",  # type: ignore[call-arg]
            )

    def test_register_file_exchange_rejects_fetch_tool_name_kwarg(self) -> None:
        """Tool name is pvl-core's shape decision; not overridable."""
        mcp = _new_mcp()
        with pytest.raises(TypeError, match="fetch_tool_name"):
            register_file_exchange(
                mcp,
                namespace="x",
                env_prefix="TEST_FE",
                fetch_tool_name="not_allowed",  # type: ignore[call-arg]
            )

    def test_register_file_exchange_rejects_legacy_capability_shape_kwarg(self) -> None:
        """legacy_capability_shape was a v0.4-amendments-window shim;
        removed in 3.0.0."""
        mcp = _new_mcp()
        with pytest.raises(TypeError, match="legacy_capability_shape"):
            register_file_exchange(
                mcp,
                namespace="x",
                env_prefix="TEST_FE",
                legacy_capability_shape=True,  # type: ignore[call-arg]
            )

    def test_register_file_exchange_rejects_consumer_sink_kwarg(self) -> None:
        """consumer_sink= was the pre-#105 consumer hook kwarg — replaced
        by sink=."""
        mcp = _new_mcp()
        with pytest.raises(TypeError, match="consumer_sink"):
            register_file_exchange(
                mcp,
                namespace="x",
                env_prefix="TEST_FE",
                consumer_sink=_sink,  # type: ignore[call-arg]
            )
