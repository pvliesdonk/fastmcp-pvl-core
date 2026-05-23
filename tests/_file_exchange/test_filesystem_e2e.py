"""End-to-end: two pvl-core-built mock servers exchange a file over the
filesystem transport only, sharing one volume.

Pull (provider->fetcher, both hooks) is a full round-trip; push
(receiver->sender) goes to deposit (receiver lazy-ingest is #144/#148,
out of scope).
"""

import hashlib
import io

from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core.file_exchange import (
    ArtifactMetadata,
    FilesystemSink,
    FilesystemSource,
    select_sink,
    select_source,
)


class _MockServer:
    """A server that can both produce (ArtifactSource) and store (ArtifactSink)."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.meta: dict[str, ArtifactMetadata] = {}
        self.received: dict[str | None, bytes] = {}

    async def open_artifact(self, key):
        return io.BytesIO(self.blobs[key]), self.meta[key]

    async def store_artifact(self, artifact_id, metadata, stream):
        self.received[artifact_id] = stream.read()


async def test_pull_round_trip(tmp_path):
    volume_map = {"shared": tmp_path}
    a, b = _MockServer(), _MockServer()
    payload = b"quarterly-report-bytes"
    a.blobs["rep"] = payload
    a.meta["rep"] = ArtifactMetadata(
        id="rep-1", name="rep.bin", mimeType="application/octet-stream"
    )

    handle = await file_exchange.filesystem_provider_mint(
        a, "rep", volume="shared", volume_map=volume_map
    )
    assert handle.artifact.digest == f"sha-256:{hashlib.sha256(payload).hexdigest()}"

    source = select_source(
        handle, is_accessible=file_exchange.filesystem_source_readable(volume_map)
    )
    assert isinstance(source, FilesystemSource)
    await file_exchange.filesystem_fetcher_consume(
        handle, source, b, volume_map=volume_map
    )

    assert b.received["rep-1"] == payload


async def test_push_to_deposit(tmp_path):
    volume_map = {"shared": tmp_path}
    a = _MockServer()
    payload = b"uploaded-dataset-bytes"
    a.blobs["ds"] = payload
    a.meta["ds"] = ArtifactMetadata(id="ds-1", name="ds.bin")

    ticket = file_exchange.filesystem_receiver_mint(
        "intake-9", volume="shared", volume_map=volume_map
    )

    sink = select_sink(
        ticket, is_accessible=file_exchange.filesystem_sink_writable(volume_map)
    )
    assert isinstance(sink, FilesystemSink)
    await file_exchange.filesystem_sender_consume(sink, a, "ds", volume_map=volume_map)

    relpath = sink.uri.removeprefix("exchange://shared/")
    deposit = tmp_path / relpath
    assert deposit.read_bytes() == payload
    assert deposit.stat().st_mode & 0o777 == 0o664
