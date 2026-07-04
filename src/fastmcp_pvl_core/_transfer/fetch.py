"""SSRF-hardened URL→bytes fetch primitive (ADR 0001 §11 #1).

A standalone, reusable capability: safely pull the bytes at an operator- or
caller-supplied HTTP(S) URL without becoming a server-side request-forgery
vector. Consumed by the transfer ingest plane, but useful anywhere a server
needs to fetch a URL.

Hardening (lifted from ``markdown-vault-mcp``'s proven fetch, ADR §8):

- **Scheme allowlist** — only ``http``/``https`` (fixed shape, not
  configurable; loosening it is an SSRF policy change pvl-core owns).
- **Address validation + DNS-rebind pin** — the host is resolved, *every*
  resolved address is rejected if non-public, and the validated IP is pinned
  into the connection (we dial the IP literal, carrying the real hostname only
  in the ``Host`` header and TLS SNI). This closes the TOCTOU window between
  validation and connect. Fails **closed** on any resolution error.
- **No redirects** — a 3xx response is refused (never followed to a possibly
  internal target). The explicit ``is_redirect`` check refuses every 3xx up
  front with a purpose-specific error, rather than letting a redirect surface
  later as httpx's generic status error.
- **No ambient environment** — the client runs with ``trust_env=False`` so an
  ``HTTP(S)_PROXY`` env var cannot divert the request past the pinned IP and
  ``.netrc`` cannot attach ambient credentials.
- **Streaming size cap** — the body is read in chunks and aborted the moment it
  exceeds ``max_bytes``; an oversize payload never fully materialises.
- **Credential hygiene** — embedded ``user:pass@`` userinfo is dropped from the
  dialled request (never relayed to the origin), and every URL this primitive
  *itself* emits — its log line and the exceptions it raises, including the
  ``HTTPStatusError`` re-raised on a 4xx/5xx — is redacted of userinfo + query
  via :func:`_redact_url` (ADR §8: redaction covers *all* the emit-paths this
  primitive owns, not only logs). httpx's own internal request logger also
  emits the URL; that is the operator's logging-config concern (pvl-core ships
  ``SecretMaskFilter``), not something a primitive should globally reconfigure.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_FETCH_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB
DEFAULT_FETCH_TIMEOUT_S = 30.0

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "localhost.localdomain", "metadata.google.internal"}
)
_SCHEME_DEFAULT_PORTS = {"http": 80, "https": 443}
_BLOCKED_ADDR_MSG = (
    "URLs targeting private, loopback, link-local, or reserved addresses "
    "are not allowed."
)
_CHUNK_SIZE = 65536
# RFC 6052 NAT64 well-known prefix. stdlib does not decode the IPv4 embedded in
# its low 32 bits for is_global, so we validate that IPv4 explicitly.
_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")


@dataclass(frozen=True)
class FetchResult:
    """The outcome of a successful :func:`fetch_url`.

    Attributes:
        body: The full response body (materialised in memory, bounded by the
            ``max_bytes`` cap the caller passed).
        content_type: The response ``Content-Type`` header, or ``None``.

    The byte count is exposed as the derived :attr:`size` property rather than a
    stored field, so it can never drift from ``len(body)``.
    """

    body: bytes
    content_type: str | None

    @property
    def size(self) -> int:
        """Number of bytes downloaded — ``len(body)``."""
        return len(self.body)


def _ip_is_blocked(ip: str) -> bool:
    """Return ``True`` if *ip* (an IP literal) is not a public, routable address.

    Uses a **permit-list** model — block anything that is not globally routable
    (``not is_global``) — which fails *closed* for every non-public range
    without enumerating them one by one: private, loopback, link-local,
    unspecified, reserved, benchmarking (198.18/15), and CGNAT / shared address
    space (100.64/10, RFC 6598) are all covered structurally, on IPv4, IPv6,
    and their IPv4-mapped forms. Multicast is blocked explicitly because the
    stdlib classifies it as ``is_global`` on the supported interpreters. This is
    the stricter posture the repo's earlier SSRF guard settled on (a plain
    deny-list of ``is_private``/``is_loopback``/… silently allowed CGNAT).

    NAT64 (RFC 6052 well-known prefix ``64:ff9b::/96``) is validated by decoding
    its embedded IPv4 (the low 32 bits) and recursing — otherwise a deployed
    NAT64 translator would route ``[64:ff9b::10.0.0.1]`` to a private/metadata
    target, and ``is_global`` does not decode it. A NAT64 address embedding a
    *public* IPv4 stays allowed. NAT64 is the **only** IPv4-embedding IPv6 form
    decoded here, deliberately: ``ipv4_mapped`` (``::ffff:a.b.c.d``) and 6to4
    (``2002::/16``) are already classified correctly by ``is_global``, and other
    embedding literals (e.g. the deprecated IPv4-compatible ``::/96``) have no
    routing guarantee — a modern stack dials the literal IPv6 address, which is
    unreachable, rather than translating to the embedded IPv4 — so they are not
    a practical SSRF vector.
    """
    addr = ipaddress.ip_address(ip)
    if addr.version == 6 and addr in _NAT64_WKP:
        embedded = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        return _ip_is_blocked(str(embedded))
    return addr.is_multicast or not addr.is_global


async def _resolve_addresses(hostname: str, port: int) -> list[str]:
    """Resolve *hostname* to a list of IP-literal strings via the event loop.

    A seam kept module-level so tests can inject resolution without real DNS.

    Raises:
        OSError: If resolution fails (``socket.gaierror`` is an ``OSError``).
        UnicodeError: If the hostname fails IDNA encoding (e.g. an over-long
            label). The caller converts both into a redacted ``ValueError``.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _resolve_pinned_ip(hostname: str, port: int) -> str:
    """Resolve *hostname* and return one validated public IP to pin to.

    Fails **closed**: raises :class:`ValueError` if the host is blocklisted,
    resolution errors, returns no records, or **any** resolved address is
    non-public. An IP-literal *hostname* is validated directly, without DNS.

    Args:
        hostname: The URL host (an IP literal or a domain name).
        port: Target port, used as the ``getaddrinfo`` service hint.

    Returns:
        A validated, public IP literal to connect to.
    """
    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError(_BLOCKED_ADDR_MSG)

    # IP-literal fast path: validate directly, no DNS lookup.
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if _ip_is_blocked(hostname):
            raise ValueError(_BLOCKED_ADDR_MSG)
        return hostname

    # Domain name: resolve and validate every returned address. Fail closed on
    # any resolution error so a DNS hiccup can never become an SSRF bypass.
    try:
        resolved = await _resolve_addresses(hostname, port)
    except (OSError, UnicodeError) as exc:
        # OSError = socket.gaierror (real resolution failure); UnicodeError =
        # IDNA encoding failure on a malformed/over-long hostname label. Both
        # funnel into one redacted ValueError so every resolution-failure class
        # fails closed with the same shape.
        raise ValueError(
            f"Could not resolve host {hostname!r} for the fetch URL: {exc}"
        ) from exc
    if not resolved:
        raise ValueError(f"Host {hostname!r} did not resolve to any address.")
    for ip in resolved:
        if _ip_is_blocked(ip):
            raise ValueError(
                f"Host {hostname!r} resolves to a non-public address ({ip}); "
                "refusing to fetch."
            )
    return resolved[0]


def _bracket_host(host: str) -> str:
    """Wrap an IPv6 literal in ``[]`` for a netloc / ``Host`` header.

    ``urlparse`` returns IPv6 hosts unbracketed, but a netloc or ``Host`` header
    must bracket them (``[::1]``) to be well-formed. A domain or IPv4 literal
    (no ``:``) is returned unchanged.
    """
    return f"[{host}]" if ":" in host else host


def _redact_url(url: str) -> str:
    """Return *url* with userinfo and query/fragment stripped.

    Operator-/caller-supplied URLs carry credentials in ``user:pass@`` userinfo
    and pre-signed tokens in the query string. Every message this primitive
    itself emits — log lines and the exceptions it raises alike — passes the URL
    through here first (ADR §8).
    """
    parsed = urlparse(url)
    netloc = _bracket_host(parsed.hostname or "")
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc, query="", fragment=""))


async def fetch_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_FETCH_MAX_BYTES,
    timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchResult:
    """Fetch the bytes at *url* with SSRF hardening and a streaming size cap.

    Args:
        url: An ``http``/``https`` URL. The host is resolved and rejected if any
            address is non-public; the validated IP is pinned for the
            connection (DNS-rebind safe); redirects are not followed.
        max_bytes: Positive per-fetch size cap; the download aborts the moment
            it is exceeded.
        timeout_s: Overall request timeout in seconds.
        transport: An optional ``httpx`` transport (an extension point used by
            tests to inject a ``MockTransport``); ``None`` uses real networking.

    Returns:
        A :class:`FetchResult` with the body, content type, and byte size.

    Raises:
        ValueError: On a non-positive *max_bytes* or *timeout_s*, a non-http(s)
            scheme, a missing host, a blocked/non-public address, a resolution
            failure, a refused redirect, or a body that exceeds *max_bytes*.
        httpx.HTTPStatusError: On a 4xx/5xx response status (re-raised with a
            redacted message; the type and ``request``/``response`` are kept for
            programmatic inspection). Note: the retained ``exc.request.url``
            still contains the query string, so callers should not log it
            verbatim — the redacted message is the safe surface.
        httpx.TransportError: Transport-level failures propagate uncaught — e.g.
            ``httpx.TimeoutException`` when *timeout_s* expires, or a connection
            error. This primitive does not wrap them.
    """
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive, got {max_bytes}")
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be positive, got {timeout_s}")

    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Only http and https URLs are allowed, got {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("The fetch URL has no host.")
    port = parsed.port or _SCHEME_DEFAULT_PORTS[parsed.scheme]

    # Resolve + validate now and pin the resulting IP into the connection: the
    # address validated here is the one actually dialled. Fails closed.
    pinned_ip = await _resolve_pinned_ip(hostname, port)

    # Dial the validated IP, but keep the real hostname in the Host header and
    # TLS SNI so vhost routing and certificate verification still work. Any
    # userinfo in the original URL is intentionally dropped (never relayed).
    ip_host = _bracket_host(pinned_ip)
    pinned_netloc = f"{ip_host}:{parsed.port}" if parsed.port else ip_host
    pinned_url = urlunparse(parsed._replace(netloc=pinned_netloc))
    # The Host header carries the real hostname; bracket it if it is an IPv6
    # literal (urlparse hands it back unbracketed) so the header is well-formed.
    host_for_header = _bracket_host(hostname)
    if parsed.port is None or parsed.port == _SCHEME_DEFAULT_PORTS.get(parsed.scheme):
        host_header = host_for_header
    else:
        host_header = f"{host_for_header}:{parsed.port}"

    chunks: list[bytes] = []
    downloaded = 0
    async with (
        httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=False,
            trust_env=False,
            # trust_env=False: ignore ambient HTTP(S)_PROXY / .netrc. A proxy env
            # var would divert the request through an operator-uncontrolled host,
            # bypassing the resolve-validate-pin chain entirely (an SSRF bypass);
            # .netrc could attach ambient credentials to the outbound request.
            transport=transport,
        ) as client,
        client.stream(
            "GET",
            pinned_url,
            headers={"Host": host_header},
            extensions={"sni_hostname": hostname},
        ) as response,
    ):
        if response.is_redirect:
            raise ValueError(
                f"Refusing the 3xx redirect response from {_redact_url(url)} "
                "(redirects are not followed)."
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Re-raise with a redacted message: httpx embeds the request URL
            # (query string included) in the default message, which carries any
            # pre-signed token. Keep the type + request/response so callers can
            # still inspect status programmatically. `from None` sets
            # __suppress_context__ — otherwise httpx's original un-redacted
            # message would still surface via __context__ (implicit chaining)
            # in a rendered traceback (ADR §8: redact the messages this
            # primitive itself raises).
            raise httpx.HTTPStatusError(
                f"HTTP {response.status_code} response from {_redact_url(url)}",
                request=exc.request,
                response=exc.response,
            ) from None
        content_type = response.headers.get("content-type")
        async for chunk in response.aiter_bytes(chunk_size=_CHUNK_SIZE):
            downloaded += len(chunk)
            if downloaded > max_bytes:
                raise ValueError(
                    f"Download from {_redact_url(url)} exceeded the size cap "
                    f"of {max_bytes} bytes."
                )
            chunks.append(chunk)

    body = b"".join(chunks)
    logger.info(
        "fetch_url: downloaded %d bytes from %s (content-type=%s)",
        downloaded,
        _redact_url(url),
        content_type,
    )
    return FetchResult(body=body, content_type=content_type)
