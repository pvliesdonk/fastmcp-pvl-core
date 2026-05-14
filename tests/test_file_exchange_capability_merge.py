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


def test_builder_legacy_shape_no_download_tool_omits_http_block() -> None:
    """Legacy v0.2 shape with only an upload (and no download) emits no http block.

    Upload cannot ride the flat v0.2 shape (no nested ``http.upload`` key),
    so without a download tool the http block is omitted entirely; the only
    transfer methods left would be ``exchange``. With nothing else set, the
    builder returns ``None``.
    """
    b = _FileExchangeCapabilityBuilder(
        namespace="ns",
        legacy_capability_shape=True,
    )
    b.set_upload(tool_name="create_upload_link", max_bytes=10, max_ttl_seconds=60)
    assert b.build() is None


def test_builder_upload_with_explicit_accepts_includes_accepts_in_capability() -> None:
    """Non-default ``accepts`` is reflected in the upload capability dict."""
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_upload(
        tool_name="create_upload_link",
        max_bytes=1000,
        max_ttl_seconds=300,
        accepts=("text/markdown", "application/octet-stream"),
    )
    cap = b.build()
    assert cap is not None
    upload = cap.to_capability_dict()["transfer_methods"]["http"]["upload"]
    assert upload["accepts"] == ["text/markdown", "application/octet-stream"]


def test_emit_capability_returns_none_when_no_builder_registered() -> None:
    """``_emit_capability`` is idempotent on a FastMCP that has no builder.

    Covers the early-out branch in ``_emit_capability``: a caller may
    invoke it on a FastMCP instance no registrar has touched (e.g. a
    transport-noop branch that still tries to publish), and the helper
    must return ``None`` rather than raising.
    """
    from fastmcp_pvl_core.file_exchange import (
        _BUILDER_ATTR,
        _emit_capability,
    )

    mcp = FastMCP(name="no-builder")
    # Sanity: builder is not yet attached to the instance.
    assert getattr(mcp, _BUILDER_ATTR, None) is None
    assert _emit_capability(mcp) is None


def test_builder_set_exchange_false_drops_exchange_method() -> None:
    """``set_exchange(False)`` is the explicit no-op path (toggle off after on)."""
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_exchange(True)
    b.set_exchange(False)
    # No download/upload either, so the builder should produce nothing.
    assert b.build() is None
    # And with a download tool added, exchange must be absent from the
    # transfer_methods even though set_exchange was called once with True.
    b.set_download(tool_name="create_download_link")
    cap = b.build()
    assert cap is not None
    assert "exchange" not in cap.to_capability_dict()["transfer_methods"]


def test_builder_is_attached_per_instance_not_module_level() -> None:
    """Capability builders live on the FastMCP instance, not in a global dict.

    The earlier implementation kept builders in a module-level
    ``_capability_builders`` dict keyed by ``id(mcp)``; CPython is free
    to reuse the id of a gc'd FastMCP for a future instance, which
    would alias unrelated capability state across tests. The current
    implementation stashes the builder as a private attribute on the
    FastMCP itself, so the builder's lifetime is tied to the instance.

    This test verifies (a) the attribute is set on the instance the
    registrar acted on, and (b) a freshly-constructed FastMCP starts
    out without the attribute — i.e. there is no shared global state
    leaking across instances.
    """
    from fastmcp_pvl_core.file_exchange import (
        _BUILDER_ATTR,
        _get_or_create_builder,
    )

    mcp_a = FastMCP(name="probe-a")
    _get_or_create_builder(mcp_a, namespace="ns-a")
    assert getattr(mcp_a, _BUILDER_ATTR, None) is not None
    assert mcp_a._pvl_file_exchange_builder.namespace == "ns-a"  # type: ignore[attr-defined]

    # A second, untouched FastMCP must not see ``mcp_a``'s state.
    mcp_b = FastMCP(name="probe-b")
    assert getattr(mcp_b, _BUILDER_ATTR, None) is None


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

    builder = mcp._pvl_file_exchange_builder  # type: ignore[attr-defined]
    cap = builder.build()
    assert cap is not None
    d = cap.to_capability_dict()

    assert d["version"] == "0.4"
    http = d["transfer_methods"]["http"]
    assert "download" in http and http["download"]["tool"] == "create_download_link"
    assert "upload" in http and http["upload"]["tool"] == "create_upload_link"
    # ``accepts`` is advertised verbatim — including the default
    # ``("*/*",)`` wildcard. Per Amendment 11 an absent ``accepts`` key
    # means the route inherits the server-wide ``consumes`` list, so a
    # wildcard route MUST advertise ``["*/*"]`` explicitly to avoid
    # misleading clients into thinking only ``consumes`` types are
    # accepted.
    assert http["upload"]["accepts"] == ["*/*"]
