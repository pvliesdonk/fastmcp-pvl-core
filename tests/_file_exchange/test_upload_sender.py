"""Matrix rows B2, B3, B5, C3, D1, D2, D3, D9, F6: upload_sender_consume."""

import base64
import contextlib
import hashlib
import io
import os
from datetime import datetime, timezone

import pytest

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactMetadata,
    UploadSink,
)


def _sink() -> UploadSink:
    return UploadSink(
        transport="upload",
        url="https://b.example/fx/u/tok",
        method="PUT",
        expiresAt=datetime.now(timezone.utc),
    )


def _cfg():
    return ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_http_timeout=30.0,
    )


class _StreamSource:
    """ArtifactSource that yields a fixed payload."""

    def __init__(self, payload: bytes, mime: str | None = "text/plain"):
        self.payload = payload
        self.mime = mime

    async def open_artifact(self, key):
        return io.BytesIO(self.payload), ArtifactMetadata(mimeType=self.mime)


def _fake_guarded_stream(captured: dict, *, status: int = 204):
    """Return a patch for guarded_stream that captures request and returns status."""

    @contextlib.asynccontextmanager
    async def fake(method, url, *, config, transport, headers=None, content=None):
        captured["method"] = method
        captured["url"] = url
        captured["transport"] = transport
        captured["headers"] = dict(headers or {})
        body = b""
        if content is not None:
            async for chunk in content:
                body += chunk
        captured["body"] = body

        class _Resp:
            def __init__(self, st: int):
                self.status = st

        yield _Resp(status)

    return fake


async def test_sender_happy_path_puts_with_headers(monkeypatch, tmp_path):
    """F6 + D1 success arm."""
    captured: dict = {}
    monkeypatch.setattr(_upload, "guarded_stream", _fake_guarded_stream(captured))
    # Sandbox temp creation into tmp_path so we can assert no leftovers.
    monkeypatch.setattr(_upload.tempfile, "tempdir", str(tmp_path))

    await _upload.upload_sender_consume(
        _sink(),
        _StreamSource(b"hello world"),
        "art-1",
        config=_cfg(),
    )
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://b.example/fx/u/tok"
    assert captured["transport"] == "upload"
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["content-type"] == "text/plain"
    assert headers["content-length"] == str(len(b"hello world"))
    raw = hashlib.sha256(b"hello world").digest()
    expected_cd = "sha-256=:" + base64.b64encode(raw).decode("ascii") + ":"
    assert headers["content-digest"] == expected_cd
    assert captured["body"] == b"hello world"
    # No stray fx-upload temp files in our sandbox
    assert not any(n.startswith("fx-upload-") for n in os.listdir(str(tmp_path)))


async def test_sender_non_2xx_maps_to_transfer_failed(monkeypatch, tmp_path):
    """D1."""
    captured: dict = {}
    monkeypatch.setattr(
        _upload, "guarded_stream", _fake_guarded_stream(captured, status=500)
    )
    monkeypatch.setattr(_upload.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(
            _sink(), _StreamSource(b"x"), "art-1", config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
    assert ei.value.transport == "upload"


async def test_sender_guard_refusal_propagates(monkeypatch, tmp_path):
    """C3."""

    @contextlib.asynccontextmanager
    async def refusing(method, url, **kw):
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE, transport="upload", detail="blocked"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(_upload, "guarded_stream", refusing)
    monkeypatch.setattr(_upload.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(
            _sink(), _StreamSource(b"x"), "art-1", config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_sender_source_raises_propagates_no_temp_leak(monkeypatch, tmp_path):
    """D2."""

    class _Boom:
        async def open_artifact(self, key):
            raise RuntimeError("hook failure")

    monkeypatch.setattr(_upload.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(RuntimeError):
        await _upload.upload_sender_consume(_sink(), _Boom(), "art-1", config=_cfg())
    assert not any(n.startswith("fx-upload-") for n in os.listdir(str(tmp_path)))


async def test_sender_mkstemp_failure_maps_to_transfer_failed(monkeypatch):
    """D3."""

    def boom(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(_upload.tempfile, "mkstemp", boom)
    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(
            _sink(), _StreamSource(b"x"), "art-1", config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED


async def test_sender_closes_source_stream(monkeypatch, tmp_path):
    """B5: source stream closed on success."""

    class _TrackedBytes:
        def __init__(self, data: bytes):
            self._buf = bytearray(data)
            self.closed = False

        def read(self, n=-1):
            if n is None or n < 0:
                data, self._buf = bytes(self._buf), bytearray()
                return data
            data = bytes(self._buf[:n])
            del self._buf[:n]
            return data

        def close(self):
            self.closed = True

    tracked = _TrackedBytes(b"hello")

    class _S:
        async def open_artifact(self, key):
            return tracked, ArtifactMetadata(mimeType="text/plain")

    captured: dict = {}
    monkeypatch.setattr(_upload, "guarded_stream", _fake_guarded_stream(captured))
    monkeypatch.setattr(_upload.tempfile, "tempdir", str(tmp_path))
    await _upload.upload_sender_consume(_sink(), _S(), "art-1", config=_cfg())
    assert tracked.closed is True


async def test_sender_body_iter_oserror_maps_to_transfer_failed(monkeypatch, tmp_path):
    """D9: OSError while iterating the staged body -> TRANSFER_FAILED."""
    import builtins

    monkeypatch.setattr(_upload.tempfile, "tempdir", str(tmp_path))

    # Stub guarded_stream so the response is 204 if we ever get there (we won't).
    captured: dict = {}
    monkeypatch.setattr(_upload, "guarded_stream", _fake_guarded_stream(captured))

    real_open = builtins.open

    class _BadFile:
        def __init__(self, inner):
            self._inner = inner

        def read(self, n=-1):
            raise OSError("simulated read failure")

        def close(self):
            self._inner.close()

    def wrap(path, mode="r", *a, **kw):
        f = real_open(path, mode, *a, **kw)
        if isinstance(path, str) and "fx-upload-" in path and "rb" in mode:
            return _BadFile(f)
        return f

    monkeypatch.setattr(builtins, "open", wrap)
    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(
            _sink(), _StreamSource(b"hello world"), "art-1", config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
