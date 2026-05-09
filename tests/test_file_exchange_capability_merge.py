"""Tests for capability-merge across download/upload registrars."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._file_exchange_protocol import (
    _FileExchangeCapabilityBuilder,
)


def test_builder_download_only_emits_flat_http_in_legacy_shape() -> None:
    b = _FileExchangeCapabilityBuilder(
        namespace="ns",
        legacy_capability_shape=True,
    )
    b.set_download(tool_name="create_download_link")
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["version"] == "0.2"
    assert d["transfer_methods"]["http"] == {"tool": "create_download_link"}


def test_builder_download_only_emits_nested_http_in_v04_shape() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_download(tool_name="create_download_link")
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["version"] == "0.4"
    assert d["transfer_methods"]["http"] == {
        "download": {"tool": "create_download_link"},
    }


def test_builder_upload_only_emits_nested_http_with_upload_only() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_upload(
        tool_name="create_upload_link",
        max_bytes=10_000_000,
        max_ttl_seconds=300,
    )
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["transfer_methods"]["http"] == {
        "upload": {
            "tool": "create_upload_link",
            "max_bytes": 10_000_000,
            "max_ttl_seconds": 300,
        },
    }


def test_builder_both_directions_merge_under_single_http_block() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_download(tool_name="create_download_link")
    b.set_upload(tool_name="create_upload_link", max_bytes=10, max_ttl_seconds=60)
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    http = d["transfer_methods"]["http"]
    assert set(http) == {"download", "upload"}
    assert http["download"]["tool"] == "create_download_link"
    assert http["upload"]["tool"] == "create_upload_link"


def test_builder_both_directions_in_legacy_shape_keeps_only_download_http(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    b = _FileExchangeCapabilityBuilder(
        namespace="ns",
        legacy_capability_shape=True,
    )
    b.set_download(tool_name="create_download_link")
    with caplog.at_level(logging.WARNING):
        b.set_upload(tool_name="create_upload_link", max_bytes=10, max_ttl_seconds=60)
        cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["version"] == "0.2"
    # In legacy shape, the http block is the flat tool: <name>; upload
    # cannot ride along (there's no nested upload key in v0.2). The
    # builder logs a warning but still emits a download-only flat shape.
    assert d["transfer_methods"]["http"] == {"tool": "create_download_link"}
    # The warning was emitted at some point during set_upload + build.
    assert any(
        "legacy" in r.getMessage().lower() or "v0.2" in r.getMessage()
        for r in caplog.records
    )


def test_builder_with_neither_direction_returns_none() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    assert b.build() is None


@pytest.mark.asyncio
async def test_register_both_directions_emits_merged_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: both registrars on the same FastMCP merge into one capability.

    Exercises the full public path — ``register_file_exchange`` (download
    direction) followed by ``register_file_exchange_upload`` (upload
    direction) on a shared :class:`FastMCP` instance — and confirms that
    the per-id-of-mcp builder accumulates both contributions and emits a
    single ``transfer_methods.http`` block carrying nested ``download``
    and ``upload`` keys.
    """
    monkeypatch.setenv("TEST_DUAL_TRANSPORT", "http")
    monkeypatch.setenv("TEST_DUAL_BASE_URL", "http://srv.test")
    # ``MCP_EXCHANGE_DIR`` set-but-empty raises FileExchangeConfigError
    # (see _file_exchange_runtime.FileExchange.from_env). Unset it so the
    # test focuses on the http-direction merge.
    monkeypatch.delenv("MCP_EXCHANGE_DIR", raising=False)

    from fastmcp_pvl_core import (
        register_file_exchange,
        register_file_exchange_upload,
    )

    mcp = FastMCP(name="dual")
    register_file_exchange(
        mcp,
        namespace="ns",
        env_prefix="TEST_DUAL",
        produces=["image/png"],
    )
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_DUAL",
        receiver=lambda rec, body: {"ok": True},
    )

    from fastmcp_pvl_core.file_exchange import _capability_builders

    builder = _capability_builders[id(mcp)]
    cap = builder.build()
    assert cap is not None
    d = cap.to_capability_dict()

    assert d["version"] == "0.4"
    http = d["transfer_methods"]["http"]
    assert "download" in http and http["download"]["tool"] == "create_download_link"
    assert "upload" in http and http["upload"]["tool"] == "create_upload_link"
