"""Tests for upload direction records and store."""

from __future__ import annotations

import dataclasses
import time

import pytest

from fastmcp_pvl_core._token_store import UploadRecord, UploadStore


class TestUploadRecord:
    def test_is_frozen(self) -> None:
        record = UploadRecord(
            target_id="vault/foo.md",
            max_bytes=1024,
            extra={},
            expires_at=time.time() + 60,
        )
        assert dataclasses.is_dataclass(record)
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.target_id = "x"  # type: ignore[misc]

    def test_extra_is_held_by_reference(self) -> None:
        """UploadRecord does not snapshot ``extra`` on construction.

        The snapshot policy lives one layer up in ``UploadStore.reserve``
        (Task 4); direct construction holds the caller's dict by reference.
        Pinning this behavior here makes any future change of mind explicit
        rather than silent.
        """
        extra = {"k": "v1"}
        record = UploadRecord(target_id="a", max_bytes=10, extra=extra, expires_at=0.0)
        extra["k"] = "v2"
        assert record.extra["k"] == "v2"
        assert record.extra is extra

    def test_required_fields(self) -> None:
        with pytest.raises(TypeError):
            UploadRecord()  # type: ignore[call-arg]


class TestUploadStore:
    def test_reserve_returns_token_and_url(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(target_id="vault/foo.md", max_bytes=1024)
        assert isinstance(token, str) and len(token) == 32
        url = store.build_url(token)
        assert url == f"https://srv.test/uploads/{token}"

    def test_reserve_with_explicit_ttl_and_extra(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(
            target_id="vault/x.md", max_bytes=10, ttl_seconds=42, extra={"k": 1}
        )
        record = store.peek(token)
        assert record is not None
        assert record.extra == {"k": 1}
        assert record.expires_at - time.time() == pytest.approx(42, abs=2)

    def test_consume_returns_record_then_none(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(target_id="x", max_bytes=10)
        first = store.consume(token)
        assert first is not None and first.target_id == "x"
        assert store.consume(token) is None

    def test_consume_returns_none_for_expired(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        token = store.reserve(target_id="x", max_bytes=10, ttl_seconds=-1)
        assert store.consume(token) is None

    def test_consume_returns_none_for_unknown(self) -> None:
        store = UploadStore(base_url="https://srv.test")
        assert store.consume("not-a-real-token") is None

    def test_build_url_requires_base_url(self) -> None:
        store = UploadStore()
        token = store.reserve(target_id="x", max_bytes=10)
        with pytest.raises(RuntimeError, match="base_url"):
            store.build_url(token)


def test_upload_store_singleton_accessors() -> None:
    from fastmcp_pvl_core._token_store import (
        get_upload_store,
        set_upload_store,
    )

    set_upload_store(None)
    with pytest.raises(RuntimeError, match="set_upload_store"):
        get_upload_store()
    s = UploadStore(base_url="https://srv.test")
    set_upload_store(s)
    assert get_upload_store() is s
    set_upload_store(None)  # leave clean
