import ipaddress
import socket

import httpx
import pytest

from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange import _outbound
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError


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


def test_allowlist_permits_ipv4_mapped_private():
    allowed = _outbound._parse_allowed_networks(("192.168.0.0/16",))
    assert (
        _outbound._is_permitted(ipaddress.ip_address("::ffff:192.168.1.1"), allowed)
        is True
    )


def test_malformed_cidr_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        _outbound._parse_allowed_networks(("not-a-cidr",))


def test_select_pinned_skips_blocked_picks_global():
    assert (
        _outbound._select_pinned(["127.0.0.1", "93.184.216.34"], []) == "93.184.216.34"
    )


def test_select_pinned_none_when_all_blocked():
    assert _outbound._select_pinned(["127.0.0.1", "10.0.0.1"], []) is None


def test_select_pinned_picks_global_ipv6_over_blocked_ipv4():
    resolved = ["10.0.0.1", "2606:2800:220:1:248:1893:25c8:1946"]
    assert (
        _outbound._select_pinned(resolved, []) == "2606:2800:220:1:248:1893:25c8:1946"
    )


# ---------------------------------------------------------------------------
# Task 3: guarded_stream core tests
# ---------------------------------------------------------------------------


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
        assert client.timeout == httpx.Timeout(30.0)
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


async def test_caller_cannot_inject_host_header(monkeypatch):
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
        headers={"Host": "evil.com", "host": "evil2.com"},
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    assert seen[0].headers["host"] == "example.com"


async def test_client_closed_after_send_pinned_raises(monkeypatch):
    holder = {}

    async def fake_resolve(host, port):
        return ["127.0.0.1"]  # non-global -> refusal before yield

    def fake_make_client(timeout):
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        holder["client"] = client
        return client

    monkeypatch.setattr(_outbound, "_resolve", fake_resolve)
    monkeypatch.setattr(_outbound, "_make_client", fake_make_client)
    with pytest.raises(FileExchangeTransferError):
        async with _outbound.guarded_stream(
            "GET", "https://example.com/x", config=_cfg(), transport="download"
        ):
            pass
    assert holder["client"].is_closed is True
