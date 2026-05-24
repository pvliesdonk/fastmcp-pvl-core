import ipaddress
import logging
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


def test_ipv4_mapped_cidr_in_allowlist_raises_configuration_error():
    # An IPv4-mapped IPv6 CIDR would silently never match (membership tests the
    # unwrapped IPv4 form), so it is rejected loudly — the operator means the
    # canonical IPv4 CIDR.
    with pytest.raises(ConfigurationError):
        _outbound._parse_allowed_networks(("::ffff:10.0.0.0/104",))


def test_non_mapped_ipv6_cidr_in_allowlist_accepted():
    # A normal (non-mapped) IPv6 range must still parse — the mapped-CIDR guard
    # uses subnet_of, so a broad/ULA range is not over-rejected.
    nets = _outbound._parse_allowed_networks(("fc00::/7",))
    assert len(nets) == 1


@pytest.mark.parametrize(
    ("addr", "cidr"),
    [("10.0.0.1", "fc00::/7"), ("fc00::1", "10.0.0.0/8")],
)
def test_is_permitted_mixed_version_returns_false_not_raises(addr, cidr):
    # An address tested against a different-version allowlist network must yield
    # False (ipaddress __contains__ version-checks), never raise — the guard
    # docstring's "never raises" contract.
    allowed = _outbound._parse_allowed_networks((cidr,))
    assert _outbound._is_permitted(ipaddress.ip_address(addr), allowed) is False


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


# ---------------------------------------------------------------------------
# Task 4: Redirect handling tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task 5: Userinfo / no-ambient-credentials tests
# ---------------------------------------------------------------------------


async def test_initial_url_userinfo_not_sent_as_credentials(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"x")

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET",
        "https://user:pass@example.com/d/tok",
        config=_cfg(),
        transport="download",
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    assert "authorization" not in seen[0].headers


async def test_redirect_userinfo_not_sent_as_credentials(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                302, headers={"location": "https://user:pass@example.com/final"}
            )
        return httpx.Response(200, content=b"ok")

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET", "https://example.com/start", config=_cfg(), transport="download"
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    assert "authorization" not in seen[1].headers


async def test_empty_location_redirect_is_yielded(monkeypatch):
    # has_redirect_location is True for a present-but-empty Location; an empty
    # value is not a usable redirect target, so it must be yielded as terminal
    # rather than looping (join("") -> current) until "too many redirects".
    def handler(request):
        return httpx.Response(302, headers={"location": ""})

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET", "https://example.com/x", config=_cfg(), transport="download"
    ) as resp:
        assert resp.status == 302
        async for _ in resp.aiter_bytes():
            pass


async def test_refuses_malformed_redirect_location(monkeypatch):
    # A Location that httpx.URL.join cannot parse must surface as a coded
    # FileExchangeTransferError, not a raw httpx.InvalidURL escaping the guard.
    def handler(request):
        return httpx.Response(302, headers={"location": "ht\x00tp://x"})

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https://example.com/start", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_refuses_scheme_downgrade_redirect(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "http://example.com/x"})

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https://example.com/start", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_refuses_redirect_that_rebinds_to_blocked_ip(monkeypatch):
    seen = []
    resolves = [["93.184.216.34"], ["127.0.0.1"]]  # hop 1 global, hop 2 loopback

    async def fake_resolve(host, port):
        return resolves[len(seen)]

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://example.com/next"})

    def fake_make_client(timeout):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr(_outbound, "_resolve", fake_resolve)
    monkeypatch.setattr(_outbound, "_make_client", fake_make_client)
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https://example.com/start", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE
    assert ei.value.detail == "address not permitted"


async def test_refuses_redirect_on_request_with_body(monkeypatch):
    async def body():
        yield b"upload-bytes"

    def handler(request):
        return httpx.Response(302, headers={"location": "https://example.com/final"})

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "PUT",
            "https://example.com/start",
            config=_cfg(),
            transport="upload",
            content=body(),
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_refuses_empty_resolution(monkeypatch):
    async def fake_resolve(host, port):
        return []

    monkeypatch.setattr(_outbound, "_resolve", fake_resolve)
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https://example.com/x", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE
    assert ei.value.detail == "host could not be resolved"


async def test_nonpositive_timeout_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        async with _outbound.guarded_stream(
            "GET",
            "https://example.com/x",
            config=_cfg(timeout=0.0),
            transport="download",
        ):
            pass


async def test_http_timeout_threads_to_client(monkeypatch):
    seen = {}

    def handler(request):
        return httpx.Response(200, content=b"x")

    async def fake_resolve(host, port):
        return ["93.184.216.34"]

    def fake_make_client(timeout):
        seen["timeout"] = timeout
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr(_outbound, "_resolve", fake_resolve)
    monkeypatch.setattr(_outbound, "_make_client", fake_make_client)
    async with _outbound.guarded_stream(
        "GET",
        "https://example.com/x",
        config=_cfg(timeout=12.5),
        transport="download",
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    assert seen["timeout"] == 12.5


async def test_guarded_response_repr_hides_token(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x")

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET",
        "https://example.com/d/SECRETTOKEN?sig=abc",
        config=_cfg(),
        transport="download",
    ) as resp:
        rendered = repr(resp)
        assert "SECRETTOKEN" not in rendered
        assert "/d/" not in rendered
        # Pin repr=False on _response: with repr=True the field (and the
        # underlying response/request that carries the URL) would be rendered.
        assert "_response" not in rendered
        async for _ in resp.aiter_bytes():
            pass


async def test_client_closed_on_success(monkeypatch):
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
    async with _outbound.guarded_stream(
        "GET", "https://example.com/x", config=_cfg(), transport="download"
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    assert holder["client"].is_closed is True


async def test_client_closed_on_cross_origin_refusal(monkeypatch):
    holder = {}

    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

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
    with pytest.raises(FileExchangeTransferError):
        async with _outbound.guarded_stream(
            "GET", "https://example.com/start", config=_cfg(), transport="download"
        ):
            pass
    assert holder["client"].is_closed is True


async def test_non_redirect_3xx_is_yielded(monkeypatch):
    def handler(request):
        return httpx.Response(304)

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET", "https://example.com/x", config=_cfg(), transport="download"
    ) as resp:
        assert resp.status == 304
        async for _ in resp.aiter_bytes():
            pass


async def test_3xx_without_location_is_yielded(monkeypatch):
    def handler(request):
        return httpx.Response(302)  # 3xx but no Location header

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET", "https://example.com/x", config=_cfg(), transport="download"
    ) as resp:
        assert resp.status == 302
        async for _ in resp.aiter_bytes():
            pass


async def test_host_header_includes_non_default_port(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"x")

    _install_mock(monkeypatch, handler, resolve_to=["93.184.216.34"])
    async with _outbound.guarded_stream(
        "GET", "https://example.com:8443/x", config=_cfg(), transport="download"
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    req = seen[0]
    assert req.headers["host"] == "example.com:8443"
    assert req.url.host == "93.184.216.34"
    assert req.url.port == 8443
    assert req.extensions.get("sni_hostname") == "example.com"


async def test_refuses_url_without_host():
    with pytest.raises(FileExchangeTransferError) as ei:
        async with _outbound.guarded_stream(
            "GET", "https:///path", config=_cfg(), transport="download"
        ):
            pass
    assert ei.value.code == TransferErrorCode.NOT_ACCESSIBLE


async def test_body_iteration_error_propagates_raw_and_closes_client(monkeypatch):
    holder = {}

    async def raising_stream():
        yield b"partial"
        raise httpx.ReadError("mid-stream failure")

    def handler(request):
        return httpx.Response(200, content=raising_stream())

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
    # A failure during body iteration must surface as a raw httpx error (so the
    # data plane can map/recover), NOT remapped to FileExchangeTransferError.
    with pytest.raises(httpx.HTTPError):
        async with _outbound.guarded_stream(
            "GET", "https://example.com/x", config=_cfg(), transport="download"
        ) as resp:
            async for _ in resp.aiter_bytes():
                pass
    assert holder["client"].is_closed is True


@pytest.mark.parametrize(
    "scenario",
    ["dns-failure", "empty-resolution", "non-global", "connect-error"],
)
async def test_failure_logs_do_not_leak_credentials_or_token(
    monkeypatch, caplog, scenario
):
    # Sweep every logger.debug emit-path in _send_pinned, not just one — the
    # URL-credential-redaction discipline (PR #122) requires all of them be
    # proven to log only the redacted hostname.
    async def fake_resolve(host, port):
        if scenario == "dns-failure":
            raise socket.gaierror("resolution failed")
        if scenario == "empty-resolution":
            return []
        if scenario == "non-global":
            return ["127.0.0.1"]
        return ["93.184.216.34"]  # connect-error: reach the send path

    def handler(request):
        raise httpx.ConnectError("connection refused")

    def fake_make_client(timeout):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr(_outbound, "_resolve", fake_resolve)
    monkeypatch.setattr(_outbound, "_make_client", fake_make_client)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(FileExchangeTransferError):
            async with _outbound.guarded_stream(
                "GET",
                "https://carol:hunter2@example.com/d/SECRETTOKEN",
                config=_cfg(),
                transport="download",
            ):
                pass
    blob = " ".join(record.getMessage() for record in caplog.records)
    assert "hunter2" not in blob
    assert "carol" not in blob
    assert "SECRETTOKEN" not in blob


async def test_host_header_brackets_ipv6_literal(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"x")

    ipv6 = "2606:2800:220:1:248:1893:25c8:1946"
    _install_mock(monkeypatch, handler, resolve_to=[ipv6])
    async with _outbound.guarded_stream(
        "GET", f"https://[{ipv6}]:8443/x", config=_cfg(), transport="download"
    ) as resp:
        async for _ in resp.aiter_bytes():
            pass
    req = seen[0]
    assert req.headers["host"] == f"[{ipv6}]:8443"
    assert req.extensions.get("sni_hostname") == ipv6  # bare, no brackets
    assert req.url.host == ipv6
