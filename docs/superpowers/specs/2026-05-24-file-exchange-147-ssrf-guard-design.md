# File-Exchange #147 — SSRF + DNS-rebind guard for outbound HTTP

> **Status:** Contemporaneous design record for issue #147 (9/10 of EPIC
> #138). The implementation in the same PR is the source of truth; this
> captures the shape agreed before implementation. This is **not** a wire
> spec — #147 is pvl-core's own internal outbound-HTTP mechanism layer,
> governed by `CLAUDE.md` and the project's framing principle, not by
> `mcp-file-exchange-ext`. No `docs/specs/` wire-format file is touched.

**Goal:** Provide the single, centralized outbound-HTTP primitive that the
`download` fetcher (#145) and the `upload` sender (#146) issue their requests
through. It enforces the §15 SSRF mitigations (https-only, private/non-global
address refusal, no cross-origin redirect, no ambient credentials) and the §15
DNS-rebinding mitigation (resolve-once-pin-IP), and carries the URL-redaction
discipline. It does **not** do Range recovery, size/digest verification, or any
artifact handling — those are the consuming data planes' own deliverables
(#145/#146).

## Scope (from #138's decomposition + #147's scope statement)

#147 owns exactly the outbound connection: scheme enforcement, single DNS
resolution + address validation, IP pinning, redirect policy, no-ambient-creds,
bounded timeouts, and redaction. It yields a streaming response; the **caller**
reads the body. This boundary is deliberate — the EPIC gives "verify `size` and
`digest`, recover a dropped connection with `Range`" to #145 and the upload body
to #146. Folding either into the guard would collapse that split, so the guard
stays a thin §15 primitive.

The guard is **package-internal** plumbing: #145/#146 import it from
`_file_exchange._outbound`. It is *not* re-exported through the public
`file_exchange.py` namespace — downstream servers never call it directly (they
wire the data plane via #148's register helpers). Only the two new `ServerConfig`
fields are operator-facing.

## Module shape

New module `src/fastmcp_pvl_core/_file_exchange/_outbound.py`, the outbound-HTTP
sibling of `_filesystem.py`. **Free functions / an async context manager**,
mirroring the rest of the package — no service-object pattern. pvl-core already
depends on `httpx` (used by `_auth.py`); the guard builds on it.

The name is `_outbound` rather than `_ssrf`/`_http_guard`: SSRF refusal is what
it *enforces*, not the whole of what it does (it is the one place all
file-exchange outbound HTTP goes through).

## Public function

```python
@asynccontextmanager
async def guarded_stream(
    method: str,
    url: str,
    *,
    config: ServerConfig,
    transport: str,                       # "download" | "upload" — §13 envelope label
    headers: Mapping[str, str] | None = None,
    content: AsyncIterable[bytes] | bytes | None = None,   # request body (#146 upload)
) -> AsyncIterator[GuardedResponse]: ...
```

`GuardedResponse` is a thin frozen dataclass exposing `status: int`,
`headers: Mapping[str, str]`, and `aiter_bytes() -> AsyncIterator[bytes]`. It
wraps the streaming `httpx.Response` so the raw URL (with its capability token in
the path/query) cannot leak through httpx's `repr`/traceback. The caller drives
the body read inside the `async with`; the context manager closes the response
and the client on exit (including on caller exceptions).

`content` is present from the start so #146's sender streams a request body
without changing this signature later; #145's fetcher leaves it `None` and passes
a `Range` header via `headers`.

## Per-call / per-redirect-hop algorithm

Steps 1–6 run once per request, and re-run in full for each followed redirect hop
(a hop is a new URL → a new resolve+validate+pin; this is what makes
redirect-following rebind-safe).

1. **Scheme.** Parse `url`; require `https://`. Otherwise refuse.
2. **Resolve once.** `await loop.getaddrinfo(host, port, type=SOCK_STREAM)` —
   a single resolution whose result is the only address set used for both the
   check and the connection. The hostname is never re-resolved between them.
3. **Validate.** For each returned address: parse with `ipaddress`, unwrap an
   IPv4-mapped IPv6 address (`ip.ipv4_mapped`) first, then permit it iff
   `ip.is_global` **or** it is contained in one of the operator's allowlisted
   networks. Pick the first permitted address as the pinned IP. If no address is
   permitted → refuse (blocked). If resolution fails or returns nothing → refuse
   (unresolvable).
4. **Pin + connect.** Issue the request to `https://<pinned-ip>:<port><path+query>`
   with `extensions={"sni_hostname": host}` — so TLS SNI and certificate
   verification still target the hostname while the socket connects to the
   validated IP. The guard **owns** the `Host` header: it strips any
   caller-supplied `host` key (case-insensitive) and forces the validated
   hostname (a non-default port is preserved and an IPv6 literal bracketed), so a
   caller cannot smuggle a different vhost onto the pinned IP. It also strips any
   userinfo (`user:pass@`) from the URL, since httpx would otherwise synthesise an
   `Authorization: Basic` header from it. `follow_redirects=False`. `timeout=`
   from `config.file_exchange_http_timeout`. Streamed (the body is not read here).
   The `httpx.AsyncClient` is built with **`trust_env=False`** and no auth/cookies:
   no ambient credentials, and no `HTTP(S)_PROXY`/`NETRC` interference (a proxy
   env var would otherwise route around the IP pin — an SSRF bypass). Caller
   headers other than `Host` pass through; the guard adds no credential headers.
5. **Redirects.** On a redirect status carrying a `Location` header (httpx
   `has_redirect_location` — a `304` or a `Location`-less 3xx is *not* a redirect
   and is yielded as a terminal response). `has_redirect_location` is true on
   header *presence* alone, so a present-but-**empty** `Location` is likewise
   treated as terminal and yielded (not a usable redirect target). Otherwise
   resolve `Location` relative to the current request URL; a value `httpx.URL.join`
   cannot parse is refused as a **malformed redirect location** (a raw
   `httpx.InvalidURL` must not escape the guard). If the request carries a body
   (`content is not None`), refuse — a streaming request body cannot be safely
   replayed across a redirect hop. If it is **same-origin** (case-normalized
   scheme+host+port match) and the hop count is below the cap (5), recurse from
   step 1 against the new URL (full re-resolve + re-validate + re-pin). If it is
   **cross-origin**, refuse. If the hop cap is exceeded, refuse.
6. **Yield.** Otherwise wrap the streaming response in `GuardedResponse` and
   yield it.

## Decisions

### Address posture: deny all non-global

Refuse any resolved address that is not globally routable
(`not ipaddress.ip_address(...).is_global`), rather than enumerating only the
three ranges in the issue text. `is_global` already covers loopback, link-local,
and RFC 1918, and additionally closes CGNAT (`100.64.0.0/10`), reserved,
multicast, the unspecified address (`0.0.0.0`/`::`), and — after unwrapping —
IPv4-mapped IPv6 (`::ffff:127.0.0.1`) and similar. Enumerating a fixed blocklist
leaves exactly those as bypasses; deny-all-non-global is defense in depth, with
the allowlist as the single escape hatch. This is a pvl-core **shape** decision:
downstream conforms.

### Allowlist: CIDR networks, not hostnames or a blanket toggle

`config.file_exchange_allowed_networks` is a list of CIDR strings; a resolved IP
contained in any listed network bypasses the non-global refusal. The check stays
an **IP** membership test *after* pinning, so it cannot reintroduce DNS rebinding
(a hostname allowlist would, since a hostname can resolve to anything). It is
granular per the spec's "explicitly allowed a *specific* target," unlike a single
"allow private" boolean that would disable the protection wholesale.

### Resolve-once-pin via httpx `sni_hostname`

The DNS-rebind mitigation is "resolve the target hostname once and connect to
that pinned IP, rather than re-resolving between the check and the connection"
(§15). httpx's documented `sni_hostname` request extension makes this exact: the
request URL carries the validated IP, the `Host` header and `sni_hostname` carry
the hostname, so the certificate is verified against the hostname while the socket
connects only to the address that passed validation. The window between check and
connect contains no second resolution. Across *separate* requests (e.g. #145's
`Range` reconnect after a dropped connection), re-resolving is correct and
expected — the mitigation is per-request, and each reconnect is a new guarded
call.

### Redirects: refuse cross-origin, follow same-origin (bounded, re-guarded)

The spec's hard rule is "MUST NOT follow redirects to a different origin"; the
issue phrases it "no-cross-origin-redirect behaviour." Same-origin redirects are
therefore permitted and are followed, but only after re-running the full guard on
each hop and only up to 5 hops. This is the only genuinely fiddly part of the
module (relative-`Location` resolution, origin comparison, per-hop re-guard, hop
cap); it is accepted over the simpler "refuse all redirects" to stay faithful to
the spec wording and the issue's intent. Following without re-guarding each hop
would be a redirect-driven rebind hole — hence the re-run.
A redirect on a request that carries a body is refused outright — the guard cannot safely replay a streaming request body across hops, so the upload sender (#146) re-issues rather than risk a silently-truncated upload.

### No ambient credentials and `trust_env=False`

§10.2/§10.3 require the request to carry no cookies, no `Authorization`, no
client certificates — the capability URL is the only credential. The guard
builds a fresh client per call with no auth and no cookie jar, and crucially sets
`trust_env=False` so environment proxies and `netrc` cannot inject credentials or
divert the connection past the pinned IP. Two further leaks are closed at request
build: any userinfo in the URL (`user:pass@`) is stripped, because httpx would
otherwise synthesise an `Authorization: Basic` header from it; and any
caller-supplied `Host` header is stripped in favour of the validated hostname, so
a caller cannot smuggle the request onto a different vhost on the pinned IP. Both
apply to the initial URL and to every same-origin redirect hop.

### Timeouts

A single operator field `config.file_exchange_http_timeout` (default `30.0`
seconds) is applied to httpx's connect/read/write phases. A stalled connect or a
body that dribbles bytes below the per-read timeout is a resource-exhaustion
vector (§15) the guard bounds. A **total**-transfer budget is deliberately *not*
imposed here — it would wrongly kill legitimate large transfers; bounding overall
duration is the Tasks / #145 concern.

### Error surface: `FileExchangeTransferError`, code `not-accessible`

Every refusal and connection failure raises
`FileExchangeTransferError(TransferErrorCode.NOT_ACCESSIBLE, transport=transport,
detail="<generic>")` — the same typed exception `_filesystem.py` raises, matching
the established package pattern (no parallel exception hierarchy is introduced).
All guard failures are pre-body (scheme, resolution, address, redirect, connect,
timeout) and §13's `not-accessible` is documented as "an endpoint was
unreachable," so they map to one code; no per-caller judgment is needed. The
guard takes the `transport` label as a parameter because it is shared by the
`download` and `upload` data planes and cannot know its own caller's role. The
original cause is chained for local logs; #148's middleware renders the wire
envelope via `build_file_exchange_error`, exactly as for the filesystem transport.

### URL redaction

A module-private `_redact(url) -> str` returns the **hostname only**. The
capability token lives in the URL path/query, so those are never logged and never
placed in a wire `detail`. Wire `detail` strings stay generic (no URL parts at
all — they cross to the peer); local debug logs may include the hostname,
consistent with `_kv_store.py` logging `parsed.hostname`. This carries forward
the redaction discipline a prior reviewer caught missing on PR #122.

## Config (ServerConfig, file-exchange group)

Two new fields, parsed by `from_env`, sitting beside the existing
`file_exchange_token_ttl` / `file_exchange_max_artifact_size`:

| Field | Env var | Default | Meaning |
|---|---|---|---|
| `file_exchange_allowed_networks: tuple[str, ...]` | `<PREFIX>_FILE_EXCHANGE_ALLOWED_NETWORKS` | `()` | comma-separated CIDRs that bypass the non-global refusal |
| `file_exchange_http_timeout: float` | `<PREFIX>_FILE_EXCHANGE_HTTP_TIMEOUT` | `30.0` | connect/read/write timeout (seconds) for guarded requests |

CIDR strings are parsed to `ipaddress.ip_network` in the guard at first use; a
malformed entry raises `ConfigurationError` (loud operator-misconfiguration
failure, consistent with the rest of the package). These are operator-side
**configuration**, not domain hooks and not shape — the env-var axis, per
`CLAUDE.md`.

## Error handling

The single failure type is `FileExchangeTransferError` with code
`not-accessible` (see the decision above). The async context manager is
fail-safe: the streaming response and client are closed on `__aexit__`, including
when the caller's body-reading block raises, so no connection leaks. The guard
buffers no bytes — it hands back a stream the caller consumes.

## Testing (`tests/_file_exchange/test_outbound.py`)

Driven by TDD, mirroring `test_filesystem`'s structure. Network is never touched
for real: `getaddrinfo` is monkeypatched to return chosen addresses, and an
`httpx.MockTransport` captures the actual outgoing request so the pin can be
asserted.

Refusals (each → `not-accessible`):

1. Non-`https` scheme.
2. Resolution to loopback / link-local / RFC 1918 / CGNAT / IPv4-mapped-loopback
   / `0.0.0.0`.
3. Unresolvable host (empty / failing `getaddrinfo`).
4. Cross-origin redirect.
5. Redirect hop cap exceeded (>5).
6. Connect failure / timeout (MockTransport raising `httpx.ConnectError` /
   `httpx.ConnectTimeout`).

Behaviour:

7. **Allowlist override** — an address in a blocked range becomes permitted when
   its network is in `file_exchange_allowed_networks`; a malformed CIDR raises
   `ConfigurationError`.
8. **Pin behaviour** — `getaddrinfo` returns IP `X`; assert the captured request
   targets `X`, with `Host` and `sni_hostname` equal to the hostname, and assert
   `getaddrinfo` is called **once per hop** (DNS-rebind proof — no re-resolution
   between check and connect).
9. **Same-origin redirect** — followed (re-resolved + re-validated each hop),
   terminal response reaches the caller; relative `Location` is resolved against
   the current URL.
10. **No ambient creds** — the captured request carries no `Authorization`/cookie
    header the caller did not pass; the client is `trust_env=False`.
11. **Streaming + cleanup** — the body is yielded chunked (never buffered whole);
    the response/client are closed even when the caller's block raises.
12. **Redaction** — no token / path / query appears in any captured log record or
    in the exception `detail`; `detail` is generic.

## Public surface

The guard is **package-internal**. `guarded_stream` and `GuardedResponse` are
importable from `fastmcp_pvl_core._file_exchange._outbound` by #145/#146; they are
**not** added to `file_exchange.py`'s or the subpackage `__init__.py`'s public
`__all__` (downstream does not call them). The two `ServerConfig` fields are the
only operator-facing additions. `FileExchangeTransferError` / `TransferErrorCode`
are already exported (reused, not re-declared).

## References

- EPIC #138 (adopt mcp-file-exchange-ext v0.1); this is 9/10. Depends on #139
  (wire format — provides `FileExchangeTransferError` / `TransferErrorCode`).
- #145 (download data plane) and #146 (upload data plane) — the two consumers;
  they own Range recovery, size/digest verification, and artifact handling, which
  this guard deliberately excludes.
- #148 (register helpers + Tasks integration) — renders the §13 wire envelope
  from the `FileExchangeTransferError` this guard raises.
- Wire spec (`mcp-file-exchange-ext`, pinned commit `5f50a4e…`): §7.2.2/§7.2.4
  (download/upload descriptors), §10.2/§10.3 (download/upload transport
  obligations — no ambient creds, https-only, no cross-origin redirect), §12
  (capability URLs), §15 (security considerations — SSRF, DNS rebinding,
  redaction, resource exhaustion). This document is **not** a wire spec.
- `CLAUDE.md` — the framing principle (operator config is the env-var axis, not a
  kwarg; pvl-core owns shape decisions like the deny-all-non-global posture); the
  redaction discipline carried forward from PR #122.
- httpx `sni_hostname` request extension — the documented mechanism for
  connect-to-IP-with-hostname-cert-verification.
