"""Tests for capability-merge across the http / http_upload registrars.

Capability declarations use the v0.3.0 ``source``/``sink`` role-keyed
``transfer_methods`` shape (spec §"Transfer Methods").
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from typing import Any, BinaryIO

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    ResolvedSource,
    SinkContext,
    SourceHook,
    register_file_exchange,
    register_file_exchange_upload,
)
from fastmcp_pvl_core._file_exchange_protocol import (
    _FileExchangeCapabilityBuilder,
)


def _src(payload: bytes, content_type: str | None = None) -> SourceHook:
    """A sync ``SourceHook`` returning ``payload`` for any ``origin_id``."""

    def _hook(origin_id: str) -> ResolvedSource:
        return ResolvedSource(
            stream=io.BytesIO(payload),
            content_type=content_type,
            size_bytes=len(payload),
        )

    return _hook


async def _sink(stream: BinaryIO, ctx: SinkContext) -> Mapping[str, Any]:
    """An async ``SinkHook`` that drains the stream and reports the byte count."""
    data = stream.read()
    return {"stored": True, "bytes": len(data)}


def test_builder_http_source_only_emits_source_role() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_source(tool_name="create_download_link")
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["version"] == "0.5"
    assert d["transfer_methods"]["http"] == {
        "source": {"tool": "create_download_link"},
    }


def test_builder_http_sink_only_emits_sink_role() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_sink(tool_name="fetch_file")
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert d["transfer_methods"]["http"] == {"sink": {"tool": "fetch_file"}}


def test_builder_http_both_roles_emits_source_and_sink() -> None:
    """Dual-role guard: a producer-and-consumer server advertises both roles."""
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_source(tool_name="create_download_link")
    b.set_http_sink(tool_name="fetch_file")
    cap = b.build()
    assert cap is not None
    http = cap.to_capability_dict()["transfer_methods"]["http"]
    assert http == {
        "source": {"tool": "create_download_link"},
        "sink": {"tool": "fetch_file"},
    }


def test_builder_http_upload_sink_emits_under_own_method_key() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_upload_sink(
        tool_name="create_upload_link",
        max_bytes=10_000_000,
        max_ttl_seconds=300,
    )
    cap = b.build()
    assert cap is not None
    d = cap.to_capability_dict()
    assert "http" not in d["transfer_methods"]
    assert d["transfer_methods"]["http_upload"] == {
        "sink": {
            "tool": "create_upload_link",
            "max_bytes": 10_000_000,
            "max_ttl_seconds": 300,
        },
    }


def test_builder_http_upload_sink_includes_explicit_accepts() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_upload_sink(
        tool_name="create_upload_link",
        max_bytes=1000,
        max_ttl_seconds=300,
        accepts=("text/markdown", "application/octet-stream"),
    )
    cap = b.build()
    assert cap is not None
    sink = cap.to_capability_dict()["transfer_methods"]["http_upload"]["sink"]
    assert sink["accepts"] == ["text/markdown", "application/octet-stream"]


def test_builder_http_and_http_upload_are_separate_top_level_keys() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_source(tool_name="create_download_link")
    b.set_http_upload_sink(
        tool_name="create_upload_link",
        max_bytes=10,
        max_ttl_seconds=60,
    )
    cap = b.build()
    assert cap is not None
    tm = cap.to_capability_dict()["transfer_methods"]
    assert tm["http"] == {"source": {"tool": "create_download_link"}}
    assert tm["http_upload"]["sink"]["tool"] == "create_upload_link"


def test_builder_with_no_method_returns_none() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    assert b.build() is None


def test_builder_set_exchange_false_drops_exchange_method() -> None:
    """``set_exchange(False)`` is the explicit no-op path (toggle off after on)."""
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_exchange(True)
    b.set_exchange(False)
    # No http/http_upload either, so the builder produces nothing.
    assert b.build() is None
    # With a download tool added, exchange must be absent even though
    # set_exchange was called once with True.
    b.set_http_source(tool_name="create_download_link")
    cap = b.build()
    assert cap is not None
    assert "exchange" not in cap.to_capability_dict()["transfer_methods"]


def test_emit_capability_returns_none_when_no_builder_registered() -> None:
    """``_emit_capability`` is idempotent on a FastMCP that has no builder."""
    from fastmcp_pvl_core.file_exchange import _BUILDER_ATTR, _emit_capability

    mcp = FastMCP(name="no-builder")
    assert getattr(mcp, _BUILDER_ATTR, None) is None
    assert _emit_capability(mcp) is None


def test_builder_is_attached_per_instance_not_module_level() -> None:
    """Capability builders live on the FastMCP instance, not in a global dict."""
    from fastmcp_pvl_core.file_exchange import _BUILDER_ATTR, _get_or_create_builder

    mcp_a = FastMCP(name="probe-a")
    _get_or_create_builder(mcp_a, namespace="ns-a")
    assert getattr(mcp_a, _BUILDER_ATTR, None) is not None
    assert mcp_a._pvl_file_exchange_builder.namespace == "ns-a"  # type: ignore[attr-defined]

    mcp_b = FastMCP(name="probe-b")
    assert getattr(mcp_b, _BUILDER_ATTR, None) is None


def test_builder_http_upload_source_only() -> None:
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_upload_source(tool_name="upload")
    cap = b.build()
    assert cap is not None
    assert cap.to_capability_dict()["transfer_methods"]["http_upload"] == {
        "source": {"tool": "upload"},
    }


def test_builder_http_upload_both_roles() -> None:
    """A server that both sends and receives advertises both sub-keys."""
    b = _FileExchangeCapabilityBuilder(namespace="ns")
    b.set_http_upload_source(tool_name="upload")
    b.set_http_upload_sink(
        tool_name="create_upload_link", max_bytes=10, max_ttl_seconds=60
    )
    cap = b.build()
    assert cap is not None
    block = cap.to_capability_dict()["transfer_methods"]["http_upload"]
    assert block["source"] == {"tool": "upload"}
    assert block["sink"]["tool"] == "create_upload_link"


# ---------------------------------------------------------------------------
# Public-call-site e2e tests (register_file_exchange* → capability shape)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test runs with no file-exchange env vars set.

    The builder-unit tests above do not read env; the public-call-site
    tests below do. Clearing keeps both kinds hermetic and order-independent.
    """
    for var in (
        "MCP_EXCHANGE_DIR",
        "MCP_EXCHANGE_ID",
        "MCP_EXCHANGE_NAMESPACE",
        "FASTMCP_TRANSPORT",
        "TEST_FE_TRANSPORT",
        "TEST_FE_BASE_URL",
        "TEST_FE_FILE_EXCHANGE_ENABLED",
        "TEST_FE_FILE_EXCHANGE_PRODUCE",
        "TEST_FE_FILE_EXCHANGE_CONSUME",
        "TEST_FE_FILE_EXCHANGE_TTL",
        "TEST_UP_TRANSPORT",
        "TEST_UP_BASE_URL",
        "TEST_UP_UPLOAD_ENABLED",
        "TEST_UP_UPLOAD_MAX_BYTES",
        "TEST_UP_UPLOAD_TTL",
        "TEST_UP_UPLOAD_TTL_MAX",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_register_file_exchange_dual_role_advertises_http_source_and_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A produce-and-consume server advertises BOTH http roles.

    Regression guard for #86: ``register_file_exchange`` chained the
    consumer (``sink``) role assignment onto the producer branch as an
    ``elif``, so whenever the producer branch was taken a server doing
    both advertised only the producer (``source``) tool. #86 made the
    ``set_http_sink`` call an independent ``if consume:``. #86's own
    guard is at the builder-unit level
    (``test_builder_http_both_roles_emits_source_and_sink``); this is the
    integration-level twin, exercising the ``register_file_exchange``
    public call site where the fix actually lives.
    """
    monkeypatch.setenv("TEST_FE_TRANSPORT", "http")
    monkeypatch.setenv("TEST_FE_BASE_URL", "http://test.example")
    # FILE_EXCHANGE_PRODUCE / _CONSUME both default to "true", so the
    # producer side (with ``source`` supplied) and the consumer side
    # (with ``sink`` supplied) both activate without setting those env
    # vars explicitly.

    mcp = FastMCP(name="dual-role-probe")
    handle = register_file_exchange(
        mcp,
        namespace="image-mcp",
        env_prefix="TEST_FE",
        produces=("image/png",),
        source=_src(b"png-bytes", content_type="image/png"),
        sink=_sink,
    )

    assert handle.capability is not None
    http = handle.capability.to_capability_dict()["transfer_methods"]["http"]
    assert http == {
        "source": {"tool": "create_download_link"},
        "sink": {"tool": "fetch_file"},
    }


def test_register_file_exchange_upload_default_accepts_wildcard_on_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default ("*/*",) accepts reaches the http_upload.sink wire shape.

    Complements ``test_builder_http_upload_sink_includes_explicit_accepts``,
    which covers an *explicit* accepts value at the builder-unit level. This
    confirms the default propagates through the ``register_file_exchange_upload``
    public helper unchanged.
    """
    from fastmcp_pvl_core.file_exchange import _BUILDER_ATTR

    monkeypatch.setenv("TEST_UP_TRANSPORT", "http")
    monkeypatch.setenv("TEST_UP_BASE_URL", "http://test.example")

    mcp = FastMCP(name="upload-accepts-probe")
    register_file_exchange_upload(
        mcp,
        namespace="ns",
        env_prefix="TEST_UP",
        sink=_sink,
    )

    builder = getattr(mcp, _BUILDER_ATTR)
    cap = builder.build()
    assert cap is not None
    sink = cap.to_capability_dict()["transfer_methods"]["http_upload"]["sink"]
    assert sink["accepts"] == ["*/*"]
