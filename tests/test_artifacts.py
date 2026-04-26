"""Tests for :mod:`fastmcp_pvl_core._artifacts`."""

from __future__ import annotations

import dataclasses
import time

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    ArtifactStore,
    TokenRecord,
    get_artifact_store,
    set_artifact_store,
)


class TestTokenRecord:
    def test_is_frozen(self) -> None:
        record = TokenRecord(
            content=b"x",
            filename="x.txt",
            mime_type="text/plain",
            expires_at=0.0,
        )
        assert dataclasses.is_dataclass(record)
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.filename = "y.txt"  # type: ignore[misc]


class TestArtifactStore:
    def test_add_returns_token(self) -> None:
        store = ArtifactStore()
        token = store.add(b"hello", filename="a.txt", mime_type="text/plain")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_add_returns_unique_tokens(self) -> None:
        store = ArtifactStore()
        t1 = store.add(b"a", filename="a", mime_type="text/plain")
        t2 = store.add(b"b", filename="b", mime_type="text/plain")
        assert t1 != t2

    def test_pop_returns_data_and_removes_token(self) -> None:
        store = ArtifactStore()
        token = store.add(b"hello", filename="a.txt", mime_type="text/plain")
        record = store.pop(token)
        assert record is not None
        assert record.content == b"hello"
        assert record.filename == "a.txt"
        assert record.mime_type == "text/plain"
        # Second pop is idempotent — returns None.
        assert store.pop(token) is None

    def test_pop_unknown_token_returns_none(self) -> None:
        store = ArtifactStore()
        assert store.pop("nonexistent") is None

    def test_expired_tokens_are_purged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = ArtifactStore(ttl_seconds=1)
        token = store.add(b"x", filename="x", mime_type="application/octet-stream")
        original_time = time.time()
        monkeypatch.setattr(time, "time", lambda: original_time + 10)
        assert store.pop(token) is None

    def test_purge_runs_on_add(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Expired tokens should be pruned from memory when a new token is added."""
        store = ArtifactStore(ttl_seconds=1)
        t1 = store.add(b"one", filename="one.txt", mime_type="text/plain")
        original_time = time.time()
        monkeypatch.setattr(time, "time", lambda: original_time + 10)
        # Add a fresh token at the shifted time — the prior one should purge.
        store.add(b"two", filename="two.txt", mime_type="text/plain")
        # Internal store no longer retains t1.
        assert t1 not in store._records  # type: ignore[attr-defined]

    def test_custom_ttl(self) -> None:
        store = ArtifactStore(ttl_seconds=42.0)
        token = store.add(b"x", filename="x.txt", mime_type="text/plain")
        record = store.pop(token)
        assert record is not None
        # expires_at roughly now + 42 seconds
        assert record.expires_at > time.time()
        assert record.expires_at <= time.time() + 42.0 + 1.0

    def test_per_token_ttl_overrides_default(self) -> None:
        store = ArtifactStore(ttl_seconds=3600.0)
        before = time.time()
        token = store.add(
            b"x",
            filename="x.txt",
            mime_type="text/plain",
            ttl_seconds=5.0,
        )
        record = store.pop(token)
        assert record is not None
        # Must be ~5s, not 3600s — the per-token override wins.
        assert record.expires_at - before < 30.0

    def test_per_token_ttl_none_keeps_default(self) -> None:
        store = ArtifactStore(ttl_seconds=42.0)
        token = store.add(
            b"x",
            filename="x.txt",
            mime_type="text/plain",
            ttl_seconds=None,
        )
        record = store.pop(token)
        assert record is not None
        # Default TTL still applies when override is None.
        assert record.expires_at > time.time() + 30.0


class TestBuildUrl:
    def test_returns_url_when_base_url_configured(self) -> None:
        store = ArtifactStore(base_url="https://example.com")
        token = store.add(b"x", filename="x.txt", mime_type="text/plain")

        url = store.build_url(token)

        assert url == f"https://example.com/artifacts/{token}"

    def test_strips_trailing_slash_from_base_url(self) -> None:
        store = ArtifactStore(base_url="https://example.com/")

        url = store.build_url("abc")

        assert url == "https://example.com/artifacts/abc"

    def test_uses_custom_route_path(self) -> None:
        store = ArtifactStore(
            base_url="https://example.com",
            route_path="/files/{token}",
        )

        url = store.build_url("abc")

        assert url == "https://example.com/files/abc"

    def test_raises_when_base_url_unset(self) -> None:
        store = ArtifactStore()

        with pytest.raises(RuntimeError, match="base_url"):
            store.build_url("abc")

    def test_route_path_must_contain_token_placeholder(self) -> None:
        # Catching the misconfiguration at construction time prevents a
        # silent "URL doesn't actually point at any token" footgun.
        with pytest.raises(ValueError, match=r"\{token\}"):
            ArtifactStore(route_path="/files/static")


class TestPutEphemeral:
    def test_returns_url_pointing_at_stored_content(self) -> None:
        store = ArtifactStore(base_url="https://example.com")

        url = store.put_ephemeral(
            b"payload",
            content_type="application/octet-stream",
            filename="p.bin",
        )

        assert url.startswith("https://example.com/artifacts/")
        token = url.rsplit("/", 1)[-1]
        record = store.pop(token)
        assert record is not None
        assert record.content == b"payload"
        assert record.mime_type == "application/octet-stream"
        assert record.filename == "p.bin"

    def test_one_time_false_raises_not_implemented(self) -> None:
        store = ArtifactStore(base_url="https://example.com")

        with pytest.raises(NotImplementedError, match="one_time"):
            store.put_ephemeral(
                b"x",
                content_type="text/plain",
                filename="x.txt",
                one_time=False,
            )

    def test_per_token_ttl_passes_through(self) -> None:
        store = ArtifactStore(ttl_seconds=3600.0, base_url="https://example.com")
        before = time.time()

        url = store.put_ephemeral(
            b"x",
            content_type="text/plain",
            filename="x.txt",
            ttl_seconds=5.0,
        )

        token = url.rsplit("/", 1)[-1]
        record = store.pop(token)
        assert record is not None
        assert record.expires_at - before < 30.0

    def test_raises_when_base_url_unset(self) -> None:
        store = ArtifactStore()

        with pytest.raises(RuntimeError, match="base_url"):
            store.put_ephemeral(
                b"x",
                content_type="text/plain",
                filename="x.txt",
            )

    async def test_url_actually_resolves_via_register_route(self) -> None:
        # Round-trip: put_ephemeral builds a URL whose path is served by
        # register_route, and the bytes come back exactly once.
        from starlette.testclient import TestClient

        mcp = FastMCP("test")
        store = ArtifactStore(base_url="http://testserver")
        ArtifactStore.register_route(mcp, store)

        url = store.put_ephemeral(
            b"hello",
            content_type="text/plain",
            filename="hello.txt",
        )
        path = url[len("http://testserver") :]

        app = mcp.http_app()
        with TestClient(app) as client:
            resp1 = client.get(path)
            resp2 = client.get(path)

        assert resp1.status_code == 200
        assert resp1.content == b"hello"
        assert resp2.status_code == 404


class TestSingletonAccessor:
    def setup_method(self) -> None:
        # Reset the module-level singleton between tests so ordering
        # doesn't matter.
        from fastmcp_pvl_core import _artifacts

        _artifacts._artifact_store = None

    def teardown_method(self) -> None:
        from fastmcp_pvl_core import _artifacts

        _artifacts._artifact_store = None

    def test_get_before_set_raises(self) -> None:
        with pytest.raises(RuntimeError, match="set_artifact_store"):
            get_artifact_store()

    def test_set_then_get_returns_same_instance(self) -> None:
        store = ArtifactStore()
        set_artifact_store(store)

        assert get_artifact_store() is store

    def test_set_replaces_previous_store(self) -> None:
        store1 = ArtifactStore()
        store2 = ArtifactStore()
        set_artifact_store(store1)
        set_artifact_store(store2)

        assert get_artifact_store() is store2


class TestRegisterRoute:
    """HTTP behaviour via a real FastMCP instance."""

    async def test_handler_returns_404_for_unknown_token(self) -> None:
        from starlette.testclient import TestClient

        mcp = FastMCP("test")
        store = ArtifactStore()
        ArtifactStore.register_route(mcp, store)

        app = mcp.http_app()
        with TestClient(app) as client:
            resp = client.get("/artifacts/nonexistent")
        assert resp.status_code == 404

    async def test_handler_returns_content_and_invalidates(self) -> None:
        from starlette.testclient import TestClient

        mcp = FastMCP("test")
        store = ArtifactStore()
        ArtifactStore.register_route(mcp, store)

        token = store.add(
            b"hello world",
            filename="greeting.txt",
            mime_type="text/plain; charset=utf-8",
        )

        app = mcp.http_app()
        with TestClient(app) as client:
            resp = client.get(f"/artifacts/{token}")
            assert resp.status_code == 200
            assert resp.content == b"hello world"
            assert resp.headers["content-type"].startswith("text/plain")
            cd = resp.headers["content-disposition"]
            assert "attachment" in cd
            assert 'filename="greeting.txt"' in cd

            # Second request is 404 — token is single-use.
            resp2 = client.get(f"/artifacts/{token}")
        assert resp2.status_code == 404

    async def test_handler_returns_404_for_expired_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from starlette.testclient import TestClient

        mcp = FastMCP("test")
        store = ArtifactStore(ttl_seconds=1)
        ArtifactStore.register_route(mcp, store)

        token = store.add(b"x", filename="x.txt", mime_type="text/plain")
        original_time = time.time()
        monkeypatch.setattr(time, "time", lambda: original_time + 10)

        app = mcp.http_app()
        with TestClient(app) as client:
            resp = client.get(f"/artifacts/{token}")
        assert resp.status_code == 404

    async def test_register_route_custom_path(self) -> None:
        from starlette.testclient import TestClient

        mcp = FastMCP("test")
        store = ArtifactStore()
        ArtifactStore.register_route(mcp, store, path="/downloads/{token}")

        token = store.add(
            b"payload", filename="payload.bin", mime_type="application/octet-stream"
        )

        app = mcp.http_app()
        with TestClient(app) as client:
            resp = client.get(f"/downloads/{token}")
        assert resp.status_code == 200
        assert resp.content == b"payload"

    async def test_filename_with_quote_is_sanitised(self) -> None:
        """Quote/backslash/newline in filename must not break the header."""
        from starlette.testclient import TestClient

        mcp = FastMCP("test")
        store = ArtifactStore()
        ArtifactStore.register_route(mcp, store)

        token = store.add(
            b"data",
            filename='evil".txt\r\nX-Injected: yes',
            mime_type="text/plain",
        )

        app = mcp.http_app()
        with TestClient(app) as client:
            resp = client.get(f"/artifacts/{token}")
        assert resp.status_code == 200
        cd = resp.headers["content-disposition"]
        # No raw CR/LF in the header value.
        assert "\r" not in cd
        assert "\n" not in cd
        # No unescaped injection header leaked.
        assert "X-Injected" not in resp.headers
