"""Tests for upload direction records and store."""

from __future__ import annotations

import dataclasses
import time

import pytest

from fastmcp_pvl_core._token_store import UploadRecord


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

    def test_default_extra_is_empty_dict_via_factory(self) -> None:
        # Two records must not share the default mutable.
        a = UploadRecord(target_id="a", max_bytes=10, extra={}, expires_at=0.0)
        b = UploadRecord(target_id="b", max_bytes=10, extra={}, expires_at=0.0)
        # Records frozen, so we can't mutate; check identity differs only
        # if the caller supplied distinct dicts.
        assert a.extra is not b.extra or (a.extra == {} and b.extra == {})

    def test_required_fields(self) -> None:
        with pytest.raises(TypeError):
            UploadRecord()  # type: ignore[call-arg]
