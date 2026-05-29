"""End-to-end: two pvl-core-built servers using the #148 umbrella helpers."""

from __future__ import annotations

import contextlib
import hashlib
import io
from typing import BinaryIO

import httpx
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _download, _helpers, _upload
from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints, ArtifactMetadata


class _InMemSource:
    def __init__(self, payload: bytes, mime: str = "application/pdf") -> None:
        self.payload = payload
        self.mime = mime

    async def open_artifact(self, key: str):
        return io.BytesIO(self.payload), ArtifactMetadata(mimeType=self.mime)


class _InMemSink:
    def __init__(self) -> None:
        self.received: tuple[str | None, ArtifactMetadata, bytes] | None = None

    async def store_artifact(
        self, artifact_id: str | None, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        self.received = (artifact_id, metadata, stream.read())


def _cfg() -> ServerConfig:
    return ServerConfig(
        kv_store_url="memory://",
        file_exchange_token_ttl=3600.0,
        file_exchange_max_artifact_size=1024 * 1024,
        file_exchange_http_timeout=30.0,
    )


class _FakeGuarded:
    """Mirror the production GuardedResponse surface (status + aiter_bytes)."""

    def __init__(self, resp: httpx.Response) -> None:
        self.status = resp.status_code
        self._resp = resp

    def aiter_bytes(self):
        return self._resp.aiter_bytes()


async def test_e2e_provider_to_fetcher_via_umbrella(monkeypatch):
    """A registers a provider tool offering bytes; B registers a fetcher.
    The TransferHandle flows A->B and B pulls A's bytes via patched
    guarded_stream into B's sink.
    """
    payload = b"Provider over umbrella helpers PDF body"

    # Server A — offers reports.
    mcp_a = FastMCP("A")
    fxctx_a = _helpers.register_file_exchange(
        mcp_a,
        config=_cfg(),
        base_url="https://a.test",
        source=_InMemSource(payload),
    )

    digest = "sha-256:" + hashlib.sha256(payload).hexdigest()

    @_helpers.register_file_exchange_provider(mcp_a, "get_report", fxctx_a)
    async def get_report(report_id: str) -> tuple[ArtifactMetadata, str]:
        return (
            ArtifactMetadata(
                size=len(payload), mimeType="application/pdf", digest=digest
            ),
            report_id,
        )

    # Server B — fetches.
    mcp_b = FastMCP("B")
    sink_b = _InMemSink()
    fxctx_b = _helpers.register_file_exchange(
        mcp_b,
        config=_cfg(),
        base_url="https://b.test",
        sink=sink_b,
    )
    _helpers.register_file_exchange_fetcher(mcp_b, "consume_transfer", fxctx_b)

    # Patch B's outbound guarded_stream so its HTTP GET lands on A's ASGI app.
    app_a = mcp_a.http_app()

    @contextlib.asynccontextmanager
    async def fake_gs(method, url, *, config, transport, headers=None, content=None):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        )
        try:
            path = url.split("a.test", 1)[1]
            req = client.build_request(method, path, headers=headers or {})
            resp = await client.send(req, stream=True)
            try:
                yield _FakeGuarded(resp)
            finally:
                await resp.aclose()
        finally:
            await client.aclose()

    monkeypatch.setattr(_download, "guarded_stream", fake_gs)

    # Run the flow.
    provider_tool = await mcp_a.get_tool("get_report")
    handle = await provider_tool.fn(report_id="rpt-1")

    fetcher_tool = await mcp_b.get_tool("consume_transfer")
    await fetcher_tool.fn(handle=handle)

    assert sink_b.received is not None
    aid, meta, body = sink_b.received
    assert body == payload
    assert meta.size == len(payload)
    assert meta.digest == digest


async def test_e2e_receiver_to_sender_via_umbrella(monkeypatch):
    """B registers a receiver tool minting an IntakeTicket; A registers a
    sender. A pushes its bytes to B via patched guarded_stream onto B's
    ASGI app.
    """
    payload = b'{"hello":"umbrella"}'

    # Server B — accepts uploads.
    mcp_b = FastMCP("B")
    sink_b = _InMemSink()
    fxctx_b = _helpers.register_file_exchange(
        mcp_b,
        config=_cfg(),
        base_url="https://b.test",
        sink=sink_b,
    )

    @_helpers.register_file_exchange_receiver(mcp_b, "accept_doc", fxctx_b)
    async def accept_doc(case_id: str) -> tuple[str, ArtifactConstraints | None]:
        return f"case-{case_id}-doc", None

    # Server A — sends.
    mcp_a = FastMCP("A")
    fxctx_a = _helpers.register_file_exchange(
        mcp_a,
        config=_cfg(),
        base_url="https://a.test",
        source=_InMemSource(payload, mime="application/json"),
    )
    _helpers.register_file_exchange_sender(mcp_a, "send_to_receiver", fxctx_a)

    # Patch A's outbound guard to land on B's ASGI app.
    transport_b = httpx.ASGITransport(app=mcp_b.http_app())
    client_b = httpx.AsyncClient(transport=transport_b, base_url="https://b.test")

    @contextlib.asynccontextmanager
    async def fake_gs(method, url, *, config, transport, headers=None, content=None):
        body = b""
        if content is not None:
            async for chunk in content:
                body += chunk
        resp = await client_b.request(method, url, headers=headers, content=body)

        class R:
            status = resp.status_code

        yield R()

    monkeypatch.setattr(_upload, "guarded_stream", fake_gs)

    # Run.
    receiver_tool = await mcp_b.get_tool("accept_doc")
    ticket = await receiver_tool.fn(case_id="42")

    sender_tool = await mcp_a.get_tool("send_to_receiver")
    try:
        await sender_tool.fn(ticket=ticket, key="local-doc")
    finally:
        await client_b.aclose()

    assert sink_b.received is not None
    aid, meta, body = sink_b.received
    assert aid == "case-42-doc"
    assert body == payload
    assert meta.mimeType == "application/json"
