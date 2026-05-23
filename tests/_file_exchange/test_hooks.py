"""Tests for the mechanism-agnostic artifact source/sink hook protocols."""

from __future__ import annotations

from io import BytesIO

from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _DummySource:
    async def open_artifact(self, key):  # noqa: ANN001, ANN202
        return BytesIO(b"hello"), ArtifactMetadata(name="a.bin")


class _DummySink:
    def __init__(self):
        self.stored: bytes | None = None

    async def store_artifact(self, artifact_id, metadata, stream):  # noqa: ANN001, ANN202
        self.stored = stream.read()


def test_source_runtime_checkable_matches_conforming():
    assert isinstance(_DummySource(), ArtifactSource)


def test_sink_runtime_checkable_matches_conforming():
    assert isinstance(_DummySink(), ArtifactSink)


def test_runtime_checkable_rejects_nonconforming():
    assert not isinstance(object(), ArtifactSource)
    assert not isinstance(object(), ArtifactSink)


async def test_source_returns_stream_and_metadata():
    stream, meta = await _DummySource().open_artifact("k")
    assert stream.read() == b"hello"
    assert meta.name == "a.bin"


async def test_sink_reads_stream_and_returns_none():
    sink = _DummySink()
    result = await sink.store_artifact(
        "id-1", ArtifactMetadata(name="x"), BytesIO(b"payload")
    )
    assert result is None
    assert sink.stored == b"payload"
