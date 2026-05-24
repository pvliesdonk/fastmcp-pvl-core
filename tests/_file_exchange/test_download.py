from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _download
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
