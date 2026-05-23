"""Tests for the mechanism-agnostic artifact source/sink hook protocols."""

from __future__ import annotations

import inspect
from io import BytesIO

import pytest

from fastmcp_pvl_core._file_exchange import _hooks
from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


class _DummySource:
    async def open_artifact(self, key):  # noqa: ANN001, ANN202
        return BytesIO(b"hello"), ArtifactMetadata(name="a.bin")


class _DummySink:
    def __init__(self):
        self.stored: bytes | None = None

    async def store_artifact(self, artifact_id, metadata, stream):  # noqa: ANN001, ANN202
        self.stored = stream.read()


def test_source_runtime_checkable_matches_conforming():
    assert isinstance(_DummySource(), ArtifactSource)


def test_sink_runtime_checkable_matches_conforming():
    assert isinstance(_DummySink(), ArtifactSink)


def test_runtime_checkable_rejects_nonconforming():
    assert not isinstance(object(), ArtifactSource)
    assert not isinstance(object(), ArtifactSink)


async def test_source_returns_stream_and_metadata():
    stream, meta = await _DummySource().open_artifact("k")
    assert stream.read() == b"hello"
    assert meta.name == "a.bin"


async def test_sink_reads_stream_and_returns_none():
    sink = _DummySink()
    result = await sink.store_artifact(
        "id-1", ArtifactMetadata(name="x"), BytesIO(b"payload")
    )
    assert result is None
    assert sink.stored == b"payload"


# Transport / mechanism names that must never appear in a hook signature.
_FORBIDDEN_TOKENS = (
    "filesystem",
    "download",
    "upload",
    "http",
    "https",
    "exchange",
    "url",
    "volume",
)

# Protocol classes defined in _hooks (not imported ones like ArtifactMetadata):
_HOOK_PROTOCOLS = [
    obj
    for _name, obj in inspect.getmembers(_hooks, inspect.isclass)
    if obj.__module__ == _hooks.__name__
]


def _signature_tokens(method) -> list[str]:  # noqa: ANN001
    """Param names + annotation reprs + return annotation, as strings."""
    sig = inspect.signature(method)
    tokens: list[str] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        tokens.append(name)
        tokens.append(str(param.annotation))
    tokens.append(str(sig.return_annotation))
    return tokens


def _public_methods(cls):  # noqa: ANN001, ANN202
    return [
        m
        for name, m in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    ]


def test_hook_protocols_discovered():
    # Guard the guard: if discovery breaks (0 protocols), the parametrized
    # test below would pass vacuously. Pin that both hooks are found.
    names = {p.__name__ for p in _HOOK_PROTOCOLS}
    assert names == {"ArtifactSource", "ArtifactSink"}


@pytest.mark.parametrize("proto", _HOOK_PROTOCOLS, ids=lambda p: p.__name__)
def test_no_transport_name_in_hook_signatures(proto):
    for method in _public_methods(proto):
        for token in _signature_tokens(method):
            low = token.lower()
            for forbidden in _FORBIDDEN_TOKENS:
                assert forbidden not in low, (
                    f"{proto.__name__}.{method.__name__}: transport token "
                    f"{forbidden!r} leaked into the signature ({token!r})"
                )


def test_introspection_guard_has_teeth():
    # Negative control: a signature carrying a transport token is detectable
    # by the same token extraction, so a real leak could not pass silently.
    def bad(self, http_url: str) -> None: ...  # noqa: ANN001

    tokens = [t.lower() for t in _signature_tokens(bad)]
    assert any("http" in t for t in tokens)
