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
    # The guard owns the Host header (it must match the validated hostname so a
    # caller cannot redirect the request to a different vhost on the pinned IP);
    # strip any caller-supplied host key (case-insensitive) before forcing ours.
    safe_headers = {k: v for k, v in (headers or {}).items() if k.lower() != "host"}
    request = client.build_request(
        method,
        httpx.URL(url).copy_with(host=pinned),
        headers={"Host": host, **safe_headers},
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
        current = url
        for _hop in range(_MAX_REDIRECTS + 1):
            response = await _send_pinned(
                client, method, current, transport, allowed, headers, content
            )
            if response.is_redirect:
                target = str(
                    httpx.URL(current).join(response.headers.get("location", ""))
                )
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
