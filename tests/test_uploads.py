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
