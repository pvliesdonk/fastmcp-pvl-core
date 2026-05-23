import hashlib
import io

import pytest

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange import _filesystem
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata, FilesystemSource


def test_hashing_reader_tracks_size_and_digest():
    payload = b"the quick brown fox" * 1000
    reader = _filesystem._HashingReader(io.BytesIO(payload))
    sink = io.BytesIO()
    # copyfileobj-style chunked drain
    while True:
        chunk = reader.read(64)
        if not chunk:
            break
        sink.write(chunk)
    assert sink.getvalue() == payload
    assert reader.size == len(payload)
    assert reader.hexdigest() == hashlib.sha256(payload).hexdigest()


def test_hashing_reader_read_all():
    payload = b"the quick brown fox" * 1000
    reader = _filesystem._HashingReader(io.BytesIO(payload))
    data = reader.read(-1)
    assert data == payload
    assert reader.size == len(payload)
    assert reader.hexdigest() == hashlib.sha256(payload).hexdigest()


class _DummySource:
    def __init__(self, payload: bytes, meta: ArtifactMetadata) -> None:
        self._payload = payload
        self._meta = meta
        self.closed = False

    async def open_artifact(self, key):
        stream = io.BytesIO(self._payload)
        original_close = stream.close

        def _close():
            self.closed = True
            original_close()

        stream.close = _close  # type: ignore[method-assign]
        return stream, self._meta


async def test_stage_writes_bytes_size_digest_and_mode(tmp_path):
    payload = b"staged-bytes"
    meta = ArtifactMetadata(name="f.bin", mimeType="application/octet-stream")
    source = _DummySource(payload, meta)
    target = tmp_path / "vol" / "art"
    target.parent.mkdir()

    size, digest, returned_meta = await _filesystem._stage(source, "k", target)

    assert target.read_bytes() == payload
    assert size == len(payload)
    assert digest == f"sha-256:{hashlib.sha256(payload).hexdigest()}"
    assert target.stat().st_mode & 0o777 == 0o664
    assert returned_meta is meta
    assert source.closed is True


async def test_provider_mint_builds_handle_with_computed_size_and_digest(tmp_path):
    payload = b"report-bytes"
    meta = ArtifactMetadata(
        id="rep-1", name="r.bin", mimeType="application/octet-stream"
    )
    source = _DummySource(payload, meta)
    volume_map = {"vol": tmp_path}

    handle = await _filesystem.filesystem_provider_mint(
        source, "k", volume="vol", volume_map=volume_map
    )

    assert handle.artifact.id == "rep-1"
    assert handle.artifact.name == "r.bin"
    assert handle.artifact.mimeType == "application/octet-stream"
    assert handle.artifact.size == len(payload)
    assert handle.artifact.digest == f"sha-256:{hashlib.sha256(payload).hexdigest()}"
    assert len(handle.sources) == 1
    descriptor = handle.sources[0]
    assert isinstance(descriptor, FilesystemSource)
    assert descriptor.uri.startswith("exchange://vol/")
    relpath = descriptor.uri.removeprefix("exchange://vol/")
    assert (tmp_path / relpath).read_bytes() == payload


async def test_provider_mint_unknown_volume_raises_configuration_error(tmp_path):
    source = _DummySource(b"x", ArtifactMetadata(name="x"))
    with pytest.raises(ConfigurationError):
        await _filesystem.filesystem_provider_mint(
            source, "k", volume="missing", volume_map={"vol": tmp_path}
        )


def test_open_confined_readonly_opens_regular_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    f = _filesystem._open_confined_readonly(p)
    try:
        assert f.read() == b"abc"
    finally:
        f.close()


def test_open_confined_readonly_rejects_symlink_final_component(tmp_path):
    real = tmp_path / "real.bin"
    real.write_bytes(b"secret")
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._open_confined_readonly(link)
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


def test_open_confined_readonly_rejects_directory(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._open_confined_readonly(d)
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


def test_verify_stream_accepts_matching_size_and_digest():
    payload = b"verify-me"
    artifact = ArtifactMetadata(
        size=len(payload),
        digest=f"sha-256:{hashlib.sha256(payload).hexdigest()}",
    )
    _filesystem._verify_stream(io.BytesIO(payload), artifact)  # no raise


def test_verify_stream_raises_size_mismatch():
    artifact = ArtifactMetadata(size=999)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._verify_stream(io.BytesIO(b"short"), artifact)
    assert ei.value.code is TransferErrorCode.SIZE_MISMATCH


def test_verify_stream_raises_digest_mismatch():
    artifact = ArtifactMetadata(digest="sha-256:" + "0" * 64)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._verify_stream(io.BytesIO(b"data"), artifact)
    assert ei.value.code is TransferErrorCode.DIGEST_MISMATCH


def test_verify_stream_unsupported_algorithm_is_digest_mismatch():
    artifact = ArtifactMetadata(digest="md5:" + "0" * 32)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._verify_stream(io.BytesIO(b"data"), artifact)
    assert ei.value.code is TransferErrorCode.DIGEST_MISMATCH


def test_verify_stream_accepts_uppercase_algorithm_label():
    payload = b"verify-me"
    artifact = ArtifactMetadata(
        size=len(payload),
        digest=f"SHA-256:{hashlib.sha256(payload).hexdigest()}",
    )
    _filesystem._verify_stream(io.BytesIO(payload), artifact)  # no raise


class _RecordingSink:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}
        self.called = False

    async def store_artifact(self, artifact_id, metadata, stream):
        self.called = True
        self.stored[artifact_id] = stream.read()


async def test_ingest_verifies_then_hands_verified_bytes_to_sink(tmp_path):
    payload = b"ingest-bytes"
    p = tmp_path / "src.bin"
    p.write_bytes(payload)
    artifact = ArtifactMetadata(
        id="a1",
        size=len(payload),
        digest=f"sha-256:{hashlib.sha256(payload).hexdigest()}",
    )
    sink = _RecordingSink()

    await _filesystem._ingest(p, artifact, sink, "a1")

    assert sink.stored["a1"] == payload


async def test_ingest_does_not_call_sink_on_digest_mismatch(tmp_path):
    p = tmp_path / "src.bin"
    p.write_bytes(b"tampered")
    artifact = ArtifactMetadata(id="a1", digest="sha-256:" + "0" * 64)
    sink = _RecordingSink()

    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem._ingest(p, artifact, sink, "a1")

    assert ei.value.code is TransferErrorCode.DIGEST_MISMATCH
    assert sink.called is False
