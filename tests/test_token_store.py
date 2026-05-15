"""Tests for the generic token-store base class."""

from __future__ import annotations

import dataclasses
import logging
import time

import pytest

from fastmcp_pvl_core._token_store import (
    UploadStore,
    _BaseTokenStore,
)


@dataclasses.dataclass(frozen=True)
class _DummyRecord:
    expires_at: float
    payload: str


def test_base_token_store_create_returns_unique_tokens() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    t1 = store._mint_token()
    t2 = store._mint_token()
    assert t1 != t2
    assert isinstance(t1, str) and len(t1) == 32


def test_base_token_store_atomic_consume_returns_record_once() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    token = store._mint_token()
    rec = _DummyRecord(expires_at=time.time() + 60, payload="hi")
    store._records[token] = rec
    out = store._atomic_consume(token)
    assert out is rec
    assert store._atomic_consume(token) is None


def test_base_token_store_atomic_consume_treats_expired_as_missing() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    token = store._mint_token()
    store._records[token] = _DummyRecord(expires_at=time.time() - 1, payload="x")
    assert store._atomic_consume(token) is None
    # Expired record must be removed even though the consume returned None.
    assert token not in store._records


def test_base_token_store_purge_expired_removes_only_expired() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    fresh = store._mint_token()
    stale = store._mint_token()
    now = time.time()
    store._records[fresh] = _DummyRecord(expires_at=now + 60, payload="fresh")
    store._records[stale] = _DummyRecord(expires_at=now - 1, payload="stale")
    store._purge_expired()
    assert fresh in store._records
    assert stale not in store._records


def test_base_token_store_atomic_consume_unknown_returns_none() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    assert store._atomic_consume("does-not-exist") is None


def test_base_token_store_purge_expired_logs_count_when_nonempty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The DEBUG sweep log fires only when at least one record was expired."""
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    fresh = store._mint_token()
    stale = store._mint_token()
    now = time.time()
    store._records[fresh] = _DummyRecord(expires_at=now + 60, payload="fresh")
    store._records[stale] = _DummyRecord(expires_at=now - 1, payload="stale")
    with caplog.at_level(logging.DEBUG, logger="fastmcp_pvl_core._token_store"):
        store._purge_expired()
    assert any(
        "token_store_purge" in rec.getMessage() and "count=1" in rec.getMessage()
        for rec in caplog.records
    )


def test_base_token_store_purge_expired_silent_when_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No log line when the sweep removes nothing — keeps debug noise down."""
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    store._records[store._mint_token()] = _DummyRecord(
        expires_at=time.time() + 60, payload="fresh"
    )
    with caplog.at_level(logging.DEBUG, logger="fastmcp_pvl_core._token_store"):
        store._purge_expired()
    assert not any("token_store_purge" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# UploadStore-specific edges not covered by ``test_uploads.py``
# ---------------------------------------------------------------------------


def test_upload_store_rejects_route_path_without_token_placeholder() -> None:
    """``route_path`` MUST contain ``{token}`` — symmetry with ArtifactStore."""
    with pytest.raises(ValueError, match=r"\{token\}"):
        UploadStore(route_path="/uploads/missing-placeholder")


def test_upload_store_has_base_url_property() -> None:
    """``has_base_url`` mirrors :class:`ArtifactStore.has_base_url`."""
    assert UploadStore(base_url="https://srv.test").has_base_url is True
    assert UploadStore().has_base_url is False


def test_upload_store_peek_for_tests_returns_none_for_expired() -> None:
    """The test-only peek treats expired the same as missing."""
    store = UploadStore()
    token = store.reserve(origin_id="x", max_bytes=10, ttl_seconds=-1)
    # Record sat in the table after reserve (the negative TTL only affects
    # subsequent purge); peek must observe expires_at and report None.
    assert store._peek_for_tests(token) is None
    # And for a token that was never reserved.
    assert store._peek_for_tests("never-existed") is None
