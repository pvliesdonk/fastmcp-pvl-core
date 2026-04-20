"""Tests for :mod:`fastmcp_pvl_core._artifacts`."""

from __future__ import annotations

import dataclasses
import time

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import ArtifactStore, TokenRecord


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
