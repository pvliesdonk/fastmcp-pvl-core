import hashlib
import os

import httpx
import pytest

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
                yield resp
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_download, "guarded_stream", fake_guarded_stream)


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
