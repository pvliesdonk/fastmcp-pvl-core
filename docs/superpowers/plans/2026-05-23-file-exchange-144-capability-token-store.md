# File-Exchange #144 — Capability Token Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A high-entropy capability-token minter + a mint/lookup/consume/revoke token store built on the #122 `build_kv_store` factory, consumed by the #145/#146 data planes.

**Architecture:** A `CapabilityTokenStore` wraps an `AsyncKeyValue` (from `build_kv_store(config, namespace="file-exchange-tokens")`) and a TTL ceiling. Expiry is delegated to the KV layer (`put(ttl=…)` + `get`→`None` after expiry); single-use is enforced by an atomic `delete` whose bool gives at-most-once. The token is the KV key (`secrets.token_urlsafe(32)`); the per-token `metadata` is an opaque dict the store never interprets. Two env config fields land on `ServerConfig`; a `capability_url` helper joins `base_url`+path+token with §12 https enforcement.

**Tech Stack:** Python 3.10+, `secrets`, `py-key-value-aio` `AsyncKeyValue` (via `build_kv_store`), frozen dataclasses, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`).

**Design doc:** `docs/superpowers/specs/2026-05-23-file-exchange-144-capability-token-store-design.md`

---

## File Structure

- **Modify** `src/fastmcp_pvl_core/_config.py` — add `file_exchange_token_ttl: float = 3600.0` and `file_exchange_max_artifact_size: int | None = None` fields + `from_env` parsing.
- **Create** `src/fastmcp_pvl_core/_file_exchange/_tokens.py` — `MintedToken`, `TokenRecord`, `capability_url`, `CapabilityTokenStore`, `build_capability_token_store`.
- **Modify** `src/fastmcp_pvl_core/_file_exchange/__init__.py` — re-export the five public names.
- **Modify** `src/fastmcp_pvl_core/file_exchange.py` — mirror the re-exports.
- **Modify** the config test file (`tests/test_config.py` — confirm with `grep -rln "from_env" tests/`) — the two new fields.
- **Create** `tests/_file_exchange/test_tokens.py` — token store + helper tests.

**Local checks (after each task, and before the final commit):**
```bash
uv run pytest tests/_file_exchange/ tests/test_config.py -q
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```

**AsyncKeyValue API** (already verified against the installed `py-key-value-aio`):
- `await store.put(key, value: Mapping[str, Any], *, collection=None, ttl: float | None = None) -> None` — `ttl` MUST be positive (a `ttl<=0` raises in the backend).
- `await store.get(key, *, collection=None) -> dict[str, Any] | None` — `None` after expiry/absence.
- `await store.delete(key, *, collection=None) -> bool` — `True` if the key existed (atomic; gives at-most-once).
- `await store.ttl(key, *, collection=None) -> tuple[dict | None, float | None]` — value + remaining seconds (used in tests to assert the clamp without waiting).

---

## Task 1: `ServerConfig` env fields

**Files:**
- Modify: `src/fastmcp_pvl_core/_config.py`
- Test: the config test file (find via `grep -rln "from_env" tests/` — likely `tests/test_config.py`)

- [ ] **Step 1: Write the failing tests**

Append to the config test file (use its existing import of `ServerConfig`; if it uses `monkeypatch.setenv`, match that style):

```python
def test_from_env_file_exchange_token_ttl_default_and_override(monkeypatch):
    from fastmcp_pvl_core import ServerConfig

    cfg = ServerConfig.from_env("MYAPP")
    assert cfg.file_exchange_token_ttl == 3600.0

    monkeypatch.setenv("MYAPP_FILE_EXCHANGE_TOKEN_TTL", "900")
    cfg2 = ServerConfig.from_env("MYAPP")
    assert cfg2.file_exchange_token_ttl == 900.0


def test_from_env_file_exchange_max_artifact_size(monkeypatch):
    from fastmcp_pvl_core import ServerConfig

    cfg = ServerConfig.from_env("MYAPP")
    assert cfg.file_exchange_max_artifact_size is None

    monkeypatch.setenv("MYAPP_FILE_EXCHANGE_MAX_ARTIFACT_SIZE", "1048576")
    cfg2 = ServerConfig.from_env("MYAPP")
    assert cfg2.file_exchange_max_artifact_size == 1048576
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k "file_exchange" -v`
Expected: FAIL — `ServerConfig` has no attribute `file_exchange_token_ttl`.

- [ ] **Step 3: Add the dataclass fields**

In `src/fastmcp_pvl_core/_config.py`, add these two fields to the `ServerConfig` dataclass body, immediately after the existing `event_store_url` / `app_domain` fields (keep them grouped with the other storage/config fields; match the file's field style):

```python
    # File-exchange capability-token config (#144). token_ttl is the TTL
    # *ceiling* the token store clamps mint() requests to; max_artifact_size
    # is consumed by the #146 upload route, surfaced here per #144's scope.
    file_exchange_token_ttl: float = 3600.0
    file_exchange_max_artifact_size: int | None = None
```

- [ ] **Step 4: Parse them in `from_env`**

In `from_env`, before the `return cls(...)`, add the parsing:

```python
        token_ttl_str = env(env_prefix, "FILE_EXCHANGE_TOKEN_TTL", "3600")
        max_size_raw = env(env_prefix, "FILE_EXCHANGE_MAX_ARTIFACT_SIZE")
```

and add these two keyword args inside the `cls(...)` call (alongside the other fields):

```python
            file_exchange_token_ttl=float(token_ttl_str),
            file_exchange_max_artifact_size=(
                int(max_size_raw) if max_size_raw else None
            ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -k "file_exchange" -q`
Expected: PASS. Then `uv run mypy src` — clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_config.py tests/test_config.py
git commit -m "feat(file-exchange): ServerConfig token-ttl + max-artifact-size env fields (#144)"
```

---

## Task 2: `MintedToken`, `TokenRecord`, `capability_url`

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_tokens.py`
- Test: `tests/_file_exchange/test_tokens.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/_file_exchange/test_tokens.py`:

```python
import pytest

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_tokens.py -v`
Expected: FAIL — module `_tokens` does not exist.

- [ ] **Step 3: Create the module with the dataclasses + helper**

Create `src/fastmcp_pvl_core/_file_exchange/_tokens.py` with exactly this (minimal-imports-per-task: only what Task 2 uses; Task 3 adds the rest):

```python
"""Capability-token minter and token store for the file-exchange data plane.

A :class:`CapabilityTokenStore` mints high-entropy URL-safe tokens (§12) and
runs their mint/lookup/consume/revoke lifecycle on the unified
``build_kv_store`` factory (#122) — no new storage abstraction. Expiry is
delegated to the KV layer's TTL; single-use is enforced by an atomic
``delete``. The per-token ``metadata`` is opaque (the store never interprets
it); the download (#145) and upload (#146) data planes own the metadata shape,
the routes, and the full-URL assembly. See
``docs/superpowers/specs/2026-05-23-file-exchange-144-capability-token-store-design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastmcp_pvl_core._errors import ConfigurationError

# Token byte length: 32 bytes = 256 bits, well above §12's ≥128-bit floor.
_TOKEN_BYTES = 32

# Collection name within the (already namespace-prefixed) KV store.
_COLLECTION = "tokens"


@dataclass(frozen=True)
class MintedToken:
    """Result of :meth:`CapabilityTokenStore.mint`.

    ``expires_at`` reflects the *clamped* TTL (a caller cannot otherwise
    observe the ceiling clamp), so a route can build a descriptor's
    ``expiresAt`` directly from it. tz-aware UTC, matching the wire models'
    ``AwareDatetime`` fields.
    """

    token: str
    expires_at: datetime


@dataclass(frozen=True)
class TokenRecord:
    """A looked-up token's stored state. ``metadata`` is opaque to the store."""

    metadata: dict[str, Any]
    single_use: bool


def capability_url(base_url: str, path: str, token: str) -> str:
    """Join ``base_url`` + ``path`` + ``token`` into a §12 capability URL.

    ``path`` is the route path supplied by the caller (#145/#146, which own
    their routes). Enforces §12's ``https`` requirement; raises
    :class:`ConfigurationError` (operator misconfiguration) when ``base_url``
    is unset or not ``https``.
    """
    if not base_url:
        raise ConfigurationError(
            "base_url is required to build a capability URL; set "
            "<PREFIX>_BASE_URL to the server's public https origin."
        )
    if not base_url.startswith("https://"):
        raise ConfigurationError(
            "capability URLs must use https (§12); base_url must start with "
            "'https://'."
        )
    segments = [base_url.rstrip("/")]
    trimmed = path.strip("/")
    if trimmed:
        segments.append(trimmed)
    segments.append(token)
    return "/".join(segments)
```

(`_TOKEN_BYTES` is unused until Task 3 but is a module constant — ruff does not flag unused module-level constants. Task 3 widens the imports to add `secrets`, `timedelta`, `timezone`, `TYPE_CHECKING`, `Mapping`, `AsyncKeyValue`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_tokens.py -q`
Expected: PASS (5 tests).
Then: `uv run ruff check src/fastmcp_pvl_core/_file_exchange/_tokens.py` and `uv run mypy src` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_tokens.py tests/_file_exchange/test_tokens.py
git commit -m "feat(file-exchange): MintedToken/TokenRecord + capability_url helper (#144)"
```

---

## Task 3: `CapabilityTokenStore.mint` + `lookup`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_tokens.py`
- Test: `tests/_file_exchange/test_tokens.py`

- [ ] **Step 1: Write the failing tests**

Add a fixture + tests to `tests/_file_exchange/test_tokens.py`. The fixture builds a store over an in-process `MemoryStore` (no env/config needed):

```python
from key_value.aio.stores.memory import MemoryStore


@pytest.fixture
def store() -> _tokens.CapabilityTokenStore:
    return _tokens.CapabilityTokenStore(MemoryStore(), ttl_ceiling=3600.0)


async def test_mint_returns_urlsafe_token_and_expiry(store):
    minted = await store.mint({"k": "v"}, ttl=60.0)
    assert isinstance(minted.token, str)
    # token_urlsafe(32) -> 43 chars, all URL-safe
    assert len(minted.token) >= 43
    assert all(c.isalnum() or c in "-_" for c in minted.token)
    # ~60s out (unclamped)
    from datetime import datetime, timezone

    delta = (minted.expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 55 <= delta <= 61


async def test_mint_clamps_ttl_to_ceiling(store):
    minted = await store.mint({"k": "v"}, ttl=10_000.0)  # ceiling is 3600
    from datetime import datetime, timezone

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
    import asyncio

    minted = await store.mint({"k": "v"}, ttl=0.05)
    await asyncio.sleep(0.12)
    assert await store.lookup(minted.token) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_tokens.py -k "mint or lookup" -v`
Expected: FAIL — `_tokens` has no attribute `CapabilityTokenStore`.

- [ ] **Step 3: Add the imports + the class with `mint`/`lookup`**

In `_tokens.py`, extend the imports to:

```python
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fastmcp_pvl_core._errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from key_value.aio.protocols.key_value import AsyncKeyValue
```

Append the class (after the dataclasses + `capability_url`):

```python
class CapabilityTokenStore:
    """Mint/lookup/consume/revoke high-entropy capability tokens.

    Wraps an ``AsyncKeyValue`` (from :func:`build_capability_token_store`'s
    ``build_kv_store`` call) plus the operator TTL ceiling. Expiry rides on
    the KV layer's TTL; single-use rides on an atomic ``delete``.
    """

    def __init__(self, store: AsyncKeyValue, *, ttl_ceiling: float) -> None:
        self._store = store
        self._ttl_ceiling = ttl_ceiling

    async def mint(
        self,
        metadata: Mapping[str, Any],
        *,
        ttl: float,
        single_use: bool = True,
    ) -> MintedToken:
        """Mint a token, store ``metadata`` under it with a clamped TTL.

        ``ttl`` is clamped to the operator ceiling (§10.2 "shortest value").
        ``metadata`` must be a JSON-serialisable mapping (it is persisted via
        the KV backend). Returns a :class:`MintedToken` whose ``expires_at``
        reflects the clamped TTL.
        """
        effective_ttl = min(ttl, self._ttl_ceiling)
        if effective_ttl <= 0:
            raise ValueError("ttl must be positive")
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        await self._store.put(
            token,
            {"metadata": dict(metadata), "single_use": single_use},
            collection=_COLLECTION,
            ttl=effective_ttl,
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=effective_ttl)
        return MintedToken(token=token, expires_at=expires_at)

    async def lookup(self, token: str) -> TokenRecord | None:
        """Return the token's record, or ``None`` if absent/expired.

        Does not mutate. ``None`` covers an unknown token, an expired one
        (KV TTL), and a consumed single-use one (deleted by ``consume``).
        """
        record = await self._store.get(token, collection=_COLLECTION)
        if record is None:
            return None
        return TokenRecord(
            metadata=record["metadata"], single_use=record["single_use"]
        )
```

(`secrets`, `timedelta`, `timezone`, `TYPE_CHECKING`, `Mapping`, `AsyncKeyValue` are now all used. Adjust docstrings if ruff D-rules complain.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_tokens.py -k "mint or lookup" -q`
Expected: PASS (6 tests). `uv run mypy src` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_tokens.py tests/_file_exchange/test_tokens.py
git commit -m "feat(file-exchange): CapabilityTokenStore mint + lookup (#144)"
```

---

## Task 4: `consume` + `revoke`

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_tokens.py`
- Test: `tests/_file_exchange/test_tokens.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_tokens.py` (reuse the `store` fixture):

```python
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


async def test_multi_use_consume_is_noop(store):
    minted = await store.mint({"k": "v"}, ttl=60.0, single_use=False)
    assert await store.consume(minted.token) is True
    # still valid for repeat retrieval
    assert await store.lookup(minted.token) is not None


async def test_revoke_invalidates_unconditionally(store):
    minted = await store.mint({"k": "v"}, ttl=60.0, single_use=False)
    await store.revoke(minted.token)
    assert await store.lookup(minted.token) is None


async def test_revoke_absent_token_does_not_raise(store):
    await store.revoke("nope")  # no exception
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_tokens.py -k "consume or revoke" -v`
Expected: FAIL — `CapabilityTokenStore` has no attribute `consume`.

- [ ] **Step 3: Implement `consume` + `revoke`**

Append to the `CapabilityTokenStore` class:

```python
    async def consume(self, token: str) -> bool:
        """Enforce single-use after a *successful* transfer.

        For a single-use token, atomically invalidate it and return whether
        *this* call won the race (the ``delete`` bool — at-most-once, §10.3).
        For a multi-use token (``download`` ``singleUse: false``), this is a
        no-op returning ``True`` (the token stays valid until its TTL).
        Returns ``False`` for an absent/expired/already-consumed token.

        Call this only on transfer completion — opening a connection alone
        MUST NOT invalidate the descriptor (§10.2).
        """
        record = await self._store.get(token, collection=_COLLECTION)
        if record is None:
            return False
        if record["single_use"]:
            return await self._store.delete(token, collection=_COLLECTION)
        return True

    async def revoke(self, token: str) -> None:
        """Unconditionally invalidate a token (§15 early invalidation).

        Idempotent — revoking an absent/expired token is a no-op.
        """
        await self._store.delete(token, collection=_COLLECTION)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_tokens.py -k "consume or revoke" -q`
Expected: PASS (6 tests). `uv run mypy src` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_tokens.py tests/_file_exchange/test_tokens.py
git commit -m "feat(file-exchange): CapabilityTokenStore consume + revoke (#144)"
```

---

## Task 5: `build_capability_token_store` factory

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_tokens.py`
- Test: `tests/_file_exchange/test_tokens.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/_file_exchange/test_tokens.py`:

```python
async def test_build_capability_token_store_from_config(monkeypatch):
    from fastmcp_pvl_core import ServerConfig

    config = ServerConfig(kv_store_url="memory://", file_exchange_token_ttl=120.0)
    built = _tokens.build_capability_token_store(config)
    assert isinstance(built, _tokens.CapabilityTokenStore)
    # ceiling threaded from config: a 9999s request clamps to ~120s
    minted = await built.mint({"k": "v"}, ttl=9999.0)
    from datetime import datetime, timezone

    delta = (minted.expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 115 <= delta <= 121
    # round-trips through the built store
    assert (await built.lookup(minted.token)) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/_file_exchange/test_tokens.py -k build_capability -v`
Expected: FAIL — `_tokens` has no attribute `build_capability_token_store`.

- [ ] **Step 3: Implement the factory**

Append to `_tokens.py` (module-level function, after the class). Add the `build_kv_store` + `ServerConfig` imports — `build_kv_store` is a runtime call, `ServerConfig` is annotation-only:

In the TYPE_CHECKING block add:
```python
    from fastmcp_pvl_core._config import ServerConfig
```

Append:
```python
def build_capability_token_store(config: ServerConfig) -> CapabilityTokenStore:
    """Build a :class:`CapabilityTokenStore` from operator config.

    Resolves the backend via :func:`~fastmcp_pvl_core.build_kv_store` under
    ``namespace="file-exchange-tokens"`` (so it shares the operator's chosen
    KV backend with isolated keyspace), and threads the TTL ceiling from
    ``config.file_exchange_token_ttl``. Mirrors ``build_event_store``.
    """
    from fastmcp_pvl_core._kv_store import build_kv_store

    store = build_kv_store(config, namespace="file-exchange-tokens")
    return CapabilityTokenStore(store, ttl_ceiling=config.file_exchange_token_ttl)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/_file_exchange/test_tokens.py -k build_capability -q`
Expected: PASS. Then full file: `uv run pytest tests/_file_exchange/test_tokens.py -q` and `uv run mypy src` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_tokens.py tests/_file_exchange/test_tokens.py
git commit -m "feat(file-exchange): build_capability_token_store factory (#144)"
```

---

## Task 6: Public re-exports

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/__init__.py`
- Modify: `src/fastmcp_pvl_core/file_exchange.py`
- Test: `tests/test_file_exchange_namespace.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_file_exchange_namespace.py`:

```python
def test_capability_token_names_reexported():
    from fastmcp_pvl_core import file_exchange

    for name in (
        "CapabilityTokenStore",
        "MintedToken",
        "TokenRecord",
        "build_capability_token_store",
        "capability_url",
    ):
        assert hasattr(file_exchange, name), name
        assert name in file_exchange.__all__, name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_file_exchange_namespace.py::test_capability_token_names_reexported -v`
Expected: FAIL — names absent.

- [ ] **Step 3: Re-export in the subpackage `__init__.py`**

In `src/fastmcp_pvl_core/_file_exchange/__init__.py`, add a new import group (alphabetically — `_tokens` sorts after `_spec`/`_selection`, before `_validation`/`_wire`; match the file's existing ordering):

```python
from fastmcp_pvl_core._file_exchange._tokens import (
    CapabilityTokenStore,
    MintedToken,
    TokenRecord,
    build_capability_token_store,
    capability_url,
)
```

Add the five names to `__all__`, preserving the file's existing sort convention (ALL_CAPS / TitleCase / lowercase grouping — place `CapabilityTokenStore`/`MintedToken`/`TokenRecord` among the TitleCase entries and `build_capability_token_store`/`capability_url` among the lowercase entries).

- [ ] **Step 4: Mirror in `file_exchange.py`**

In `src/fastmcp_pvl_core/file_exchange.py`, add the same five names to the `from fastmcp_pvl_core._file_exchange import (...)` block and to that file's `__all__`, following its alphabetical/section convention.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_file_exchange_namespace.py -q`
Expected: PASS. Then `uv run ruff check . && uv run mypy src` — clean.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/__init__.py src/fastmcp_pvl_core/file_exchange.py tests/test_file_exchange_namespace.py
git commit -m "feat(file-exchange): re-export capability token store surface (#144)"
```

---

## Task 7: Full local gate

**Files:** none (verification only).

- [ ] **Step 1: Sync deps**

Run: `uv sync --all-extras`
Expected: clean resolve.

- [ ] **Step 2: Full suite on the minimum interpreter**

Run: `uv run pytest -q`
Expected: all green (new + pre-existing).

- [ ] **Step 3: Full suite on the maximum interpreter**

Run: `uv run --python 3.13 pytest -q`
Expected: all green (catches version-dependent behavior — see PR #152).

- [ ] **Step 4: Format, lint, type-check**

Run:
```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```
Expected: no diffs, no errors.

> PR opens separately after the mandatory `preflight-circus` local review. The PR body must include `Closes #144`.

---

## Self-Review

**Spec coverage:**
- Token generator ≥128 bits URL-safe → Task 3 (`secrets.token_urlsafe(32)`), tested. ✓
- `mint`/`lookup`/`consume`/`revoke` API → Tasks 3, 4. ✓
- Backing via `build_kv_store` factory (no new storage) → Task 5. ✓
- Env config: token TTL ceiling + max artifact size → Task 1; base_url reused by `capability_url` (Task 2). ✓
- Tests: expiry boundary (Task 3), single-use lifecycle (Task 4), double-consume rejection (Task 4), revocation (Task 4). ✓
- §12 capability URL (https, ≥128-bit) → Task 2 `capability_url` + Task 3 token entropy. ✓
- TTL clamp to ceiling → Task 3, tested; threaded from config → Task 5, tested. ✓

**Placeholder scan:** No TBD/TODO. Task 2 explicitly resolves the minimal-imports wrinkle (the placeholder import line is called out for deletion and the exact Task-2 import set is given). ✓

**Type consistency:** `MintedToken(token, expires_at)`, `TokenRecord(metadata, single_use)`, `CapabilityTokenStore(store, *, ttl_ceiling)`, `mint(metadata, *, ttl, single_use) -> MintedToken`, `lookup -> TokenRecord | None`, `consume -> bool`, `revoke -> None`, `build_capability_token_store(config) -> CapabilityTokenStore`, `capability_url(base_url, path, token) -> str` — consistent across tasks. `_COLLECTION`/`_TOKEN_BYTES` defined once in Task 2, used in Task 3/4. The two config fields (`file_exchange_token_ttl`, `file_exchange_max_artifact_size`) named identically in Task 1 and Task 5's test. ✓
