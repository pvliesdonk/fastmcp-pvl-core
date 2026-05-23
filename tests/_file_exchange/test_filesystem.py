import hashlib
import io

import pytest

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange import _filesystem
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
