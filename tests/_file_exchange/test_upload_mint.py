"""Matrix row E1, E4: upload_receiver_mint builds an IntakeTicket."""

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints, UploadSink


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_mint_returns_intake_ticket_with_one_upload_sink():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="https://b.example",
        ttl=120.0,
    )
    assert ticket.type == TICKET_TYPE
    assert ticket.version == SPEC_VERSION
    assert ticket.artifactId == "art-1"
    assert ticket.expected is None
    assert len(ticket.sinks) == 1
    sink = ticket.sinks[0]
    assert isinstance(sink, UploadSink)
    assert sink.transport == "upload"
    assert sink.url.startswith("https://b.example/fx/u/")
    assert sink.method == "PUT"
    token = sink.url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata == {"artifact_id": "art-1", "expected": None}
    assert rec.single_use is True


async def test_mint_method_post_threads_through():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "art-2",
        token_store=store,
        base_url="https://b.example",
        ttl=120.0,
        method="POST",
    )
    assert ticket.sinks[0].method == "POST"


async def test_mint_expected_round_trips_onto_ticket_and_metadata():
    store = _store()
    expected = ArtifactConstraints(
        maxSize=1024, acceptMimeTypes=["application/json"], requireDigest=["sha-256"]
    )
    ticket = await _upload.upload_receiver_mint(
        "art-3",
        token_store=store,
        base_url="https://b.example",
        ttl=120.0,
        expected=expected,
    )
    assert ticket.expected == expected
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["artifact_id"] == "art-3"
    assert rec.metadata["expected"] == expected.model_dump()


async def test_repeated_mint_yields_distinct_tokens_with_same_artifact_id():
    """E1: minting twice for the same artifact_id yields two distinct
    tokens; both lookups round-trip to the same artifact_id metadata."""
    store = _store()
    t1 = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://b.example", ttl=120.0
    )
    t2 = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://b.example", ttl=120.0
    )
    tok1 = t1.sinks[0].url.rsplit("/", 1)[1]
    tok2 = t2.sinks[0].url.rsplit("/", 1)[1]
    assert tok1 != tok2
    rec1 = await store.lookup(tok1)
    rec2 = await store.lookup(tok2)
    assert rec1 is not None and rec2 is not None
    assert rec1.metadata["artifact_id"] == "art-1"
    assert rec2.metadata["artifact_id"] == "art-1"
