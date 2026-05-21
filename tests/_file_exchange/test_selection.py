"""Tests for select_source / select_sink — the §9 selection algorithm."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fastmcp_pvl_core._file_exchange._selection import (
    select_sink,
    select_source,
)
from fastmcp_pvl_core._file_exchange._wire import (
    DownloadSource,
    FilesystemSink,
    FilesystemSource,
    IntakeTicket,
    TransferHandle,
    UploadSink,
)

# Anchor time used across the tolerance tests so they don't drift with
# wall-clock-derived `now()` differences between assertions.
_NOW = datetime(2026, 5, 21, 19, 0, 0, tzinfo=timezone.utc)


def _handle(*sources: dict) -> TransferHandle:
    return TransferHandle.from_wire(
        {
            "type": "nl.liesdonk.file-exchange/transfer-handle",
            "version": "0.1",
            "artifact": {"name": "x.bin"},
            "sources": list(sources),
        }
    )


def _ticket(*sinks: dict) -> IntakeTicket:
    return IntakeTicket.from_wire(
        {
            "type": "nl.liesdonk.file-exchange/intake-ticket",
            "version": "0.1",
            "artifactId": "art-1",
            "sinks": list(sinks),
        }
    )


# --- select_source ---


def test_select_source_skips_unknown_transport_only():
    """A handle with only an unknown-transport source returns None."""
    handle = _handle({"transport": "future-thing", "url": "x://y"})
    assert select_source(handle) is None


def test_select_source_skips_expired_download_beyond_tolerance():
    """``expiresAt`` 60s in the past is well past the 30s tolerance."""
    expired = (_NOW - timedelta(seconds=60)).isoformat()
    handle = _handle(
        {
            "transport": "download",
            "url": "https://x/y",
            "expiresAt": expired,
        }
    )
    assert select_source(handle, now=_NOW) is None


def test_select_source_selects_download_within_tolerance():
    """``expiresAt`` 5s in the past is inside the 30s tolerance — selected."""
    almost_expired = (_NOW - timedelta(seconds=5)).isoformat()
    handle = _handle(
        {
            "transport": "download",
            "url": "https://x/y",
            "expiresAt": almost_expired,
        }
    )
    chosen = select_source(handle, now=_NOW)
    assert isinstance(chosen, DownloadSource)


def test_select_source_skips_download_just_past_tolerance():
    """``expiresAt`` 35s in the past is past the 30s tolerance."""
    past = (_NOW - timedelta(seconds=35)).isoformat()
    handle = _handle(
        {
            "transport": "download",
            "url": "https://x/y",
            "expiresAt": past,
        }
    )
    assert select_source(handle, now=_NOW) is None


def test_select_source_selects_download_at_exact_tolerance_boundary():
    """``expiresAt`` exactly ``now - 30s`` is selected (strict ``<`` boundary).

    Pins the inclusive/exclusive choice: the check is
    ``expiresAt < now - tolerance``, so a descriptor at exactly the 30s
    mark is NOT skipped. A refactor to ``<=`` would flip this and only
    this test would catch it.
    """
    at_boundary = (_NOW - timedelta(seconds=30)).isoformat()
    handle = _handle(
        {
            "transport": "download",
            "url": "https://x/y",
            "expiresAt": at_boundary,
        }
    )
    assert isinstance(select_source(handle, now=_NOW), DownloadSource)


def test_select_source_does_not_consult_callback_for_download():
    """The accessibility callback is only for filesystem descriptors.

    A download-only handle must never invoke ``is_accessible`` (HTTPS
    reachability is a transfer-time concern, not selection-time).
    """
    calls: list[object] = []
    future = (_NOW + timedelta(minutes=5)).isoformat()
    handle = _handle(
        {"transport": "download", "url": "https://x/y", "expiresAt": future}
    )
    select_source(handle, is_accessible=lambda d: calls.append(d) or True, now=_NOW)
    assert calls == []


def test_select_source_selects_future_download():
    """``expiresAt`` in the future — selected."""
    future = (_NOW + timedelta(minutes=5)).isoformat()
    handle = _handle(
        {
            "transport": "download",
            "url": "https://x/y",
            "expiresAt": future,
        }
    )
    assert isinstance(select_source(handle, now=_NOW), DownloadSource)


def test_select_source_skips_filesystem_when_callback_returns_false():
    handle = _handle({"transport": "filesystem", "uri": "exchange://v/a"})
    assert select_source(handle, is_accessible=lambda d: False) is None


def test_select_source_selects_filesystem_when_callback_returns_true():
    handle = _handle({"transport": "filesystem", "uri": "exchange://v/a"})
    chosen = select_source(handle, is_accessible=lambda d: True)
    assert isinstance(chosen, FilesystemSource)


def test_select_source_skips_all_filesystem_when_callback_is_none():
    """``is_accessible=None`` means party does not support filesystem at all."""
    handle = _handle({"transport": "filesystem", "uri": "exchange://v/a"})
    assert select_source(handle) is None


def test_select_source_returns_first_surviving_in_array_order():
    """§9: iterate in order, return the first descriptor that survives."""
    expired = (_NOW - timedelta(minutes=1)).isoformat()
    future = (_NOW + timedelta(minutes=5)).isoformat()
    handle = _handle(
        {"transport": "download", "url": "https://x/y", "expiresAt": expired},
        {"transport": "filesystem", "uri": "exchange://v/a"},
        {"transport": "download", "url": "https://x/z", "expiresAt": future},
    )
    chosen = select_source(handle, is_accessible=lambda d: True, now=_NOW)
    # The filesystem source is the first survivor, not the future download.
    assert isinstance(chosen, FilesystemSource)


def test_select_source_callback_receives_typed_filesystem_source():
    """The callback gets the typed descriptor, not just a URI."""
    seen: list[FilesystemSource] = []

    def cb(src: FilesystemSource) -> bool:
        seen.append(src)
        return True

    handle = _handle({"transport": "filesystem", "uri": "exchange://v/a"})
    select_source(handle, is_accessible=cb)
    assert len(seen) == 1
    assert isinstance(seen[0], FilesystemSource)
    assert seen[0].uri == "exchange://v/a"


def test_select_source_now_overrides_wall_clock():
    """The ``now`` parameter is the reference point for tolerance arithmetic."""
    # An "expired" descriptor relative to a fictitious far-future ``now``.
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    handle = _handle(
        {
            "transport": "download",
            "url": "https://x/y",
            "expiresAt": _NOW.isoformat(),  # in 2026
        }
    )
    # Relative to wall clock the descriptor may or may not be expired
    # right now; relative to ``far_future`` it's definitely past tolerance.
    assert select_source(handle, now=far_future) is None


def test_select_source_empty_when_no_descriptor_survives():
    """Mix of expired + filesystem-without-callback returns None."""
    expired = (_NOW - timedelta(minutes=1)).isoformat()
    handle = _handle(
        {"transport": "download", "url": "https://x/y", "expiresAt": expired},
        {"transport": "filesystem", "uri": "exchange://v/a"},
    )
    # is_accessible omitted → filesystem skipped; download expired → skipped.
    assert select_source(handle, now=_NOW) is None


# --- select_sink (symmetric) ---


def test_select_sink_skips_expired_upload_beyond_tolerance():
    expired = (_NOW - timedelta(seconds=60)).isoformat()
    ticket = _ticket(
        {
            "transport": "upload",
            "url": "https://x/y",
            "expiresAt": expired,
        }
    )
    assert select_sink(ticket, now=_NOW) is None


def test_select_sink_selects_upload_within_tolerance():
    almost_expired = (_NOW - timedelta(seconds=5)).isoformat()
    ticket = _ticket(
        {
            "transport": "upload",
            "url": "https://x/y",
            "expiresAt": almost_expired,
        }
    )
    chosen = select_sink(ticket, now=_NOW)
    assert isinstance(chosen, UploadSink)


def test_select_sink_skips_upload_past_tolerance():
    past = (_NOW - timedelta(seconds=35)).isoformat()
    ticket = _ticket(
        {
            "transport": "upload",
            "url": "https://x/y",
            "expiresAt": past,
        }
    )
    assert select_sink(ticket, now=_NOW) is None


def test_select_sink_selects_filesystem_when_callback_returns_true():
    ticket = _ticket({"transport": "filesystem", "uri": "exchange://v/in"})
    chosen = select_sink(ticket, is_accessible=lambda d: True)
    assert isinstance(chosen, FilesystemSink)


def test_select_sink_skips_filesystem_when_callback_is_none():
    ticket = _ticket({"transport": "filesystem", "uri": "exchange://v/in"})
    assert select_sink(ticket) is None


def test_select_sink_returns_first_surviving_in_array_order():
    expired = (_NOW - timedelta(minutes=1)).isoformat()
    future = (_NOW + timedelta(minutes=5)).isoformat()
    ticket = _ticket(
        {"transport": "upload", "url": "https://x/y", "expiresAt": expired},
        {"transport": "filesystem", "uri": "exchange://v/in"},
        {"transport": "upload", "url": "https://x/z", "expiresAt": future},
    )
    chosen = select_sink(ticket, is_accessible=lambda d: True, now=_NOW)
    assert isinstance(chosen, FilesystemSink)


def test_select_sink_callback_receives_typed_filesystem_sink():
    seen: list[FilesystemSink] = []

    def cb(s: FilesystemSink) -> bool:
        seen.append(s)
        return True

    ticket = _ticket({"transport": "filesystem", "uri": "exchange://v/in"})
    select_sink(ticket, is_accessible=cb)
    assert len(seen) == 1
    assert isinstance(seen[0], FilesystemSink)


# --- shared structural ---


@pytest.mark.parametrize(
    "selector_fn,reference_fn,unknown_descriptor",
    [
        (select_source, _handle, {"transport": "future-src", "url": "x://y"}),
        (select_sink, _ticket, {"transport": "future-sink", "url": "x://y"}),
    ],
)
def test_unknown_transport_always_skipped(
    selector_fn, reference_fn, unknown_descriptor
):
    """Forward-compat fallthrough: party never selects an unknown transport."""
    ref = reference_fn(unknown_descriptor)
    assert selector_fn(ref) is None
