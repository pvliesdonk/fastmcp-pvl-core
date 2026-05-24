import asyncio
import contextlib
import hashlib
import io
import os

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _download
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata, DownloadSource


def _store():
    # In-memory KV-backed token store; memory:// avoids any filesystem backend.
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_provider_mint_builds_download_handle():
    store = _store()
    artifact = ArtifactMetadata(name="report.pdf", mimeType="application/pdf", size=11)
    handle = await _download.download_provider_mint(
        artifact,
        "doc-key-1",
        token_store=store,
        base_url="https://a.example",
        ttl=120.0,
        single_use=True,
    )
    assert handle.type == HANDLE_TYPE
    assert handle.version == SPEC_VERSION
    assert handle.artifact is artifact
    assert len(handle.sources) == 1
    src = handle.sources[0]
    assert isinstance(src, DownloadSource)
    assert src.transport == "download"
    assert src.url.startswith("https://a.example/fx/d/")
    assert src.singleUse is True
    # The token round-trips to the stored key.
    token = src.url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata == {"key": "doc-key-1"}
    assert rec.single_use is True


async def test_provider_mint_single_use_false_threads_through():
    store = _store()
    handle = await _download.download_provider_mint(
        ArtifactMetadata(name="x"),
        "k",
        token_store=store,
        base_url="https://a.example",
        ttl=60.0,
        single_use=False,
    )
    assert handle.sources[0].singleUse is False


def _cfg(*, max_size=None):
    return ServerConfig(file_exchange_max_artifact_size=max_size)


class _CapturingSink:
    """ArtifactSink that records the bytes it is given (proves verify-before-use:
    store_artifact is only called on a clean transfer)."""

    def __init__(self):
        self.deposited: bytes | None = None
        self.calls = 0

    async def store_artifact(self, artifact_id, metadata, stream):
        self.calls += 1
        self.deposited = stream.read()


def _handle(body: bytes, *, size=None, digest=None):
    from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata, TransferHandle

    return TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(
            name="a", size=len(body) if size is None else size, digest=digest
        ),
        sources=[
            DownloadSource(
                transport="download",
                url="https://prov.example/fx/d/tok",
                expiresAt="2099-01-01T00:00:00Z",
                singleUse=True,
            )
        ],
    )


class _FakeGuarded:
    """Mimic the real GuardedResponse surface (``status`` + ``aiter_bytes``) over
    a streamed httpx response, so fetcher tests exercise the same attributes the
    production ``guarded_stream`` yields."""

    def __init__(self, resp):
        self.status = resp.status_code
        self._resp = resp

    def aiter_bytes(self):
        return self._resp.aiter_bytes()


def _install_guard(monkeypatch, responder):
    """Patch _download.guarded_stream with a MockTransport-backed guarded_stream
    that does NOT resolve/pin (we are testing the fetcher, not the guard)."""
    import contextlib as _ctx

    @_ctx.asynccontextmanager
    async def fake_guarded_stream(
        method, url, *, config, transport, headers=None, content=None
    ):
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        try:
            req = client.build_request(method, url, headers=headers or {})
            resp = await client.send(req, stream=True)
            try:
                yield _FakeGuarded(resp)
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_download, "guarded_stream", fake_guarded_stream)


def _install_guard_seq(monkeypatch, responders):
    """Like _install_guard but uses a fresh responder per guarded_stream call, so
    a reconnect (with a Range header) hits the next responder. Records the headers
    each call received."""
    import contextlib as _ctx

    calls = {"seen_headers": []}

    @_ctx.asynccontextmanager
    async def fake(method, url, *, config, transport, headers=None, content=None):
        calls["seen_headers"].append(dict(headers or {}))
        responder = responders[min(len(calls["seen_headers"]) - 1, len(responders) - 1)]
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        try:
            req = client.build_request(method, url, headers=headers or {})
            resp = await client.send(req, stream=True)
            try:
                yield _FakeGuarded(resp)
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_download, "guarded_stream", fake)
    return calls


async def test_fetcher_happy_path_verifies_and_deposits(monkeypatch):
    body = b"hello-download-bytes"
    digest = "sha-256:" + hashlib.sha256(body).hexdigest()

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    await _download.download_fetcher_consume(
        _handle(body, digest=digest), _handle(body).sources[0], sink, config=_cfg()
    )
    assert sink.deposited == body
    assert sink.calls == 1


async def test_fetcher_digest_mismatch_does_not_call_sink(monkeypatch):
    body = b"actual-bytes"
    wrong = "sha-256:" + hashlib.sha256(b"different").hexdigest()

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body, digest=wrong), _handle(body).sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.DIGEST_MISMATCH
    assert sink.calls == 0  # verify-before-use


async def test_fetcher_size_mismatch(monkeypatch):
    body = b"twelve_bytes"

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body, size=999), _handle(body).sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.SIZE_MISMATCH
    assert sink.calls == 0


async def test_fetcher_too_large(monkeypatch):
    body = b"x" * 5000

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body), _handle(body).sources[0], sink, config=_cfg(max_size=1000)
        )
    assert ei.value.code == TransferErrorCode.TOO_LARGE


async def test_fetcher_guard_refusal_propagates(monkeypatch):
    import contextlib as _ctx

    @_ctx.asynccontextmanager
    async def refusing(method, url, *, config, transport, headers=None, content=None):
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE, transport="download", detail="refused"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(_download, "guarded_stream", refusing)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(b"x"), _handle(b"x").sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE
    assert sink.calls == 0


async def test_fetcher_cleans_up_temp_on_error(monkeypatch, tmp_path):
    # The temp file must be removed on a streaming-phase error path (regression:
    # the unlink used to sit in a second sequential try that such errors skipped).
    real_mkstemp = _download.tempfile.mkstemp
    created: list[str] = []

    def spy_mkstemp(*args, **kwargs):
        kwargs.setdefault("dir", str(tmp_path))
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(_download.tempfile, "mkstemp", spy_mkstemp)

    body = b"x" * 5000

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body), _handle(body).sources[0], sink, config=_cfg(max_size=1000)
        )
    assert ei.value.code == TransferErrorCode.TOO_LARGE
    assert created  # a temp file was created
    assert all(not os.path.exists(p) for p in created)  # ...and removed on error


class _DropAfter(httpx.AsyncByteStream):
    """Yield a prefix, then raise a mid-stream connection error."""

    def __init__(self, prefix: bytes):
        self._prefix = prefix

    async def __aiter__(self):
        yield self._prefix
        raise httpx.ReadError("connection dropped mid-stream")

    async def aclose(self):
        return


async def test_fetcher_resumes_with_range_after_drop(monkeypatch):
    body = b"0123456789abcdef" * 8  # 128 bytes
    digest = "sha-256:" + hashlib.sha256(body).hexdigest()
    split = 50

    def first(request):
        return httpx.Response(200, stream=_DropAfter(body[:split]))

    def rest(request):
        start = int(request.headers["range"][len("bytes=") :].split("-")[0])
        return httpx.Response(206, content=body[start:])

    calls = _install_guard_seq(monkeypatch, [first, rest])
    sink = _CapturingSink()
    await _download.download_fetcher_consume(
        _handle(body, digest=digest), _handle(body).sources[0], sink, config=_cfg()
    )
    assert sink.deposited == body  # hash/count continued across the reconnect
    assert calls["seen_headers"][0] == {}  # first attempt: no Range
    assert calls["seen_headers"][1] == {"Range": f"bytes={split}-"}  # resume


async def test_fetcher_resume_non_206_is_rejected(monkeypatch):
    body = b"abcd" * 32  # 128 bytes
    split = 40

    def first(request):
        return httpx.Response(200, stream=_DropAfter(body[:split]))

    def ignores_range(request):
        return httpx.Response(200, content=body)  # 200, not 206 -> must be rejected

    _install_guard_seq(monkeypatch, [first, ignores_range])
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body), _handle(body).sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
    assert sink.calls == 0


async def test_fetcher_gives_up_after_max_reconnects(monkeypatch):
    def first(request):
        return httpx.Response(200, stream=_DropAfter(b"a"))  # initial: 200

    def rest(request):
        # 206 so each resume passes the status check, then drops again.
        return httpx.Response(206, stream=_DropAfter(b"a"))

    calls = _install_guard_seq(monkeypatch, [first, rest])
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(b"a" * 100, size=100), _handle(b"x").sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
    # initial attempt + _MAX_RECONNECTS resume attempts
    assert len(calls["seen_headers"]) == _download._MAX_RECONNECTS + 1


class _BytesSource:
    """ArtifactSource serving fixed bytes for a single key.

    ``report_size=False`` models a hook that does not know the size (§7.1 makes
    it optional), exercising the route's unknown-size path.
    """

    def __init__(self, key, body, *, mime="application/octet-stream", report_size=True):
        self._key, self._body, self._mime = key, body, mime
        self._report_size = report_size
        self.opens = 0

    async def open_artifact(self, key):
        from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

        assert key == self._key
        self.opens += 1
        size = len(self._body) if self._report_size else None
        return io.BytesIO(self._body), ArtifactMetadata(
            name="a", mimeType=self._mime, size=size
        )


def _route_client(store, source):
    mcp = FastMCP("test")
    _download.register_file_exchange_routes(mcp, token_store=store, source=source)
    app = mcp.http_app()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://route.test"
    )


async def _mint_token(store, key):
    minted = await store.mint({"key": key}, ttl=300.0, single_use=True)
    return minted.token


async def test_route_unknown_token_404():
    store = _store()
    async with _route_client(store, _BytesSource("k", b"x")) as client:
        r = await client.get("/fx/d/nope")
    assert r.status_code == 404


async def test_route_serves_full_body_with_headers():
    store = _store()
    body = b"the-quick-brown-fox"
    source = _BytesSource("k1", body, mime="text/plain")
    token = await _mint_token(store, "k1")
    async with _route_client(store, source) as client:
        r = await client.get(f"/fx/d/{token}")
    assert r.status_code == 200
    assert r.content == body
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-length"] == str(len(body))
    assert r.headers["accept-ranges"] == "bytes"


async def test_route_range_returns_206_partial():
    store = _store()
    body = b"0123456789"
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=3-6"})
    assert r.status_code == 206
    assert r.content == b"3456"
    assert r.headers["content-range"] == "bytes 3-6/10"
    assert r.headers["content-length"] == "4"


async def test_route_unsatisfiable_range_416():
    store = _store()
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", b"0123456789")) as client:
        r = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=99-"})
    assert r.status_code == 416


async def test_route_full_retrieval_consumes_single_use_token():
    store = _store()
    body = b"consume-me"
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r1 = await client.get(f"/fx/d/{token}")
        assert r1.status_code == 200 and r1.content == body
        r2 = await client.get(f"/fx/d/{token}")
    assert r2.status_code == 404  # single-use token consumed after full retrieval


async def test_route_middle_range_does_not_consume():
    store = _store()
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", b"0123456789")) as client:
        r1 = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=2-5"})
        assert r1.status_code == 206
        r2 = await client.get(f"/fx/d/{token}")  # still valid
    assert r2.status_code == 200


async def test_route_ignores_ambient_credentials():
    store = _store()
    body = b"data"
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r = await client.get(
            f"/fx/d/{token}",
            headers={"Authorization": "Bearer x", "Cookie": "s=1"},
        )
    assert r.status_code == 200 and r.content == body


async def test_route_unknown_size_full_get_consumes():
    store = _store()
    body = b"unknown-size-body"
    token = await _mint_token(store, "k1")
    source = _BytesSource("k1", body, report_size=False)
    async with _route_client(store, source) as client:
        r1 = await client.get(f"/fx/d/{token}")
        assert r1.status_code == 200
        assert r1.content == body
        assert "content-length" not in r1.headers  # size unknown -> chunked
        r2 = await client.get(f"/fx/d/{token}")  # consumed on full retrieval
    assert r2.status_code == 404


async def test_route_unknown_size_ignores_range_serves_200():
    store = _store()
    body = b"0123456789"
    token = await _mint_token(store, "k1")
    source = _BytesSource("k1", body, report_size=False)
    async with _route_client(store, source) as client:
        # A 206 needs a Content-Range (impossible without total size), so the
        # route ignores Range and serves the whole body as 200.
        r = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=3-6"})
    assert r.status_code == 200
    assert r.content == body
    assert "content-range" not in r.headers


async def test_route_open_ended_range_206_and_consumes():
    store = _store()
    body = b"0123456789"
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=3-"})
        assert r.status_code == 206
        assert r.content == b"3456789"
        assert r.headers["content-range"] == "bytes 3-9/10"
        assert r.headers["content-length"] == "7"
        # Open-ended range streamed through EOF consumes the single-use token.
        r2 = await client.get(f"/fx/d/{token}")
    assert r2.status_code == 404


async def test_route_closed_range_end_clamped_to_size():
    store = _store()
    body = b"0123456789"  # size 10
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=3-99"})
    assert r.status_code == 206
    assert r.content == b"3456789"  # end clamped to size-1
    assert r.headers["content-range"] == "bytes 3-9/10"
    assert r.headers["content-length"] == "7"


async def test_route_suffix_range_returns_last_bytes():
    store = _store()
    body = b"0123456789"
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=-4"})
        assert r.status_code == 206
        assert r.content == b"6789"
        assert r.headers["content-range"] == "bytes 6-9/10"
        # Suffix range is closed (has a definite end) -> never consumes.
        r2 = await client.get(f"/fx/d/{token}")
    assert r2.status_code == 200


class _ShortStreamSource:
    """Reports a larger size than the stream actually yields — models a hook that
    breaks its 'stable bytes for the token lifetime' promise."""

    def __init__(self, key, body, reported_size):
        self._key, self._body, self._reported = key, body, reported_size

    async def open_artifact(self, key):
        from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

        assert key == self._key
        return io.BytesIO(self._body), ArtifactMetadata(name="a", size=self._reported)


async def test_route_source_shorter_than_range_start_does_not_consume(caplog):
    store = _store()
    token = await _mint_token(store, "k1")
    # Reports size 100 (so bytes=50- parses) but only yields 10 bytes.
    source = _ShortStreamSource("k1", b"0123456789", reported_size=100)
    async with _route_client(store, source) as client:
        # The under-length body trips httpx's content-length check; we only care
        # that the token was NOT consumed (the source ended before the start).
        with caplog.at_level("WARNING"), contextlib.suppress(httpx.RemoteProtocolError):
            await client.get(f"/fx/d/{token}", headers={"Range": "bytes=50-"})
    assert await store.lookup(token) is not None
    assert any("fewer bytes" in m for m in caplog.messages)  # misbehaving hook logged


async def test_route_yield_phase_truncation_does_not_consume():
    store = _store()
    token = await _mint_token(store, "k1")
    # Reports size 100 but the stream yields only 10 bytes: a full GET reaches
    # the yield loop, hits EOF early, and must NOT burn the single-use token so
    # the fetcher can retry.
    source = _ShortStreamSource("k1", b"0123456789", reported_size=100)
    async with _route_client(store, source) as client:
        with contextlib.suppress(httpx.RemoteProtocolError):
            await client.get(f"/fx/d/{token}")
    assert await store.lookup(token) is not None  # truncated delivery -> token kept


async def test_fetcher_initial_404_maps_not_accessible(monkeypatch):
    def responder(request):
        return httpx.Response(404, content=b"<html>not found</html>")

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(b"x"), _handle(b"x").sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE
    assert sink.calls == 0  # an error-page body is never ingested as artifact


async def test_fetcher_initial_500_maps_transfer_failed(monkeypatch):
    def responder(request):
        return httpx.Response(500, content=b"boom")

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(b"x"), _handle(b"x").sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
    assert sink.calls == 0


async def test_fetcher_unsupported_digest_label_fails(monkeypatch):
    body = b"some-bytes"

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body, digest="sha-1:" + "0" * 40),
            _handle(body).sources[0],
            sink,
            config=_cfg(),
        )
    assert ei.value.code == TransferErrorCode.DIGEST_MISMATCH  # cannot verify -> fail
    assert sink.calls == 0


async def test_fetcher_sink_oserror_maps_transfer_failed(monkeypatch):
    body = b"payload"

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)

    class _RaisingSink:
        async def store_artifact(self, artifact_id, metadata, stream):
            raise OSError("disk full")

    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body), _handle(body).sources[0], _RaisingSink(), config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED


async def test_fetcher_sink_transfer_error_propagates_unchanged(monkeypatch):
    body = b"payload"

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)

    class _CodedSink:
        async def store_artifact(self, artifact_id, metadata, stream):
            raise FileExchangeTransferError(
                TransferErrorCode.NOT_ACCESSIBLE, transport="download", detail="x"
            )

    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body), _handle(body).sources[0], _CodedSink(), config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE  # not re-wrapped


class _RaisingSource:
    async def open_artifact(self, key):
        raise FileNotFoundError("/srv/secret/vault/path")  # message must not leak


async def test_route_open_artifact_failure_returns_body_free_500():
    store = _store()
    token = await _mint_token(store, "k1")
    async with _route_client(store, _RaisingSource()) as client:
        r = await client.get(f"/fx/d/{token}")
    assert r.status_code == 500
    assert r.content == b""  # the hook's exception message is not echoed
    assert await store.lookup(token) is not None  # artifact unserved -> not consumed


async def test_route_consume_failure_after_delivery_is_logged(monkeypatch, caplog):
    store = _store()
    body = b"deliver-me-then-fail-consume"
    token = await _mint_token(store, "k1")

    async def _boom(_token):
        raise RuntimeError("kv backend down")

    monkeypatch.setattr(store, "consume", _boom)
    async with _route_client(store, _BytesSource("k1", body)) as client:
        with caplog.at_level("WARNING"):
            r = await client.get(f"/fx/d/{token}")
    assert r.status_code == 200
    assert r.content == body  # full body delivered despite the consume failure
    assert any("consume failed" in m for m in caplog.messages)


async def test_route_empty_artifact_full_get_200():
    store = _store()
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", b"")) as client:
        r = await client.get(f"/fx/d/{token}")
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["content-length"] == "0"


async def test_route_suffix_range_on_empty_artifact_416():
    store = _store()
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", b"")) as client:
        r = await client.get(f"/fx/d/{token}", headers={"Range": "bytes=-4"})
    assert r.status_code == 416


async def test_fetcher_empty_artifact(monkeypatch):
    body = b""
    digest = "sha-256:" + hashlib.sha256(body).hexdigest()

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    await _download.download_fetcher_consume(
        _handle(body, digest=digest), _handle(body).sources[0], sink, config=_cfg()
    )
    assert sink.deposited == b""
    assert sink.calls == 1


async def test_fetcher_digest_only_no_declared_size(monkeypatch):
    body = b"digest-only-bytes"
    digest = "sha-256:" + hashlib.sha256(body).hexdigest()
    from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata, TransferHandle

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(name="a", digest=digest),  # no size declared
        sources=[
            DownloadSource(
                transport="download",
                url="https://prov.example/fx/d/tok",
                expiresAt="2099-01-01T00:00:00Z",
                singleUse=True,
            )
        ],
    )

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    await _download.download_fetcher_consume(
        handle, handle.sources[0], sink, config=_cfg()
    )
    assert sink.deposited == body  # digest-only verification succeeds


@pytest.mark.parametrize(
    "header",
    ["items=0-1", "bytes=abc", "bytes=5-2", "bytes=-0", "bytes=", "bytes=-abc"],
)
def test_parse_range_malformed_raises(header):
    with pytest.raises(_download._UnsatisfiableError):
        _download._parse_range(header, 100)


async def test_route_multi_use_token_serves_repeatedly():
    store = _store()
    body = b"reusable-bytes"
    minted = await store.mint({"key": "k1"}, ttl=300.0, single_use=False)
    token = minted.token
    async with _route_client(store, _BytesSource("k1", body)) as client:
        r1 = await client.get(f"/fx/d/{token}")
        assert r1.status_code == 200 and r1.content == body
        r2 = await client.get(f"/fx/d/{token}")  # multi-use: still valid
        assert r2.status_code == 200 and r2.content == body
    assert await store.lookup(token) is not None  # never consumed


async def test_fetcher_size_ok_digest_wrong_raises_digest_mismatch(monkeypatch):
    body = b"some-payload"
    wrong_digest = "sha-256:" + hashlib.sha256(b"other").hexdigest()

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body, size=len(body), digest=wrong_digest),  # size ok, digest bad
            _handle(body).sources[0],
            sink,
            config=_cfg(),
        )
    assert (
        ei.value.code == TransferErrorCode.DIGEST_MISMATCH
    )  # digest checked after size
    assert sink.calls == 0


async def test_fetcher_oversize_body_aborts_early(monkeypatch):
    body = b"x" * 200

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body, size=50),  # declares 50, serves 200
            _handle(body).sources[0],
            sink,
            config=_cfg(),
        )
    assert ei.value.code == TransferErrorCode.SIZE_MISMATCH
    assert sink.calls == 0


async def test_fetcher_flush_error_maps_transfer_failed(monkeypatch):
    body = b"payload-bytes"

    def responder(request):
        return httpx.Response(200, content=body)

    _install_guard(monkeypatch, responder)

    real_fdopen = os.fdopen

    class _FlushFails:
        def __init__(self, f):
            self._f = f

        def write(self, b):
            return self._f.write(b)

        def flush(self):
            raise OSError("disk full on flush")

        def close(self):
            return self._f.close()

    monkeypatch.setattr(
        _download.os, "fdopen", lambda fd, mode: _FlushFails(real_fdopen(fd, mode))
    )
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body), _handle(body).sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
    assert sink.calls == 0  # flush failed before the sink was ever invoked


async def test_route_concurrent_single_use_both_served():
    store = _store()
    body = b"shared-single-use-bytes"
    token = await _mint_token(store, "k1")
    async with _route_client(store, _BytesSource("k1", body)) as client:
        # §10.2: opening MUST NOT invalidate (so Range resume works), so two
        # concurrent retrievals begun before either completes are both served;
        # "single-use" = invalidated after the first full retrieval.
        r1, r2 = await asyncio.gather(
            client.get(f"/fx/d/{token}"),
            client.get(f"/fx/d/{token}"),
        )
    assert r1.status_code == 200 and r1.content == body
    assert r2.status_code == 200 and r2.content == body
    assert await store.lookup(token) is None  # consumed after the retrievals


async def test_fetcher_local_write_error_not_retried(monkeypatch):
    body = b"payload-bytes"

    def responder(request):
        return httpx.Response(200, content=body)

    calls = _install_guard_seq(monkeypatch, [responder])

    def boom(tmp, hasher, chunk):
        raise OSError("disk full")

    monkeypatch.setattr(_download, "_write_chunk", boom)
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body), _handle(body).sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
    assert sink.calls == 0
    # A local write failure is not a resumable network drop: no reconnect attempt.
    assert len(calls["seen_headers"]) == 1


async def test_fetcher_resume_wrong_bytes_caught_by_digest(monkeypatch):
    body = b"0123456789abcdef" * 8  # 128 bytes
    digest = "sha-256:" + hashlib.sha256(body).hexdigest()
    split = 50

    def first(request):
        return httpx.Response(200, stream=_DropAfter(body[:split]))

    def rest(request):
        start = int(request.headers["range"][len("bytes=") :].split("-")[0])
        # Same length, wrong content -> size passes, digest must catch it.
        return httpx.Response(206, content=b"X" * (len(body) - start))

    _install_guard_seq(monkeypatch, [first, rest])
    sink = _CapturingSink()
    with pytest.raises(FileExchangeTransferError) as ei:
        await _download.download_fetcher_consume(
            _handle(body, digest=digest), _handle(body).sources[0], sink, config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.DIGEST_MISMATCH
    assert sink.calls == 0
