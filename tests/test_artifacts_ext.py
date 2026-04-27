"""Tests for the additive ergonomics on :class:`ArtifactStore`.

The existing core API (``add`` / ``pop`` / ``register_route``) is covered
by ``test_artifacts.py``; this file covers the four extensions added to
support the file-exchange facade:

- per-token TTL on :meth:`ArtifactStore.add`
- ``base_url`` / ``route_path`` on :meth:`ArtifactStore.__init__` plus
  :meth:`ArtifactStore.build_url`
- :meth:`ArtifactStore.put_ephemeral` end-to-end through
  :meth:`ArtifactStore.register_route`
- module-level :func:`set_artifact_store` / :func:`get_artifact_store`
  singleton accessor
"""

from __future__ import annotations

import time

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import ArtifactStore, get_artifact_store, set_artifact_store


class TestPerTokenTTL:
    def test_per_token_ttl_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Default would expire in 1s; per-token override sets it to 60s.
        store = ArtifactStore(ttl_seconds=1)
        token = store.add(
            b"x", filename="x.txt", mime_type="text/plain", ttl_seconds=60
        )
        original = time.time()
        # Move past the default TTL but well before the per-token one.
        monkeypatch.setattr(time, "time", lambda: original + 5)
        record = store.pop(token)
        assert record is not None
        assert record.content == b"x"

    def test_per_token_ttl_can_be_shorter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Default would not expire for an hour; per-token override makes it 1s.
        store = ArtifactStore(ttl_seconds=3600)
        token = store.add(b"x", filename="x.txt", mime_type="text/plain", ttl_seconds=1)
        original = time.time()
        monkeypatch.setattr(time, "time", lambda: original + 5)
        assert store.pop(token) is None

    def test_default_ttl_used_when_none_passed(self) -> None:
        store = ArtifactStore(ttl_seconds=42.0)
        token = store.add(b"x", filename="x.txt", mime_type="text/plain")
        record = store.pop(token)
        assert record is not None
        # Within ±1s of "now + 42".
        assert abs(record.expires_at - (time.time() + 42.0)) < 1.0


class TestBuildURL:
    def test_build_url_default_path(self) -> None:
        store = ArtifactStore(base_url="https://mcp.example.com")
        url = store.build_url("abc123")
        assert url == "https://mcp.example.com/artifacts/abc123"

    def test_build_url_strips_trailing_slash(self) -> None:
        store = ArtifactStore(base_url="https://mcp.example.com/")
        assert store.build_url("abc") == "https://mcp.example.com/artifacts/abc"

    def test_build_url_custom_route_path(self) -> None:
        store = ArtifactStore(
            base_url="https://mcp.example.com",
            route_path="/downloads/{token}/file",
        )
        url = store.build_url("xyz")
        assert url == "https://mcp.example.com/downloads/xyz/file"

    def test_build_url_without_base_url_raises(self) -> None:
        store = ArtifactStore()
        with pytest.raises(RuntimeError, match="base_url is required"):
            store.build_url("abc")

    def test_route_path_must_contain_token_placeholder(self) -> None:
        with pytest.raises(ValueError, match=r"\{token\}"):
            ArtifactStore(route_path="/no-placeholder")


class TestPutEphemeral:
    async def test_put_ephemeral_round_trip_via_register_route(self) -> None:
        from starlette.testclient import TestClient

        mcp = FastMCP("test")
        store = ArtifactStore(base_url="http://testserver")
        ArtifactStore.register_route(mcp, store)

        url = store.put_ephemeral(
            b"hello",
            content_type="text/plain; charset=utf-8",
            filename="hello.txt",
        )
        assert url.startswith("http://testserver/artifacts/")

        app = mcp.http_app()
        with TestClient(app) as client:
            # Use the path portion since TestClient is anchored at the test host.
            path = url.replace("http://testserver", "")
            resp = client.get(path)
        assert resp.status_code == 200
        assert resp.content == b"hello"
        assert resp.headers["content-type"].startswith("text/plain")
        assert 'filename="hello.txt"' in resp.headers["content-disposition"]

    async def test_put_ephemeral_is_one_time(self) -> None:
        from starlette.testclient import TestClient

        mcp = FastMCP("test")
        store = ArtifactStore(base_url="http://testserver")
        ArtifactStore.register_route(mcp, store)

        url = store.put_ephemeral(
            b"data", content_type="application/octet-stream", filename="d.bin"
        )
        path = url.replace("http://testserver", "")

        app = mcp.http_app()
        with TestClient(app) as client:
            assert client.get(path).status_code == 200
            assert client.get(path).status_code == 404

    def test_put_ephemeral_per_token_ttl_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = ArtifactStore(ttl_seconds=1, base_url="http://testserver")
        url = store.put_ephemeral(
            b"x",
            content_type="text/plain",
            filename="x.txt",
            ttl_seconds=60,
        )
        token = url.rsplit("/", 1)[-1]
        original = time.time()
        monkeypatch.setattr(time, "time", lambda: original + 5)
        # Per-token TTL of 60s wins over the store's default of 1s.
        assert store.pop(token) is not None

    def test_put_ephemeral_without_base_url_raises(self) -> None:
        store = ArtifactStore()
        with pytest.raises(RuntimeError, match="base_url is required"):
            store.put_ephemeral(b"x", content_type="text/plain", filename="x.txt")


class TestSingleton:
    def setup_method(self) -> None:
        # Each test starts with no store installed.
        set_artifact_store(None)

    def teardown_method(self) -> None:
        set_artifact_store(None)

    def test_get_before_set_raises(self) -> None:
        with pytest.raises(RuntimeError, match="not set"):
            get_artifact_store()

    def test_set_and_get(self) -> None:
        store = ArtifactStore()
        set_artifact_store(store)
        assert get_artifact_store() is store

    def test_set_replaces_existing(self) -> None:
        first = ArtifactStore()
        second = ArtifactStore()
        set_artifact_store(first)
        set_artifact_store(second)
        assert get_artifact_store() is second

    def test_set_none_clears(self) -> None:
        set_artifact_store(ArtifactStore())
        set_artifact_store(None)
        with pytest.raises(RuntimeError):
            get_artifact_store()
