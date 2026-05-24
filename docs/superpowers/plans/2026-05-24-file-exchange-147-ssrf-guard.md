# File-Exchange #147 — SSRF + DNS-rebind guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the package-internal outbound-HTTP guard (`guarded_stream`) that the download fetcher (#145) and upload sender (#146) issue every request through, enforcing the §15 SSRF + DNS-rebind mitigations.

**Architecture:** A thin async-context-manager primitive in a new module `_file_exchange/_outbound.py`. It enforces https-only, deny-all-non-global address refusal (with a CIDR allowlist), resolve-once-pin-IP via httpx's `sni_hostname` extension, cross-origin-redirect refusal (same-origin followed, bounded), and no-ambient-credentials. Every refusal/failure raises `FileExchangeTransferError(NOT_ACCESSIBLE, transport=<caller label>)`. Verification and `Range` recovery are NOT here — they belong to #145/#146.

**Tech Stack:** Python 3.10+, `httpx` (already a dep), stdlib `ipaddress`/`socket`/`asyncio`, `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), `httpx.MockTransport` for tests.

**Design reference:** `docs/superpowers/specs/2026-05-24-file-exchange-147-ssrf-guard-design.md`.

**No public surface change:** `guarded_stream`/`GuardedResponse` are imported by #145/#146 via `fastmcp_pvl_core._file_exchange._outbound`. They are NOT added to `file_exchange.py` or the subpackage `__init__.py` `__all__`. The only operator-facing additions are two `ServerConfig` fields.

> **The shipped module is the source of truth, not the code blocks below.** This is a forward-looking TDD plan: the code in each task is the *planned starting point*. During implementation, review surfaced additional security mechanisms that the shipped `_outbound.py` carries and these snippets do not — the userinfo strip and caller-`Host` strip in `_send_pinned`, `has_redirect_location` (not `is_redirect`) gating with a body-on-redirect refusal in `guarded_stream`, the non-positive-timeout `ConfigurationError` guard in `_make_client`, and the empty-resolution guard. Read `_outbound.py` at HEAD for the actual behaviour.

---

### Task 1: ServerConfig fields

**Files:**
- Modify: `src/fastmcp_pvl_core/_config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_file_exchange_outbound_defaults():
    from fastmcp_pvl_core import ServerConfig

    cfg = ServerConfig.from_env("MYAPP")
    assert cfg.file_exchange_allowed_networks == ()
    assert cfg.file_exchange_http_timeout == 30.0


def test_from_env_file_exchange_allowed_networks(monkeypatch):
    from fastmcp_pvl_core import ServerConfig

    monkeypatch.setenv(
        "MYAPP_FILE_EXCHANGE_ALLOWED_NETWORKS", "10.0.0.0/8, 192.168.5.0/24"
    )
    cfg = ServerConfig.from_env("MYAPP")
    assert cfg.file_exchange_allowed_networks == ("10.0.0.0/8", "192.168.5.0/24")


def test_from_env_file_exchange_http_timeout(monkeypatch):
    from fastmcp_pvl_core import ServerConfig

    monkeypatch.setenv("MYAPP_FILE_EXCHANGE_HTTP_TIMEOUT", "12.5")
    cfg = ServerConfig.from_env("MYAPP")
    assert cfg.file_exchange_http_timeout == 12.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::test_file_exchange_outbound_defaults tests/test_config.py::test_from_env_file_exchange_allowed_networks tests/test_config.py::test_from_env_file_exchange_http_timeout -v`
Expected: FAIL — `AttributeError: 'ServerConfig' object has no attribute 'file_exchange_allowed_networks'`.

- [ ] **Step 3: Add the import**

In `src/fastmcp_pvl_core/_config.py`, extend the `_env` import (line 15):

```python
from fastmcp_pvl_core._env import env, parse_bool, parse_list, parse_scopes
```

- [ ] **Step 4: Add the dataclass fields**

In `src/fastmcp_pvl_core/_config.py`, immediately after the `file_exchange_max_artifact_size` field (line 64):

```python
    # File-exchange outbound-HTTP guard config (#147). allowed_networks are
    # CIDRs that bypass the deny-all-non-global SSRF refusal (parsed in
    # _file_exchange._outbound); http_timeout bounds connect/read/write on
    # every guarded request.
    file_exchange_allowed_networks: tuple[str, ...] = ()
    file_exchange_http_timeout: float = 30.0
```

- [ ] **Step 5: Parse them in `from_env`**

In `src/fastmcp_pvl_core/_config.py`, after the `max_size_raw = ...` line (line 130):

```python
        allowed_networks_raw = env(env_prefix, "FILE_EXCHANGE_ALLOWED_NETWORKS")
        http_timeout_str = env(env_prefix, "FILE_EXCHANGE_HTTP_TIMEOUT", "30")
```

And in the `return cls(...)` block, immediately after the `file_exchange_max_artifact_size=...` entry:

```python
            file_exchange_allowed_networks=(
                tuple(parse_list(allowed_networks_raw)) if allowed_networks_raw else ()
            ),
            file_exchange_http_timeout=float(http_timeout_str),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all config tests, including the three new ones).

- [ ] **Step 7: Commit**

```bash
git add src/fastmcp_pvl_core/_config.py tests/test_config.py
git commit -m "feat(file-exchange): add outbound-HTTP guard config fields (#147)"
```

---

### Task 2: SSRF pure core (`_outbound.py` helpers)

The security heart — pure, synchronous, exhaustively tested before any async/httpx code.

**Files:**
- Create: `src/fastmcp_pvl_core/_file_exchange/_outbound.py`
- Test: `tests/_file_exchange/test_outbound.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/_file_exchange/test_outbound.py`:

```python
import ipaddress

import pytest

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange import _outbound


def test_redact_returns_hostname_only():
    assert (
        _outbound._redact("https://h.example.com/d/SECRETTOKEN?sig=abc")
        == "h.example.com"
    )


def test_redact_handles_missing_host():
    assert _outbound._redact("::not a url::") == "<unknown-host>"


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "::1",
        "169.254.1.1",
        "fe80::1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
    ],
)
def test_non_global_addresses_refused(addr):
    assert _outbound._is_permitted(ipaddress.ip_address(addr), []) is False


@pytest.mark.parametrize(
    "addr",
    ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
)
def test_global_addresses_permitted(addr):
    assert _outbound._is_permitted(ipaddress.ip_address(addr), []) is True


def test_allowlist_permits_private_cidr():
    allowed = _outbound._parse_allowed_networks(("10.0.0.0/8",))
    assert _outbound._is_permitted(ipaddress.ip_address("10.1.2.3"), allowed) is True


def test_malformed_cidr_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        _outbound._parse_allowed_networks(("not-a-cidr",))


def test_select_pinned_skips_blocked_picks_global():
    assert (
        _outbound._select_pinned(["127.0.0.1", "93.184.216.34"], [])
        == "93.184.216.34"
    )


def test_select_pinned_none_when_all_blocked():
    assert _outbound._select_pinned(["127.0.0.1", "10.0.0.1"], []) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_outbound.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fastmcp_pvl_core._file_exchange._outbound'`.

- [ ] **Step 3: Create the module with docstring, imports, and pure helpers**

Create `src/fastmcp_pvl_core/_file_exchange/_outbound.py`:

```python
"""Outbound-HTTP guard for the file-exchange data plane (#147).

The single primitive the download fetcher (#145) and upload sender (#146)
issue their requests through. Enforces the §15 SSRF mitigations (https-only,
deny-all-non-global address refusal with a CIDR allowlist, no cross-origin
redirect, no ambient credentials) and the §15 DNS-rebinding mitigation
(resolve-once-pin-IP via httpx's ``sni_hostname`` extension). Carries the
URL-redaction discipline: only the hostname ever reaches a log line, and no
URL parts reach the wire ``detail``.

It does NOT verify size/digest, recover with ``Range``, or touch artifacts —
those are the consuming data planes' deliverables (#145/#146). See
``docs/superpowers/specs/2026-05-24-file-exchange-147-ssrf-guard-design.md``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator, Mapping

    from fastmcp_pvl_core._config import ServerConfig

logger = logging.getLogger(__name__)

# Max same-origin redirect hops followed before refusing (§15: cross-origin
# is always refused; this caps a same-origin redirect chain).
_MAX_REDIRECTS = 5

_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _redact(url: str) -> str:
    """Return only the hostname of ``url``.

    The capability token lives in the URL path/query, so those must never
    reach a log line or a wire ``detail`` (§15). Falls back to a placeholder
    for an unparseable value.
    """
    host = urlsplit(url).hostname
    return host or "<unknown-host>"


def _parse_allowed_networks(cidrs: tuple[str, ...]) -> list[_IPNetwork]:
    """Parse operator CIDR strings into networks.

    A malformed entry is operator misconfiguration → :class:`ConfigurationError`
    (loud), consistent with the rest of the package.
    """
    networks: list[_IPNetwork] = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:
            raise ConfigurationError(
                f"file-exchange: malformed CIDR in allowed-networks: {cidr!r}"
            ) from exc
    return networks


def _is_permitted(ip: _IPAddress, allowed: list[_IPNetwork]) -> bool:
    """A resolved address is permitted iff globally routable or allowlisted.

    An IPv4-mapped IPv6 address is unwrapped first, so a mapped loopback/
    private address (``::ffff:127.0.0.1``) cannot slip past ``is_global``.
    Allowlist membership is checked against the unwrapped form; an address/
    network version mismatch yields ``False`` (never raises).
    """
    mapped = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
    candidate: _IPAddress = mapped if mapped is not None else ip
    if candidate.is_global:
        return True
    return any(candidate in net for net in allowed)


def _select_pinned(resolved: list[str], allowed: list[_IPNetwork]) -> str | None:
    """Pick the first permitted address to pin, or ``None`` if all are refused.

    Picking a permitted address out of a mixed result set is safe because the
    connection is then pinned to exactly that address — a DNS result that
    mixes a private and a public record cannot induce a connection to the
    private one.
    """
    for addr in resolved:
        if _is_permitted(ipaddress.ip_address(addr), allowed):
            return addr
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_outbound.py -v`
Expected: PASS (all pure-core tests).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_outbound.py tests/_file_exchange/test_outbound.py
git commit -m "feat(file-exchange): SSRF address-validation core for outbound guard (#147)"
```

---

### Task 3: `guarded_stream` core (single request, pin, refusals, cleanup)

Implements the full single-request path: scheme check, resolve-once, pin, no-ambient-creds, timeout, streaming, and refusal mapping. Redirect following is added in Task 4.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_outbound.py`
- Test: `tests/_file_exchange/test_outbound.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_outbound.py` (add `import socket` and `import httpx` to the existing imports at the top, plus `from fastmcp_pvl_core._config import ServerConfig`, `from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode`, and `from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError`):

```python
def _cfg(*, allowed=(), timeout=30.0):
    return ServerConfig(
        file_exchange_allowed_networks=allowed,
        file_exchange_http_timeout=timeout,
    )


def _install_mock(monkeypatch, handler, *, resolve_to):
    """Pin _resolve to a fixed address list and route the client through a
    MockTransport. Returns a dict tracking the _resolve call count."""
    calls = {"resolve": 0}

    async def fake_resolve(host, port):
        calls["resolve"] += 1
        return list(resolve_to)

    def fake_make_client(timeout):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr(_outbound, "_resolve", fake_resolve)
    monkeypatch.setattr(_outbound, "_make_client", fake_make_client)
    return calls


async def test_guarded_stream_streams_body_and_pins(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"hello-bytes")

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    data = b""
    async with _outbound.guarded_stream(
        "GET", "https://example.com/d/tok", config=_cfg(), transport="download"
    ) as resp:
        assert resp.status == 200
        async for chunk in resp.aiter_bytes():
            data += chunk
    assert data == b"hello-bytes"
    req = seen[0]
    assert req.url.host == "93.184.216.34"
    assert req.headers["host"] == "example.com"
    assert req.extensions.get("sni_hostname") == "example.com"


async def test_resolve_called_once_per_request(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x")

    calls = _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET", "https://example.com/x", config=_cfg(), transport="download"
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    assert calls["resolve"] == 1


async def test_refuses_non_https():
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "http://example.com/x", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE
    assert ei.value.transport == "download"
    assert "http://" not in (ei.value.detail or "")


async def test_refuses_non_global_address(monkeypatch):
    def handler(request):
        raise AssertionError("must not connect to a refused address")

    _install_mock(monkeypatch, handler, resolve_to=["127.0.0.1"])
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https://evil.example/x", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_allowlist_permits_private_target(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    _install_mock(monkeypatch, handler, resolve_to=["10.1.2.3"])
    async with _outbound.guarded_stream(
        "GET",
        "https://internal.example/x",
        config=_cfg(allowed=("10.0.0.0/8",)),
        transport="download",
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    assert seen[0].url.host == "10.1.2.3"


async def test_refuses_unresolvable_host(monkeypatch):
    async def fake_resolve(host, port):
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(_outbound, "_resolve", fake_resolve)
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https://nope.example/x", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_maps_connect_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https://example.com/x", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_passes_caller_headers_adds_no_credentials(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"x")

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET",
        "https://example.com/x",
        config=_cfg(),
        transport="download",
        headers={"Range": "bytes=10-"},
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    req = seen[0]
    assert req.headers["range"] == "bytes=10-"
    assert "authorization" not in req.headers
    assert "cookie" not in req.headers


async def test_make_client_security_defaults():
    client = _outbound._make_client(30.0)
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout.read == 30.0
        assert client.timeout.connect == 30.0
    finally:
        await client.aclose()


async def test_client_closed_on_caller_exception(monkeypatch):
    holder = {}

    def handler(request):
        return httpx.Response(200, content=b"data")

    async def fake_resolve(host, port):
        return ["93.184.216.34"]

    def fake_make_client(timeout):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )
        holder["client"] = client
        return client

    monkeypatch.setattr(_outbound, "_resolve", fake_resolve)
    monkeypatch.setattr(_outbound, "_make_client", fake_make_client)
    with pytest.raises(RuntimeError):
        async with _outbound.guarded_stream(
            "GET", "https://example.com/x", config=_cfg(), transport="download"
        ):
            raise RuntimeError("boom")
    assert holder["client"].is_closed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_outbound.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'guarded_stream'` (and `_resolve`/`_make_client` for the monkeypatch).

- [ ] **Step 3: Add `_resolve`, `_make_client`, `GuardedResponse`, `_send_pinned`, and `guarded_stream`**

Append to `src/fastmcp_pvl_core/_file_exchange/_outbound.py`:

```python
async def _resolve(host: str, port: int) -> list[str]:
    """Resolve ``host`` once to a list of IP strings (single getaddrinfo call).

    Kept as a module-level function so tests can pin it; this single call is
    the resolution the §15 DNS-rebind mitigation anchors on — the connection
    uses only addresses from this result.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _make_client(timeout: float) -> httpx.AsyncClient:
    """Build the outbound client.

    No ambient credentials and ``trust_env=False`` (so ``HTTP(S)_PROXY`` /
    ``netrc`` cannot inject credentials or divert past the pinned IP — an SSRF
    bypass). Redirects are handled by :func:`guarded_stream`, not httpx.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
    )


@dataclass(frozen=True)
class GuardedResponse:
    """A streaming response yielded by :func:`guarded_stream`.

    Wraps the underlying ``httpx.Response`` so the raw URL (carrying the
    capability token) cannot leak through httpx's ``repr``/traceback. The
    caller reads the body via :meth:`aiter_bytes` inside the ``async with``.
    """

    status: int
    headers: Mapping[str, str]
    _response: httpx.Response = field(repr=False)

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        return self._response.aiter_bytes()


async def _send_pinned(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    transport: str,
    allowed: list[_IPNetwork],
    headers: Mapping[str, str] | None,
    content: AsyncIterable[bytes] | bytes | None,
) -> httpx.Response:
    """Resolve-once, validate, pin, and send a single streamed request.

    Raises :class:`FileExchangeTransferError` (``not-accessible``) for a
    non-https URL, a missing host, an unresolvable host, a non-permitted
    address, or a connection failure. The returned response is streamed
    (body not yet read).
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport=transport,
            detail="refused non-https URL",
        )
    host = parts.hostname
    if host is None:
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport=transport,
            detail="URL has no host",
        )
    port = parts.port or 443
    try:
        resolved = await _resolve(host, port)
    except OSError as exc:
        logger.debug("file-exchange: DNS resolution failed for %s", _redact(url))
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport=transport,
            detail="host could not be resolved",
        ) from exc
    pinned = _select_pinned(resolved, allowed)
    if pinned is None:
        logger.debug("file-exchange: refused non-global address for %s", _redact(url))
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport=transport,
            detail="address not permitted",
        )
    request = client.build_request(
        method,
        httpx.URL(url).copy_with(host=pinned),
        headers={"Host": host, **(headers or {})},
        content=content,
        extensions={"sni_hostname": host},
    )
    try:
        return await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        logger.debug("file-exchange: outbound request failed for %s", _redact(url))
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport=transport,
            detail="endpoint unreachable",
        ) from exc


@asynccontextmanager
async def guarded_stream(
    method: str,
    url: str,
    *,
    config: ServerConfig,
    transport: str,
    headers: Mapping[str, str] | None = None,
    content: AsyncIterable[bytes] | bytes | None = None,
) -> AsyncIterator[GuardedResponse]:
    """Issue a guarded, streamed outbound request and yield the response.

    Enforces the §15 SSRF + DNS-rebind mitigations (see module docstring).
    ``transport`` is the §13 envelope label (``"download"``/``"upload"``)
    carried on any raised :class:`FileExchangeTransferError`. The caller reads
    the body via the yielded :class:`GuardedResponse` inside the ``async
    with``; the response and client are closed on exit, including on error.
    """
    allowed = _parse_allowed_networks(config.file_exchange_allowed_networks)
    client = _make_client(config.file_exchange_http_timeout)
    response: httpx.Response | None = None
    try:
        response = await _send_pinned(
            client, method, url, transport, allowed, headers, content
        )
        yield GuardedResponse(
            status=response.status_code,
            headers=response.headers,
            _response=response,
        )
    finally:
        if response is not None:
            await response.aclose()
        await client.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_outbound.py -v`
Expected: PASS (all Task 2 + Task 3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_outbound.py tests/_file_exchange/test_outbound.py
git commit -m "feat(file-exchange): guarded_stream core with resolve-once-pin (#147)"
```

---

### Task 4: Redirect handling (refuse cross-origin, follow same-origin)

Replaces the single-request body with a bounded redirect loop that re-guards each hop.

**Files:**
- Modify: `src/fastmcp_pvl_core/_file_exchange/_outbound.py`
- Test: `tests/_file_exchange/test_outbound.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/_file_exchange/test_outbound.py`:

```python
async def test_follows_same_origin_redirect_and_reresolves(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(
                302, headers={"location": "https://example.com/final"}
            )
        return httpx.Response(200, content=b"final-bytes")

    calls = _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    data = b""
    async with _outbound.guarded_stream(
        "GET", "https://example.com/start", config=_cfg(), transport="download"
    ) as resp:
        async for chunk in resp.aiter_bytes():
            data += chunk
    assert data == b"final-bytes"
    assert calls["resolve"] == 2  # re-resolved + re-validated per hop


async def test_follows_relative_redirect(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "/elsewhere"})
        return httpx.Response(200, content=b"ok")

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET", "https://example.com/start", config=_cfg(), transport="download"
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    assert seen[1] == "/elsewhere"


async def test_refuses_cross_origin_redirect(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https://example.com/start", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE
    assert "evil.example" not in (ei.value.detail or "")


async def test_refuses_too_many_redirects(monkeypatch):
    counter = {"n": 0}

    def handler(request):
        counter["n"] += 1
        return httpx.Response(
            302, headers={"location": f"https://example.com/hop{counter['n']}"}
        )

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https://example.com/start", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE
    assert counter["n"] == _outbound._MAX_REDIRECTS + 1  # initial + 5 follows
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/_file_exchange/test_outbound.py -k redirect -v`
Expected: FAIL — the current single-request `guarded_stream` yields the 302 instead of following/refusing (e.g. `test_follows_same_origin_redirect_and_reresolves` sees `status == 302`, body empty).

- [ ] **Step 3: Add `_same_origin` and replace the `guarded_stream` body with the redirect loop**

Add this helper above `guarded_stream` in `src/fastmcp_pvl_core/_file_exchange/_outbound.py`:

```python
def _same_origin(a: str, b: str) -> bool:
    """Whether two URLs share scheme + host + port (case-insensitive host).

    https default port 443 is normalised so ``https://h/x`` and
    ``https://h:443/x`` compare equal.
    """
    pa, pb = urlsplit(a), urlsplit(b)
    return (
        pa.scheme.lower() == pb.scheme.lower()
        and (pa.hostname or "").lower() == (pb.hostname or "").lower()
        and (pa.port or 443) == (pb.port or 443)
    )
```

Replace the `try:` block inside `guarded_stream` (the `response = await _send_pinned(...)` + `yield ...` portion) with the bounded loop:

```python
    try:
        current = url
        for _hop in range(_MAX_REDIRECTS + 1):
            response = await _send_pinned(
                client, method, current, transport, allowed, headers, content
            )
            if response.is_redirect:
                target = str(httpx.URL(current).join(response.headers.get("location", "")))
                await response.aclose()
                response = None
                if not _same_origin(current, target):
                    raise FileExchangeTransferError(
                        TransferErrorCode.NOT_ACCESSIBLE,
                        transport=transport,
                        detail="refused cross-origin redirect",
                    )
                current = target
                continue
            yield GuardedResponse(
                status=response.status_code,
                headers=response.headers,
                _response=response,
            )
            return
        raise FileExchangeTransferError(
            TransferErrorCode.NOT_ACCESSIBLE,
            transport=transport,
            detail="too many redirects",
        )
    finally:
        if response is not None:
            await response.aclose()
        await client.aclose()
```

Note: the loop runs at most `_MAX_REDIRECTS + 1` times (1 initial request + up to 5 followed redirects). A redirect on the final iteration falls through to the "too many redirects" refusal. Each hop re-runs `_send_pinned`, so every redirect target is independently resolved + validated + pinned (no redirect-driven rebind).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/_file_exchange/test_outbound.py -v`
Expected: PASS (all Task 2/3/4 tests — the earlier non-redirect tests still pass against the loop, since a single terminal response yields on the first iteration).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_file_exchange/_outbound.py tests/_file_exchange/test_outbound.py
git commit -m "feat(file-exchange): same-origin redirect following for outbound guard (#147)"
```

---

### Task 5: Full local quality gates + draft PR

**Files:** none (verification + PR).

- [ ] **Step 1: Run the full repo check suite**

Run (matches CI and `CLAUDE.md`'s "Local checks before pushing"):

```bash
uv sync --all-extras
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: all green. Fix any failure and re-run before proceeding. If `ruff format --check` reports diffs, run `uv run ruff format .`, re-review, and amend the relevant commit or add a formatting commit.

- [ ] **Step 2: Confirm no public surface drift**

Run: `uv run python -c "import fastmcp_pvl_core.file_exchange as fx; assert not hasattr(fx, 'guarded_stream'); print('internal-only OK')"`
Expected: prints `internal-only OK` — the guard stays package-internal (not re-exported).

- [ ] **Step 3: Pre-push review (mandatory) and open the draft PR**

Per `CLAUDE.md`: invoke the `preflight-circus` skill on the cumulative `BASE..HEAD` diff and iterate until it returns `clean` (nothing flagged ≥80). Only then push the branch and open the PR **as draft** with `Closes #147` in the body. Bot iteration after open is capped at one round; re-run `preflight-circus` before any fix-push. Flip to ready only when local review was clean, bot bodies say LGTM, and CI is fully green. Do not merge autonomously.

---

## Self-Review

**1. Spec coverage** (design doc → task):
- https-only enforcement → Task 3 (`_send_pinned` scheme check; `test_refuses_non_https`).
- Deny-all-non-global + IPv4-mapped unwrap → Task 2 (`_is_permitted`; parametrized refusal test incl. `::ffff:127.0.0.1`, CGNAT, `0.0.0.0`).
- CIDR allowlist + malformed→ConfigurationError → Task 2 (`_parse_allowed_networks`) + Task 3 (`test_allowlist_permits_private_target`).
- Resolve-once-pin via `sni_hostname` → Task 3 (`_resolve`, `_send_pinned`; `test_..._pins`, `test_resolve_called_once_per_request`).
- No cross-origin redirect / follow same-origin bounded + re-guard → Task 4 (`_same_origin`, loop; four redirect tests incl. re-resolve count + hop cap).
- No ambient credentials + `trust_env=False` → Task 3 (`_make_client`; `test_make_client_security_defaults`, `test_passes_caller_headers_adds_no_credentials`).
- Timeout config → Task 1 (field) + Task 3 (`_make_client`; `test_make_client_security_defaults`).
- Error surface (`FileExchangeTransferError(NOT_ACCESSIBLE, transport=...)`) → Tasks 3/4 (every refusal asserts code + label).
- Redaction (hostname-only, generic wire detail) → Task 2 (`_redact` + tests) + Tasks 3/4 (`detail` asserts no URL parts).
- Streaming, no whole-buffer → Task 3 (`stream=True`, `aiter_bytes`).
- Cleanup on exit/exception → Task 3 (`finally`; `test_client_closed_on_caller_exception`).
- Two config fields + env parsing → Task 1.
- Package-internal (no public re-export) → Task 5 Step 2 guard.

No gaps found.

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to". Every code step shows complete code; every command shows expected output.

**3. Type consistency:** `_IPNetwork`/`_IPAddress` aliases used consistently; `_parse_allowed_networks → list[_IPNetwork]` consumed by `_is_permitted`/`_select_pinned`; `_resolve → list[str]` consumed by `_select_pinned`; `guarded_stream`/`_send_pinned`/`_make_client`/`GuardedResponse` signatures match their call sites and the test monkeypatch targets (`_outbound._resolve`, `_outbound._make_client`). `_MAX_REDIRECTS` referenced in impl and in `test_refuses_too_many_redirects`. Config field names (`file_exchange_allowed_networks`, `file_exchange_http_timeout`) match Task 1 and the `_cfg` test helper.
