# MCP File Exchange Specification

**Version:** 0.3.0
**Status:** experimental
**Tags:** mcp, spec, interop

## About this document

This specification is a **protocol extension** for MCP. It defines a wire-level convention — the `FileRef` envelope, the `exchange://` URI scheme, the capability declaration, the per-method tool contracts, and the security rules — that independently developed MCP servers MUST share to interoperate.

It is not a design document for any particular implementation. Implementation strategies (lazy materialisation, per-server TTL ceilings and similar limit-enforcement policy, route mechanics, framework-specific helpers, downstream tool naming and registration mechanics) are out of scope and belong in implementor docs, not here. Spec-level defaults that conformant implementations coordinate on — see §"Defaults" — remain part of the spec. Implementation feedback that surfaces a real spec gap is resolved through a proper spec evolution (a new release with the version field bumped per §"Versioning and compatibility"), not through inline amendments to a published version.

## Problem

MCP servers cannot communicate directly with each other. The client mediates all interactions. When one server produces a file that another server needs to consume (e.g. an image generator producing an image that a vault server stores), the file content must pass through the client's context window as base64, wasting tokens and hitting size limits.

## Goal

Define a lightweight convention that allows independently developed MCP servers to exchange files efficiently when co-deployed, with graceful degradation to remote transfer when they are not.

## Relationship to MCP

MCP has no sideband for bulk data. The protocol is JSON-RPC over stdio or HTTP: every piece of content, including binary files, passes through the message stream as base64-encoded text inside tool results or resource contents. The client (typically an LLM host) receives this content into its context window. There is no protocol-level mechanism for a server to send a file directly to a client's filesystem, to another server, or to stream bytes outside the JSON-RPC channel.

Specifically:

- **Resources** (`BlobResourceContents`) can serve binary content, but the client reads it into context. There is no "download to disk without entering the context window."
- **Streamable HTTP transport** means MCP servers are already HTTP endpoints, but the HTTP layer carries only JSON-RPC messages. Serving files at custom HTTP paths (as `create_download_link` does) works but is outside the MCP specification.
- **Tool results** can contain base64 image data, but this consumes context window space proportional to the file size.
- **MCP Apps** render HTML in a sandboxed iframe and can make network requests, but they are designed for interactive UI, not data transfer.

The practical consequence is that passing a 5 MB image between two MCP servers costs thousands of tokens even though the LLM only needs to know "there is a PNG, 1024x768, of a circuit board diagram." The core problem this specification solves is **pass-by-reference**: the LLM receives lightweight metadata about a file (type, size, thumbnail, description) while the actual bytes travel outside the context window via shared filesystem or direct server-to-server transfer.

This specification is designed as a **stopgap convention**. It intentionally uses MCP's `experimental` capability field and imposes no changes to the MCP protocol itself. If MCP later adds native file transfer or a bulk data sideband, implementations of this spec should be straightforward to migrate. The conventions are structured to be forward-compatible with that outcome: the file reference object maps naturally to a hypothetical MCP-native file handle, and the transfer methods abstraction can accommodate a future `mcp-native` method alongside the current `exchange` and `http` methods.

## Concepts

### File Reference

The interop surface. When an MCP tool produces a file intended for cross-server use, it returns a **file reference** alongside or instead of inline content:

```json
{
  "origin_server": "image-mcp",
  "origin_id": "a1b2c3",
  "mime_type": "image/png",
  "size_bytes": 245760,
  "preview": {
    "description": "Generated circuit board diagram, top-down view",
    "dimensions": {"width": 1024, "height": 768}
  },
  "transfer": {
    "exchange": {
      "uri": "exchange://hades-01/image-mcp/a1b2c3.png"
    },
    "http": {
      "tool": "create_download_link"
    }
  }
}
```

| Field | Required | Description |
|---|---|---|
| `origin_server` | MUST | Namespace of the producing server. The client uses this to identify which server connection to call for transfer negotiation. |
| `origin_id` | MUST | Opaque round-trip handle for this file on the origin server. The producer MAY interpret it as a path, document id, image id with embedded variant, HMAC token, or any other internally-meaningful handle; clients and consumers MUST treat it as opaque. The producer's only obligation is round-trip: the exact string returned in `file_ref.origin_id` MUST, when handed back to the producer via any transfer method that accepts an `origin_id` parameter (e.g. the `http` method's producer tool — see [Transfer Methods](#transfer-methods)), resolve to the same file (subject to TTL). |
| `mime_type` | SHOULD | MIME type of the file. |
| `size_bytes` | MAY | File size in bytes. |
| `transfer` | MUST | Object whose keys are transfer method names and whose values are method-specific metadata. At least one method MUST be present. See [Transfer Methods](#transfer-methods). |
| `preview` | SHOULD | Lightweight representation of the file for LLM context. See below. |

The file reference does **not** contain a download URL or inline content. Transfer is initiated lazily by the client through the declared methods.

#### Preview

The `preview` field gives the LLM enough information to reason about a file without ingesting the full binary. This is the key to pass-by-reference: the LLM sees metadata, not megabytes.

```json
"preview": {
  "description": "Generated circuit board diagram, top-down view, 4-layer PCB",
  "dimensions": {"width": 1024, "height": 768},
  "thumbnail_base64": "/9j/4AAQSkZJRg...",
  "thumbnail_mime_type": "image/jpeg",
  "metadata": {
    "prompt": "top-down view of a 4-layer PCB",
    "model": "flux-schnell"
  }
}
```

All `preview` fields are optional. Producers SHOULD include at least a `description` so the LLM can make informed decisions about the file without requesting the full content.

| Field | Description |
|---|---|
| `description` | Human/LLM-readable summary of the file content. |
| `dimensions` | For images/video: `width` and `height` in pixels. |
| `thumbnail_base64` | Small preview image, base64-encoded. SHOULD be under 10 KB to keep context costs minimal. |
| `thumbnail_mime_type` | MIME type of the thumbnail (e.g. `image/jpeg`). Required when `thumbnail_base64` is present. |
| `metadata` | Arbitrary key-value pairs with producer-specific context (prompt, model, page count, duration, etc.). |

The `preview` field is intentionally unstructured beyond the common fields listed above. Different file types benefit from different metadata (images need dimensions, PDFs need page counts, audio needs duration). Producers include what is relevant; consumers and LLMs use what they recognise.

A file reference MAY be embedded as a field within a larger tool response. For example, an image generation tool might return prompt metadata, dimensions, and a `file_ref` field containing the file reference. The spec does not prescribe the field name, but `file_ref` is conventional.

### Usage Patterns

File references support two patterns with different trade-offs:

#### Augmented response (backward-compatible)

The tool returns its normal output to the LLM (including inline content like thumbnails, metadata, or text) and additionally includes a file reference for cross-server transfer:

```json
{
  "image_id": "a1b2c3",
  "prompt": "top-down view of a 4-layer PCB",
  "content_type": "image/png",
  "dimensions": {"width": 1024, "height": 768},
  "thumbnail_b64": "/9j/4AAQSkZJRg...",
  "file_ref": {
    "origin_server": "image-mcp",
    "origin_id": "a1b2c3",
    "mime_type": "image/png",
    "size_bytes": 245760,
    "transfer": {
      "exchange": {"uri": "exchange://hades-01/image-mcp/a1b2c3.png"},
      "http": {"tool": "create_download_link"}
    }
  }
}
```

The LLM already has everything it needs from the native response: it can see the thumbnail, knows the dimensions, understands what was generated. The `file_ref` is purely a transfer handle. `preview` is redundant and can be omitted.

This is the recommended adoption path for existing tools. The tool keeps working exactly as before for clients that don't understand file references; clients that do can use the `file_ref` for efficient server-to-server transfer.

#### Reference-only (bandwidth-optimised)

The tool returns only a file reference. The full content never enters the context window. The LLM reasons about the file based solely on the preview:

```json
{
  "file_ref": {
    "origin_server": "image-mcp",
    "origin_id": "a1b2c3",
    "mime_type": "image/png",
    "size_bytes": 245760,
    "preview": {
      "description": "Generated circuit board diagram, top-down view, 4-layer PCB",
      "dimensions": {"width": 1024, "height": 768},
      "thumbnail_base64": "/9j/4AAQSkZJRg..."
    },
    "transfer": {
      "exchange": {"uri": "exchange://hades-01/image-mcp/a1b2c3.png"},
      "http": {"tool": "create_download_link"}
    }
  }
}
```

Here `preview` is essential: it is the only information the LLM receives about the file. Without it, the LLM cannot make informed decisions about where to store the file, how to reference it, or whether it meets the user's intent.

This pattern is appropriate when the full content would waste significant context (large images, PDFs, datasets) and the LLM only needs to orchestrate transfer, not inspect the content in detail.

#### Choosing between patterns

Producers SHOULD default to the augmented response pattern for backward compatibility. The reference-only pattern is an optimisation that trades LLM visibility for context efficiency. It is most valuable for large files, batch operations, or pipelines where the LLM's role is orchestration rather than content inspection.

A producer MAY offer both patterns, controlled by a tool parameter (e.g. `return_ref_only: true`). This lets the client or LLM choose based on the situation.

### Transfer Methods

A transfer method defines how a file moves between a *source* server (where the bytes originate) and a *sink* server (where they land). The spec defines three methods (`exchange`, `http`, `http_upload`); future extensions may add more.

Each method is identified by a string key (e.g. `"exchange"`, `"http"`, `"http_upload"`) and has method-specific metadata in the capability declaration and, where the method participates in file-reference-based transfer, the file reference. Pull-direction methods (`exchange`, `http`) appear in both; push-direction methods (`http_upload`) appear only in the capability declaration, never in a file reference.

**Capability-declaration shape.** Within the capability declaration's `transfer_methods` object, every *tool-based* method declares its tool(s) under `source` / `sink` role sub-objects. `source` is the endpoint bytes originate from; `sink` is the endpoint bytes land at. Within each role sub-object, `tool` is the one mandatory field; any further fields are method-specific metadata a caller needs up front. A server populates whichever role(s) it implements — both sub-keys for a server that fills both roles of a method, one for a single-role server. The role is identified by sub-key presence, never by the tool-name string (tool names are implementation-defined). `exchange` is the sole *tool-less* method and carries `{}`.

**`transfer` (file reference) vs `transfer_methods` (capability declaration).** These are different objects with different shapes; do not conflate them. The `transfer` object inside a file reference is per-file, producer-emitted, and inherently single-role — it advertises how to retrieve one specific file — so it stays flat (`{tool: ...}`, no `source`/`sink`). The `transfer_methods` object in a capability declaration is server-wide and describes every role the server fills, so it uses the `source`/`sink` sub-objects described above.

#### `exchange` (shared volume)

The producer and consumer share a filesystem directory. The producer writes the file; the consumer reads it by path. No network transfer, no serialisation cost.

In a file reference:

```json
"exchange": {
  "uri": "exchange://hades-01/image-mcp/a1b2c3.png"
}
```

In a capability declaration:

```json
"exchange": {}
```

No tool declarations needed: the consumer resolves the URI to a local path directly.

#### `http` (download URL)

The producer exposes a tool that generates a download URL. The consumer exposes a tool that fetches from a URL. The client orchestrates the handoff.

The `http` method serves double duty: the generated URL can be used for server-to-server transfer (consumer calls its fetch tool with the URL) or for **direct human download** (the LLM includes the URL in its response for the user to click). This means the `http` method is useful even without a consuming server: a producer can generate a download link that the LLM presents to the user as a clickable link in the conversation.

**HTTP-server capability.** The `http` method requires the *producer* to be reachable as an HTTP server — it mints and serves the download URL. The consumer needs only the ability to make an *outbound* HTTP request; it does not accept inbound connections and need not itself be an HTTP-transport MCP server. A stdio MCP server can therefore be the consumer for `http` (it issues an outbound `GET`), but cannot be the producer. The spec cares about the *capability* — can a side serve a URL, can it make outbound requests — not how the server obtains HTTP access.

In a file reference:

```json
"http": {
  "tool": "create_download_link"
}
```

In a capability declaration, the `http` method uses `source` / `sink` role sub-objects (see §"Transfer Methods" → "Capability-declaration shape"). The `source` role is the producer (mints the download URL via `create_download_link`); the `sink` role is the consumer (fetches from the URL).

A producer-only server:

```json
"http": {
  "source": {"tool": "create_download_link"}
}
```

A consumer-only server:

```json
"http": {
  "sink": {"tool": "fetch"}
}
```

A server that is both producer and consumer populates both sub-keys:

```json
"http": {
  "source": {"tool": "create_download_link"},
  "sink": {"tool": "fetch"}
}
```

**Standard parameters for the `http` method:**
- Producer tool MUST accept a parameter named `origin_id`.
- Producer tool MUST return a JSON object with at minimum a `url` field. It MAY include `ttl_seconds` and `mime_type`.
- The generated URL MUST be cryptographically unguessable (e.g. containing a UUID or HMAC token in the path or query string). The producer SHOULD invalidate the URL after a single successful download (one-time use). TLS/HTTPS is assumed; the URL path is encrypted in transit, so embedding secrets in the URL is equivalent in security to using an `Authorization` header while being compatible with any consumer that can fetch a URL.
- Consumer tool MUST accept a parameter named `url`. It SHOULD accept an optional parameter named `path` to allow client-directed placement. If `path` is omitted or invalid, the consumer MUST auto-generate a safe local path (e.g. derived from `origin_id` or a UUID). This prevents failures caused by LLMs hallucinating invalid directory structures.

#### `http_upload` (push to receiver-issued URL)

The reverse of the `http` method: the *receiver* mints a one-time POST URL; any party with the URL pushes bytes. The sender can be an LLM/agent, another MCP server, or a human with an HTTP client (`curl`, browser, custom script) — the spec does not constrain who pushes.

**HTTP-server capability.** `http_upload` mirrors `http` with the roles inverted: the method requires the *receiver* to be reachable as an HTTP server — it mints and serves the upload URL. The sender needs only the ability to make an *outbound* HTTP request; it does not accept inbound connections and need not itself be an HTTP-transport MCP server. A stdio MCP server can therefore be the sender for `http_upload` (it issues an outbound `POST`), but cannot be the receiver. This is the method's reason to exist: the `http` (download) method requires the *producer* to serve the URL, so it cannot move bytes out of a producer that has no HTTP server; `http_upload` puts the URL-serving on the receiver instead. Between the two methods, whichever side can serve HTTP, one method places the URL-serving there; `exchange` (shared volume) covers the case where neither side can.

Like the existing `http` (download) method, both the URL-mint tool on the receiver side and the POST-perform tool on the sender side are wire-optional from the spec's perspective. Any HTTP client that can issue a `POST` is a valid sender, just as any HTTP client that can `GET` is a valid consumer of the existing `http` method. The tool definitions exist to standardize MCP-mediated transfer between MCP servers; they are not the only valid implementation of either side.

In a capability declaration, the `http_upload` method uses `source` / `sink` role sub-objects (see §"Transfer Methods" → "Capability-declaration shape"). The `sink` role is the receiver (mints the upload URL via `create_upload_link`, accepts the bytes); the `source` role is the sender (POSTs the bytes via `upload`). Note the asymmetry with `http`: for `http` the `source` mints the URL, for `http_upload` the `sink` mints it — the role names track data direction; the mint mechanics are defined per method.

A receiver-only server:

```json
"http_upload": {
  "sink": {
    "tool": "create_upload_link",
    "accepts": ["application/pdf", "text/markdown"],
    "max_bytes": 10485760,
    "max_ttl_seconds": 3600
  }
}
```

A sender-only server (the sender side is optional):

```json
"http_upload": {
  "source": {"tool": "upload"}
}
```

A server that implements both roles populates both sub-keys:

```json
"http_upload": {
  "source": {"tool": "upload"},
  "sink": {
    "tool": "create_upload_link",
    "accepts": ["*/*"],
    "max_bytes": 10485760,
    "max_ttl_seconds": 3600
  }
}
```

Fields within each role sub-object:

- `tool` (MUST, both roles) — name of the MCP tool for that role (`create_upload_link` on the `sink` side, `upload` on the `source` side; the names are implementation-defined).
- `accepts` / `max_bytes` / `max_ttl_seconds` (`sink` only) — the receiver's admission policy: accepted `Content-Type` filter, body-size ceiling, TTL ceiling.

The role is identified by **sub-key presence** (`source` vs `sink`), not by the tool-name string (which is implementation-defined). A server that implements both roles advertises both sub-keys — the `source`/`sink` structure expresses dual-role servers without ambiguity.

**Receiver-side tool: `create_upload_link`**

The receiver registers a tool that mints upload URLs given a sender's identifier and (optionally) a destination instruction.

| Param | Cardinality | Rules | Description |
|---|---|---|---|
| `origin_id` | MUST | Same rules as `origin_id` in the `http` method's `create_download_link` (raw-JSON validation; no path separators `/` or `\`; not equal to `.` or `..`; no null bytes / control characters; no leading or trailing whitespace). | The sender's opaque stable handle for the bytes (the *what*). The receiver MAY treat it as a filename, document id, content hash, or any internally-meaningful key, but MUST NOT interpret it as a path component. |
| `destination` | MAY | Forbids only null bytes, control characters (U+0000 through U+001F), and leading/trailing whitespace. Path separators, dots, and traversal-shaped strings are **NOT** spec-rejected. The receiver MUST validate per its own domain rules before any filesystem interaction. | The sender's destination instruction (the *where*). The receiver decides semantics — path, slot, parent document key, anything. The relaxed character rules vs. `origin_id` reflect the asymmetric role: `destination` is consumed only by the receiver's own domain logic and never embedded in a URI by anyone else. |
| `ttl_seconds` | MAY | Positive number of seconds. | Sender's TTL hint for the minted URL. The receiver MAY clamp to its own ceiling (`max_ttl_seconds`); the effective TTL is returned. |
| `max_bytes` | MAY | Positive integer. | Sender's intended upper bound on the upload size, used as a hint to the receiver. The receiver MAY clamp to its own ceiling (`max_bytes` in its capability declaration); the *effective* ceiling — the value the receiver will enforce at POST time — is returned in the response's `max_bytes` field. The receiver MAY use this hint to reject the link request early (via `transfer_failed`) if the requested size already exceeds policy, sparing a POST round-trip; no resource is pre-allocated. |
| `content_type` | MAY | Standard MIME type string. | Sender's hint about what `Content-Type` the upload will declare. The receiver MAY pre-filter against its `accepts` list at link-mint time and surface a `transfer_failed` envelope in-band, sparing the sender a 415 round-trip. |

The tool MUST return:

```json
{
  "url": "https://receiver.example/uploads/<token>",
  "ttl_seconds": 3600,
  "max_bytes": 10485760
}
```

- `url` (MUST) — the POST endpoint.
- `ttl_seconds` (MUST) — effective TTL after clamping. Same field name as the `http` method's `create_download_link` response.
- `max_bytes` (MUST) — effective body-size ceiling the receiver will enforce at POST time. Receivers MUST always return this field, so senders can pre-validate body size and avoid unnecessary `413` round-trips. (Receivers MUST enforce a ceiling per the §"Receiver server (`http_upload`)" conformance checklist, so a "no ceiling" case does not arise; if the receiver chose not to clamp the sender's `max_bytes` hint, it returns the sender's value verbatim.)

On in-band failure (invalid `destination`, `content_type` not in `accepts`, quota exhausted, dedup conflict, etc.), the receiver returns a `transfer_failed` envelope:

```json
{
  "error": "transfer_failed",
  "method": "http_upload",
  "receiver_server": "<receiver namespace>",
  "origin_id": "<the origin_id passed in>",
  "message": "destination validation failed: ..."
}
```

**Clients that handle `transfer_failed` from both directions MUST branch on `method` before reading the server-identifying field.** The download direction's `transfer_failed` carries `origin_server` (the file's provenance — the producer server), because the failure is in retrieving an already-produced file. The `http_upload` `transfer_failed` carries `receiver_server` instead, because no file has been produced yet — the responding server is the *receiver* of an attempted upload, not the origin of any file. The two field names reflect the role split; the per-direction shapes are intentional.

**POST contract (at the minted URL):**

The sender POSTs raw bytes to the receiver-issued URL.

- **Method**: `POST`.
- **Body**: raw bytes.
- **`Content-Type` header**: MUST be set by the sender. The receiver MAY enforce per its `accepts` list at this point; mismatch yields `415 Unsupported Media Type`.
- **`Content-Length` header**: SHOULD be set by the sender. The receiver MAY require it.

The URL token:

- MUST be cryptographically unguessable (≥128 bits of entropy in the URL path or query).
- MUST be one-time use: the receiver MUST atomically consume the token on the first POST attempt — success OR failure. A retry on the same URL returns `404`, not the original error. Senders that need to retry MUST call `create_upload_link` again.
- MUST be TTL-bounded: the receiver MUST reject expired tokens.

Status code classes (the receiver picks specific codes within each class; senders SHOULD treat the class as the actionable signal):

| Class | When | Spec rule |
|---|---|---|
| `2xx` | bytes accepted | MUST emit one of these on success. |
| `404 Not Found` | token unknown, expired, OR already consumed | MUST NOT distinguish between these three conditions (anti-leak: avoid revealing token-existence to a probing caller). MUST emit with an empty body — no `transfer_failed` envelope, no framework-default HTML — for the same reason. |
| `413 Payload Too Large` | body exceeds the receiver's enforced `max_bytes` (either `Content-Length` declares too much, or the running body total exceeds the cap mid-stream) | MUST emit when the cap is breached. |
| `415 Unsupported Media Type` | `Content-Type` does not match the receiver's `accepts` filter | MUST emit when the filter rejects. |
| Other `4xx` | receiver-domain rejection (invalid destination, quota, dedup conflict, etc.) | The receiver picks the code; the response body MUST carry a `transfer_failed` envelope. |
| `5xx` | server-side error | The body MAY be generic; the receiver MUST NOT echo internal error details (the full traceback is logged server-side). |

Success body: the spec does NOT mandate a shape. Receivers MAY return JSON with domain-specific data (saved-path confirmation, generated id, etc.). Senders SHOULD parse JSON when the response `Content-Type` indicates JSON; otherwise treat the body as opaque acknowledgment.

Failure body for the "Other 4xx" class only (as specified in the status-code table above): a `transfer_failed` envelope, same shape as the in-band failure example above. The mandatory 4xx classes (`404`, `413`, `415`) do NOT carry a `transfer_failed` body — `404` intentionally has no body (anti-leak), and `413`/`415` are unambiguously identified by the status code alone.

**Sender-side tool: `upload` (optional)**

A server that wants to act as an MCP-mediated *pusher* of bytes (e.g., a mover-server that reads from a remote source and POSTs to a receiver-issued upload URL) advertises a sender-side tool. Servers that don't have a push role simply omit this side of the capability.

| Param | Cardinality | Description |
|---|---|---|
| `url` | MUST | The receiver-issued POST endpoint (returned from `create_upload_link`). |
| `origin_id` | MUST | The sender's opaque stable handle for the bytes to push. Same raw-JSON validation rules as `origin_id` in the `http` method's `create_download_link` (no path separators `/` or `\`; not `.` or `..`; no null bytes / control characters; no leading or trailing whitespace). The sender resolves it to bytes by its own domain logic — a file, a database row, an in-memory object, anything; callers treat it as opaque. |
| `content_type` | SHOULD | The MIME type the sender will declare in the POST `Content-Type` header. If omitted, the sender SHOULD sniff or default. |

The tool MUST return:

```json
{
  "status": 201,
  "body": "<receiver's response body, passed through>"
}
```

- `status` (MUST) — the receiver's HTTP status code.
- `body` (MAY) — the receiver's response body, passed through to the caller (opaque to the sender tool itself).

On 4xx with a structured `transfer_failed` envelope, the sender tool SHOULD unwrap and re-raise as a tool error, mirroring how the existing `http` method's `fetch` tool propagates `transfer_failed`.

**Worked example — agent push:**

An LLM agent has a local PDF at `/tmp/draft.pdf` and wants to upload it to a vault server with `namespace: "vault-mcp"`. The agent calls the vault's `create_upload_link` tool:

```jsonc
// request
{
  "origin_id": "draft-2026-05-15.pdf",
  "destination": "projects/research/papers/draft.pdf",
  "content_type": "application/pdf"
}

// response
{
  "url": "https://vault-mcp.example/uploads/8f3a9e2b...",
  "ttl_seconds": 3600,
  "max_bytes": 10485760
}
```

The agent then pushes the bytes with `curl`:

```
curl -X POST --data-binary @/tmp/draft.pdf \
     -H "Content-Type: application/pdf" \
     https://vault-mcp.example/uploads/8f3a9e2b...
```

The receiver responds `201 Created` with an optional JSON body (e.g., `{"saved_path": "projects/research/papers/draft.pdf"}`).

**Worked example — MCP-mediated push:**

A mover-server is asked to copy bytes from an internal `exchange://` URI into an external vault. The mover calls the vault's `create_upload_link` to obtain the URL, then calls its own `upload` tool to actually POST the bytes:

```jsonc
// step 1: vault.create_upload_link
{
  "origin_id": "moved-from-vault",
  "destination": "incoming/2026-05-15/movement.bin"
}
// -> {"url": "https://vault-mcp.example/uploads/<token>", "ttl_seconds": 3600, "max_bytes": 10485760}

// step 2: mover.upload
{
  "url": "https://vault-mcp.example/uploads/<token>",
  "origin_id": "moved-from-vault",
  "content_type": "application/octet-stream"
}
// -> {"status": 201, "body": {"saved_path": "incoming/2026-05-15/movement.bin"}}
```

#### Method priority

When multiple methods are available, the client SHOULD prefer them in this order:

1. `exchange` (zero-cost local read, no public URL created)
2. `http` (network transfer, creates a temporary public endpoint)

Future methods slot into this priority list by convention. Methods with lower latency, lower cost, or stronger privacy properties are preferred.

The `http_upload` method introduced in v0.3 is NOT included in the priority list. The priority list compares methods for the same transfer *direction* (consumer pull from a producer-side endpoint); `http_upload` is the inverse direction (sender push to a receiver-side endpoint). It is selected by a different mechanism — the sender looks for `http_upload` in a receiver's capability declaration and uses it when the use case calls for an upload, not when comparing alternative consumer-pull methods.

#### Adding future methods

A new transfer method (e.g. `s3`, `scp`, `gdrive`) is defined by:

1. A method key string.
2. The metadata it carries in the file reference (if the method participates in file-reference-based transfer at all — push-direction methods like `http_upload` do not appear in file references).
3. The metadata it carries in the capability declaration (tool names and standard parameter names).
4. Its position in the priority order, **if** it is a consumer-pull (download-direction) method. Push-direction methods (e.g. `http_upload`) are not slotted into the priority list; they are selected by presence in the receiver's capability declaration.

Servers that do not recognise a method ignore it. Clients that do not recognise a method skip it and try the next one. This makes the protocol forward-compatible: old clients degrade gracefully when new methods appear. The same skip-unknown-keys rule applies inside the `transfer_methods` object of a server's **capability declaration**: implementations MUST silently ignore any `transfer_methods` key they do not recognise rather than rejecting the handshake. The rule applies recursively: implementations MUST also silently ignore any unrecognised *sub-fields* inside a method's block (e.g. a future `min_bytes` field added to `http_upload` blocks alongside the current `max_bytes`), so that within-minor additive extensions to a method's shape stay backward-compatible.

Note that whether a new method's introduction *also* requires a spec-version bump is governed separately by §"Versioning and compatibility" (the bump-trigger checklist). The structural recipe above is necessary but not always sufficient — methods that introduce a new direction, validation regime, error class, or tool-contract shape additionally warrant a minor-version bump.

### Exchange Group

An exchange group is a set of MCP servers that share a filesystem directory and can use the `exchange` transfer method. Membership is opt-in via environment variables:

| Variable | Required | Description |
|---|---|---|
| `MCP_EXCHANGE_DIR` | Yes | Absolute path to the shared directory. |
| `MCP_EXCHANGE_ID` | No | Unique identifier for this exchange group. Auto-generated if unset (see [Deployer Setup](#deployer-setup)). |
| `MCP_EXCHANGE_NAMESPACE` | No | Server namespace within the exchange group. Defaults to MCP server name. |

Servers that find `MCP_EXCHANGE_DIR` set and pointing to a valid directory participate in the exchange group. Servers that do not find this variable omit the `exchange` method from their file references and capability declarations but can still participate via other methods.

### Exchange URI

```
exchange://{exchange-id}/{namespace}/{id}.{ext}
```

- **exchange-id**: Identifies the exchange group. Scoped to the shared volume, not the server.
- **namespace**: Namespace of the producing server. Each server writes only to its own namespace.
- **id.ext**: File identifier with extension. The extension is informational and SHOULD match the `mime_type`.

### Security and Path Resolution

All servers MUST sanitise the `{namespace}` and `{id}.{ext}` segments of exchange URIs before any filesystem interaction.

**URI decoding scope:** validation rules apply differently depending on the source of the data:

- When parsing an `exchange://` URI, validation MUST occur after exactly one pass of URI decoding. Iterative or recursive decoding MUST NOT be applied, as double-encoded payloads (e.g. `%252e%252e%252f`) could bypass validation on a first-pass decode and execute traversal on a second.
- When handling direct JSON-RPC parameters (such as `origin_id`), validation MUST be applied to the raw string as-is. Servers MUST NOT apply URI decoding to JSON parameters. An `origin_id` value is an opaque string, not a URI component; applying URI decoding would corrupt legitimate `%` characters (e.g. `req-%20-id` would be mutated to `req- -id`).

After decoding (for URIs) or direct extraction (for JSON parameters), segments:

- MUST NOT contain path separators (`/` or `\`).
- MUST NOT be equal to `.` or `..`.
- MUST NOT contain null bytes (`\0`) or control characters (U+0000 through U+001F).
- MUST NOT contain leading or trailing whitespace.

The `destination` parameter passed to a receiver's `create_upload_link` tool (new in v0.3, used by the `http_upload` method) is **not** subject to the segment-validation rules above. It is opaque to anyone but the receiver — the spec mandates only minimum safety constraints (no null bytes, no control characters U+0000 through U+001F, no leading or trailing whitespace). Path separators, dots, and traversal-shaped strings are NOT spec-rejected; the receiver MUST validate per its own domain rules before any filesystem interaction. The asymmetric rules vs. `origin_id` reflect the role split: `origin_id` MAY be echoed by the receiver into URIs or filenames (so it MUST be URI-safe), but `destination` is consumed only by the receiver's own domain logic and never embedded in a URI by anyone else.

In addition, `exchange://` URIs themselves MUST NOT contain a query component (`?...`) or fragment (`#...`). A URI with either is rejected as `exchange_uri_invalid`. This closes a parser-bypass class where a query string or fragment could slip past naive parsing and be misinterpreted as part of a path segment or file extension.

If a server detects an invalid segment, it MUST abort and return an error:

```json
{
  "error": "exchange_uri_invalid",
  "message": "Path segment contains directory traversal sequence"
}
```

Both producers (when writing) and consumers (when reading) MUST apply these rules.

### Server Identification

Each server in an exchange group needs a unique namespace to prevent filesystem collisions.

| Variable | Required | Description |
|---|---|---|
| `MCP_EXCHANGE_NAMESPACE` | No | Explicit namespace override. |

If unset, the server's MCP server name (from the `initialize` handshake) is used. The deployer only overrides this when running multiple instances of the same server in one exchange group.

The namespace serves double duty: it is the directory name under `$MCP_EXCHANGE_DIR/` and the `{namespace}` component in `exchange://` URIs. In addition to the general segment rules above, namespace values MUST NOT start with a dot.

The `origin_server` field in a file reference MUST match the producing server's namespace. This allows the client to map the file reference back to the correct server connection. The field is named `origin_server` rather than `origin_namespace` because it is more intuitive for LLMs and human readers reasoning about file provenance ("which server produced this?"). The value is always identical to the server's `namespace` in its capability declaration.

### Discovery

#### Capability declaration

During the MCP `initialize` handshake, a participating server declares exchange support in the `experimental` field of its capabilities:

**Producer example:**

```json
{
  "capabilities": {
    "experimental": {
      "file_exchange": {
        "version": "0.3",
        "namespace": "image-mcp",
        "exchange_id": "hades-01",
        "produces": ["image/png", "image/webp", "image/jpeg"],
        "consumes": [],
        "transfer_methods": {
          "exchange": {},
          "http": {
            "source": {"tool": "create_download_link"}
          }
        }
      }
    }
  }
}
```

**Consumer example:**

```json
{
  "capabilities": {
    "experimental": {
      "file_exchange": {
        "version": "0.3",
        "namespace": "vault-mcp",
        "exchange_id": "hades-01",
        "produces": [],
        "consumes": ["image/png", "image/webp", "image/jpeg", "application/pdf"],
        "transfer_methods": {
          "exchange": {},
          "http": {
            "sink": {"tool": "fetch"}
          },
          "http_upload": {
            "sink": {
              "tool": "create_upload_link",
              "accepts": ["application/pdf", "text/markdown"],
              "max_bytes": 10485760,
              "max_ttl_seconds": 3600
            }
          }
        }
      }
    }
  }
}
```

| Field | Required | Description |
|---|---|---|
| `version` | MUST | Spec version as `MAJOR.MINOR` (e.g. `"0.3"`). Patch versions are spec-internal and MUST NOT appear in the capability declaration. A server implementing spec version `0.3.0` MUST advertise `"0.3"`; patch-level differences do not change the wire-level capability. |
| `namespace` | MUST | The server's exchange namespace. |
| `exchange_id` | SHOULD | The exchange group ID. Present when the server participates in an exchange group. |
| `produces` | SHOULD | MIME types this server can produce as file references. |
| `consumes` | SHOULD | MIME types this server can accept via file references (the pull-flow / `fetch` path). The push-flow `http_upload` method has its own independent `accepts` filter inside `transfer_methods.http_upload.sink`; the two lists are not required to match. |
| `transfer_methods` | MUST | Object whose keys are supported transfer method names. For tool-based methods (`http`, `http_upload`) the value carries `source` / `sink` role sub-objects, each with a `tool` field plus method-specific metadata; a server populates whichever role(s) it fills. `exchange` carries `{}`. See §"Transfer Methods". |

A capability-aware client can determine before any tool calls:

- Which servers produce or consume file references.
- Which pairs share an exchange group (matching `exchange_id`).
- Which transfer methods are available between any two servers, and in which direction — by matching one server's `source` role for a method against the other server's `sink` role.
- Which tools to call on each side.

#### Implicit discovery

A client that does not inspect capabilities can still participate. File references are self-describing: the `transfer` object lists available methods with their tool names. On resolution failure, the consumer relays the remaining methods in the error payload (see [Transfer Negotiation](#transfer-negotiation)).

Implicit discovery provides enough information for the client to orchestrate the producer side of any transfer method. However, the client must know the consumer's intake tool by configuration or reasoning. Capability-aware clients avoid this gap entirely.

## Deployer Setup

### Single host (typical)

Mount a shared volume into all participating MCP server containers and set the environment variable:

```yaml
services:
  image-mcp:
    volumes:
      - mcp-exchange:/mcp-exchange
    environment:
      - MCP_EXCHANGE_DIR=/mcp-exchange

  vault-mcp:
    volumes:
      - mcp-exchange:/mcp-exchange
    environment:
      - MCP_EXCHANGE_DIR=/mcp-exchange

volumes:
  mcp-exchange:
```

`MCP_EXCHANGE_ID` is auto-generated on first use. The first server to start checks for `$MCP_EXCHANGE_DIR/.exchange-id`. If absent, it generates a UUID and attempts to create the file using an exclusive-create operation (e.g. `O_CREAT | O_EXCL` on POSIX, which fails atomically if the file already exists). If the exclusive create fails with a file-exists error (`EEXIST`), another server won the race; the server MUST read the UUID from the existing file instead. Implementations MUST NOT use rename-based initialisation for this file, because POSIX `rename(2)` silently overwrites an existing destination, causing split-brain if multiple servers race.

The `.exchange-id` file is UTF-8 plaintext containing the UUID (8-4-4-4-12 hex with hyphens, lower or upper case), with or without a single trailing newline. Consumers MUST strip trailing whitespace and compare the UUID case-insensitively (e.g. by lowercasing both sides before equality). Producers SHOULD write the UUID in lowercase to match the convention emitted by common UUID libraries. The file MUST be created with mode `0o644` so every server in the group (potentially running as different UIDs) can read it.

### Multi-host

Each host gets its own exchange volume with its own exchange ID. Servers on the same host share the `exchange` method. Cross-host transfers use `http` or other remote methods.

## Directory Layout

```
$MCP_EXCHANGE_DIR/
  .exchange-id              # Auto-generated UUID for this group
  image-mcp/                # Namespace for image-mcp
    a1b2c3.png
    .d4e5f6.webp.tmp        # In-progress write (ignored by consumers)
  vault-mcp/                # Namespace for vault-mcp
  scholar-mcp/
    g7h8i9.pdf
```

Each server MUST write only to its own namespace (`$MCP_EXCHANGE_DIR/{namespace}/`). Any server MAY read from any namespace.

Consumers MUST ignore dotfiles. Producers use dotfile-prefixed temporary files during atomic writes (see [Producing Server](#producing-server)).

## Transfer Negotiation

When a client receives a file reference and needs to deliver it to a consuming server:

### Step 1: Method selection

**Capability-aware client:** the file reference's `transfer` object lists the methods the *producer* (the `source`) supports for this file. For each, check whether the destination server advertises the matching `sink` role for that method in its `transfer_methods` — for `http`, the consumer needs `transfer_methods.http.sink`; for `exchange`, a matching `exchange_id`. Pick the highest-priority method where the producer's `source` side and the consumer's `sink` side both line up. `http_upload` does not appear in file references (it is push-direction); a client pushing bytes *into* a server instead looks for `transfer_methods.http_upload.sink` in that server's capability declaration.

**Implicit client:** pass the file reference to the consumer and let it attempt the highest-priority method it recognises.

### Step 2: Attempt transfer

#### For `exchange` method:

The consumer parses the `exchange` URI, compares the exchange ID with its own, and reads the file locally on match.

If the consumer cannot resolve the URI (group mismatch, no exchange configured, or no `exchange` entry in the file reference), it returns a structured error with the remaining methods:

```json
{
  "error": "transfer_failed",
  "method": "exchange",
  "origin_server": "image-mcp",
  "origin_id": "a1b2c3",
  "remaining_transfer": {
    "http": {
      "tool": "create_download_link"
    }
  },
  "message": "Exchange group mismatch: local group is 'cloud-02', file reference specifies 'hades-01'"
}
```

The `remaining_transfer` object is the file reference's `transfer` with the failed method removed. This gives implicit clients everything they need to try the next method.

#### For `http` method:

The client orchestrates a two-step handoff:

1. Call the producer's tool (from `transfer.http.tool` or `remaining_transfer.http.tool`) with `origin_id` set to the file's `origin_id`.
2. The tool returns `{"url": "https://...", "ttl_seconds": 3600}`.
3. Call the consumer's tool (from `transfer_methods.http.sink.tool` in the consumer's capabilities, or known by configuration) with `url` and optionally `path`. If the LLM cannot determine a sensible path, it should omit the parameter and let the consumer auto-generate one.

### Step 3: Exhaustion

If all methods fail or no methods are mutually supported, the consumer or client SHOULD return a `transfer_exhausted` error:

```json
{
  "error": "transfer_exhausted",
  "origin_server": "image-mcp",
  "origin_id": "a1b2c3",
  "attempted_methods": ["exchange", "http"],
  "message": "All transfer methods failed or no mutually supported methods available"
}
```

This signals definitively to the client that retrying is pointless. The client SHOULD report the failure to the user, including which methods were attempted.

## Server Requirements

### Producing server

- **MUST** return a file reference from tools that produce files for cross-server use.
- **MUST** include at least one entry in the file reference's `transfer` object.
- **SHOULD** include a `preview` with at least a `description` field when using the reference-only pattern. When using the augmented response pattern, `preview` is redundant and may be omitted since the native tool response already provides LLM context. For image files, `dimensions` and a small `thumbnail_base64` (under 10 KB) are recommended in previews.
- **MUST** create its namespace directory `$MCP_EXCHANGE_DIR/{namespace}/` if it does not exist (when exchange is configured).
- **MUST** write exchange files atomically: write to a temporary dotfile (e.g. `.{id}.{ext}.tmp`), close the file descriptor, then rename to the final path. This prevents consumers from reading partially written files.
- **MUST** own the complete lifecycle of exchange files it produces. Only the producer deletes its own files. Implementation-specific (SQLite TTL, cron, stat-based, etc.).
- **SHOULD** implement a storage ceiling or LRU eviction policy alongside time-based TTL to prevent shared volume exhaustion during high-throughput operation (e.g. generating thousands of images). TTL alone is insufficient if the production rate exceeds the expiry rate.
- **MUST** validate `origin_id` against the path segment rules before writing. This validation applies to the raw JSON string; producers MUST NOT apply URI decoding to the `origin_id` parameter.
- **MUST**, for tools declared in `transfer_methods.http.source`, accept a parameter named `origin_id` and return a JSON object with at minimum a `url` field.
- **SHOULD** support the `exchange` method when `MCP_EXCHANGE_DIR` is configured.
- **SHOULD** support the `http` method to enable cross-host transfers.

### Consuming server

- **MUST** provide at least one tool that accepts file references (either as a dedicated parameter or by resolving `exchange://` URIs).
- **MUST** attempt `exchange` resolution before signalling failure when a file reference includes an `exchange` entry.
- **MUST** treat the exchange directory as read-only. Consumers MUST NOT modify exchange files. Lifecycle management is the exclusive responsibility of the producing server.
- **MUST** ignore dotfiles in namespace directories.
- **MUST** validate all path segments from exchange URIs after a single pass of URI decoding. JSON-RPC parameters (such as `origin_id`) MUST be validated as raw strings without URI decoding.
- **MUST** include `remaining_transfer` in the `transfer_failed` error, containing the file reference's `transfer` with the failed method removed.
- **SHOULD**, for tools declared in `transfer_methods.http.sink`, accept a parameter named `url` and an optional parameter named `path`. If `path` is omitted, the tool MUST auto-generate a safe local path.

### Receiver server (`http_upload`)

Servers that advertise `http_upload` on the receiver side (i.e. that register a `create_upload_link` tool to accept pushed bytes) MUST meet the obligations listed below. The wire-format details are defined normatively in §"Transfer Methods / `http_upload`"; this section is a conformance checklist that points back to the canonical text.

- **MUST** validate the `destination` parameter per the server's own domain rules **before** any filesystem interaction (spec-level rules in §"Security and Path Resolution"; receiver-specific rules are domain-defined).
- **MUST** honour the URL token requirements — cryptographically unguessable entropy, one-time atomic consumption on first POST, TTL-bounded expiry, indistinguishable `404` for the three "token unusable" conditions (never existed / expired / already consumed). See §"Transfer Methods / `http_upload` / POST contract / URL token".
- **MUST** honour the status-code class table in §"Transfer Methods / `http_upload` / POST contract": `2xx` on success, `404` on token issues, `413` on body-size overflow, `415` on `Content-Type` mismatch with `accepts`, other `4xx` for receiver-domain rejection with `transfer_failed` envelope, `5xx` for server errors with the no-internal-detail-echo rule.
- **MUST** enforce a `max_bytes` body-size ceiling. If `max_bytes` is advertised in the capability declaration, that value is the ceiling. If no capability-declared ceiling applies, the receiver MUST establish and enforce a server-defined default ceiling and return it in every `create_upload_link` response, so the response's `max_bytes` (MUST) field always has a concrete value to echo. This obligation backs the always-return rule in §"Transfer Methods / `http_upload` / Receiver-side tool: `create_upload_link`".

Implementors writing a `http_upload` receiver should treat this checklist plus the wire-format section as a unit; the two are kept separate to surface the receiver-side obligations alongside §"Producing server" and §"Consuming server" without duplicating normative content.

### Defaults

| Parameter | Default |
|---|---|
| Exchange file TTL | 1 hour |
| Exchange ID | Auto-generated UUIDv4, persisted in `$MCP_EXCHANGE_DIR/.exchange-id` via exclusive create |
| Namespace | MCP server name from `initialize` handshake |
| Method priority | `exchange` > `http` |
| Storage ceiling | No default (implementation-specific, but SHOULD be configured for high-throughput producers) |

## Design Decisions

### Transfer methods as an extension point

Rather than hardcoding `exchange` and `http` as the only two tiers, the spec treats them as instances of a general concept. New methods can be added by defining a key, metadata, tool contract, and priority position. Existing clients and servers ignore methods they don't recognise, making the protocol forward-compatible.

### No inline content in file references

File references carry transfer metadata, not file content. The actual bytes are either already in the native tool response (augmented pattern) or accessible via the transfer methods (reference-only pattern). This separation means file references are always small and cheap to pass through context, regardless of file size.

### Exchange ID scoped to volume, not server

Two deployments on different hosts each get their own exchange ID. A consuming server immediately detects a group mismatch without ambiguity.

### Producer-owned lifecycle

Consumers never delete exchange files. This prevents a class of bugs where one consumer deletes a file that another consumer has not yet read. The producer is the single authority over its namespace directory.

### Standardised parameter names per method

Each transfer method defines standard parameter names (e.g. `origin_id` for the `http` producer, `url` and `path` for the `http` consumer). Servers that internally use different names MUST alias. This trades a one-time implementation cost for permanent simplicity: clients never need parameter mappings.

### Remaining methods in error payloads

When a transfer method fails, the consumer returns the remaining untried methods from the file reference. This makes implicit clients viable: they don't need to read capabilities upfront, they just follow the error chain. Capability-aware clients can skip failed methods proactively.

### Atomic file writes

Producers write to dotfile-prefixed temporary files and atomically rename. Consumers ignore dotfiles. This makes partially written files invisible without coordination. Note that atomic rename is safe here because the producer controls both the source (temp file) and destination (final file) within its own namespace. The rename-is-unsafe warning applies only to `.exchange-id` initialisation, where multiple writers race for the same destination.

### Exclusive create for exchange ID

The `.exchange-id` file uses `O_CREAT | O_EXCL` instead of the write-then-rename pattern used for exchange files. This is because POSIX `rename(2)` silently overwrites existing files, making it unsafe when multiple processes race to create the same file. `O_EXCL` fails atomically on collision, giving a clear signal to read the winner's value instead.

### Unguessable one-time URLs for http method

The `http` method generates download URLs that are cryptographically unguessable and ideally single-use. This follows the S3 presigned URL pattern: embedding the secret in the URL maximises consumer compatibility (any tool that can fetch a URL works) while providing equivalent security to an `Authorization` header under TLS. Adding header-based auth would require every consuming server to implement custom header injection, violating the goal of a lightweight convention. The same URL pattern also supports direct human download: the LLM can include the URL in its response for the user to click, with no additional infrastructure needed.

### http method as universal fallback

The `http` method is deliberately simple (produce a URL, consume a URL) because this pattern is universally supported: every MCP server with a fetch tool can consume it, every server with a public HTTP endpoint can produce it, and humans can use the URLs directly. This makes `http` the lowest-common-denominator method that always works, even across hosts, across networks, and for direct user access. Higher-priority methods like `exchange` optimise for specific deployment topologies.

### Validate after single decode, but only for URIs

Path validation after URI decoding applies strictly to `exchange://` URI parsing. JSON-RPC parameters like `origin_id` are opaque strings that MUST be validated as-is, never URI-decoded. This distinction prevents a subtle data corruption bug: an `origin_id` containing a literal `%` character (e.g. `file-%2F-name`) would be mutated by URI decoding into `file-/-name`, which would then fail path validation or, worse, create a traversal path. The two validation contexts (URI components vs JSON strings) share the same rules but differ in preprocessing.

### Implicit discovery is deliberately incomplete

Implicit discovery fully solves the producer side (file references and error payloads carry method-specific tool names). It does not solve the consumer side (the consumer's intake tool name is only in capabilities). This is a deliberate trade-off: full implicit routing would require the consumer to embed its own tool names in error payloads, adding complexity for a marginal case. Capability-aware clients get full deterministic routing.

### Preview for LLM context, not human display

The `preview` field exists for the reference-only usage pattern, where the LLM never sees the full file content. In the augmented response pattern, the native tool response already provides LLM context and `preview` is redundant. This dual-pattern approach lets producers adopt file exchange incrementally: start with augmented responses (add a `file_ref` to existing tool output), then optionally move to reference-only when context efficiency matters. The `preview` field is intentionally loosely structured because different file types need different metadata, and over-specifying the schema would limit producer flexibility.

## Future Considerations

### Additional transfer methods

The transfer methods abstraction is designed for extension. Candidate methods include `s3` (presigned URLs), `scp` (SSH copy), `gdrive` (Google Drive sharing), and `webdav`. Each requires defining its metadata, tool contract, parameter names, and priority position.

### Content negotiation

A producing server could check the consuming server's `consumes` list and produce files in a preferred format (e.g. WebP over PNG). Enabled by the existing `produces`/`consumes` fields but out of scope for the current spec version.

### Streaming / large files

The current spec assumes files fit on disk. Chunked transfer or streaming methods may be needed for very large files.

### Formalisation as MCP extension

This specification is designed to be superseded. The ideal outcome is that MCP adopts native file transfer or a bulk data sideband, making these conventions unnecessary. The current design is structured for that transition: the file reference maps naturally to a hypothetical MCP-native file handle, the transfer methods abstraction can accommodate an `mcp-native` method alongside the current ones, and the `preview` field serves the same purpose regardless of how the underlying bytes move. If MCP does not add native support, this convention can also graduate from `experimental` to a formal community standard.

### Versioning and compatibility

The spec uses semantic versioning (`major.minor`). The `version` field in capability declarations advertises the spec version the server implements.

**Within a minor version** (e.g. 0.2.0 to 0.2.3): changes are additive only. New optional fields, new transfer methods, new error codes. Existing implementations continue to work without changes. A server advertising `0.2` is compatible with any client or server that understands `0.2`, regardless of patch level.

**Across minor versions** (e.g. 0.2 to 0.3): may introduce new required fields or change semantics. Servers and clients SHOULD accept file references from older minor versions on a best-effort basis: ignore unrecognised fields, tolerate missing optional fields, and attempt transfer with whatever methods are mutually understood. A server that receives a file reference with an unrecognised spec version SHOULD still attempt resolution rather than rejecting outright.

**Across major versions** (e.g. 0.x to 1.0): no backward compatibility guaranteed. Major version changes signal a fundamental redesign, likely prompted by MCP adopting native file transfer.

Transfer methods provide additional agility: because methods are identified by string keys and unknown methods are silently skipped, new methods can usually be introduced without a spec version bump. A server advertising version `0.3` can include a `gdrive` transfer method that older clients simply ignore.

The general rule above ("Across minor versions: may introduce new required fields or change semantics") manifests in practice as the **bump-trigger checklist** for new methods. A new method warrants a minor version bump when it introduces wire-level constructs that other implementations need to know about beyond the method key itself — specifically, any of: a new transfer *direction* (push vs pull), a new validation regime with carve-outs in the spec's security rules, a new error class or envelope field, or a new tool-contract shape that requires explicit advertisement. The `http_upload` method introduced in v0.3 met all four of these criteria and warranted the 0.2 → 0.3 bump; a hypothetical `gdrive` method that just adds a new pull-direction method key with the existing `http`-style contract would not. The rule of thumb: if a v0.x implementation that doesn't know about the new method would behave correctly when ignoring it AND the method introduces no new validation rules or error vocabulary, ship without a bump. Otherwise bump minor and document the new constructs. §"Adding future methods" defines the structural recipe for declaring a method; this section governs whether the declaration also requires a spec-version bump.

**Reading a pre-`source`/`sink` capability declaration.** A server emitting a v0.2.x-era capability declares `transfer_methods.http` as a flat `{tool: <name>}` with no `source` / `sink` sub-objects. A reader encountering a flat `http` block treats it as a single-role declaration. When the peer's `produces` is non-empty and `consumes` is empty, the flat tool is the `source`-side tool; when `consumes` is non-empty and `produces` is empty, it is the `sink`-side tool. When the lists do not single out one role — both non-empty, or both empty — the declaration does not pin down which `http` role the flat block fills (a v0.2.x server expresses at most one `http` role regardless of how many MIME-type lists it populates, and may populate none), so the reader attempts the role its transfer needs and falls back through the normal negotiation rules (§"Transfer Negotiation") if the call does not fit. The role is never inferred from the tool-name string; tool names are implementation-defined. A v0.2.x server's inability to advertise both `http` roles at once is exactly what the `source` / `sink` shape resolves.

**Version `0.4` is permanently skipped.** The `0.4` label was used by an earlier set of inline amendments that were later reverted, and by a stale implementation-side version constant; reusing the number would be ambiguous about which `0.4` is meant. The minor release after `0.3` is `0.5`.

### Mixed-OS exchange groups

The spec assumes POSIX filesystem semantics. Mixed-OS exchange groups would require standardising path handling. Out of scope since Docker containers are Linux regardless of host OS.

## Reference Implementations

- **markdown-vault-mcp** ([pvliesdonk/markdown-vault-mcp](https://github.com/pvliesdonk/markdown-vault-mcp)): Consumer. Has `fetch` tool (accepts URL + path). Would add exchange resolution and declare `transfer_methods: {exchange: {}, http: {sink: {tool: "fetch"}}}`.
- **image-mcp**: Producer. Has `create_download_link` tool with TTL. Would add exchange writes, file references in tool responses, and declare `transfer_methods: {exchange: {}, http: {source: {tool: "create_download_link"}}}`. The `create_download_link` tool would need to accept `origin_id` as a parameter.
