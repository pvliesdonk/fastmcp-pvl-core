"""Tests for upload direction records and store."""

from __future__ import annotations

import dataclasses
import time

import pytest

from fastmcp_pvl_core._token_store import (
    UploadRecord,
    UploadStore,
    get_upload_store,
    set_upload_store,
)


class TestUploadRecord:
    def test_is_frozen(self) -> None:
        record = UploadRecord(
            origin_id="foo.md",
            destination=None,
            content_type=None,
            max_bytes=1024,
            expires_at=time.time() + 60,
        )
        assert dataclasses.is_dataclass(record)
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.origin_id = "x"  # type: ignore[misc]

    def test_required_fields(self) -> None:
        with pytest.raises(TypeError):
            UploadRecord()  # type: ignore[call-arg]

    def test_upload_record_carries_origin_id_destination_content_type(self) -> None:
        rec = UploadRecord(
            origin_id="a",
            destination="d/x.md",
            content_type="text/markdown",
            max_bytes=10,
            expires_at=time.time() + 60,
        )
        assert rec.origin_id == "a"
        assert rec.destination == "d/x.md"
        assert rec.content_type == "text/markdown"
        assert not hasattr(rec, "target_id")
        assert not hasattr(rec, "extra")


class TestUploadStore:
    def test_reserve_returns_token_and_url(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(origin_id="foo.md", max_bytes=1024)
        assert isinstance(token, str) and len(token) == 32
        url = store.build_url(token)
        assert url == f"https://srv.test/uploads/{token}"

    def test_reserve_with_explicit_ttl_destination_content_type(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(
            origin_id="x.md",
            max_bytes=10,
            ttl_seconds=42,
            destination="vault/x.md",
            content_type="text/markdown",
        )
        record = store._peek_for_tests(token)
        assert record is not None
        assert record.destination == "vault/x.md"
        assert record.content_type == "text/markdown"
        assert record.expires_at - time.time() == pytest.approx(42, abs=2)

    def test_consume_returns_record_then_none(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(origin_id="x", max_bytes=10)
        first = store.consume(token)
        assert first is not None and first.origin_id == "x"
        assert store.consume(token) is None

    def test_consume_returns_none_for_expired(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(origin_id="x", max_bytes=10, ttl_seconds=-1)
        assert store.consume(token) is None

    def test_consume_returns_none_for_unknown(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        assert store.consume("not-a-real-token") is None

    def test_build_url_requires_base_url(self) -> None:
        store = UploadStore()
        token = store.reserve(origin_id="x", max_bytes=10)
        with pytest.raises(RuntimeError, match="base_url"):
            store.build_url(token)


def test_upload_store_singleton_accessors() -> None:
    set_upload_store(None)
    with pytest.raises(RuntimeError, match="set_upload_store"):
        get_upload_store()
    s = UploadStore(base_url="https://srv.test")
    set_upload_store(s)
    assert get_upload_store() is s
    set_upload_store(None)  # leave clean
