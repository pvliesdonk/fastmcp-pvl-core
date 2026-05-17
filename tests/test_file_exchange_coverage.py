"""Coverage-gap tests for the file-exchange internals.

Originally added in response to the PR #33 review; rewritten for the
issue #105 hook-API unification (the four divergent hook shapes
collapsed into a single ``source`` hook and a single ``sink`` hook).

Specifically targets:

- ``_consume_http`` (happy path, 4xx, oversize cap, redirect-not-followed)
  via ``httpx.MockTransport``.
- ``_ssrf_guard`` parametrised matrix (IPv6 loopback, link-local,
  RFC1918, multicast, plus a positive DNS-name pass-through).
- Umask-resistant 0o644 / 0o755 file modes for ``.exchange-id``,
  namespace dirs, and exchange data files (cross-UID multi-container
  deployments).
- ``_resolve_source`` / ``_invoke_sink`` sync-vs-async dispatch and
  bad-return ``TypeError`` guards.
- ``create_download_link`` translating a ``source``-hook ``ValueError``
  into a ``transfer_failed`` envelope (folds in former issue #78), and
  clamping ``ttl_seconds`` to the handle's configured ceiling.
- Concurrent ``sweep`` + ``write_atomic`` race.
- ``client_orchestration_required`` envelope when a file_ref offers
  only the ``http`` method.
"""

from __future__ import annotations

import io
import os
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    FileExchange,
    FileExchangeHandle,
    FileRef,
    ResolvedSource,
    SinkContext,
    register_file_exchange,
    set_artifact_store,
)
from fastmcp_pvl_core._file_exchange_runtime import (
    _exchange_segment,
    _resolve_exchange_id,
)
from fastmcp_pvl_core.file_exchange import (
    FetchTransportError,
    SinkHook,
    SourceHook,
    _invoke_sink,
    _resolve_source,
    _ssrf_guard,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in (
        "MCP_EXCHANGE_DIR",
        "MCP_EXCHANGE_ID",
        "MCP_EXCHANGE_NAMESPACE",
        "TEST_FE_FILE_EXCHANGE_ENABLED",
        "TEST_FE_BASE_URL",
        "TEST_FE_TRANSPORT",
        "FASTMCP_TRANSPORT",
    ):
        monkeypatch.delenv(var, raising=False)
    set_artifact_store(None)
    yield
    set_artifact_store(None)


# ---------------------------------------------------------------------------
# Module-level hook helpers
# ---------------------------------------------------------------------------
#
# The downstream surface is exactly two hooks. These factories build the
# minimal sync/async variants the coverage suite exercises.


def _src(payload: bytes, content_type: str | None = None) -> SourceHook:
    """Build a sync ``source`` hook that resolves any id to ``payload``."""

    def source(origin_id: str) -> ResolvedSource:
        return ResolvedSource(
            stream=io.BytesIO(payload),
            content_type=content_type,
            size_bytes=len(payload),
        )

    return source


def _async_src(payload: bytes, content_type: str | None = None) -> SourceHook:
    """Build an async ``source`` hook that resolves any id to ``payload``."""

    async def source(origin_id: str) -> ResolvedSource:
        return ResolvedSource(
            stream=io.BytesIO(payload),
            content_type=content_type,
            size_bytes=len(payload),
        )

    return source


def _rejecting_src(message: str = "unknown origin_id") -> SourceHook:
    """Build a ``source`` hook that rejects every id with ``ValueError``."""

    def source(origin_id: str) -> ResolvedSource:
        raise ValueError(message)

    return source


def _sink(captured: dict[str, Any] | None = None) -> SinkHook:
    """Build a sync ``sink`` hook that reads the stream and returns a dict."""

    def sink(stream: BinaryIO, ctx: SinkContext) -> Mapping[str, Any]:
        data = stream.read()
        if captured is not None:
            captured["data"] = data
            captured["mime_type"] = ctx.mime_type
            captured["origin_id"] = ctx.origin_id
            captured["size_bytes"] = ctx.size_bytes
        return {"stored_at": "memory", "bytes_written": len(data)}

    return sink


def _async_sink(captured: dict[str, Any] | None = None) -> SinkHook:
    """Build an async ``sink`` hook that reads the stream and returns a dict."""

    async def sink(stream: BinaryIO, ctx: SinkContext) -> Mapping[str, Any]:
        data = stream.read()
        if captured is not None:
            captured["data"] = data
            captured["mime_type"] = ctx.mime_type
            captured["origin_id"] = ctx.origin_id
            captured["size_bytes"] = ctx.size_bytes
        return {"stored_at": "memory", "bytes_written": len(data)}

    return sink


# ---------------------------------------------------------------------------
# _consume_http via httpx.MockTransport
# ---------------------------------------------------------------------------


def _new_consumer_mcp(monkeypatch: pytest.MonkeyPatch, sink: SinkHook) -> FastMCP:
    monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
    mcp = FastMCP("test-fe")
    register_file_exchange(
        mcp,
        namespace="vault-mcp",
        env_prefix="TEST_FE",
        sink=sink,
    )
    return mcp


async def _call_fetch(mcp: FastMCP, **kwargs: Any) -> dict[str, Any]:
    import json

    tool = await mcp.get_tool("fetch_file")
    assert tool is not None
    result = await tool.run(kwargs)
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    text_blocks = [b.text for b in result.content if hasattr(b, "text")]
    return json.loads(text_blocks[0]) if text_blocks else {}


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    """Patch ``httpx.AsyncClient`` so ``_consume_http`` uses a mock transport."""
    original = httpx.AsyncClient

    def mock_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)


class TestConsumeHTTP:
    async def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"hello-bytes",
                headers={
                    "content-type": "image/png",
                    "content-disposition": 'attachment; filename="img.png"',
                },
            )

        _mock_http(monkeypatch, handler)
        mcp = _new_consumer_mcp(monkeypatch, _sink(captured))

        out = await _call_fetch(mcp, url="https://example.com/img.png")
        assert out["method"] == "http"
        assert out["bytes_written"] == 11
        assert out["stored_at"] == "memory"
        assert captured["data"] == b"hello-bytes"
        # The Content-Type header flows into the SinkContext.
        assert captured["mime_type"] == "image/png"
        # A bare-url fetch carries no origin_id.
        assert captured["origin_id"] is None

    async def test_async_sink_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Same path as above but the sink hook is async — _invoke_sink
        # must dispatch a coroutine function correctly.
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"async-bytes",
                headers={"content-type": "image/webp"},
            )

        _mock_http(monkeypatch, handler)
        mcp = _new_consumer_mcp(monkeypatch, _async_sink(captured))

        out = await _call_fetch(mcp, url="https://example.com/img.webp")
        assert out["method"] == "http"
        assert out["bytes_written"] == 11
        assert captured["data"] == b"async-bytes"
        assert captured["mime_type"] == "image/webp"

    async def test_4xx_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"nope")

        _mock_http(monkeypatch, handler)
        mcp = _new_consumer_mcp(monkeypatch, _sink())

        out = await _call_fetch(mcp, url="https://example.com/missing")
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert "http fetch failed" in out["message"]

    async def test_oversize_response_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastmcp_pvl_core import file_exchange as fx_module

        # Shrink the cap so we don't have to fabricate 256 MiB.
        monkeypatch.setattr(fx_module, "_DEFAULT_HTTP_FETCH_MAX_BYTES", 100)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 1000)

        _mock_http(monkeypatch, handler)
        mcp = _new_consumer_mcp(monkeypatch, _sink())

        out = await _call_fetch(mcp, url="https://example.com/big")
        assert out["error"] == "transfer_failed"
        assert "exceeds" in out["message"]

    async def test_content_length_exceeds_cap_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the response advertises a Content-Length that already
        # exceeds the cap, fail before streaming any bytes — saves
        # bandwidth and surfaces the rejection earlier. The sink must
        # never be invoked.
        from fastmcp_pvl_core import file_exchange as fx_module

        monkeypatch.setattr(fx_module, "_DEFAULT_HTTP_FETCH_MAX_BYTES", 100)
        sink_called = False

        def sink(stream: BinaryIO, ctx: SinkContext) -> Mapping[str, Any]:
            nonlocal sink_called
            sink_called = True
            return {"bytes_written": len(stream.read())}

        def handler(request: httpx.Request) -> httpx.Response:
            # Advertise a giant content-length but return a short body so
            # the test fails fast on the header check, not the streaming
            # check (the streaming check would also reject this).
            return httpx.Response(
                200,
                content=b"x" * 50,  # any body — header check fires first
                headers={"content-length": "999999"},
            )

        _mock_http(monkeypatch, handler)
        mcp = _new_consumer_mcp(monkeypatch, sink)

        out = await _call_fetch(mcp, url="https://example.com/big")
        assert out["error"] == "transfer_failed"
        assert "content-length" in out["message"]
        assert sink_called is False

    async def test_3xx_response_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A 3xx response with follow_redirects=False must be treated as a
        # failure, not silently consume the redirect body as file content.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                301,
                content=b"<html>Moved Permanently</html>",
                headers={"location": "https://elsewhere.example/x"},
            )

        _mock_http(monkeypatch, handler)
        mcp = _new_consumer_mcp(monkeypatch, _sink())

        out = await _call_fetch(mcp, url="https://example.com/redirect")
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert "redirect" in out["message"]

    async def test_body_above_spool_threshold_spills_to_disk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _consume_http streams the GET response into a
        # SpooledTemporaryFile(max_size=_FETCH_SPOOL_MAX_BYTES). A body
        # above that threshold exercises the on-disk spill path; the
        # sink must still receive the exact bytes (full round-trip
        # through the spilled spool).
        from fastmcp_pvl_core import file_exchange as fx_module

        # Shrink the spool threshold so a modest body spills to disk;
        # keep the hard cap comfortably above the body so the size
        # guard does not fire first.
        monkeypatch.setattr(fx_module, "_FETCH_SPOOL_MAX_BYTES", 1024)
        monkeypatch.setattr(fx_module, "_DEFAULT_HTTP_FETCH_MAX_BYTES", 1024 * 1024)

        body = b"S" * (4 * 1024)  # 4 KiB — above the 1 KiB spool threshold
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/octet-stream"},
            )

        _mock_http(monkeypatch, handler)
        mcp = _new_consumer_mcp(monkeypatch, _sink(captured))

        out = await _call_fetch(mcp, url="https://example.com/big-but-ok")
        assert out["method"] == "http"
        assert out["bytes_written"] == len(body)
        # Full round-trip through the spilled (on-disk) spool.
        assert captured["data"] == body

    async def test_redirect_not_followed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Redirect to an internal IP — if follow_redirects became True,
        # the SSRF guard wouldn't catch it (only the initial URL is
        # checked) and we'd get the redirected body.
        def handler(request: httpx.Request) -> httpx.Response:
            if "redirected" in str(request.url):
                return httpx.Response(200, content=b"BAD-redirected-body")
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/redirected"},
            )

        _mock_http(monkeypatch, handler)
        mcp = _new_consumer_mcp(monkeypatch, _sink())

        out = await _call_fetch(mcp, url="https://example.com/redirect")
        # 302 with raise_for_status → http error → transfer_failed.
        # Crucially the response body must NOT be the redirected one.
        assert out["error"] == "transfer_failed"


# ---------------------------------------------------------------------------
# _ssrf_guard matrix
# ---------------------------------------------------------------------------


class TestSSRFGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/x",
            "http://127.255.255.255/x",  # loopback range
            "http://[::1]/x",  # IPv6 loopback
            "http://10.0.0.1/x",  # RFC1918
            "http://172.16.0.1/x",  # RFC1918
            "http://192.168.1.1/x",  # RFC1918
            "http://169.254.169.254/x",  # AWS IMDS (link-local)
            "http://[fe80::1]/x",  # IPv6 link-local
            "http://224.0.0.1/x",  # IPv4 multicast
            "http://0.0.0.0/x",  # unspecified
        ],
    )
    def test_blocks_private_loopback_link_local(self, url: str) -> None:
        with pytest.raises(FetchTransportError, match="private/loopback"):
            _ssrf_guard(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/x",  # public DNS — out of guard scope
            "http://8.8.8.8/x",  # public IPv4
            "https://example.org/x",  # https public
        ],
    )
    def test_passes_public_addresses(self, url: str) -> None:
        # Should not raise.
        _ssrf_guard(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/admin",
            "http://LOCALHOST/x",  # case-insensitive
            "http://metadata.google.internal/computeMetadata/v1",
            "http://metadata.amazonaws.com/x",
        ],
    )
    def test_blocks_named_aliases(self, url: str) -> None:
        with pytest.raises(FetchTransportError, match="denylisted"):
            _ssrf_guard(url)


# ---------------------------------------------------------------------------
# Umask-resistant file modes
# ---------------------------------------------------------------------------


class TestUmaskResistance:
    def test_exchange_id_is_0o644_under_restrictive_umask(self, tmp_path: Path) -> None:
        previous = os.umask(0o077)
        try:
            _resolve_exchange_id(tmp_path, explicit=None)
        finally:
            os.umask(previous)
        st = (tmp_path / ".exchange-id").stat()
        # Bottom 9 bits — only "user/group/other read+write/execute" matter.
        assert (st.st_mode & 0o777) == 0o644

    def test_namespace_dir_and_exchange_file_are_world_readable(
        self, tmp_path: Path
    ) -> None:
        previous = os.umask(0o077)
        try:
            (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
            fx = FileExchange(
                base_dir=tmp_path,
                exchange_id="g",
                namespace="image-mcp",
            )
            uri = fx.write_atomic(origin_id="abc", ext="png", content=b"x")
        finally:
            os.umask(previous)
        ns_st = (tmp_path / "image-mcp").stat()
        file_st = (tmp_path / "image-mcp" / uri.filename).stat()
        assert (ns_st.st_mode & 0o777) == 0o755
        assert (file_st.st_mode & 0o777) == 0o644


# ---------------------------------------------------------------------------
# ResolvedSource — frozen-dataclass validation
# ---------------------------------------------------------------------------


class TestResolvedSource:
    def test_negative_size_bytes_raises_value_error(self) -> None:
        # __post_init__ rejects a negative size_bytes — a byte length
        # can never be negative, so a negative value is a programmer bug
        # caught at construction.
        with pytest.raises(ValueError, match="non-negative"):
            ResolvedSource(stream=io.BytesIO(b""), size_bytes=-1)

    def test_zero_size_bytes_is_allowed(self) -> None:
        # Zero is a valid byte length (an empty file); only negative
        # values are rejected.
        resolved = ResolvedSource(stream=io.BytesIO(b""), size_bytes=0)
        assert resolved.size_bytes == 0


# ---------------------------------------------------------------------------
# _resolve_source — sync / async dispatch and bad-return guard
# ---------------------------------------------------------------------------


class TestResolveSource:
    async def test_sync_hook_resolved(self) -> None:
        resolved = await _resolve_source(_src(b"sync-bytes", "text/plain"), "id-1")
        assert isinstance(resolved, ResolvedSource)
        assert resolved.stream.read() == b"sync-bytes"
        assert resolved.content_type == "text/plain"

    async def test_async_hook_resolved(self) -> None:
        resolved = await _resolve_source(_async_src(b"async-bytes"), "id-1")
        assert isinstance(resolved, ResolvedSource)
        assert resolved.stream.read() == b"async-bytes"

    async def test_sync_hook_returning_awaitable_is_awaited(self) -> None:
        # A plain (non-coroutine) function that returns a coroutine —
        # unusual but supported: _resolve_source awaits the result.
        async def inner() -> ResolvedSource:
            return ResolvedSource(stream=io.BytesIO(b"awaited"))

        def hook(origin_id: str) -> Any:
            return inner()

        resolved = await _resolve_source(hook, "id-1")
        assert resolved.stream.read() == b"awaited"

    async def test_non_resolvedsource_return_raises_typeerror(self) -> None:
        def bad(origin_id: str) -> Any:
            return b"raw bytes, not a ResolvedSource"

        with pytest.raises(TypeError, match="must return a ResolvedSource"):
            await _resolve_source(bad, "id-1")

    async def test_value_error_propagates_unchanged(self) -> None:
        # A source hook rejecting an id raises ValueError; _resolve_source
        # lets it through untouched (callers translate to transfer_failed).
        with pytest.raises(ValueError, match="bad id"):
            await _resolve_source(_rejecting_src("bad id"), "id-1")


# ---------------------------------------------------------------------------
# _invoke_sink — sync / async dispatch and bad-return guard
# ---------------------------------------------------------------------------


class TestInvokeSink:
    def _ctx(self) -> SinkContext:
        return SinkContext(
            origin_id=None,
            mime_type=None,
            size_bytes=None,
            file_ref=None,
            params={},
            handle=None,
        )

    async def test_sync_sink_invoked(self) -> None:
        result = await _invoke_sink(_sink(), io.BytesIO(b"abc"), self._ctx())
        assert result == {"stored_at": "memory", "bytes_written": 3}

    async def test_async_sink_invoked(self) -> None:
        result = await _invoke_sink(_async_sink(), io.BytesIO(b"abcd"), self._ctx())
        assert result == {"stored_at": "memory", "bytes_written": 4}

    async def test_sync_sink_returning_awaitable_is_awaited(self) -> None:
        async def inner() -> Mapping[str, Any]:
            return {"ok": True}

        def hook(stream: BinaryIO, ctx: SinkContext) -> Any:
            return inner()

        result = await _invoke_sink(hook, io.BytesIO(b"x"), self._ctx())
        assert result == {"ok": True}

    async def test_non_mapping_return_raises_typeerror(self) -> None:
        def bad(stream: BinaryIO, ctx: SinkContext) -> Any:
            return "not a mapping"

        with pytest.raises(TypeError, match="must return a mapping"):
            await _invoke_sink(bad, io.BytesIO(b"x"), self._ctx())


# ---------------------------------------------------------------------------
# create_download_link — source-hook rejection and TTL clamping
# ---------------------------------------------------------------------------


def _new_producer_mcp(
    monkeypatch: pytest.MonkeyPatch,
    source: SourceHook,
    *,
    ttl_env: str | None = None,
) -> FastMCP:
    monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
    monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
    if ttl_env is not None:
        monkeypatch.setenv("TEST_FE_FILE_EXCHANGE_TTL", ttl_env)
    mcp = FastMCP("test-fe")
    register_file_exchange(
        mcp,
        namespace="image-mcp",
        env_prefix="TEST_FE",
        produces=("image/png",),
        source=source,
    )
    return mcp


async def _call_download_link(mcp: FastMCP, **kwargs: Any) -> dict[str, Any]:
    import json

    tool = await mcp.get_tool("create_download_link")
    assert tool is not None
    result = await tool.run(kwargs)
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    text_blocks = [b.text for b in result.content if hasattr(b, "text")]
    return json.loads(text_blocks[0]) if text_blocks else {}


class TestCreateDownloadLink:
    async def test_source_hook_value_error_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The source hook rejecting an id (unknown / expired / not
        # permitted) surfaces as a structured transfer_failed envelope —
        # this folds in the former "unknown/expired id" path (issue #78).
        mcp = _new_producer_mcp(monkeypatch, _rejecting_src("no such image"))

        out = await _call_download_link(mcp, origin_id="abc")
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert out["origin_server"] == "image-mcp"
        assert out["origin_id"] == "abc"
        assert "no such image" in out["message"]

    async def test_non_value_error_from_source_hook_reraised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-ValueError from the source hook is a server-side bug, not
        # a caller error — it is logged and re-raised, not swallowed.
        def boom(origin_id: str) -> ResolvedSource:
            raise RuntimeError("source hook crashed")

        mcp = _new_producer_mcp(monkeypatch, boom)
        tool = await mcp.get_tool("create_download_link")
        assert tool is not None
        with pytest.raises(RuntimeError, match="source hook crashed"):
            await tool.run({"origin_id": "abc"})

    async def test_sync_source_hook_mints_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _new_producer_mcp(monkeypatch, _src(b"PNG", "image/png"))
        out = await _call_download_link(mcp, origin_id="abc")
        assert out["url"].startswith("http://test.example/artifacts/")
        assert out["mime_type"] == "image/png"

    async def test_async_source_hook_mints_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The sync-vs-async dimension now lives in the source hook;
        # an async hook drives create_download_link just as a sync one does.
        mcp = _new_producer_mcp(monkeypatch, _async_src(b"PNG", "image/png"))
        out = await _call_download_link(mcp, origin_id="abc")
        assert out["url"].startswith("http://test.example/artifacts/")
        assert out["mime_type"] == "image/png"

    async def test_ttl_clamped_to_handle_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The download-URL TTL is clamped to the server's configured
        # maximum — a caller can never request a longer-lived URL than
        # the server is willing to retain. (Replaces the old publish-side
        # TTL-expiry test: pvl-core no longer holds producer bytes.)
        mcp = _new_producer_mcp(monkeypatch, _src(b"PNG", "image/png"), ttl_env="600")
        out = await _call_download_link(mcp, origin_id="abc", ttl_seconds=99999.0)
        assert out["ttl_seconds"] == 600.0

    async def test_ttl_default_used_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _new_producer_mcp(monkeypatch, _src(b"PNG", "image/png"), ttl_env="600")
        out = await _call_download_link(mcp, origin_id="abc")
        assert out["ttl_seconds"] == 600.0

    async def test_floor_violation_origin_id_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # v0.5: ``../escape`` is now an opaque reference, not a rejection.
        # The minimal-safety floor still rejects control characters.
        mcp = _new_producer_mcp(monkeypatch, _src(b"PNG", "image/png"))
        out = await _call_download_link(mcp, origin_id="bad\x00id")
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert "validation" in out["message"]

    async def test_opaque_origin_id_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A path-shaped origin_id reaches the source hook and yields a URL.
        mcp = _new_producer_mcp(monkeypatch, _src(b"PNG", "image/png"))
        out = await _call_download_link(mcp, origin_id="../escape")
        assert "error" not in out
        assert "url" in out


# ---------------------------------------------------------------------------
# make_file_ref — http-only producer path
# ---------------------------------------------------------------------------


class TestMakeFileRef:
    async def test_http_only_builds_ref_without_invoking_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # http-only (no MCP_EXCHANGE_DIR): make_file_ref just builds the
        # ref naming the download tool; the source hook is NOT invoked
        # until a client calls create_download_link.
        invoked = False

        def source(origin_id: str) -> ResolvedSource:
            nonlocal invoked
            invoked = True
            return ResolvedSource(stream=io.BytesIO(b"x"))

        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = FastMCP("test-fe")
        handle = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            source=source,
        )
        ref = await handle.make_file_ref("my-id", mime_type="image/png")
        assert ref.origin_server == "image-mcp"
        assert ref.origin_id == "my-id"
        assert ref.transfer["http"]["tool"] == "create_download_link"
        assert "exchange" not in ref.transfer
        assert invoked is False

    async def test_make_file_ref_accepts_dotted_origin_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # v0.5: a leading dot is opaque data — make_file_ref accepts it
        # and echoes it back; no segment-rule rejection.
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = FastMCP("test-fe")
        handle = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            source=_src(b"x", "image/png"),
        )
        ref = await handle.make_file_ref(".hidden", mime_type="image/png")
        assert ref.origin_id == ".hidden"

    async def test_make_file_ref_rejects_floor_violation_origin_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastmcp_pvl_core._file_exchange_protocol import ExchangeURIError

        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
        mcp = FastMCP("test-fe")
        handle = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            source=_src(b"x", "image/png"),
        )
        with pytest.raises(ExchangeURIError):
            await handle.make_file_ref("bad\x00id", mime_type="image/png")


# ---------------------------------------------------------------------------
# Concurrent sweep + write_atomic
# ---------------------------------------------------------------------------


class TestConcurrentSweepAndWrite:
    def test_sweep_does_not_delete_fresh_writes(self, tmp_path: Path) -> None:
        (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
        fx = FileExchange(
            base_dir=tmp_path,
            exchange_id="g",
            namespace="image-mcp",
            ttl_seconds=3600,  # so TTL never triggers
        )
        sweep_errors: list[BaseException] = []
        write_errors: list[BaseException] = []
        write_count = 0
        # Bound the writer so the test wall-clock is predictable. Sweep
        # iterations N << writer iterations M means a sweep call always
        # races at least one write. We don't need millions to verify
        # the dotfile-skip invariant — a few hundred is plenty.
        target_writes = 100

        def writer() -> None:
            nonlocal write_count
            try:
                for i in range(target_writes):
                    fx.write_atomic(
                        origin_id=f"id-{i}",
                        ext="bin",
                        content=b"x",
                    )
                    write_count += 1
            except BaseException as exc:
                write_errors.append(exc)

        def sweeper() -> None:
            try:
                for _ in range(20):
                    fx.sweep()
            except BaseException as exc:
                sweep_errors.append(exc)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=sweeper)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert not sweep_errors
        assert not write_errors
        assert write_count == target_writes
        # All written files survive (TTL never triggered).
        files = list((tmp_path / "image-mcp").iterdir())
        non_dotfiles = [f for f in files if not f.name.startswith(".")]
        assert len(non_dotfiles) == target_writes


# ---------------------------------------------------------------------------
# read_exchange_uri size cap
# ---------------------------------------------------------------------------


class TestSizeCap:
    """``read_exchange_uri`` needs a size cap to match the http path's
    256 MiB guard, otherwise a malicious or accidental large file in the
    exchange volume could OOM the consumer process.
    """

    def test_read_exchange_uri_rejects_oversize_file(self, tmp_path: Path) -> None:
        (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
        fx = FileExchange(base_dir=tmp_path, exchange_id="g", namespace="image-mcp")
        uri = fx.write_atomic(origin_id="big", ext="bin", content=b"x" * 1000)
        with pytest.raises(OSError, match="exceeds max_bytes"):
            fx.read_exchange_uri(str(uri), max_bytes=100)

    def test_read_exchange_uri_no_cap_by_default(self, tmp_path: Path) -> None:
        (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
        fx = FileExchange(base_dir=tmp_path, exchange_id="g", namespace="image-mcp")
        uri = fx.write_atomic(origin_id="big", ext="bin", content=b"x" * 1000)
        # Direct callers (without max_bytes) get the unguarded read —
        # only fetch_file passes the cap.
        assert len(fx.read_exchange_uri(str(uri))) == 1000


class TestStreamingWriteAtomic:
    """``write_atomic(content=Path)`` stream-copies in 64 KiB chunks
    instead of requiring the whole file in memory.
    """

    def test_path_source_writes_correct_bytes(self, tmp_path: Path) -> None:
        (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
        fx = FileExchange(base_dir=tmp_path, exchange_id="g", namespace="image-mcp")
        # Source file larger than the chunk size, to exercise the loop.
        src = tmp_path / "src.bin"
        body = b"".join(bytes((i,)) * 256 for i in range(256))  # 64 KiB exactly
        src.write_bytes(body * 3)  # 192 KiB → 3 chunks

        uri = fx.write_atomic(origin_id="big", ext="bin", content=src)
        assert uri.id == _exchange_segment("big")
        out = (tmp_path / "image-mcp" / uri.filename).read_bytes()
        assert out == body * 3

    def test_path_source_does_not_load_full_file_in_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch Path.read_bytes to raise; the streaming write must not
        # call it. This proves the streaming path goes through .open()
        # + .read(chunk) only.
        (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
        fx = FileExchange(base_dir=tmp_path, exchange_id="g", namespace="image-mcp")
        src = tmp_path / "src.bin"
        src.write_bytes(b"hello")

        original_read_bytes = Path.read_bytes

        def boom(self: Path) -> bytes:
            if self == src:
                raise AssertionError("read_bytes called on source — streaming bypassed")
            return original_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", boom)
        uri = fx.write_atomic(origin_id="abc", ext="bin", content=src)
        assert (tmp_path / "image-mcp" / uri.filename).read_bytes() == b"hello"


class TestHTTPClientReuse:
    """``FileExchangeHandle._get_http_client`` lazy-creates a single
    ``httpx.AsyncClient`` and reuses it across fetch calls so repeated
    transfers to the same producer host enjoy connection pooling and
    TLS-session resumption.
    """

    async def test_client_is_lazy_and_reused(self) -> None:
        h = FileExchangeHandle(
            namespace="x",
            enabled=True,
            produce=False,
            consume=True,
            artifact_store=None,
            exchange=None,
            capability=None,
        )
        # Lazy: nothing built yet.
        assert h._http_client is None

        c1 = h._get_http_client()
        c2 = h._get_http_client()
        assert c1 is c2  # reused
        assert h._http_client is c1

        await h.aclose()
        assert h._http_client is None

    async def test_aclose_idempotent(self) -> None:
        h = FileExchangeHandle(
            namespace="x",
            enabled=True,
            produce=False,
            consume=True,
            artifact_store=None,
            exchange=None,
            capability=None,
        )
        # No client created — aclose should be a no-op.
        await h.aclose()
        # Create one, close, then close again.
        h._get_http_client()
        await h.aclose()
        await h.aclose()


class TestDefensiveErrorPaths:
    """Locks in the try/except guards that absorb rare OSErrors instead
    of crashing the caller (background sweepers, producer tool handlers).
    """

    def test_sweep_iterdir_oserror_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
        fx = FileExchange(base_dir=tmp_path, exchange_id="g", namespace="image-mcp")
        # Make the namespace dir exist so sweep gets past the early return,
        # then have iterdir raise.
        (tmp_path / "image-mcp").mkdir()
        original_iterdir = Path.iterdir

        def boom(self: Path) -> Any:
            if self.name == "image-mcp":
                raise PermissionError("simulated permission flip")
            return original_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", boom)
        # Should NOT raise — sweep absorbs the OSError and returns 0.
        assert fx.sweep() == 0

    def test_write_atomic_cleans_up_tmp_on_rename_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
        fx = FileExchange(base_dir=tmp_path, exchange_id="g", namespace="image-mcp")

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("simulated rename failure")

        monkeypatch.setattr(os, "rename", boom)
        with pytest.raises(OSError, match="simulated"):
            fx.write_atomic(origin_id="abc", ext="png", content=b"x")
        # The dotfile temp must NOT be left orphaned on disk.
        ns_dir = tmp_path / "image-mcp"
        leftover = list(ns_dir.glob(".*.tmp"))
        assert leftover == []

    def test_resolve_exchange_id_unlinks_on_write_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force os.fdopen to raise *after* O_CREAT|O_EXCL succeeded so we
        # exercise the "open OK, write failed" cleanup path. Otherwise
        # the next init attempt would see an empty .exchange-id forever.
        original_fdopen = os.fdopen

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("simulated write failure")

        monkeypatch.setattr(os, "fdopen", boom)
        with pytest.raises(OSError, match="simulated"):
            _resolve_exchange_id(tmp_path, explicit=None)
        # The corrupt .exchange-id was removed; a follow-up init can succeed.
        assert not (tmp_path / ".exchange-id").exists()
        monkeypatch.setattr(os, "fdopen", original_fdopen)
        new_id = _resolve_exchange_id(tmp_path, explicit=None)
        assert new_id


class TestClientOrchestrationRequired:
    async def test_file_ref_with_only_http_returns_orchestration_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _new_consumer_mcp(monkeypatch, _sink())

        ref = FileRef(
            origin_server="image-mcp",
            origin_id="abc",
            transfer={"http": {"tool": "create_download_link"}},
        ).to_dict()
        out = await _call_fetch(mcp, file_ref=ref)
        assert out["error"] == "client_orchestration_required"
        assert out["http_tool"] == "create_download_link"
        assert out["origin_server"] == "image-mcp"
        assert out["origin_id"] == "abc"

    async def test_file_ref_with_no_dispatchable_methods(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _new_consumer_mcp(monkeypatch, _sink())

        # Future-method only — consumer can't attempt or orchestrate it.
        ref = FileRef(
            origin_server="image-mcp",
            origin_id="abc",
            transfer={"s3": {"key": "img.png"}},
        ).to_dict()
        out = await _call_fetch(mcp, file_ref=ref)
        assert out["error"] == "transfer_exhausted"
        assert out["attempted_methods"] == []
