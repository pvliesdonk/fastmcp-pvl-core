import hashlib
import io
import os

import pytest

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange import _filesystem
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    ArtifactMetadata,
    FilesystemSink,
    FilesystemSource,
    TransferHandle,
)


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
        self.stored: dict[str | None, bytes] = {}
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


async def test_ingest_passes_none_artifact_id_to_sink(tmp_path):
    payload = b"id-none"
    p = tmp_path / "src.bin"
    p.write_bytes(payload)
    artifact = ArtifactMetadata(
        size=len(payload),
        digest=f"sha-256:{hashlib.sha256(payload).hexdigest()}",
    )
    sink = _RecordingSink()
    await _filesystem._ingest(p, artifact, sink, None)
    assert sink.stored[None] == payload


async def test_ingest_does_not_call_sink_on_digest_mismatch(tmp_path):
    p = tmp_path / "src.bin"
    p.write_bytes(b"tampered")
    artifact = ArtifactMetadata(id="a1", digest="sha-256:" + "0" * 64)
    sink = _RecordingSink()

    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem._ingest(p, artifact, sink, "a1")

    assert ei.value.code is TransferErrorCode.DIGEST_MISMATCH
    assert sink.called is False


async def test_fetcher_consume_resolves_verifies_and_deposits(tmp_path):
    payload = b"fetch-bytes"
    (tmp_path / "art").write_bytes(payload)
    artifact = ArtifactMetadata(
        id="a1",
        size=len(payload),
        digest=f"sha-256:{hashlib.sha256(payload).hexdigest()}",
    )
    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=artifact,
        sources=[FilesystemSource(transport="filesystem", uri="exchange://vol/art")],
    )
    source = handle.sources[0]
    assert isinstance(source, FilesystemSource)
    sink = _RecordingSink()

    await _filesystem.filesystem_fetcher_consume(
        handle, source, sink, volume_map={"vol": tmp_path}
    )

    assert sink.stored["a1"] == payload


async def test_fetcher_consume_unresolvable_uri_is_not_accessible(tmp_path):
    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(id="a1", name="x"),
        sources=[FilesystemSource(transport="filesystem", uri="exchange://other/art")],
    )
    source = handle.sources[0]
    assert isinstance(source, FilesystemSource)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_fetcher_consume(
            handle, source, _RecordingSink(), volume_map={"vol": tmp_path}
        )
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


async def test_fetcher_consume_sink_failure_is_transfer_failed(tmp_path):
    (tmp_path / "art").write_bytes(b"x")

    class _BoomSink:
        async def store_artifact(self, artifact_id, metadata, stream):
            raise RuntimeError("downstream storage exploded")

    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(id="a1", name="x"),
        sources=[FilesystemSource(transport="filesystem", uri="exchange://vol/art")],
    )
    source = handle.sources[0]
    assert isinstance(source, FilesystemSource)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_fetcher_consume(
            handle, source, _BoomSink(), volume_map={"vol": tmp_path}
        )
    assert ei.value.code is TransferErrorCode.TRANSFER_FAILED
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_receiver_mint_builds_ticket_with_filesystem_sink(tmp_path):
    ticket = _filesystem.filesystem_receiver_mint(
        "intake-1",
        volume="vol",
        volume_map={"vol": tmp_path},
        expected=ArtifactConstraints(maxSize=1024),
    )

    assert ticket.artifactId == "intake-1"
    assert ticket.expected is not None and ticket.expected.maxSize == 1024
    assert len(ticket.sinks) == 1
    sink = ticket.sinks[0]
    assert isinstance(sink, FilesystemSink)
    assert sink.uri.startswith("exchange://vol/")
    assert "intake-1" not in sink.uri


def test_receiver_mint_unknown_volume_raises_configuration_error(tmp_path):
    with pytest.raises(ConfigurationError):
        _filesystem.filesystem_receiver_mint(
            "intake-1", volume="missing", volume_map={"vol": tmp_path}
        )


async def test_sender_consume_writes_deposit_atomically_at_0664(tmp_path):
    payload = b"pushed-bytes"
    source = _DummySource(payload, ArtifactMetadata(name="p.bin"))
    sink_desc = FilesystemSink(transport="filesystem", uri="exchange://vol/deposit")

    await _filesystem.filesystem_sender_consume(
        sink_desc, source, "k", volume_map={"vol": tmp_path}
    )

    deposit = tmp_path / "deposit"
    assert deposit.read_bytes() == payload
    assert deposit.stat().st_mode & 0o777 == 0o664
    assert source.closed is True


async def test_sender_consume_unresolvable_uri_is_not_accessible(tmp_path):
    sink_desc = FilesystemSink(transport="filesystem", uri="exchange://other/deposit")
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_sender_consume(
            sink_desc,
            _DummySource(b"x", ArtifactMetadata(name="x")),
            "k",
            volume_map={"vol": tmp_path},
        )
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


async def test_sender_consume_source_failure_is_transfer_failed(tmp_path):
    class _BoomSource:
        async def open_artifact(self, key):
            raise RuntimeError("cannot read domain artifact")

    sink_desc = FilesystemSink(transport="filesystem", uri="exchange://vol/deposit")
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_sender_consume(
            sink_desc, _BoomSource(), "k", volume_map={"vol": tmp_path}
        )
    assert ei.value.code is TransferErrorCode.TRANSFER_FAILED
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_source_readable_predicate(tmp_path):
    (tmp_path / "art").write_bytes(b"x")
    is_readable = _filesystem.filesystem_source_readable({"vol": tmp_path})
    assert (
        is_readable(FilesystemSource(transport="filesystem", uri="exchange://vol/art"))
        is True
    )
    assert (
        is_readable(
            FilesystemSource(transport="filesystem", uri="exchange://vol/missing")
        )
        is False
    )
    assert (
        is_readable(
            FilesystemSource(transport="filesystem", uri="exchange://other/art")
        )
        is False
    )


def test_sink_writable_predicate(tmp_path):
    is_writable = _filesystem.filesystem_sink_writable({"vol": tmp_path})
    # deposit file need not exist; its parent (the volume root) is writable
    assert (
        is_writable(
            FilesystemSink(transport="filesystem", uri="exchange://vol/deposit")
        )
        is True
    )
    assert (
        is_writable(
            FilesystemSink(transport="filesystem", uri="exchange://other/deposit")
        )
        is False
    )
    # volume maps to a path that doesn't exist as a directory -> parent check fails
    is_writable_bad = _filesystem.filesystem_sink_writable(
        {"vol": tmp_path / "nonexistent"}
    )
    assert (
        is_writable_bad(
            FilesystemSink(transport="filesystem", uri="exchange://vol/deposit")
        )
        is False
    )


def test_open_confined_readonly_closes_fd_on_nonregular_rejection(
    tmp_path, monkeypatch
):
    import os as _os

    d = tmp_path / "adir"
    d.mkdir()
    captured: list[int] = []
    real_open = _os.open

    def _spy_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        captured.append(fd)
        return fd

    monkeypatch.setattr(_filesystem.os, "open", _spy_open)
    with pytest.raises(_filesystem.FileExchangeTransferError):
        _filesystem._open_confined_readonly(d)
    assert captured, "os.open was not called"
    with pytest.raises(OSError):
        os.fstat(captured[0])  # fd must be closed -> EBADF


def test_open_confined_readonly_rejects_fifo(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        _filesystem._open_confined_readonly(fifo)
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


async def test_fetcher_consume_size_mismatch_surfaces_size_code(tmp_path):
    payload = b"fetch-bytes"
    (tmp_path / "art").write_bytes(payload)
    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(id="a1", size=len(payload) + 1),
        sources=[FilesystemSource(transport="filesystem", uri="exchange://vol/art")],
    )
    source = handle.sources[0]
    assert isinstance(source, FilesystemSource)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_fetcher_consume(
            handle, source, _RecordingSink(), volume_map={"vol": tmp_path}
        )
    assert ei.value.code is TransferErrorCode.SIZE_MISMATCH


async def test_fetcher_consume_digest_mismatch_surfaces_digest_code(tmp_path):
    payload = b"fetch-bytes"
    (tmp_path / "art").write_bytes(payload)
    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(id="a1", digest="sha-256:" + "0" * 64),
        sources=[FilesystemSource(transport="filesystem", uri="exchange://vol/art")],
    )
    source = handle.sources[0]
    assert isinstance(source, FilesystemSource)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_fetcher_consume(
            handle, source, _RecordingSink(), volume_map={"vol": tmp_path}
        )
    assert ei.value.code is TransferErrorCode.DIGEST_MISMATCH


async def test_fetcher_consume_nonregular_source_surfaces_not_accessible(tmp_path):
    os.mkfifo(tmp_path / "art")
    handle = TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=ArtifactMetadata(id="a1", name="x"),
        sources=[FilesystemSource(transport="filesystem", uri="exchange://vol/art")],
    )
    source = handle.sources[0]
    assert isinstance(source, FilesystemSource)
    with pytest.raises(_filesystem.FileExchangeTransferError) as ei:
        await _filesystem.filesystem_fetcher_consume(
            handle, source, _RecordingSink(), volume_map={"vol": tmp_path}
        )
    assert ei.value.code is TransferErrorCode.NOT_ACCESSIBLE


def test_open_confined_readonly_closes_fd_when_fdopen_fails(tmp_path, monkeypatch):
    import os as _os

    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    captured: list[int] = []
    real_open = _os.open

    def _spy_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        captured.append(fd)
        return fd

    def _boom_fdopen(*args, **kwargs):
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(_filesystem.os, "open", _spy_open)
    monkeypatch.setattr(_filesystem.os, "fdopen", _boom_fdopen)
    with pytest.raises(OSError):
        _filesystem._open_confined_readonly(p)
    assert captured, "os.open was not called"
    with pytest.raises(OSError):
        os.fstat(captured[0])  # fd must be closed -> EBADF
