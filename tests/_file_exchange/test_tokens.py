import asyncio
from datetime import datetime, timezone

import pytest
from key_value.aio.stores.memory import MemoryStore

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange import _tokens


def test_capability_url_joins_base_path_token():
    url = _tokens.capability_url("https://x.example.com", "/d", "abc123")
    assert url == "https://x.example.com/d/abc123"


def test_capability_url_normalizes_slashes():
    assert (
        _tokens.capability_url("https://x.example.com/", "/d/", "tok")
        == "https://x.example.com/d/tok"
    )


def test_capability_url_empty_path():
    assert _tokens.capability_url("https://x.example.com", "", "tok") == (
        "https://x.example.com/tok"
    )


def test_capability_url_requires_base_url():
    with pytest.raises(ConfigurationError):
        _tokens.capability_url("", "/d", "tok")


def test_capability_url_requires_https():
    with pytest.raises(ConfigurationError):
        _tokens.capability_url("http://x.example.com", "/d", "tok")


def test_capability_url_requires_nonempty_token():
    with pytest.raises(ValueError):
        _tokens.capability_url("https://x.example.com", "/d", "")


@pytest.fixture
def store() -> _tokens.CapabilityTokenStore:
    return _tokens.CapabilityTokenStore(MemoryStore(), ttl_ceiling=3600.0)


async def test_mint_returns_urlsafe_token_and_expiry(store):
    minted = await store.mint({"k": "v"}, ttl=60.0)
    assert isinstance(minted.token, str)
    assert len(minted.token) >= 43
    assert all(c.isalnum() or c in "-_" for c in minted.token)
    delta = (minted.expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 55 <= delta <= 61


async def test_mint_clamps_ttl_to_ceiling(store):
    minted = await store.mint({"k": "v"}, ttl=10_000.0)
    delta = (minted.expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 3595 <= delta <= 3601


async def test_mint_rejects_nonpositive_ttl(store):
    with pytest.raises(ValueError):
        await store.mint({"k": "v"}, ttl=0.0)


async def test_lookup_round_trips_metadata_and_single_use(store):
    minted = await store.mint({"artifact": "a1", "n": 7}, ttl=60.0, single_use=False)
    record = await store.lookup(minted.token)
    assert record is not None
    assert record.metadata == {"artifact": "a1", "n": 7}
    assert record.single_use is False


async def test_lookup_absent_token_returns_none(store):
    assert await store.lookup("nope") is None


async def test_lookup_returns_none_after_expiry(store):
    minted = await store.mint({"k": "v"}, ttl=0.05)
    await asyncio.sleep(0.12)
    assert await store.lookup(minted.token) is None


async def test_mint_default_single_use_is_true(store):
    minted = await store.mint({"k": "v"}, ttl=60.0)
    record = await store.lookup(minted.token)
    assert record is not None
    assert record.single_use is True


def test_store_rejects_nonpositive_ttl_ceiling():
    with pytest.raises(ValueError):
        _tokens.CapabilityTokenStore(MemoryStore(), ttl_ceiling=0.0)


async def test_single_use_consume_invalidates(store):
    minted = await store.mint({"k": "v"}, ttl=60.0, single_use=True)
    assert await store.lookup(minted.token) is not None
    assert await store.consume(minted.token) is True
    assert await store.lookup(minted.token) is None


async def test_double_consume_returns_false(store):
    minted = await store.mint({"k": "v"}, ttl=60.0, single_use=True)
    assert await store.consume(minted.token) is True
    assert await store.consume(minted.token) is False


async def test_consume_absent_token_returns_false(store):
    assert await store.consume("nope") is False


async def test_consume_returns_false_after_expiry(store):
    minted = await store.mint({"k": "v"}, ttl=0.05, single_use=True)
    await asyncio.sleep(0.12)
    assert await store.consume(minted.token) is False


async def test_multi_use_consume_is_noop(store):
    minted = await store.mint({"k": "v"}, ttl=60.0, single_use=False)
    assert await store.consume(minted.token) is True
    assert await store.lookup(minted.token) is not None
    # second consume: still a no-op, token survives
    assert await store.consume(minted.token) is True
    assert await store.lookup(minted.token) is not None


async def test_revoke_invalidates_unconditionally(store):
    minted = await store.mint({"k": "v"}, ttl=60.0, single_use=False)
    await store.revoke(minted.token)
    assert await store.lookup(minted.token) is None


async def test_revoke_absent_token_does_not_raise(store):
    await store.revoke("nope")  # no exception


async def test_revoke_single_use_token(store):
    minted = await store.mint({"k": "v"}, ttl=60.0, single_use=True)
    await store.revoke(minted.token)
    assert await store.lookup(minted.token) is None


async def test_build_capability_token_store_from_config():
    from fastmcp_pvl_core import ServerConfig

    config = ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=120.0)
    built = _tokens.build_capability_token_store(config)
    assert isinstance(built, _tokens.CapabilityTokenStore)
    # ceiling threaded from config: a 9999s request clamps to ~120s
    minted = await built.mint({"k": "v"}, ttl=9999.0)
    delta = (minted.expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 115 <= delta <= 121
    # round-trips through the built store
    assert (await built.lookup(minted.token)) is not None
