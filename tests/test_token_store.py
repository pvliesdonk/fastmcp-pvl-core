"""Tests for the generic token-store base class."""

from __future__ import annotations

import dataclasses
import time

from fastmcp_pvl_core._token_store import _BaseTokenStore


@dataclasses.dataclass(frozen=True)
class _DummyRecord:
    expires_at: float
    payload: str


def test_base_token_store_create_returns_unique_tokens() -> None:
    store: _BaseTokenStore[_DummyRecord] = _BaseTokenStore()
    t1 = store._mint_token()
    t2 = store._mint_token()
    assert t1 != t2
    assert isinstance(t1, str) and len(t1) >= 32


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
