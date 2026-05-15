# Design: sender-side http_upload primitive (issue #85)

**Status**: approved (brainstorm 2026-05-15)
**Issue**: [pvliesdonk/fastmcp-pvl-core#85](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/85)
**Umbrella**: #75
**Depends on**: #74 (receiver side) and #93 (the corrected sender spec) — both merged.

## Problem

The `http_upload` transfer method is bidirectional. #74 implemented the
**receiver** (`sink`) side — `create_upload_link`, the POST route. The
**sender** (`source`) side — a tool that POSTs bytes to a receiver-issued
URL — has no implementation. #85 builds it, against the spec corrected by
#93: the sender `upload` tool takes an opaque `origin_id`, not the
removed `source` tagged union.

## The design

### 1. The `register_file_exchange_upload_sender` helper

A new pvl-core helper, the counterpart of #74's
`register_file_exchange_upload` (which is the *receiver* — it registers
`create_upload_link`). The sender helper registers the `upload` tool.

```python
def register_file_exchange_upload_sender(
    mcp: FastMCP,
    *,
    namespace: str,
    env_prefix: str,
    byte_source: ByteSourceResolver,
) -> UploadSenderHandle:
```

Three kwargs — `namespace` / `env_prefix` (server identity) and the one
required domain hook, `byte_source`. The helper is pointless without a
resolver, so `byte_source` is required, not optional. No `accepts` kwarg:
`accepts` is a *receiver* admission policy; a sender declares
`content_type` per call. Operator configuration (transport resolution, an
outbound-POST timeout / size ceiling) flows through `{PREFIX}_*`
environment variables, not kwargs — matching #74's discipline. The
kwarg surface is purely domain hooks (#72/#73 framing principle).

The helper returns a frozen `UploadSenderHandle` (`namespace`,
`tool_name`, `enabled`).

**Transport gating.** Per #93's HTTP-server-capability clarification, the
sender side needs only *outbound* HTTP — which a stdio MCP server has.
So `register_file_exchange_upload_sender` is **not** gated on HTTP-server
capability or a base URL: it registers the `upload` tool regardless of
transport. (Contrast the receiver helper, which disables itself without an
HTTP server / `{PREFIX}_BASE_URL`.)

### 2. The `upload` MCP tool

The tool name and tags are pvl-core-fixed (`upload`, `{"write"}`) — not
kwargs (shape is pvl-core's per the framing principle).

Parameters (spec-conformant, post-#93):

| Param | Cardinality | Meaning |
|---|---|---|
| `url` | MUST | The receiver-issued POST endpoint (from a prior `create_upload_link`). |
| `origin_id` | MUST | The sender's opaque handle for the bytes to push. Spec segment rules (no `/` or `\`; not `.`/`..`; no null bytes / control characters; no leading/trailing whitespace). Resolved by the `byte_source` hook. |
| `content_type` | optional | MIME type for the POST `Content-Type` header. |

Return — exactly the spec shape: `{"status": <int>, "body": <receiver
response body, passed through>}`.

`origin_id` is validated against the spec segment rules
(`ExchangeURI.validate_segment`), like the receiver's `create_upload_link`
`origin_id`. A validation failure surfaces as a `transfer_failed`-form
tool error.

**`origin_id` independence.** The spec settles the question carried
forward from #97: the `upload` tool's `origin_id` and `create_upload_link`'s
`origin_id` are independent opaque handles, each the sender's own, bound
only by the sender's orchestration. pvl-core's `upload` tool never sees
the `create_upload_link` `origin_id`; it passes its own `origin_id`
parameter to the `byte_source` hook. #85 is independent-by-construction.

### 3. The byte-source resolver hook

The single domain hook. pvl-core cannot know what an `origin_id` resolves
to — a file, a database blob, an in-memory image — so the downstream
supplies a resolver:

```python
ByteSourceResolver = Callable[
    [str],                                       # origin_id
    "ResolvedSource | Awaitable[ResolvedSource]",
]


@dataclass(frozen=True)
class ResolvedSource:
    stream: BinaryIO            # a file-like binary object — the bytes to POST
    content_type: str | None    # the resource's MIME type, if the downstream knows it
    size_bytes: int | None      # length if known up front (lets pvl-core set Content-Length)
```

One hook, returning a file-like `stream` — not the buffered/stream split
#74's receiver had. A file-like object covers both modes: pvl-core reads
it in chunks and streams it into the POST body, then closes it. The
downstream resolver opens the file / fetches the row / wraps the in-memory
bytes.

- **Sync or async** resolvers both supported, dispatched via
  `inspect.iscoroutinefunction` (the pattern #74 uses for its callbacks).
- **Rejection.** The resolver raises `ValueError` for a caller-facing
  rejection (unknown `origin_id`, not permitted) → the tool returns the
  `transfer_failed` form. Any other exception is logged at ERROR and
  propagates as a server bug. The resolver *is* the gate — there is no
  separate `pre_send_validator` hook (the resolver already decides whether
  an `origin_id` is resolvable; a second hook would be redundant).
- **Content-Type precedence.** The tool's `content_type` parameter
  (explicit caller intent) wins; else `ResolvedSource.content_type`; else
  `application/octet-stream`.
- **Content-Length.** Set from `ResolvedSource.size_bytes` when the
  downstream provides it; otherwise pvl-core streams without it
  (`Content-Length` is SHOULD, not MUST, in the spec POST contract).

### 4. POST mechanics, SSRF, error handling

**The POST.** pvl-core reuses the existing shared lazy `httpx.AsyncClient`
(the one `fetch_file` uses — `follow_redirects=False`, 30 s timeout). The
`upload` tool resolves `origin_id` → `ResolvedSource`, then issues one
`POST url` with the file-like `stream` as a chunked body, the effective
`Content-Type`, and `Content-Length` when known. The body is streamed —
pvl-core does not buffer the whole resource in memory.

**SSRF guard.** `url` is LLM-supplied (the `upload` tool is LLM-callable),
so it is an exfiltration vector — an LLM steered to
`url: http://169.254.169.254/...` would POST the sender's bytes there.
pvl-core runs the existing `_ssrf_guard` on `url` before the POST — the
same denylist (`localhost`, cloud-metadata endpoints) and
private/loopback/link-local IP rejection that `fetch_file` applies to its
GET URL. A rejected URL produces a `transfer_failed`-form tool error. The
rare legitimate "POST to a private receiver" case is out of scope —
symmetric with the download side's stance.

**Response handling** — maps to the `{status, body}` return and the
`transfer_failed` unwrap:

- **2xx** → `{"status": <code>, "body": <parsed JSON if the response
  `Content-Type` is JSON, else the raw text>}`.
- **4xx carrying a `transfer_failed` envelope** → unwrap and re-raise as a
  tool error so the LLM sees the structured failure (mirroring how
  `fetch_file` propagates `transfer_failed`).
- **Other 4xx / 5xx without an envelope** → re-raise as a tool error
  carrying the status and a generic message; no internal detail echoed.
- **Transport failure** (connection error, timeout, SSRF rejection) → a
  tool error in the `transfer_failed` shape with a clear message.

**Single POST, no retry.** The receiver consumes its URL token on the
first POST attempt — success or failure — per the v0.3.0 POST contract. So
the `upload` tool makes exactly one POST attempt; a failure is terminal,
and the caller must call `create_upload_link` again for a fresh URL.

### 5. Capability wiring

Add to `_FileExchangeCapabilityBuilder`:

- a `_http_upload_source_tool: str | None` field;
- `set_http_upload_source(*, tool_name: str)` — records the sender tool;
- `_build_http_upload_block()` extended to emit both roles —
  `http_upload: {source?: {tool}, sink?: {tool, accepts, max_bytes, max_ttl_seconds}}`,
  whichever the server fills.

A server registering only the sender helper advertises
`http_upload: {source: {tool: "upload"}}`. A server registering both #74's
receiver and #85's sender advertises both sub-keys — the dual-role shape
#83 built. The helper calls `builder.set_http_upload_source(tool_name="upload")`
then `_emit_capability(mcp)`.

## Testing

TDD throughout. Coverage:

- The `upload` tool parameter and `{status, body}` return shape;
  `origin_id` segment validation.
- The `byte_source` resolver dispatched both sync and async.
- `content_type` precedence (param > resolver > `application/octet-stream`).
- `Content-Length` set when `size_bytes` is known, omitted otherwise.
- The SSRF guard rejecting a private / loopback / metadata `url`.
- A 2xx response returning `{status, body}` (JSON body parsed; non-JSON
  passed through).
- A 4xx `transfer_failed` body unwrapped and re-raised as a tool error.
- A resolver `ValueError` surfacing as a `transfer_failed`-form error.
- The single-POST-no-retry behaviour.
- The capability emitting `http_upload: {source: {tool: "upload"}}`, and
  the dual-role both-sub-keys case when paired with the receiver helper.

Outbound HTTP is exercised against a stub / mock receiver endpoint — no
real network.

## Backward compatibility

Purely additive: a new helper, a new tool, a new capability builder
method and field. No existing helper, tool, or capability shape changes.
`register_file_exchange_upload` (the #74 receiver) is untouched.

## Out of scope

- Any change to the receiver side (#74), the `http` method, or `exchange`.
- The spec — #93 already corrected the sender spec; #85 implements it.
- Retry / resumable-upload logic — the v0.3.0 one-time-token contract makes
  a failed POST terminal; a fresh `create_upload_link` call is the retry.

## Acceptance (from #85)

- [ ] `register_file_exchange_upload_sender(mcp, *, namespace, env_prefix, byte_source)` — three domain-hook kwargs; operator config on env vars.
- [ ] The `upload` tool: `url` / `origin_id` / `content_type` parameters; `{status, body}` return; `transfer_failed` unwrap on 4xx.
- [ ] `ByteSourceResolver` / `ResolvedSource` hook; sync and async; resolver `ValueError` → `transfer_failed`-form error.
- [ ] The outbound POST reuses the shared `httpx` client, streams the body, SSRF-guards `url`, makes exactly one attempt.
- [ ] `set_http_upload_source` on the capability builder; `http_upload.source = {tool: "upload"}` advertised; dual-role case works.
- [ ] The helper is not gated on HTTP-server capability (a stdio server can register it).
- [ ] Tests pass; `ruff` + `mypy` clean.
