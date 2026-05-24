"""Tests for the ``upload`` transport data plane (#146)."""

from __future__ import annotations

import base64
import hashlib

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


def test_format_content_digest_rfc9530():
    raw = hashlib.sha256(b"abc").digest()
    out = _upload._format_content_digest("sha-256", raw)
    assert out == "sha-256=:" + base64.b64encode(raw).decode("ascii") + ":"


def test_parse_content_digest_roundtrip():
    raw = hashlib.sha256(b"abc").digest()
    header = _upload._format_content_digest("sha-256", raw)
    assert _upload._parse_content_digest(header) == ("sha-256", raw)


def test_parse_content_digest_picks_first_supported():
    raw = hashlib.sha512(b"abc").digest()
    md5_b64 = base64.b64encode(b"x").decode()
    sha512_b64 = base64.b64encode(raw).decode()
    header = f"md5=:{md5_b64}:, sha-512=:{sha512_b64}:"
    assert _upload._parse_content_digest(header) == ("sha-512", raw)


def test_parse_content_digest_rejects_unparseable():
    assert _upload._parse_content_digest("sha-256=not-a-byte-sequence") is None
    assert _upload._parse_content_digest("sha-256=:!!!notbase64!!!:") is None
    assert (
        _upload._parse_content_digest("md5=:" + base64.b64encode(b"x").decode() + ":")
        is None
    )


def test_parse_content_digest_ignores_member_params():
    raw = hashlib.sha256(b"abc").digest()
    b64 = base64.b64encode(raw).decode()
    # RFC 8941 member parameters after the byte sequence are stripped, not rejected.
    assert _upload._parse_content_digest(f"sha-256=:{b64}:;preference=3") == (
        "sha-256",
        raw,
    )


def test_parse_content_digest_supported_malformed_does_not_fall_through():
    raw = hashlib.sha512(b"abc").digest()
    b64 = base64.b64encode(raw).decode()
    # A supported algo with corrupt bytes is rejected outright, not skipped in
    # favour of a later well-formed member.
    assert _upload._parse_content_digest(f"sha-256=:!!!:, sha-512=:{b64}:") is None


@pytest.mark.parametrize(
    ("content_type", "accept", "ok"),
    [
        ("text/plain", ["text/plain"], True),
        ("text/plain; charset=utf-8", ["text/plain"], True),
        ("text/plain", ["text/*"], True),
        ("text/plain", ["*/*"], True),
        ("application/json", ["text/*"], False),
        ("application/json", ["text/*", "application/json"], True),
        (None, ["text/*"], False),
        ("not-a-media-type", ["*/*"], False),
        ("*/*", ["application/octet-stream"], False),
        ("*/*", ["*/*"], True),
    ],
)
def test_media_type_accepted(content_type, accept, ok):
    assert _upload._media_type_accepted(content_type, accept) is ok
