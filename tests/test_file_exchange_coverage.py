"""Coverage-gap tests added in response to the PR #33 review.

Specifically targets:

- ``_consume_http`` (happy path, 4xx, oversize cap, redirect-not-followed)
  via ``httpx.MockTransport``.
- ``_ssrf_guard`` parametrised matrix (IPv6 loopback, link-local,
  RFC1918, multicast, plus a positive DNS-name pass-through).
- ``_filename_from_disposition`` parsing variants.
- Umask-resistant 0o644 / 0o755 file modes for ``.exchange-id``,
  namespace dirs, and exchange data files (cross-UID multi-container
  deployments).
- ``_resolve_lazy`` edge cases (sync returning awaitable, non-bytes).
- Expired-record path for ``create_download_link``.
- Concurrent ``sweep`` + ``write_atomic`` race.
- ``client_orchestration_required`` envelope when a file_ref offers
  only the ``http`` method.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    FetchContext,
    FetchResult,
    FileExchange,
    FileExchangeHandle,
    FileRef,
    register_file_exchange,
    set_artifact_store,
)
from fastmcp_pvl_core._file_exchange_runtime import _resolve_exchange_id
from fastmcp_pvl_core.file_exchange import (
    FetchTransportError,
    _filename_from_disposition,
    _resolve_lazy,
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
# _consume_http via httpx.MockTransport
# ---------------------------------------------------------------------------


async def _capture_sink(captured: dict[str, Any]) -> Any:
    async def sink(data: bytes, ctx: FetchContext) -> FetchResult:
        captured["data"] = data
        captured["mime_type"] = ctx.mime_type
        captured["suggested_filename"] = ctx.suggested_filename
        return FetchResult(
            stored_at="memory", bytes_written=len(data), extra={"ok": True}
        )

    return sink


def _new_consumer_mcp(monkeypatch: pytest.MonkeyPatch, sink: Any) -> Any:
    mcp = FastMCP("test-fe")
    register_file_exchange(
        mcp,
        namespace="vault-mcp",
        env_prefix="TEST_FE",
        consumer_sink=sink,
        transport="http",
    )
    return mcp


async def _call_fetch(mcp: Any, **kwargs: Any) -> dict[str, Any]:
    import json

    tool = await mcp.get_tool("fetch_file")
    result = await tool.run(kwargs)
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    text_blocks = [b.text for b in result.content if hasattr(b, "text")]
    return json.loads(text_blocks[0]) if text_blocks else {}


class TestConsumeHTTP:
    async def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        async def sink(data: bytes, ctx: FetchContext) -> FetchResult:
            captured["data"] = data
            captured["mime_type"] = ctx.mime_type
            captured["suggested_filename"] = ctx.suggested_filename
            return FetchResult(
                stored_at="vault://x", bytes_written=len(data), extra={"id": 7}
            )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"hello-bytes",
                headers={
                    "content-type": "image/png",
                    "content-disposition": 'attachment; filename="img.png"',
                },
            )

        # Patch httpx.AsyncClient at the module level so _consume_http
        # uses the mock transport.
        original = httpx.AsyncClient

        def mock_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)
        mcp = _new_consumer_mcp(monkeypatch, sink)

        out = await _call_fetch(mcp, url="https://example.com/img.png")
        assert out["method"] == "http"
        assert out["bytes_written"] == 11
        assert out["id"] == 7
        assert captured["data"] == b"hello-bytes"
        assert captured["mime_type"] == "image/png"
        assert captured["suggested_filename"] == "img.png"

    async def test_4xx_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def sink(data: bytes, ctx: FetchContext) -> FetchResult:
            return FetchResult(bytes_written=len(data))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"nope")

        original = httpx.AsyncClient

        def mock_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)
        mcp = _new_consumer_mcp(monkeypatch, sink)

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

        async def sink(data: bytes, ctx: FetchContext) -> FetchResult:
            return FetchResult(bytes_written=len(data))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 1000)

        original = httpx.AsyncClient

        def mock_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)
        mcp = _new_consumer_mcp(monkeypatch, sink)

        out = await _call_fetch(mcp, url="https://example.com/big")
        assert out["error"] == "transfer_failed"
        assert "exceeds" in out["message"]

    async def test_3xx_response_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-7 fix: a 3xx response with follow_redirects=False must
        # be treated as a failure, not silently consume the redirect
        # body as file content.
        async def sink(data: bytes, ctx: FetchContext) -> FetchResult:
            return FetchResult(bytes_written=len(data))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                301,
                content=b"<html>Moved Permanently</html>",
                headers={"location": "https://elsewhere.example/x"},
            )

        original = httpx.AsyncClient

        def mock_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)
        mcp = _new_consumer_mcp(monkeypatch, sink)

        out = await _call_fetch(mcp, url="https://example.com/redirect")
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert "redirect" in out["message"]

    async def test_redirect_not_followed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def sink(data: bytes, ctx: FetchContext) -> FetchResult:
            return FetchResult(bytes_written=len(data))

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

        original = httpx.AsyncClient

        def mock_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)
        mcp = _new_consumer_mcp(monkeypatch, sink)

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
# _filename_from_disposition
# ---------------------------------------------------------------------------


class TestFilenameFromDisposition:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (None, None),
            ("", None),
            ("attachment", None),
            ('attachment; filename="x.png"', "x.png"),
            ("inline; filename=bare.txt", "bare.txt"),
            ('attachment; foo=bar; filename="multi.bin"', "multi.bin"),
            ('attachment; FILENAME="upper.png"', "upper.png"),  # case-insensitive
            # Embedded semicolon inside quoted value — the old hand-rolled
            # parser truncated this; the stdlib parser handles it.
            ('attachment; filename="report;v1.csv"', "report;v1.csv"),
            # RFC 5987 extended form with UTF-8 charset.
            (
                "attachment; filename*=UTF-8''hello%20world.txt",
                "hello world.txt",
            ),
        ],
    )
    def test_parses_common_shapes(
        self, header: str | None, expected: str | None
    ) -> None:
        assert _filename_from_disposition(header) == expected


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
            fx.write_atomic(origin_id="abc", ext="png", content=b"x")
        finally:
            os.umask(previous)
        ns_st = (tmp_path / "image-mcp").stat()
        file_st = (tmp_path / "image-mcp" / "abc.png").stat()
        assert (ns_st.st_mode & 0o777) == 0o755
        assert (file_st.st_mode & 0o777) == 0o644


# ---------------------------------------------------------------------------
# _resolve_lazy edge cases
# ---------------------------------------------------------------------------


class TestResolveLazyEdgeCases:
    async def test_sync_returning_awaitable_is_awaited(self) -> None:
        async def inner() -> bytes:
            return b"awaited"

        # Sync callable returning a coroutine — unusual but supported.
        def lazy() -> Any:
            return inner()

        out = await _resolve_lazy(lazy)
        assert out == b"awaited"

    async def test_sync_returning_non_bytes_raises_typeerror(self) -> None:
        def lazy() -> Any:
            return 42

        with pytest.raises(TypeError, match="must return bytes"):
            await _resolve_lazy(lazy)

    async def test_async_returning_non_bytes_raises_typeerror(self) -> None:
        async def lazy() -> Any:
            return "not bytes"

        with pytest.raises(TypeError, match="must return bytes"):
            await _resolve_lazy(lazy)


# ---------------------------------------------------------------------------
# create_download_link — expired-record path
# ---------------------------------------------------------------------------


class TestCreateDownloadLinkExpiry:
    async def test_expired_origin_id_returns_transfer_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        mcp = FastMCP("test-fe")
        h = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            transport="http",
        )
        await h.publish(source=b"x", mime_type="image/png", origin_id="abc")
        # Backdate the publish-side expiry.
        h.publish_registry["abc"].expires_at = time.time() - 60

        import json

        tool = await mcp.get_tool("create_download_link")
        result = await tool.run({"origin_id": "abc"})
        sc = getattr(result, "structured_content", None)
        out = (
            sc
            if isinstance(sc, dict)
            else json.loads(next(b.text for b in result.content if hasattr(b, "text")))
        )
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert "abc" not in h.publish_registry  # swept out


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
# client_orchestration_required envelope
# ---------------------------------------------------------------------------


class TestSizeCap:
    """Round-5 finding: ``read_exchange_uri`` needs a size cap to match
    the http path's 256 MiB guard, otherwise a malicious or accidental
    large file in the exchange volume could OOM the consumer process.
    """

    def test_read_exchange_uri_rejects_oversize_file(self, tmp_path: Path) -> None:
        (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
        fx = FileExchange(base_dir=tmp_path, exchange_id="g", namespace="image-mcp")
        fx.write_atomic(origin_id="big", ext="bin", content=b"x" * 1000)
        with pytest.raises(OSError, match="exceeds max_bytes"):
            fx.read_exchange_uri("exchange://g/image-mcp/big.bin", max_bytes=100)

    def test_read_exchange_uri_no_cap_by_default(self, tmp_path: Path) -> None:
        (tmp_path / ".exchange-id").write_text("g\n", encoding="utf-8")
        fx = FileExchange(base_dir=tmp_path, exchange_id="g", namespace="image-mcp")
        fx.write_atomic(origin_id="big", ext="bin", content=b"x" * 1000)
        # Direct callers (without max_bytes) get the unguarded read —
        # only fetch_file passes the cap.
        assert len(fx.read_exchange_uri("exchange://g/image-mcp/big.bin")) == 1000


class TestExpiredRecordThrottleRegression:
    """Round-6 finding: the round-5 throttle made
    ``expire_publish_registry`` skip its sweep within 30 s of the last
    one, which meant an expired record could still be looked up and
    served. The fix is an O(1) per-record TTL check after the
    registry lookup; this test locks it in.
    """

    async def test_expired_record_refused_inside_throttle_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
        mcp = FastMCP("test-fe")
        h = register_file_exchange(
            mcp,
            namespace="image-mcp",
            env_prefix="TEST_FE",
            produces=("image/png",),
            transport="http",
        )
        await h.publish(source=b"x", mime_type="image/png", origin_id="abc")
        # Set _last_expiry_sweep to "just now" so the throttle skips the
        # bulk sweep, then backdate the record's individual expires_at.
        h._last_expiry_sweep = time.time()
        h.publish_registry["abc"].expires_at = time.time() - 60

        tool = await mcp.get_tool("create_download_link")
        result = await tool.run({"origin_id": "abc"})
        sc = getattr(result, "structured_content", None)
        out = (
            sc
            if isinstance(sc, dict)
            else json.loads(next(b.text for b in result.content if hasattr(b, "text")))
        )
        assert out["error"] == "transfer_failed"
        assert out["method"] == "http"
        assert "expired" in out["message"]


class TestExpiryThrottle:
    """Round-5 finding: ``expire_publish_registry`` runs on every
    create_download_link call; throttle to once per N seconds so the
    O(N) scan doesn't bottleneck high-throughput producers.
    """

    def test_throttle_skips_recent_sweeps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastmcp_pvl_core.file_exchange import _PublishRecord

        h = FileExchangeHandle(
            namespace="x",
            enabled=True,
            produce=True,
            consume=False,
            artifact_store=None,
            exchange=None,
            capability=None,
        )
        # Insert an already-expired record.
        h.publish_registry["a"] = _PublishRecord(
            mime_type="text/plain",
            ext="txt",
            filename="a.txt",
            eager_bytes=b"x",
            expires_at=time.time() - 60,
        )
        # First call sweeps (last_sweep is 0 → far past the threshold).
        assert h.expire_publish_registry() == 1
        # Re-insert and call again immediately — the throttle skips it.
        h.publish_registry["b"] = _PublishRecord(
            mime_type="text/plain",
            ext="txt",
            filename="b.txt",
            eager_bytes=b"x",
            expires_at=time.time() - 60,
        )
        assert h.expire_publish_registry() == 0
        assert "b" in h.publish_registry  # not swept

    def test_force_bypasses_throttle(self) -> None:
        from fastmcp_pvl_core.file_exchange import _PublishRecord

        h = FileExchangeHandle(
            namespace="x",
            enabled=True,
            produce=True,
            consume=False,
            artifact_store=None,
            exchange=None,
            capability=None,
        )
        h._last_expiry_sweep = time.time()  # very recent → throttled
        h.publish_registry["a"] = _PublishRecord(
            mime_type="text/plain",
            ext="txt",
            filename="a.txt",
            eager_bytes=b"x",
            expires_at=time.time() - 60,
        )
        assert h.expire_publish_registry(force=True) == 1


class TestDefensiveErrorPaths:
    """Locks in the round-3 / round-4 try/except guards that absorb rare
    OSErrors instead of crashing the caller (background sweepers,
    producer tool handlers).
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
        async def sink(data: bytes, ctx: FetchContext) -> FetchResult:
            return FetchResult(bytes_written=len(data))

        mcp = _new_consumer_mcp(monkeypatch, sink)

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
        async def sink(data: bytes, ctx: FetchContext) -> FetchResult:
            return FetchResult(bytes_written=len(data))

        mcp = _new_consumer_mcp(monkeypatch, sink)

        # Future-method only — consumer can't attempt or orchestrate it.
        ref = FileRef(
            origin_server="image-mcp",
            origin_id="abc",
            transfer={"s3": {"key": "img.png"}},
        ).to_dict()
        out = await _call_fetch(mcp, file_ref=ref)
        assert out["error"] == "transfer_exhausted"
        assert out["attempted_methods"] == []
