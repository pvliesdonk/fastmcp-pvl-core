"""Unit tests for the upload transport data plane (#146).

Covers:
1. upload_receiver_mint — IntakeTicket shape, token storage.
2. Helper unit tests — _format_content_digest, _parse_content_digest,
   _media_type_accepted.
3. Route status matrix — 204 happy, 404 unknown/consumed, 413 oversize,
   415 mime reject, 400 digest mismatch / missing required / wrong algo,
   500 sink failure.
4. Ambient credentials ignored.
5. Non-sha256 digest verification through route (sha-384, sha-512).
6. Consume failure after successful store still returns 204.
7. Temp-file unlinked on every failure path.
8. Flush/close failure paths.
9. Sender tests — headers, omit Content-Type when mimeType is None,
   non-2xx, guard refusal, hook non-OSError wrapped, non-sha-256 digest_algo,
   unsupported digest_algo -> ValueError, sender temp file unlinked on non-2xx.
10. register_file_exchange_routes validation — atomicity.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import os

import httpx
import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._file_exchange import _routes, _upload
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._tokens import build_capability_token_store
from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints, UploadSink

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _store():
    return build_capability_token_store(
        ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=3600.0)
    )


def _cfg(*, max_size: int | None = None) -> ServerConfig:
    return ServerConfig(file_exchange_max_artifact_size=max_size)


class _CapturingSink:
    """ArtifactSink that records the bytes it is given."""

    def __init__(self) -> None:
        self.calls = 0
        self.artifact_ids: list[str | None] = []
        self.deposited: bytes | None = None

    async def store_artifact(self, artifact_id, metadata, stream) -> None:
        self.calls += 1
        self.artifact_ids.append(artifact_id)
        self.deposited = stream.read()


class _RaisingSink:
    async def store_artifact(self, artifact_id, metadata, stream) -> None:
        raise RuntimeError("sink boom")


def _route_client(store, sink, *, max_size: int | None = None):
    mcp = FastMCP("test")
    cfg = _cfg(max_size=max_size)
    _upload.register_upload_route(mcp, token_store=store, sink=sink, config=cfg)
    app = mcp.http_app()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://route.test"
    )


async def _mint_upload_token(store, artifact_id="art-1", expected=None):
    """Mint a raw upload token (bypassing upload_receiver_mint)."""
    minted = await store.mint(
        {
            "artifact_id": artifact_id,
            "expected": expected.model_dump() if expected is not None else None,
        },
        ttl=300.0,
        single_use=True,
    )
    return minted.token


def _sha256_content_digest(body: bytes) -> str:
    raw = hashlib.sha256(body).digest()
    return f"sha-256=:{base64.b64encode(raw).decode()}:"


def _sha384_content_digest(body: bytes) -> str:
    raw = hashlib.sha384(body).digest()
    return f"sha-384=:{base64.b64encode(raw).decode()}:"


def _sha512_content_digest(body: bytes) -> str:
    raw = hashlib.sha512(body).digest()
    return f"sha-512=:{base64.b64encode(raw).decode()}:"


# ---------------------------------------------------------------------------
# 1. upload_receiver_mint
# ---------------------------------------------------------------------------


async def test_receiver_mint_returns_intake_ticket():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "art-1",
        token_store=store,
        base_url="https://recv.example",
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
    assert sink.url.startswith("https://recv.example/fx/u/")
    assert sink.method == "PUT"


async def test_receiver_mint_method_threads_through():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "art-2",
        token_store=store,
        base_url="https://recv.example",
        ttl=60.0,
        method="POST",
    )
    assert ticket.sinks[0].method == "POST"


async def test_receiver_mint_expected_threads_through():
    store = _store()
    expected = ArtifactConstraints(
        maxSize=1024, acceptMimeTypes=["application/json"], requireDigest=["sha-256"]
    )
    ticket = await _upload.upload_receiver_mint(
        "art-3",
        token_store=store,
        base_url="https://recv.example",
        ttl=60.0,
        expected=expected,
    )
    assert ticket.expected == expected
    # Token stores artifact_id and expected.
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["artifact_id"] == "art-3"
    expected_raw = rec.metadata["expected"]
    assert expected_raw is not None
    assert expected_raw["maxSize"] == 1024
    assert expected_raw["acceptMimeTypes"] == ["application/json"]


async def test_receiver_mint_token_stores_artifact_id():
    store = _store()
    ticket = await _upload.upload_receiver_mint(
        "my-artifact",
        token_store=store,
        base_url="https://recv.example",
        ttl=60.0,
    )
    token = ticket.sinks[0].url.rsplit("/", 1)[1]
    rec = await store.lookup(token)
    assert rec is not None
    assert rec.metadata["artifact_id"] == "my-artifact"
    assert rec.metadata["expected"] is None
    assert rec.single_use is True


# ---------------------------------------------------------------------------
# 2. Helper unit tests
# ---------------------------------------------------------------------------


def test_format_content_digest_sha256():
    body = b"hello"
    raw = hashlib.sha256(body).digest()
    header = _upload._format_content_digest("sha-256", raw)
    assert header.startswith("sha-256=:")
    assert header.endswith(":")
    # Round-trip via parse.
    result = _upload._parse_content_digest(header)
    assert result is not None
    algo, decoded = result
    assert algo == "sha-256"
    assert decoded == raw


def test_format_parse_round_trip_sha384():
    body = b"round-trip-384"
    raw = hashlib.sha384(body).digest()
    header = _upload._format_content_digest("sha-384", raw)
    result = _upload._parse_content_digest(header)
    assert result is not None
    algo, decoded = result
    assert algo == "sha-384"
    assert decoded == raw


def test_format_parse_round_trip_sha512():
    body = b"round-trip-512"
    raw = hashlib.sha512(body).digest()
    header = _upload._format_content_digest("sha-512", raw)
    result = _upload._parse_content_digest(header)
    assert result is not None
    algo, decoded = result
    assert algo == "sha-512"
    assert decoded == raw


def test_parse_content_digest_first_supported_member_wins():
    body = b"multi-member"
    raw256 = hashlib.sha256(body).digest()
    raw384 = hashlib.sha384(body).digest()
    # sha-256 listed first — it wins.
    h256 = _upload._format_content_digest("sha-256", raw256)
    h384 = _upload._format_content_digest("sha-384", raw384)
    result = _upload._parse_content_digest(f"{h256},{h384}")
    assert result is not None
    assert result[0] == "sha-256"
    assert result[1] == raw256


def test_parse_content_digest_unsupported_then_supported():
    body = b"skip-unsupported"
    raw = hashlib.sha256(body).digest()
    h256 = _upload._format_content_digest("sha-256", raw)
    # sha-1 is unsupported → skip it; sha-256 follows and wins.
    header = f"sha-1=:AAAA:,{h256}"
    result = _upload._parse_content_digest(header)
    assert result is not None
    assert result[0] == "sha-256"
    assert result[1] == raw


def test_parse_content_digest_member_parameters_stripped():
    body = b"with-params"
    raw = hashlib.sha256(body).digest()
    encoded = base64.b64encode(raw).decode()
    # RFC 8941 member parameter on the value portion (after the byte-sequence).
    # Format: key=value;param=val — the semicolon separator follows the value.
    header = f"sha-256=:{encoded}:;q=0.9"
    result = _upload._parse_content_digest(header)
    assert result is not None
    assert result[0] == "sha-256"
    assert result[1] == raw


def test_parse_content_digest_supported_malformed_does_not_fall_through():
    body = b"malformed"
    raw = hashlib.sha256(body).digest()
    h384 = _upload._format_content_digest("sha-384", raw)
    # sha-256 is supported but malformed (missing colon-wrap); must return None,
    # NOT fall through to the valid sha-384 member.
    header = f"sha-256=NOTBASE64,{h384}"
    result = _upload._parse_content_digest(header)
    assert result is None  # supported-but-malformed stops iteration


def test_parse_content_digest_rejects_empty_colon_colon():
    # "::" is 2-char length guard — decodes to empty bytes.
    result = _upload._parse_content_digest("sha-256=::")
    assert result is None


def test_parse_content_digest_rejects_invalid_base64():
    result = _upload._parse_content_digest("sha-256=:not!valid!base64:")
    assert result is None


def test_parse_content_digest_rejects_unsupported_only():
    result = _upload._parse_content_digest("sha-1=:AAAA:")
    assert result is None


def test_parse_content_digest_none_header():
    assert _upload._parse_content_digest(None) is None


def test_parse_content_digest_empty_header():
    assert _upload._parse_content_digest("") is None


def test_media_type_accepted_exact_match():
    assert _upload._media_type_accepted("application/json", ["application/json"])


def test_media_type_accepted_subtype_wildcard():
    assert _upload._media_type_accepted("text/plain", ["text/*"])


def test_media_type_accepted_star_star():
    assert _upload._media_type_accepted("image/png", ["*/*"])


def test_media_type_accepted_strips_parameters():
    assert _upload._media_type_accepted("text/plain; charset=utf-8", ["text/plain"])


def test_media_type_accepted_missing_content_type():
    assert not _upload._media_type_accepted(None, ["text/plain"])


def test_media_type_accepted_no_match():
    assert not _upload._media_type_accepted("image/png", ["text/plain", "text/*"])


def test_media_type_accepted_malformed_content_type():
    # No slash in media type → no match.
    assert not _upload._media_type_accepted("notavalidtype", ["*/*"])


def test_media_type_accepted_star_star_client_reject():
    # If the list does NOT contain */* the wildcard isn't applied from client side.
    assert not _upload._media_type_accepted("application/json", ["text/plain"])


# ---------------------------------------------------------------------------
# 3. Route status matrix
# ---------------------------------------------------------------------------


async def test_route_unknown_token_404():
    store = _store()
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r = await client.put("/fx/u/unknown-token", content=b"x")
    assert r.status_code == 404


async def test_route_happy_put_204_deposits_bytes():
    store = _store()
    body = b"hello-upload-bytes"
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r = await client.put(
            f"/fx/u/{token}",
            content=body,
            headers={"Content-Digest": _sha256_content_digest(body)},
        )
    assert r.status_code == 204
    assert r.content == b""
    assert sink.calls == 1
    assert sink.deposited == body


async def test_route_single_use_token_consumed_after_success():
    store = _store()
    body = b"consume-me"
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r1 = await client.put(f"/fx/u/{token}", content=body)
        assert r1.status_code == 204
        r2 = await client.put(f"/fx/u/{token}", content=body)
    assert r2.status_code == 404  # consumed


async def test_route_oversize_operator_cap_413():
    store = _store()
    body = b"x" * 5000
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    async with _route_client(store, sink, max_size=100) as client:
        r = await client.put(f"/fx/u/{token}", content=body)
    assert r.status_code == 413
    assert sink.calls == 0
    # Token NOT consumed on oversize.
    assert await store.lookup(token) is not None


async def test_route_oversize_ticket_cap_413():
    store = _store()
    body = b"x" * 5000
    expected = ArtifactConstraints(maxSize=100)
    token = await _mint_upload_token(store, expected=expected)
    sink = _CapturingSink()
    # No operator cap — only the ticket cap applies.
    async with _route_client(store, sink) as client:
        r = await client.put(f"/fx/u/{token}", content=body)
    assert r.status_code == 413
    assert sink.calls == 0


async def test_route_uses_min_of_both_caps_operator_smaller():
    store = _store()
    # Operator cap 50, ticket cap 200 → effective = 50.
    body = b"x" * 100
    expected = ArtifactConstraints(maxSize=200)
    token = await _mint_upload_token(store, expected=expected)
    sink = _CapturingSink()
    async with _route_client(store, sink, max_size=50) as client:
        r = await client.put(f"/fx/u/{token}", content=body)
    assert r.status_code == 413
    assert sink.calls == 0


async def test_route_uses_min_of_both_caps_ticket_smaller():
    store = _store()
    # Operator cap 200, ticket cap 50 → effective = 50.
    body = b"x" * 100
    expected = ArtifactConstraints(maxSize=50)
    token = await _mint_upload_token(store, expected=expected)
    sink = _CapturingSink()
    async with _route_client(store, sink, max_size=200) as client:
        r = await client.put(f"/fx/u/{token}", content=body)
    assert r.status_code == 413
    assert sink.calls == 0


async def test_route_mime_reject_415():
    store = _store()
    expected = ArtifactConstraints(acceptMimeTypes=["application/json"])
    token = await _mint_upload_token(store, expected=expected)
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r = await client.put(
            f"/fx/u/{token}",
            content=b"x",
            headers={"Content-Type": "text/plain"},
        )
    assert r.status_code == 415
    assert sink.calls == 0
    # Token NOT consumed on mime reject.
    assert await store.lookup(token) is not None


async def test_route_mime_accept_matching_type():
    store = _store()
    expected = ArtifactConstraints(acceptMimeTypes=["application/json"])
    token = await _mint_upload_token(store, expected=expected)
    sink = _CapturingSink()
    body = b'{"ok":1}'
    async with _route_client(store, sink) as client:
        r = await client.put(
            f"/fx/u/{token}",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 204
    assert sink.calls == 1


async def test_route_digest_mismatch_400_sink_not_called():
    store = _store()
    body = b"correct-bytes"
    wrong_digest = _sha256_content_digest(b"wrong-bytes")
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r = await client.put(
            f"/fx/u/{token}",
            content=body,
            headers={"Content-Digest": wrong_digest},
        )
    assert r.status_code == 400
    assert sink.calls == 0  # verify-before-use: sink never called on bad digest
    # Token NOT consumed on digest mismatch.
    assert await store.lookup(token) is not None


async def test_route_missing_required_digest_400():
    store = _store()
    expected = ArtifactConstraints(requireDigest=["sha-256"])
    token = await _mint_upload_token(store, expected=expected)
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r = await client.put(f"/fx/u/{token}", content=b"no-digest-header")
    assert r.status_code == 400
    assert sink.calls == 0


async def test_route_required_digest_wrong_algo_400():
    store = _store()
    body = b"data"
    expected = ArtifactConstraints(requireDigest=["sha-256"])
    token = await _mint_upload_token(store, expected=expected)
    sink = _CapturingSink()
    # Sending sha-384 when sha-256 is required.
    async with _route_client(store, sink) as client:
        r = await client.put(
            f"/fx/u/{token}",
            content=body,
            headers={"Content-Digest": _sha384_content_digest(body)},
        )
    assert r.status_code == 400
    assert sink.calls == 0


async def test_route_sink_failure_500_body_free():
    store = _store()
    token = await _mint_upload_token(store)
    async with _route_client(store, _RaisingSink()) as client:
        r = await client.put(f"/fx/u/{token}", content=b"data")
    assert r.status_code == 500
    assert r.content == b""  # no exception message echoed
    # Token NOT consumed on sink failure.
    assert await store.lookup(token) is not None


# ---------------------------------------------------------------------------
# 4. Ambient credentials ignored
# ---------------------------------------------------------------------------


async def test_route_ignores_ambient_credentials():
    store = _store()
    body = b"data"
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r = await client.put(
            f"/fx/u/{token}",
            content=body,
            headers={"Authorization": "Bearer secret", "Cookie": "s=1"},
        )
    assert r.status_code == 204
    assert sink.deposited == body


# ---------------------------------------------------------------------------
# 5. Non-sha256 digest verification through route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "digest_fn,label",
    [
        (_sha384_content_digest, "sha-384"),
        (_sha512_content_digest, "sha-512"),
    ],
)
async def test_route_non_sha256_digest_verified(digest_fn, label):
    store = _store()
    body = b"algo-test-bytes"
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r = await client.put(
            f"/fx/u/{token}",
            content=body,
            headers={"Content-Digest": digest_fn(body)},
        )
    assert r.status_code == 204
    assert sink.deposited == body


@pytest.mark.parametrize(
    "digest_fn",
    [_sha384_content_digest, _sha512_content_digest],
)
async def test_route_non_sha256_digest_mismatch_400(digest_fn):
    store = _store()
    body = b"algo-test-bytes"
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r = await client.put(
            f"/fx/u/{token}",
            content=body,
            headers={"Content-Digest": digest_fn(b"wrong-bytes")},
        )
    assert r.status_code == 400
    assert sink.calls == 0


# ---------------------------------------------------------------------------
# 6. Consume failure after successful store still returns 204
# ---------------------------------------------------------------------------


async def test_route_consume_failure_after_store_returns_204(monkeypatch, caplog):
    store = _store()
    body = b"data"
    token = await _mint_upload_token(store)
    sink = _CapturingSink()

    async def boom_consume(_token):
        raise RuntimeError("kv backend down")

    monkeypatch.setattr(store, "consume", boom_consume)
    async with _route_client(store, sink) as client:
        import logging

        with caplog.at_level(logging.ERROR):
            r = await client.put(f"/fx/u/{token}", content=body)
    assert r.status_code == 204
    assert sink.calls == 1
    assert sink.deposited == body
    # The exception must be logged (not just warned).
    assert any(
        "consume failed" in m or "token may remain" in m for m in caplog.messages
    )


# ---------------------------------------------------------------------------
# 7. Temp-file unlinked on every failure path
# ---------------------------------------------------------------------------


async def test_route_temp_unlinked_on_413(monkeypatch, tmp_path):
    real_mkstemp = _upload.tempfile.mkstemp
    created: list[str] = []

    def spy_mkstemp(*args, **kwargs):
        kwargs.setdefault("dir", str(tmp_path))
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(_upload.tempfile, "mkstemp", spy_mkstemp)
    store = _store()
    body = b"x" * 5000
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    async with _route_client(store, sink, max_size=100) as client:
        r = await client.put(f"/fx/u/{token}", content=body)
    assert r.status_code == 413
    assert created
    assert all(not os.path.exists(p) for p in created)


async def test_route_temp_unlinked_on_400_digest(monkeypatch, tmp_path):
    real_mkstemp = _upload.tempfile.mkstemp
    created: list[str] = []

    def spy_mkstemp(*args, **kwargs):
        kwargs.setdefault("dir", str(tmp_path))
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(_upload.tempfile, "mkstemp", spy_mkstemp)
    store = _store()
    body = b"x"
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    async with _route_client(store, sink) as client:
        r = await client.put(
            f"/fx/u/{token}",
            content=body,
            headers={"Content-Digest": _sha256_content_digest(b"wrong")},
        )
    assert r.status_code == 400
    assert created
    assert all(not os.path.exists(p) for p in created)


async def test_route_temp_unlinked_on_500_sink_failure(monkeypatch, tmp_path):
    real_mkstemp = _upload.tempfile.mkstemp
    created: list[str] = []

    def spy_mkstemp(*args, **kwargs):
        kwargs.setdefault("dir", str(tmp_path))
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(_upload.tempfile, "mkstemp", spy_mkstemp)
    store = _store()
    token = await _mint_upload_token(store)
    async with _route_client(store, _RaisingSink()) as client:
        r = await client.put(f"/fx/u/{token}", content=b"data")
    assert r.status_code == 500
    assert created
    assert all(not os.path.exists(p) for p in created)


# ---------------------------------------------------------------------------
# 8. Flush / close failure paths
# ---------------------------------------------------------------------------


async def test_route_flush_failure_returns_500():
    store = _store()
    body = b"payload"
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    real_fdopen = os.fdopen

    class _FlushFails:
        def __init__(self, f):
            self._f = f

        def write(self, b):
            return self._f.write(b)

        def flush(self):
            raise OSError("disk full on flush")

        def close(self):
            return self._f.close()

    import unittest.mock

    with unittest.mock.patch.object(
        _upload.os,
        "fdopen",
        side_effect=lambda fd, mode: _FlushFails(real_fdopen(fd, mode)),
    ):
        async with _route_client(store, sink) as client:
            r = await client.put(f"/fx/u/{token}", content=body)
    assert r.status_code == 500
    assert sink.calls == 0


async def test_route_close_after_success_still_204():
    """A close() failure after flush must not turn a 204 into an error."""
    store = _store()
    body = b"payload"
    token = await _mint_upload_token(store)
    sink = _CapturingSink()
    real_fdopen = os.fdopen

    class _CloseFailsAfterFlush:
        def __init__(self, f):
            self._f = f
            self._flushed = False

        def write(self, b):
            return self._f.write(b)

        def flush(self):
            self._f.flush()
            self._flushed = True

        def close(self):
            self._f.close()
            if self._flushed:
                raise OSError("disk full on close")

    import unittest.mock

    with unittest.mock.patch.object(
        _upload.os,
        "fdopen",
        side_effect=lambda fd, mode: _CloseFailsAfterFlush(real_fdopen(fd, mode)),
    ):
        async with _route_client(store, sink) as client:
            r = await client.put(f"/fx/u/{token}", content=body)
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# 9. Sender tests
# ---------------------------------------------------------------------------


class _BytesSource:
    """ArtifactSource serving fixed bytes."""

    def __init__(
        self, key: str, body: bytes, mime: str | None = "application/octet-stream"
    ):
        self._key, self._body, self._mime = key, body, mime

    async def open_artifact(self, key: str):
        from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

        assert key == self._key
        meta = ArtifactMetadata(
            name="test",
            mimeType=self._mime,
            size=len(self._body),
        )
        return io.BytesIO(self._body), meta


class _NoMimeSource:
    """ArtifactSource with no mimeType."""

    async def open_artifact(self, key: str):
        from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

        return io.BytesIO(b"no-mime"), ArtifactMetadata(name="x")


def _make_sink_descriptor(url: str = "https://recv.example/fx/u/tok") -> UploadSink:
    return UploadSink(
        transport="upload",
        url=url,
        method="PUT",
        expiresAt="2099-01-01T00:00:00Z",
    )


def _install_upload_guard(monkeypatch, responder):
    """Patch _upload.guarded_stream to route through a MockTransport."""

    @contextlib.asynccontextmanager
    async def fake_guard(method, url, *, config, transport, headers=None, content=None):
        from fastmcp_pvl_core._file_exchange._outbound import GuardedResponse

        body_bytes = b""
        if content is not None:
            async for chunk in content:
                body_bytes += chunk
        fake_req = httpx.Request(method, url, headers=headers or {}, content=body_bytes)
        resp = responder(fake_req)
        yield GuardedResponse(
            status=resp.status_code, headers=resp.headers, _response=resp
        )

    monkeypatch.setattr(_upload, "guarded_stream", fake_guard)


async def test_sender_sends_correct_headers(monkeypatch):
    body = b"hello-sender"
    source = _BytesSource("k", body, mime="text/plain")
    sink_desc = _make_sink_descriptor()
    config = _cfg()

    seen: dict[str, object] = {}

    @contextlib.asynccontextmanager
    async def capture_guard(
        method, url, *, config, transport, headers=None, content=None
    ):
        from fastmcp_pvl_core._file_exchange._outbound import GuardedResponse

        body_bytes = b""
        if content is not None:
            async for chunk in content:
                body_bytes += chunk
        seen["method"] = method
        seen["headers"] = dict(headers or {})
        seen["body"] = body_bytes
        fake_resp = httpx.Response(204)
        yield GuardedResponse(
            status=204, headers=fake_resp.headers, _response=fake_resp
        )

    monkeypatch.setattr(_upload, "guarded_stream", capture_guard)

    await _upload.upload_sender_consume(sink_desc, source, "k", config=config)

    assert seen["method"] == "PUT"
    h = seen["headers"]
    assert h["Content-Length"] == str(len(body))
    assert h["Content-Type"] == "text/plain"
    assert "Content-Digest" in h
    # Verify the digest is correct.
    cd = _upload._parse_content_digest(h["Content-Digest"])
    assert cd is not None
    algo, raw = cd
    assert algo == "sha-256"
    assert raw == hashlib.sha256(body).digest()
    # Body streamed correctly.
    assert seen["body"] == body


async def test_sender_omits_content_type_when_mime_none(monkeypatch):
    source = _NoMimeSource()
    sink_desc = _make_sink_descriptor()
    config = _cfg()
    seen_headers: dict[str, str] = {}

    @contextlib.asynccontextmanager
    async def capture_guard(
        method, url, *, config, transport, headers=None, content=None
    ):
        from fastmcp_pvl_core._file_exchange._outbound import GuardedResponse

        if content is not None:
            async for _ in content:
                pass
        seen_headers.update(headers or {})
        fake_resp = httpx.Response(204)
        yield GuardedResponse(
            status=204, headers=fake_resp.headers, _response=fake_resp
        )

    monkeypatch.setattr(_upload, "guarded_stream", capture_guard)
    await _upload.upload_sender_consume(sink_desc, source, "k", config=config)
    assert "Content-Type" not in seen_headers


async def test_sender_non_2xx_raises_transfer_failed(monkeypatch):
    body = b"data"
    source = _BytesSource("k", body)
    sink_desc = _make_sink_descriptor()

    def responder(req):
        return httpx.Response(400)

    _install_upload_guard(monkeypatch, responder)

    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(sink_desc, source, "k", config=_cfg())
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED


async def test_sender_guard_refusal_propagates_not_accessible(monkeypatch):
    source = _BytesSource("k", b"x")
    sink_desc = _make_sink_descriptor()

    @contextlib.asynccontextmanager
    async def refusing(method, url, *, config, transport, headers=None, content=None):
        # Consume the content generator so it doesn't linger.
        if content is not None:
            async for _ in content:
                pass
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE, transport="upload", detail="refused"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(_upload, "guarded_stream", refusing)

    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(sink_desc, source, "k", config=_cfg())
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_sender_hook_non_oserror_wrapped_as_transfer_failed(monkeypatch):
    """A non-OSError from the source stream (e.g. ValueError) must be wrapped as
    TRANSFER_FAILED with the original exception as __cause__."""

    class _ExplodingSource:
        async def open_artifact(self, key: str):
            from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

            class _ExplodingStream:
                def read(self, n: int = -1) -> bytes:
                    raise ValueError("source exploded")

            return _ExplodingStream(), ArtifactMetadata(name="x")

    sink_desc = _make_sink_descriptor()

    @contextlib.asynccontextmanager
    async def dummy_guard(
        method, url, *, config, transport, headers=None, content=None
    ):
        if content is not None:
            async for _ in content:
                pass
        from fastmcp_pvl_core._file_exchange._outbound import GuardedResponse

        fake_resp = httpx.Response(204)
        yield GuardedResponse(
            status=204, headers=fake_resp.headers, _response=fake_resp
        )

    monkeypatch.setattr(_upload, "guarded_stream", dummy_guard)

    with pytest.raises(FileExchangeTransferError) as ei:
        await _upload.upload_sender_consume(
            sink_desc, _ExplodingSource(), "k", config=_cfg()
        )
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED
    assert isinstance(ei.value.__cause__, ValueError)


async def test_sender_non_sha256_digest_algo(monkeypatch):
    body = b"algo-test"
    source = _BytesSource("k", body)
    sink_desc = _make_sink_descriptor()
    seen_headers: dict[str, str] = {}

    @contextlib.asynccontextmanager
    async def capture_guard(
        method, url, *, config, transport, headers=None, content=None
    ):
        from fastmcp_pvl_core._file_exchange._outbound import GuardedResponse

        if content is not None:
            async for _ in content:
                pass
        seen_headers.update(headers or {})
        fake_resp = httpx.Response(204)
        yield GuardedResponse(
            status=204, headers=fake_resp.headers, _response=fake_resp
        )

    monkeypatch.setattr(_upload, "guarded_stream", capture_guard)
    await _upload.upload_sender_consume(
        sink_desc, source, "k", config=_cfg(), digest_algo="sha-384"
    )
    cd = _upload._parse_content_digest(seen_headers.get("Content-Digest", ""))
    assert cd is not None
    assert cd[0] == "sha-384"
    assert cd[1] == hashlib.sha384(body).digest()


def test_sender_unsupported_digest_algo_raises_value_error():
    sink_desc = _make_sink_descriptor()
    source = _BytesSource("k", b"x")

    with pytest.raises(ValueError, match="digest_algo"):
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            _upload.upload_sender_consume(
                sink_desc, source, "k", config=_cfg(), digest_algo="sha-1"
            )
        )


async def test_sender_temp_unlinked_on_non_2xx(monkeypatch, tmp_path):
    real_mkstemp = _upload.tempfile.mkstemp
    created: list[str] = []

    def spy_mkstemp(*args, **kwargs):
        kwargs.setdefault("dir", str(tmp_path))
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(_upload.tempfile, "mkstemp", spy_mkstemp)

    body = b"data"
    source = _BytesSource("k", body)
    sink_desc = _make_sink_descriptor()

    def responder(req):
        return httpx.Response(400)

    _install_upload_guard(monkeypatch, responder)

    with pytest.raises(FileExchangeTransferError):
        await _upload.upload_sender_consume(sink_desc, source, "k", config=_cfg())

    assert created
    assert all(not os.path.exists(p) for p in created)


async def test_sender_close_after_flush_failure_preserves_fete(monkeypatch):
    """A close() failure after a flush error must not replace the FETE."""
    real_fdopen = os.fdopen

    class _FlushAndCloseFail:
        def __init__(self, f):
            self._f = f

        def write(self, b):
            return self._f.write(b)

        def flush(self):
            raise OSError("disk full on flush")

        def close(self):
            self._f.close()
            raise OSError("disk full on close")

    import unittest.mock

    source = _BytesSource("k", b"data")
    sink_desc = _make_sink_descriptor()

    with unittest.mock.patch.object(
        _upload.os,
        "fdopen",
        side_effect=lambda fd, mode: _FlushAndCloseFail(real_fdopen(fd, mode)),
    ):
        with pytest.raises(FileExchangeTransferError) as ei:
            await _upload.upload_sender_consume(sink_desc, source, "k", config=_cfg())
    # The flush's FETE must propagate, not the close's raw OSError.
    assert ei.value.code == TransferErrorCode.TRANSFER_FAILED


# ---------------------------------------------------------------------------
# 10. register_file_exchange_routes validation
# ---------------------------------------------------------------------------


def test_register_requires_source_or_sink():
    mcp = FastMCP("test")
    store = _store()
    with pytest.raises(ValueError, match="at least one"):
        _routes.register_file_exchange_routes(mcp, token_store=store)


def test_register_requires_config_when_sink_given():
    mcp = FastMCP("test")
    store = _store()
    sink = _CapturingSink()
    with pytest.raises(ValueError, match="requires `config`"):
        _routes.register_file_exchange_routes(mcp, token_store=store, sink=sink)


async def test_register_atomicity_download_not_mounted_when_sink_missing_config():
    """source=X, sink=Y, config=None must raise ValueError AND the download
    route must NOT be mounted (atomicity — no partial mounts)."""
    mcp = FastMCP("test")
    store = _store()
    sink = _CapturingSink()

    class _DummySource:
        async def open_artifact(self, key):
            from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

            return io.BytesIO(b"x"), ArtifactMetadata(name="x")

    with pytest.raises(ValueError, match="requires `config`"):
        _routes.register_file_exchange_routes(
            mcp, token_store=store, source=_DummySource(), sink=sink
        )

    # Download route must NOT have been mounted (atomicity).
    app = mcp.http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/fx/d/some-token")
    assert r.status_code == 404  # route not mounted


def test_register_source_only_succeeds():
    """source only (no sink/config) is valid."""
    mcp = FastMCP("test")
    store = _store()

    class _DummySource:
        async def open_artifact(self, key):
            from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

            return io.BytesIO(b"x"), ArtifactMetadata(name="x")

    # Should not raise.
    _routes.register_file_exchange_routes(mcp, token_store=store, source=_DummySource())


def test_register_sink_with_config_succeeds():
    """sink + config (no source) is valid."""
    mcp = FastMCP("test")
    store = _store()
    sink = _CapturingSink()
    # Should not raise.
    _routes.register_file_exchange_routes(
        mcp, token_store=store, sink=sink, config=_cfg()
    )
