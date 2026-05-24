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
        _outbound._select_pinned(["127.0.0.1", "93.184.216.34"], [])
        == "93.184.216.34"
    )


def test_select_pinned_none_when_all_blocked():
    assert _outbound._select_pinned(["127.0.0.1", "10.0.0.1"], []) is None


def test_select_pinned_picks_global_ipv6_over_blocked_ipv4():
    resolved = ["10.0.0.1", "2606:2800:220:1:248:1893:25c8:1946"]
    assert (
        _outbound._select_pinned(resolved, [])
        == "2606:2800:220:1:248:1893:25c8:1946"
    )
