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
    (loud), consistent with the rest of the package. Allowlist entries must use
    the canonical address form (e.g. ``10.0.0.0/8``); an IPv4-mapped-IPv6 CIDR
    (``::ffff:10.0.0.0/104``) parses but will never match, because membership is
    tested against the unwrapped IPv4 candidate (see :func:`_is_permitted`).
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

    ``resolved`` entries must be valid IP-address strings as returned by
    ``socket.getaddrinfo`` (``ipaddress.ip_address`` would raise on a malformed
    one). Picking a permitted address out of a mixed result set is safe because
    the connection is then pinned to exactly that address — a DNS result that
    mixes a private and a public record cannot induce a connection to the
    private one.
    """
    for addr in resolved:
        if _is_permitted(ipaddress.ip_address(addr), allowed):
            return addr
    return None
