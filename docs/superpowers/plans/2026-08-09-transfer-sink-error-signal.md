# `TransferSinkError` status signal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a `TransferSink.read`/`write` signal a specific HTTP error status (4xx/5xx) for a transfer instead of collapsing to an opaque 500, via a public `TransferSinkError` base plus seven named sugar subclasses.

**Architecture:** A new public exception family in `_transfer/sink.py`. The `/transfer/{token}` handler gains an `except TransferSinkError` arm (before its `except BaseException`) that releases the token and returns the signalled status; every other exception still releases and maps to 500. The types are re-exported top-level and from `fastmcp_pvl_core.transfer`.

**Tech Stack:** Python 3.10+, Starlette (handler), httpx ASGI (route tests), pytest.

## Global Constraints

- Intra-package imports stay **relative** (`from ._x import …`); foldability per `CLAUDE.md`.
- The signal is a **domain hook**, not a shape override: core owns the invariant (status must be **400–599**; release-on-failure unchanged; non-`TransferSinkError` → 500), the sink owns the judgment of which status.
- Public API (copy verbatim):
  - `class TransferSinkError(Exception)` with `status_code: int` and `def __init__(self, status_code: int, *args: object) -> None` raising `ValueError` unless `400 <= status_code <= 599`.
  - Sugar subclasses, each `def __init__(self, *args: object) -> None` calling `super().__init__(<status>, *args)`: `TransferResourceGoneError` (410), `TransferNotFoundError` (404), `TransferForbiddenError` (403), `TransferRateLimitedError` (429), `TransferUnavailableError` (503), `TransferBadGatewayError` (502), `TransferGatewayTimeoutError` (504).
- **No 401 sugar** (the base still allows `TransferSinkError(401)`).
- The internal `Token*Error` types, `TransferStore`, `TransferToken`, `make_transfer_handler` stay **unexported**.
- Local checks before push: `uv sync --all-extras && uv run pytest && uv run ruff format --check . && uv run ruff check . && uv run mypy src`.
- Verified fact: existing `tests/test_transfer_routes.py` proves a generic `RuntimeError` from the sink → **500** + release; those tests must stay unchanged (the default path is preserved).

---

### Task 1: Exception family in `sink.py` + top-level surfacing + unit tests

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/sink.py` (add the 8 classes; update module docstring)
- Modify: `src/fastmcp_pvl_core/_transfer/__init__.py` (export + `__all__` + docstring)
- Modify: `src/fastmcp_pvl_core/__init__.py` (top-level re-export + `__all__`)
- Test: `tests/test_transfer_sink.py` (new)

**Interfaces:**
- Produces: `TransferSinkError(status_code, *args)` with `.status_code`; the seven sugar subclasses above, each an instance of `TransferSinkError`. All importable from `fastmcp_pvl_core`.

- [ ] **Step 1: Write the failing unit tests** — create `tests/test_transfer_sink.py`:

```python
"""Unit tests for the sink-raisable HTTP status signals (issue #233)."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core import (
    TransferBadGatewayError,
    TransferForbiddenError,
    TransferGatewayTimeoutError,
    TransferNotFoundError,
    TransferRateLimitedError,
    TransferResourceGoneError,
    TransferSinkError,
    TransferUnavailableError,
)

# Every sugar subclass paired with the status it must map to.
_SUGAR = [
    (TransferResourceGoneError, 410),
    (TransferNotFoundError, 404),
    (TransferForbiddenError, 403),
    (TransferRateLimitedError, 429),
    (TransferUnavailableError, 503),
    (TransferBadGatewayError, 502),
    (TransferGatewayTimeoutError, 504),
]


class TestTransferSinkErrorBase:
    def test_stores_status_code(self) -> None:
        assert TransferSinkError(418).status_code == 418

    @pytest.mark.parametrize("bad", [399, 200, 600, 0, -1])
    def test_rejects_non_4xx_5xx(self, bad: int) -> None:
        with pytest.raises(ValueError, match="4xx/5xx"):
            TransferSinkError(bad)

    @pytest.mark.parametrize("ok", [400, 499, 500, 599])
    def test_accepts_range_bounds(self, ok: int) -> None:
        assert TransferSinkError(ok).status_code == ok

    def test_message_is_preserved(self) -> None:
        assert str(TransferSinkError(503, "backend down")) == "backend down"


class TestSugarSubclasses:
    @pytest.mark.parametrize(("cls", "status"), _SUGAR)
    def test_maps_to_its_status(
        self, cls: type[TransferSinkError], status: int
    ) -> None:
        assert cls().status_code == status

    @pytest.mark.parametrize(("cls", "status"), _SUGAR)
    def test_is_a_transfer_sink_error(
        self, cls: type[TransferSinkError], status: int
    ) -> None:
        # A handler catching the base catches every sugar subclass.
        assert isinstance(cls(), TransferSinkError)

    @pytest.mark.parametrize(("cls", "status"), _SUGAR)
    def test_preserves_message(
        self, cls: type[TransferSinkError], status: int
    ) -> None:
        assert str(cls("nope")) == "nope"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transfer_sink.py -q`
Expected: FAIL — `ImportError: cannot import name 'TransferSinkError' from 'fastmcp_pvl_core'`.

- [ ] **Step 3: Add the exception family to `src/fastmcp_pvl_core/_transfer/sink.py`**

Append after the `TransferValidator` definition at the end of the file:

```python
class TransferSinkError(Exception):
    """Raised by a :class:`TransferSink` to signal a specific HTTP error status.

    A sink's :meth:`~TransferSink.read` / :meth:`~TransferSink.write` normally
    lets an unexpected failure propagate, which the ``/transfer/{token}`` handler
    turns into an opaque **500** (after releasing the token). Raising this
    instead tells the handler to return a specific status — a domain judgment
    only the sink can make (the resource is gone, the backend is briefly down,
    …). pvl-core owns the invariant: ``status_code`` must be a client- or
    server-error status (**4xx/5xx**, 400-599); the handler still releases the
    token, and any non-``TransferSinkError`` failure still maps to 500.

    Prefer a named subclass (:class:`TransferResourceGoneError`, …) for a common
    case; use the base directly for a status without one (e.g.
    ``TransferSinkError(401)``).

    Args:
        status_code: The HTTP status to return, in 400-599.
        *args: An optional message, preserved on ``str(exc)``.

    Raises:
        ValueError: If ``status_code`` is not in 400-599 — a programming error
            surfaced at the raise site rather than silently mis-mapped.
    """

    status_code: int

    def __init__(self, status_code: int, *args: object) -> None:
        if not 400 <= status_code <= 599:
            raise ValueError(
                f"TransferSinkError status_code must be a 4xx/5xx HTTP error "
                f"status (400-599), got {status_code}"
            )
        super().__init__(*args)
        self.status_code = status_code


class TransferResourceGoneError(TransferSinkError):
    """The resource the link pointed at existed and is now gone (**410 Gone**).

    Distinct from the claim-time 404: the sink runs only after a successful
    claim, so the caller held a valid link — 410 says the resource vanished, not
    that the link is bad.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(410, *args)


class TransferNotFoundError(TransferSinkError):
    """The resource the handle names was never there (**404 Not Found**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(404, *args)


class TransferForbiddenError(TransferSinkError):
    """The handle resolved but access to the resource is denied (**403**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(403, *args)


class TransferRateLimitedError(TransferSinkError):
    """A backend the sink calls is rate-limiting the request (**429**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(429, *args)


class TransferUnavailableError(TransferSinkError):
    """The backend is temporarily unavailable; the caller may retry (**503**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(503, *args)


class TransferBadGatewayError(TransferSinkError):
    """An upstream dependency returned an invalid/failed response (**502**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(502, *args)


class TransferGatewayTimeoutError(TransferSinkError):
    """An upstream dependency the sink calls timed out (**504**)."""

    def __init__(self, *args: object) -> None:
        super().__init__(504, *args)
```

- [ ] **Step 4: Note the signals in `sink.py`'s module docstring**

In the opening paragraph that lists what the sink implements, add a sentence after the "Everything else … is pvl-core's shape." sentence:

```
A sink may additionally raise :class:`TransferSinkError` (or a named subclass) to
signal a specific 4xx/5xx status for one transfer instead of the default 500;
that status is the sink's domain judgment, bounded by pvl-core to an error status.
```

- [ ] **Step 5: Export from `src/fastmcp_pvl_core/_transfer/__init__.py`**

Change the sink import line to include the family (keep alphabetical within the call):

```python
from .sink import (
    TransferBadGatewayError,
    TransferForbiddenError,
    TransferGatewayTimeoutError,
    TransferKind,
    TransferNotFoundError,
    TransferRateLimitedError,
    TransferReadResult,
    TransferResourceGoneError,
    TransferSink,
    TransferSinkError,
    TransferUnavailableError,
    TransferValidator,
)
```

Add the seven-plus-base names to `__all__` (keep it sorted), and extend the "public surface" sentence in the module docstring to mention "the sink-raisable status signals (``TransferSinkError`` and its named subclasses)".

- [ ] **Step 6: Re-export top-level in `src/fastmcp_pvl_core/__init__.py`**

Add the eight names to the `from ._transfer import (…)` block and to the top-level `__all__` (keep both sorted).

- [ ] **Step 7: Run the unit tests + lint + types**

Run: `uv run pytest tests/test_transfer_sink.py -q && uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: PASS (8 classes importable from `fastmcp_pvl_core`; range validation and sugar mappings hold).

- [ ] **Step 8: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/sink.py \
        src/fastmcp_pvl_core/_transfer/__init__.py \
        src/fastmcp_pvl_core/__init__.py \
        tests/test_transfer_sink.py
git commit -m "feat(transfer): TransferSinkError family for sink-signalled statuses (#233)"
```

---

### Task 2: Map `TransferSinkError` in the route handler

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/routes.py` (`_download`, `_upload`, imports, module docstring)
- Test: `tests/test_transfer_routes.py` (extend `_FakeSink`; add signal tests)

**Interfaces:**
- Consumes: `TransferSinkError` (Task 1) from `.sink`; existing `_release_quietly(store, claim)`, `make_transfer_handler`.

- [ ] **Step 1: Write the failing handler tests** — in `tests/test_transfer_routes.py`.

First extend the imports at the top (add to the existing `fastmcp_pvl_core` import, or add a line):

```python
from fastmcp_pvl_core import (
    TransferForbiddenError,
    TransferResourceGoneError,
    TransferSinkError,
    TransferUnavailableError,
)
```

Extend `_FakeSink.__init__` and its methods to raise a configured exception:

```python
        self.read_raises: BaseException | None = None
        self.write_raises: BaseException | None = None
```

At the top of `read`:

```python
        if self.read_raises is not None:
            raise self.read_raises
```

At the top of `write`:

```python
        if self.write_raises is not None:
            raise self.write_raises
```

Then add the tests (near the existing release-on-failure block):

```python
# sink-signalled status (issue #233)

_DL_SIGNALS = [
    (TransferResourceGoneError(), 410),
    (TransferUnavailableError(), 503),
    (TransferForbiddenError(), 403),
    (TransferSinkError(404), 404),
    (TransferSinkError(502), 502),
]


@pytest.mark.parametrize(("exc", "status"), _DL_SIGNALS)
async def test_download_sink_signal_maps_status_and_releases(
    exc: TransferSinkError, status: int
) -> None:
    store = _make_store()
    sink = _FakeSink()
    sink.reads["h1"] = (b"DATA", "text/plain", "f.txt")
    token = await _mint_download(store, handle="h1")
    async with _client(store, sink) as client:
        sink.read_raises = exc
        resp = await client.get(f"/transfer/{token}")
        assert resp.status_code == status
        # Released, not spent: a retry once the sink recovers serves 200.
        sink.read_raises = None
        retry = await client.get(f"/transfer/{token}")
        assert retry.status_code == 200
        assert retry.content == b"DATA"


@pytest.mark.parametrize(("exc", "status"), _DL_SIGNALS)
async def test_upload_sink_signal_maps_status_and_releases(
    exc: TransferSinkError, status: int
) -> None:
    store = _make_store()
    sink = _FakeSink()
    token = await _mint_upload(store, handle="h1")
    async with _client(store, sink) as client:
        sink.write_raises = exc
        resp = await client.put(f"/transfer/{token}", content=b"BODY")
        assert resp.status_code == status
        # The upload body was fully read before write, so the connection is not
        # force-closed (unlike the 413 over-cap path).
        assert resp.headers.get("connection") != "close"
        # Released: a retry once the sink recovers stores the body (200).
        sink.write_raises = None
        retry = await client.put(f"/transfer/{token}", content=b"BODY")
        assert retry.status_code == 200
        assert sink.writes["h1"] == b"BODY"
```

If `_mint_upload` does not already exist in the file, add it beside `_mint_download`:

```python
async def _mint_upload(
    store: TransferStore, *, handle: str = "h1", ttl: float = 300.0
) -> str:
    return await store.mint(kind="upload", sink_handle=handle, caps={}, ttl_seconds=ttl)
```

(Confirm `_mint_download`'s exact `store.mint(...)` call and mirror it with `kind="upload"`.)

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_transfer_routes.py -k "sink_signal" -v`
Expected: FAIL — the signals currently propagate to a generic **500**, so the status assertions fail.

- [ ] **Step 3: Add the `except TransferSinkError` arm to `_download`**

In `src/fastmcp_pvl_core/_transfer/routes.py`, import the type near the other `.store`/`.sink` imports:

```python
from .sink import TransferSink, TransferSinkError
```

In `_download`, replace the sink-read try/except so the signal arm precedes the catch-all:

```python
    try:
        body, media_type, filename = await sink.read(cast(str, claim.sink_handle))
    except TransferSinkError as exc:
        # A deliberate domain signal: release the link and return the sink's
        # chosen status. Log the status and class name only — never the message,
        # which may embed a domain path or the token-derived key.
        await _release_quietly(store, claim)
        logger.info(
            "transfer download sink signalled %d: %s",
            exc.status_code,
            type(exc).__name__,
        )
        return Response(status_code=exc.status_code)
    except BaseException:
        # Release-on-failure: the link survives a transient failure. BaseException
        # (not Exception) so a cancelled request also releases; then the error /
        # cancellation propagates (Starlette → generic 500 for an ordinary error).
        await _release_quietly(store, claim)
        raise
```

- [ ] **Step 4: Add the same arm to `_upload`**

In `_upload`, replace the `sink.write` try/except:

```python
    try:
        payload = await sink.write(cast(str, claim.sink_handle), body)
    except TransferSinkError as exc:
        # Deliberate domain signal. The body was fully read above, so nothing is
        # left undrained → no Connection: close needed (unlike the 413 path).
        await _release_quietly(store, claim)
        logger.info(
            "transfer upload sink signalled %d: %s",
            exc.status_code,
            type(exc).__name__,
        )
        return Response(status_code=exc.status_code)
    except BaseException:
        await _release_quietly(store, claim)
        raise
```

- [ ] **Step 5: Update `routes.py`'s module docstring**

In the paragraph describing settle-on-success / release-on-failure, add a sentence:

```
A sink may raise :class:`TransferSinkError` (or a named subclass) from
``read``/``write`` to return a specific 4xx/5xx status instead of the default
500; the link is still released, exactly as on any other failure.
```

- [ ] **Step 6: Run the full route suite**

Run: `uv run pytest tests/test_transfer_routes.py -q`
Expected: PASS — the new signal tests **and** the unchanged "generic `RuntimeError` → 500 + released" tests.

- [ ] **Step 7: Lint + types**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/routes.py tests/test_transfer_routes.py
git commit -m "feat(transfer): map TransferSinkError to its status in the handler (#233)"
```

---

### Task 3: Add the family to the public `transfer.py` namespace

**Files:**
- Modify: `src/fastmcp_pvl_core/transfer.py`
- Test: `tests/test_transfer_public_surface.py`

**Interfaces:**
- Consumes: the family exported from `._transfer` (Task 1).

- [ ] **Step 1: Extend the surface test** — in `tests/test_transfer_public_surface.py`, add the eight names to the `_EXPECTED` set:

```python
    "TransferSinkError",
    "TransferResourceGoneError",
    "TransferNotFoundError",
    "TransferForbiddenError",
    "TransferRateLimitedError",
    "TransferUnavailableError",
    "TransferBadGatewayError",
    "TransferGatewayTimeoutError",
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_transfer_public_surface.py -q`
Expected: FAIL — `set(transfer.__all__) != _EXPECTED` (the new names are not yet in `transfer.py`).

- [ ] **Step 3: Add the names to `src/fastmcp_pvl_core/transfer.py`**

Add the eight names to the `from ._transfer import (…)` block and to `__all__` (keep both sorted).

- [ ] **Step 4: Run the surface test + lint + types**

Run: `uv run pytest tests/test_transfer_public_surface.py -q && uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/transfer.py tests/test_transfer_public_surface.py
git commit -m "feat(transfer): re-export TransferSinkError family from transfer namespace (#233)"
```

---

### Task 4: Reconcile `register.py`'s "status codes" docstring clause

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py` (module docstring only)

- [ ] **Step 1: Narrow the clause**

In the module docstring paragraph beginning "pvl-core owns every **shape** decision on both paths", change the bare "the status codes" item to name the protocol statuses and the delegation:

```
the protocol status codes (claim, method, size-cap, success — a sink may signal
its own read/write failure status via :class:`TransferSinkError`)
```

- [ ] **Step 2: Verify nothing else references the old wording**

Run: `grep -rn "the status codes" src/`
Expected: no remaining bare "the status codes" ownership claim in `register.py`.

- [ ] **Step 3: Lint (docstring only, but keep the gate honest)**

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/register.py
git commit -m "docs(transfer): reconcile register status-code ownership with TransferSinkError (#233)"
```

---

## Final verification (after all tasks)

- [ ] `uv sync --all-extras && uv run pytest && uv run ruff format --check . && uv run ruff check . && uv run mypy src` — all PASS.
- [ ] `grep -rn "TransferStore\|TransferToken\|make_transfer_handler" src/fastmcp_pvl_core/transfer.py src/fastmcp_pvl_core/__init__.py` — no hits (internals stay internal).
- [ ] Run the `preflight-circus` skill over `origin/main..HEAD` before opening the PR.

## Self-review notes

- **Spec coverage:** base + 7 sugar (Task 1); 410-for-gone rationale (docstring in Task 1); handler mapping + release + no-close-on-upload + 500 default preserved (Task 2); surfacing (Task 1 top-level, Task 3 namespace); docstring reconciliation (Tasks 1/2/4). Every spec test bullet maps to a test (unit → Task 1, handler → Task 2, surface → Task 3).
- **Type consistency:** `TransferSinkError(status_code, *args)` / `.status_code` / subclass `(*args)` signatures identical across spec, plan, tests. Sugar-to-status table matches `_SUGAR` and `_DL_SIGNALS`.
- **No placeholders:** every code/test block is concrete; the one lookup left to the implementer (mirroring `_mint_download`'s exact `store.mint` call for `_mint_upload`) is a named, mechanical confirmation, not a design gap.
