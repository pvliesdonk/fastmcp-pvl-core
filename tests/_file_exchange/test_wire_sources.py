"""Tests for FilesystemSource + DownloadSource descriptors."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from fastmcp_pvl_core._file_exchange._wire import (
    DownloadSource,
    FilesystemSource,
)

# --- FilesystemSource ---


def test_filesystem_source_accepts_exchange_uri():
    s = FilesystemSource(transport="filesystem", uri="exchange://vol/a.bin")
    assert s.uri == "exchange://vol/a.bin"


def test_filesystem_source_accepts_file_uri():
    s = FilesystemSource(transport="filesystem", uri="file:///tmp/a.bin")
    assert s.uri == "file:///tmp/a.bin"


def test_filesystem_source_rejects_bad_uri():
    with pytest.raises(ValidationError):
        FilesystemSource(transport="filesystem", uri="http://bad/x")


def test_filesystem_source_rejects_extra_field():
    # §17.5: descriptor shape is closed.
    with pytest.raises(ValidationError):
        FilesystemSource.model_validate(
            {"transport": "filesystem", "uri": "exchange://v/a", "extra": 1}
        )


def test_filesystem_source_is_frozen():
    s = FilesystemSource(transport="filesystem", uri="exchange://v/a")
    with pytest.raises(ValidationError):
        s.uri = "exchange://v/b"  # type: ignore[misc]


# --- DownloadSource ---


def test_download_source_minimal_valid():
    s = DownloadSource(
        transport="download",
        url="https://example.com/d/Yk2p",
        expiresAt="2026-05-18T12:30:00Z",
    )
    assert s.singleUse is True  # default per §7.2.2


def test_download_source_rejects_http_url():
    with pytest.raises(ValidationError):
        DownloadSource(
            transport="download",
            url="http://example.com/d/Yk2p",
            expiresAt="2026-05-18T12:30:00Z",
        )


def test_download_source_rejects_extra_field():
    with pytest.raises(ValidationError):
        DownloadSource.model_validate(
            {
                "transport": "download",
                "url": "https://x/y",
                "expiresAt": "2026-05-18T12:30:00Z",
                "secret": "no",
            }
        )


def test_download_source_single_use_explicit_false():
    s = DownloadSource(
        transport="download",
        url="https://example.com/d/Yk2p",
        expiresAt="2026-05-18T12:30:00Z",
        singleUse=False,
    )
    assert s.singleUse is False


def test_download_source_is_frozen():
    s = DownloadSource(
        transport="download",
        url="https://example.com/d/Yk2p",
        expiresAt="2026-05-18T12:30:00Z",
    )
    with pytest.raises(ValidationError):
        s.url = "https://other"  # type: ignore[misc]


def test_download_source_rejects_naive_expires_at():
    """``AwareDatetime`` blocks direct construction with a naive datetime.

    Selection (§9) will compare against ``datetime.now(timezone.utc)``;
    a naive ``expiresAt`` would raise ``TypeError`` at comparison time.
    Catching it at validation gives the caller a clean
    ``ValidationError`` instead.
    """
    naive = datetime(2026, 5, 18, 12, 30, 0)  # no tzinfo
    with pytest.raises(ValidationError):
        DownloadSource(
            transport="download",
            url="https://example.com/d/Yk2p",
            expiresAt=naive,
        )


def test_download_source_accepts_aware_expires_at_object():
    """A tz-aware ``datetime`` instance is accepted directly."""
    aware = datetime(2026, 5, 18, 12, 30, 0, tzinfo=timezone.utc)
    s = DownloadSource(
        transport="download",
        url="https://example.com/d/Yk2p",
        expiresAt=aware,
    )
    assert s.expiresAt.tzinfo is not None
