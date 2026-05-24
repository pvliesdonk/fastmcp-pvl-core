"""Tests for the ``upload`` transport data plane (#146)."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _upload
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    IntakeTicket,
    UploadSink,
)

pytestmark = pytest.mark.anyio


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


async def test_receiver_mint_returns_ticket_with_upload_sink():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "art-1", token_store=store, base_url="https://recv.test", ttl=300.0
    )
    assert isinstance(ticket, IntakeTicket)
    assert ticket.artifactId == "art-1"
    assert len(ticket.sinks) == 1
    sink = ticket.sinks[0]
    assert isinstance(sink, UploadSink)
    assert sink.transport == "upload"
    assert sink.method == "PUT"
    assert sink.url.startswith("https://recv.test/fx/u/")
    token = sink.url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["artifact_id"] == "art-1"
    assert rec.metadata["expected"] is None


async def test_receiver_mint_threads_method_and_expected():
    store = _store()
    expected = ArtifactConstraints(maxSize=1024, acceptMimeTypes=["text/*"])
    ticket = await _upload.upload_receiver_mint(
        "art-2",
        token_store=store,
        base_url="https://recv.test",
        ttl=300.0,
        expected=expected,
        method="POST",
    )
    assert ticket.expected == expected
    assert ticket.sinks[0].method == "POST"
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["expected"] == {
        "maxSize": 1024,
        "acceptMimeTypes": ["text/*"],
        "requireDigest": None,
    }
