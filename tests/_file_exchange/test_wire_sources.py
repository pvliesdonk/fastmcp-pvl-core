"""Tests for FilesystemSource + DownloadSource descriptors."""

from __future__ import annotations

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
