# Design: re-implement the http_upload receiver primitive (issue #74)

**Status**: approved (brainstorm 2026-05-15)
**Issue**: [pvliesdonk/fastmcp-pvl-core#74](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/74)
**Umbrella**: #75
**Depends on**: #83 (dual-role spec) and #86 (capability builder) — both merged.

## Problem

`register_file_exchange_upload`, the `create_upload_link` MCP tool, and the
POST upload route were built against the LLM-invented "Amendment 11", not a
real spec. They diverge from the v0.3.0 `http_upload` contract on every
externally observable surface:

- The tool parameter is `target_id`; the spec names it `origin_id` and adds
  optional `destination` (the WHERE) and `content_type`.
- The tool return is `{upload_url, expires_in_seconds, target_id}`; the spec
  mandates `{url, ttl_seconds, max_bytes}` — and `max_bytes` is currently
  absent entirely (a MUST field).
- The POST route emits `410 Gone` for an expired token; the spec requires all
  three token-unusable conditions (unknown / expired / consumed) to be
  **indistinguishable** `404`s (anti-leak).
- In-band tool rejection returns a generic tool error, not the spec's
  `transfer_failed` envelope.
- `UploadRecord` collapses the WHAT/WHERE split into one `target_id` field
  and carries a non-spec `extra` dict.
- `register_file_exchange_upload` carries 13 kwargs — including
  `upload_tool_name` and `tool_tags` (shape overrides) and four operator-
  config kwargs — failing the #72/#73 classification test.

#74 is a clean-slate rebuild of the receiver (`sink`) side, 100% conformant
to the v0.3.0 spec. The capability declaration is already migrated (#86);
the sender (`source`) side is #85.

## Scope

In scope: `register_file_exchange_upload`, the `create_upload_link` tool, the
POST upload route, the `UploadRecord` shape, and the `_token_store.py`
`UploadRecord`/`consume` surface. The token-store *mechanics* (UUID4 token —
128 bits of entropy, atomic one-time consumption on first POST, lazy TTL
purge) are already spec-conformant and are kept as-is.

Out of scope: the sender-side `upload` tool (`http_upload.source`) — #85; the
capability builder — done in #86; the `http` (download) method — #87; the
stale "Amendment 11" comments are re-grounded against v0.3.0 spec sections
as part of this rebuild.

## The design

### 1. `register_file_exchange_upload` — kwarg surface

Six kwargs, all domain hooks (matching the `register_file_exchange` worked
example in CLAUDE.md):

```python
def register_file_exchange_upload(
    mcp: FastMCP,
    *,
    namespace: str,
    env_prefix: str,
    receiver: BufferedReceiver | None = None,
    stream_receiver: StreamReceiver | None = None,
    pre_link_validator: PreLinkValidator | None = None,
    accepts: tuple[str, ...] = ("*/*",),
) -> UploadHandle:
```

Exactly one of `receiver` / `stream_receiver` MUST be supplied. `accepts` is
a domain hook — which MIME types this receiver handles is determined by the
downstream server's domain, not by pvl-core.

**Removed kwargs:**

- `upload_tool_name`, `tool_tags` — shape overrides. pvl-core fixes the tool
  name to `create_upload_link` and the tag set to `{"write"}`. Downstream
  has no domain-specific basis to disagree; if the shape must change, it
  changes in pvl-core for everyone.
- `transport`, `max_bytes_default`, `ttl_default`, `ttl_max` — operator
  configuration, not domain hooks. They move to environment variables
  (`{PREFIX}_UPLOAD_MAX_BYTES`, `{PREFIX}_UPLOAD_TTL`, `{PREFIX}_UPLOAD_TTL_MAX`
  — which the current code already reads — plus transport resolution from
  the existing transport env var) with pvl-core built-in defaults. The
  kwargs disappear; the env vars are the operator surface.

### 2. The `create_upload_link` tool contract

**Parameters** (verbatim to the v0.3.0 spec):

| Param | Cardinality | Meaning |
|---|---|---|
| `origin_id` | MUST | Sender's opaque stable handle for the bytes (WHAT). Validated as a raw JSON string: no `/` or `\`, not `.`/`..`, no null/control chars, no leading/trailing whitespace. |
| `destination` | optional | Sender's destination instruction (WHERE). Spec-level validation forbids only null bytes, control chars (U+0000–U+001F), and leading/trailing whitespace — path separators and dots are allowed; the receiver validates per its own domain rules via `pre_link_validator`. |
| `content_type` | optional | Sender's hint of the POST `Content-Type`; pre-filtered against `accepts`. |
| `ttl_seconds` | optional | Sender's TTL hint; clamped to the receiver's `{PREFIX}_UPLOAD_TTL_MAX` ceiling. |
| `max_bytes` | optional | Sender's size hint; clamped to the receiver's `{PREFIX}_UPLOAD_MAX_BYTES` ceiling. |

**Return** — exactly the three MUST fields, all *effective* (post-clamp) values:

```json
{"url": "https://.../uploads/<token>", "ttl_seconds": 3600, "max_bytes": 10485760}
```

The non-spec `extra` parameter and the `upload_url` / `expires_in_seconds`
field names are gone.

**In-band failure** — when the tool rejects before minting a URL (a
`destination` rejected by `pre_link_validator`, or a `content_type` not in
`accepts`), it returns the `transfer_failed` envelope rather than a generic
tool error:

```json
{
  "error": "transfer_failed",
  "method": "http_upload",
  "receiver_server": "<namespace>",
  "origin_id": "<the origin_id passed in>",
  "message": "<reason>"
}
```

The envelope carries `receiver_server` (not `origin_server`): the upload
direction has produced no file, so the responding server is identified as
the receiver of an attempted upload.

### 3. The POST upload route and status codes

Route: `POST /{namespace}/uploads/{token}`. Status-code classes per the
v0.3.0 spec's POST-contract table:

| Status | Condition |
|---|---|
| `2xx` | Bytes accepted. Body is the receiver callback's returned dict, as JSON. |
| `404` | Token unknown **or** expired **or** already-consumed — indistinguishable, empty body. |
| `413` | Body exceeds the effective `max_bytes` — whether `Content-Length` declares it up front or the running total breaches it mid-stream. |
| `415` | POST `Content-Type` does not match the `accepts` filter. |
| other `4xx` | Receiver-domain rejection (the `receiver`/`stream_receiver` callback raises) — body carries the `transfer_failed` envelope. |
| `5xx` | Server error — generic body, no internal detail echoed. |

The single behavioural change versus the current route is removing the
`expired → 410 Gone` branch: `410` is deleted; expired tokens join the `404`
class. This is the spec's anti-leak rule — a probing caller MUST NOT be able
to tell "expired" from "never existed" from "already consumed".

Token consumption is unchanged: the token is atomically consumed on the
first POST attempt, success or failure; a retry on the same URL gets `404`.

### 4. `UploadRecord` and the token store

`_token_store.py` mechanics are kept. Two changes:

**`UploadRecord` restructure** — model the WHAT/WHERE split:

```python
@dataclass(frozen=True)
class UploadRecord:
    origin_id: str              # WHAT — replaces target_id
    destination: str | None     # WHERE — new; the sender's destination hint
    content_type: str | None    # new; the sender's Content-Type hint
    max_bytes: int              # effective (clamped) size ceiling
    expires_at: float           # internal TTL boundary
```

The non-spec `extra` dict field is removed — the v0.3.0 spec defines the
upload's data surface exactly (`origin_id` / `destination` / `content_type`),
and #74's mandate is binary spec compliance.

**`consume_or_status` → `consume`** — with `410` removed, the route no longer
needs to distinguish `"expired"` from `"missing"`. The 3-way
`consume_or_status(token) -> (UploadRecord | None, Literal["ok","expired","missing"])`
is replaced by `consume(token) -> UploadRecord | None`, which returns `None`
for unknown / expired / consumed alike. The atomic-pop-then-TTL-check stays
internal; the status enum (which existed only to feed the `410` split) is
deleted.

### 5. Domain-hook interfaces

**`pre_link_validator`** — runs inside `create_upload_link`, after spec-level
`origin_id` character validation, before the token is minted:

```python
PreLinkValidator = Callable[[str, str | None], None | Awaitable[None]]
#                            origin_id  destination
```

It validates `destination` against the receiver's domain rules — the spec
explicitly delegates `destination` validation to the receiver. Raising
`ValueError` causes the tool to return a `transfer_failed` envelope (§2); any
other exception propagates as a server error. Sync and async validators are
both supported.

**`receiver` / `stream_receiver`** — run inside the POST handler; they
receive the restructured `UploadRecord`:

```python
BufferedReceiver = Callable[[UploadRecord, bytes], dict[str, Any] | Awaitable[dict[str, Any]]]
StreamReceiver   = Callable[[UploadRecord, AsyncIterator[bytes]], dict[str, Any] | Awaitable[dict[str, Any]]]
```

`receiver` gets the fully-buffered body; `stream_receiver` gets a
`max_bytes`-bounded async chunk iterator. The returned dict becomes the `2xx`
JSON response body. A callback raising `ValueError` / `FileExistsError` /
similar produces a domain `4xx` carrying the `transfer_failed` envelope; sync
callbacks run via `asyncio.to_thread`.

### 6. Testing

TDD throughout. Coverage:

- `create_upload_link`: the five-parameter shape; `origin_id` raw-string
  validation; `ttl_seconds` / `max_bytes` clamping to the env ceilings; the
  exact `{url, ttl_seconds, max_bytes}` return.
- The in-band `transfer_failed` envelope on `pre_link_validator` rejection
  and on `content_type` not in `accepts` — including the `receiver_server`
  field name.
- POST status classes: `404` for all three token-unusable conditions (the
  anti-leak guard) and **no `410` anywhere**; `413` for both pre-declared
  `Content-Length` and mid-stream overflow; `415`; domain `4xx` with the
  envelope; `5xx` with no internal-detail echo.
- The restructured `UploadRecord` (`origin_id` / `destination` /
  `content_type`) reaching both `receiver` and `stream_receiver`.
- `pre_link_validator` accept and reject paths, sync and async.

The existing upload tests (`tests/test_uploads.py`,
`tests/test_file_exchange_upload_facade.py`,
`tests/test_file_exchange_upload_route.py`) are rewritten to the v0.3.0 shape,
not patched — tests asserting the removed `target_id` / `upload_url` /
`expires_in_seconds` / `410` behaviour assert the new state instead.

## Backward compatibility

`register_file_exchange_upload`'s public surface changes: the kwarg set
shrinks, and `UploadRecord` (passed to `receiver` / `stream_receiver`) is
restructured (`target_id` → `origin_id`, `+destination`, `+content_type`,
`−extra`). This is a breaking change for the one downstream consumer,
`markdown-vault-mcp`, whose adoption is tracked in
`pvliesdonk/markdown-vault-mcp#488`. pvl-core ships the breaking change;
downstream migrates — no compatibility shim, per the umbrella's discipline
and #73's framing principle.

## Acceptance (from #74)

- [ ] New `register_file_exchange_upload` — six domain-hook kwargs, every
  removed kwarg either env-var (operator config) or pvl-core-fixed (shape).
- [ ] `create_upload_link` accepts `origin_id` / `destination` /
  `content_type` / `ttl_seconds` / `max_bytes` and returns
  `{url, ttl_seconds, max_bytes}`.
- [ ] In-band rejection returns the `transfer_failed` envelope.
- [ ] POST route emits the spec status classes; no `410`; the three
  token-unusable conditions are indistinguishable `404`s.
- [ ] `UploadRecord` models the WHAT/WHERE split; no `extra` field.
- [ ] Old `target_id` / `upload_url` / `expires_in_seconds` / `410` surface
  is gone (grep-verified).
- [ ] Tests rewritten to the v0.3.0 shape; full suite + ruff + mypy clean.
- [ ] Downstream migration tracked in `markdown-vault-mcp#488`.
