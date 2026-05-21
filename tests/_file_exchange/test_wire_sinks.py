"""Tests for FilesystemSink + UploadSink descriptors."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from fastmcp_pvl_core._file_exchange._wire import (
    FilesystemSink,
    UploadSink,
)


def test_filesystem_sink_accepts_exchange_uri():
    s = FilesystemSink(transport="filesystem", uri="exchange://vol/inbox/x.bin")
    assert s.uri == "exchange://vol/inbox/x.bin"


def test_filesystem_sink_rejects_extra_field():
    with pytest.raises(ValidationError):
        FilesystemSink.model_validate(
            {"transport": "filesystem", "uri": "exchange://v/a", "extra": 1}
        )


def test_filesystem_sink_is_frozen():
    s = FilesystemSink(transport="filesystem", uri="exchange://v/in")
    with pytest.raises(ValidationError):
        s.uri = "exchange://v/in2"  # type: ignore[misc]


def test_upload_sink_minimal_valid():
    s = UploadSink(
        transport="upload",
        url="https://intake.example.com/u/Lm",
        expiresAt="2026-05-18T12:30:00Z",
    )
    assert s.method == "PUT"


def test_upload_sink_accepts_post_method():
    s = UploadSink(
        transport="upload",
        url="https://intake.example.com/u/Lm",
        method="POST",
        expiresAt="2026-05-18T12:30:00Z",
    )
    assert s.method == "POST"


def test_upload_sink_rejects_unknown_method():
    with pytest.raises(ValidationError):
        UploadSink(
            transport="upload",
            url="https://intake.example.com/u/Lm",
            method="DELETE",
            expiresAt="2026-05-18T12:30:00Z",
        )


def test_upload_sink_rejects_http_url():
    with pytest.raises(ValidationError):
        UploadSink(
            transport="upload",
            url="http://intake.example.com/u/Lm",
            expiresAt="2026-05-18T12:30:00Z",
        )


def test_upload_sink_rejects_extra_field():
    with pytest.raises(ValidationError):
        UploadSink.model_validate(
            {
                "transport": "upload",
                "url": "https://x/y",
                "expiresAt": "2026-05-18T12:30:00Z",
                "secret": "no",
            }
        )


def test_upload_sink_is_frozen():
    s = UploadSink(
        transport="upload",
        url="https://intake.example.com/u/Lm",
        expiresAt="2026-05-18T12:30:00Z",
    )
    with pytest.raises(ValidationError):
        s.url = "https://other"  # type: ignore[misc]


def test_upload_sink_rejects_naive_expires_at():
    """``AwareDatetime`` blocks direct construction with a naive datetime.

    See :func:`test_download_source_rejects_naive_expires_at` in
    ``test_wire_sources.py`` for the rationale — selection (§9)
    compares against tz-aware ``now``.
    """
    naive = datetime(2026, 5, 18, 12, 30, 0)  # no tzinfo
    with pytest.raises(ValidationError):
        UploadSink(
            transport="upload",
            url="https://intake.example.com/u/Lm",
            expiresAt=naive,
        )
