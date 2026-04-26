# MCP File Exchange Specification

**Version:** 0.3
**Status:** experimental
**Tags:** mcp, spec, interop

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
| `origin_id` | MUST | Opaque identifier for this file on the origin server. Passed as a parameter when requesting transfer via any method. |
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

A transfer method defines how a file moves from a producing server to a consuming server. The spec defines two methods; future extensions may add more.

Each method is identified by a string key (e.g. `"exchange"`, `"http"`) and has method-specific metadata in both the file reference and the capability declaration.

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

In a file reference:

```json
"http": {
  "tool": "create_download_link"
}
```

In a capability declaration (producer):

```json
"http": {
  "tool": "create_download_link"
}
```

In a capability declaration (consumer):

```json
"http": {
  "tool": "fetch"
}
```

**Standard parameters for the `http` method:**

- Producer tool MUST accept a parameter named `origin_id`. The value is **opaque** to the client — the producer MAY interpret `origin_id` as a path, document id, image id, or any internally-meaningful handle. Tools MAY additionally accept domain-named aliases (`path`, `document_id`, etc.), but `origin_id` MUST be a working alias. The handle is whatever the producer chose to put in `file_ref.origin_id` when it earlier emitted the file reference; round-tripping that exact string back to the producer's `http` tool MUST resolve to the same file.
- Producer tool MUST return a JSON object with at minimum a `url` field. It MAY include `ttl_seconds` and `mime_type`.
- The generated URL MUST be cryptographically unguessable (e.g. containing a UUID or HMAC token in the path or query string). The producer SHOULD invalidate the URL after a single successful download (one-time use). TLS/HTTPS is assumed; the URL path is encrypted in transit, so embedding secrets in the URL is equivalent in security to using an `Authorization` header while being compatible with any consumer that can fetch a URL.
- Consumer tool MUST accept a parameter named `url`. It SHOULD accept an optional parameter named `path` to allow client-directed placement. If `path` is omitted or invalid, the consumer MUST auto-generate a safe local path (e.g. derived from `origin_id` or a UUID). This prevents failures caused by LLMs hallucinating invalid directory structures.
- A consumer that advertises both `exchange` and `http` in its `transfer_methods` MUST accept both `exchange://` and `http(s)://` URIs in its declared intake tool, dispatching by URI scheme. In other words, the same tool name appears in `transfer_methods.http.tool` and is also responsible for resolving `exchange://` URIs. This keeps the client-side surface uniform: the client always calls the consumer's intake tool with a URI, regardless of method.

#### Method priority

When multiple methods are available, the client SHOULD prefer them in this order:

1. `exchange` (zero-cost local read, no public URL created)
2. `http` (network transfer, creates a temporary public endpoint)

Future methods slot into this priority list by convention. Methods with lower latency, lower cost, or stronger privacy properties are preferred.

#### Adding future methods

A new transfer method (e.g. `s3`, `scp`, `gdrive`) is defined by:

1. A method key string.
2. The metadata it carries in the file reference.
3. The metadata it carries in the capability declaration (tool names and standard parameter names).
4. Its position in the priority order.

Servers that do not recognise a method ignore it. Clients that do not recognise a method skip it and try the next one. This makes the protocol forward-compatible: old clients degrade gracefully when new methods appear.

### Exchange Group

An exchange group is a set of MCP servers that share a filesystem directory and can use the `exchange` transfer method. Membership is opt-in via environment variables:

| Variable | Required | Description |
|---|---|---|
| `MCP_EXCHANGE_DIR` | Yes | Absolute path to the shared directory. |
| `MCP_EXCHANGE_ID` | No | Unique identifier for this exchange group. Auto-generated if unset (see [Deployer Setup](#deployer-setup)). |
| `MCP_EXCHANGE_NAMESPACE` | No | Server namespace within the exchange group. Defaults to MCP server name. |

Servers that find `MCP_EXCHANGE_DIR` set and pointing to a valid directory participate in the exchange group. Servers that do not find this variable omit the `exchange` method from their file references and capability declarations but can still participate via other methods.

#### Namespace collision detection

A server starting up MUST check whether another file under `$MCP_EXCHANGE_DIR/{namespace}/` was modified within the last "liveness window" (default 60 seconds) by a writer with a different process identity. On detection it SHOULD log a warning. (A startup-blocking lock is rejected as overkill — the rare collision is human error and best caught visibly rather than fatally.) Deployers running multiple instances of the same server image SHOULD configure `MCP_EXCHANGE_NAMESPACE` explicitly to avoid such collisions.

#### `.exchange-id` file format

The `.exchange-id` file MUST be UTF-8 plaintext containing a single UUID in 8-4-4-4-12 hex form (lower or upper case), with or without a single trailing newline. Consumers MUST strip trailing whitespace before comparison. The file MUST be created with mode `0644`.

#### Filesystem ownership

The shared volume MUST be writable by the effective UID of every participating server. Deployers using the pvl-family `docker-entrypoint.sh` UID-drop pattern (`PUID`/`PGID` environment variables) MUST align UIDs across containers — a producer running as UID 1000 and a consumer running as UID 1001 will see permission errors on each other's writes. Either align UIDs explicitly, or deploy the volume with a permissive group (`PGID` shared, mode `0775`).

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

During the MCP `initialize` handshake, a participating server declares exchange support in the `experimental` field of its capabilities. The `version` field advertises the spec's `MAJOR.MINOR` only — patch-level revisions are internal to the spec and do not change capability negotiation.

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
            "tool": "create_download_link"
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
            "tool": "fetch"
          }
        }
      }
    }
  }
}
```

| Field | Required | Description |
|---|---|---|
| `version` | MUST | Spec version (`MAJOR.MINOR` only, e.g. `"0.3"`). |
| `namespace` | MUST | The server's exchange namespace. |
| `exchange_id` | SHOULD | The exchange group ID. Present when the server participates in an exchange group. |
| `produces` | SHOULD | MIME types this server can produce as file references. |
| `consumes` | SHOULD | MIME types this server can accept via file references. |
| `transfer_methods` | MUST | Object whose keys are supported transfer method names. Values contain method-specific configuration (e.g. tool names). |

A capability-aware client can determine before any tool calls:

- Which servers produce or consume file references.
- Which pairs share an exchange group (matching `exchange_id`).
- Which transfer methods are available between any two servers (intersection of their `transfer_methods` keys).
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
    user: "1000:1000"             # PUID/PGID — align across all containers
    volumes:
      - mcp-exchange:/mcp-exchange
    environment:
      - MCP_EXCHANGE_DIR=/mcp-exchange

  vault-mcp:
    user: "1000:1000"
    volumes:
      - mcp-exchange:/mcp-exchange
    environment:
      - MCP_EXCHANGE_DIR=/mcp-exchange

volumes:
  mcp-exchange:
```

`MCP_EXCHANGE_ID` is auto-generated on first use. The first server to start checks for `$MCP_EXCHANGE_DIR/.exchange-id`. If absent, it generates a UUID and attempts to create the file using an exclusive-create operation (e.g. `O_CREAT | O_EXCL` on POSIX, which fails atomically if the file already exists). If the exclusive create fails with a file-exists error (`EEXIST`), another server won the race; the server MUST read the UUID from the existing file instead. Implementations MUST NOT use rename-based initialisation for this file, because POSIX `rename(2)` silently overwrites an existing destination, causing split-brain if multiple servers race.

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

**Capability-aware client:** intersect the file reference's `transfer` keys with the consumer's `transfer_methods` keys. Pick the highest-priority method that both sides support.

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
3. Call the consumer's tool (from `transfer_methods.http.tool` in the consumer's capabilities, or known by configuration) with `url` and optionally `path`. If the LLM cannot determine a sensible path, it should omit the parameter and let the consumer auto-generate one.

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
- **MUST**, for tools declared in `transfer_methods.http`, accept a parameter named `origin_id` (treating its value as opaque per the [`http` method](#http-download-url) requirements) and return a JSON object with at minimum a `url` field.
- **SHOULD** support the `exchange` method when `MCP_EXCHANGE_DIR` is configured.
- **SHOULD** support the `http` method to enable cross-host transfers.

### Consuming server

- **MUST** provide at least one tool that accepts file references (either as a dedicated parameter or by resolving `exchange://` URIs).
- **MUST** attempt `exchange` resolution before signalling failure when a file reference includes an `exchange` entry.
- **MUST** treat the exchange directory as read-only. Consumers MUST NOT modify exchange files. Lifecycle management is the exclusive responsibility of the producing server.
- **MUST** ignore dotfiles in namespace directories.
- **MUST** validate all path segments from exchange URIs after a single pass of URI decoding. JSON-RPC parameters (such as `origin_id`) MUST be validated as raw strings without URI decoding.
- **MUST** include `remaining_transfer` in the `transfer_failed` error, containing the file reference's `transfer` with the failed method removed.
- **SHOULD**, for tools declared in `transfer_methods.http`, accept a parameter named `url` and an optional parameter named `path`. If `path` is omitted, the tool MUST auto-generate a safe local path. When the consumer also advertises `exchange`, the same tool MUST accept both `exchange://` and `http(s)://` URIs and dispatch by URI scheme.

### Defaults

| Parameter | Default |
|---|---|
| Exchange file TTL | 1 hour |
| Exchange ID | Auto-generated UUIDv4, persisted in `$MCP_EXCHANGE_DIR/.exchange-id` via exclusive create |
| Namespace | MCP server name from `initialize` handshake |
| Method priority | `exchange` > `http` |
| Storage ceiling | No default (implementation-specific, but SHOULD be configured for high-throughput producers) |
| Liveness window for collision detection | 60 seconds |

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

Each transfer method defines standard parameter names (e.g. `origin_id` for http producer, `url` and `path` for http consumer). Servers that internally use different names must alias. This trades a one-time implementation cost for permanent simplicity: clients never need parameter mappings.

### `origin_id` is an opaque round-trip handle

The `origin_id` parameter on a producer's `http` tool MUST be a working alias, but the producer is free to interpret its value as any internally meaningful handle (path, document id, image variant key, etc.). Tools MAY also accept domain-named aliases (e.g. `path`, `document_id`). The contract the client relies on is round-trip identity: whatever the producer put in `file_ref.origin_id` will resolve to the same file when sent back to `create_download_link(origin_id=...)`. This was tightened in v0.3 because v0.2.5's "tools MUST rename their parameter" reading would have broken every existing `create_download_link` signature in the family.

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

### Capability declaration via FastMCP

FastMCP 3.x exposes `experimental_capabilities` on its lowlevel server's `create_initialization_options()` method but does **not** call that method with anything from the `FastMCP(...)` constructor — every transport site invokes `create_initialization_options()` with no arguments, dropping any state set elsewhere. Until FastMCP grows a first-class hook, implementations of this spec MAY patch the lowlevel server's `create_initialization_options` on a per-instance basis to inject the `experimental.file_exchange` payload. The `fastmcp_pvl_core.register_file_exchange_capability` helper does exactly this. An upstream feature request on `jlowin/fastmcp` to expose `experimental_capabilities` as a constructor argument is the long-term fix; the helper's docstring links to the issue.

## Future Considerations

### Additional transfer methods

The transfer methods abstraction is designed for extension. Candidate methods include `s3` (presigned URLs), `scp` (SSH copy), `gdrive` (Google Drive sharing), and `webdav`. Each requires defining its metadata, tool contract, parameter names, and priority position.

### Content negotiation

A producing server could check the consuming server's `consumes` list and produce files in a preferred format (e.g. WebP over PNG). Enabled by the existing `produces`/`consumes` fields but out of scope for v0.3.

### Streaming / large files

The current spec assumes files fit on disk. Chunked transfer or streaming methods may be needed for very large files.

### Formalisation as MCP extension

This specification is designed to be superseded. The ideal outcome is that MCP adopts native file transfer or a bulk data sideband, making these conventions unnecessary. The current design is structured for that transition: the file reference maps naturally to a hypothetical MCP-native file handle, the transfer methods abstraction can accommodate an `mcp-native` method alongside the current ones, and the `preview` field serves the same purpose regardless of how the underlying bytes move. If MCP does not add native support, this convention can also graduate from `experimental` to a formal community standard.

### Versioning and compatibility

The spec uses semantic versioning (`major.minor`). The `version` field in capability declarations advertises the spec version the server implements (`MAJOR.MINOR` only — patch level is internal).

**Within a minor version** (e.g. 0.3.0 to 0.3.3): changes are additive only. New optional fields, new transfer methods, new error codes. Existing implementations continue to work without changes. A server advertising `0.3` is compatible with any client or server that understands `0.3`, regardless of patch level.

**Across minor versions** (e.g. 0.2 to 0.3): may introduce new required fields or change semantics. Servers and clients SHOULD accept file references from older minor versions on a best-effort basis: ignore unrecognised fields, tolerate missing optional fields, and attempt transfer with whatever methods are mutually understood. A server that receives a file reference with an unrecognised spec version SHOULD still attempt resolution rather than rejecting outright.

**Across major versions** (e.g. 0.x to 1.0): no backward compatibility guaranteed. Major version changes signal a fundamental redesign, likely prompted by MCP adopting native file transfer.

Transfer methods provide additional agility: because methods are identified by string keys and unknown methods are silently skipped, new methods can be introduced without a spec version bump. A server advertising version `0.3` can include a `gdrive` transfer method that older clients simply ignore.

### Mixed-OS exchange groups

The spec assumes POSIX filesystem semantics. Mixed-OS exchange groups would require standardising path handling. Out of scope since Docker containers are Linux regardless of host OS.

## Reference Implementations

- **markdown-vault-mcp** ([pvliesdonk/markdown-vault-mcp](https://github.com/pvliesdonk/markdown-vault-mcp)): Consumer. Has `fetch` tool (accepts URL + path). Will add exchange resolution and declare `transfer_methods: {exchange: {}, http: {tool: "fetch"}}`.
- **image-mcp**: Producer. Has `create_download_link` tool with TTL. Will add exchange writes, file references in tool responses, and declare `transfer_methods: {exchange: {}, http: {tool: "create_download_link"}}`. The `create_download_link` tool already accepts an `origin_id` alias for its existing `uri` parameter (per the v0.3 opaque-handle clarification).

## Changelog

### v0.3 (2026-04 — this revision)

- **Origin-id is an opaque round-trip handle, not a parameter rename.** v0.2.5 read as "tools MUST rename their parameter to `origin_id`," which would break every existing `create_download_link` signature in the family (markdown-vault uses `path`, image-gen uses `uri`, paperless uses `document_id+variant`). v0.3 keeps the requirement that `origin_id` MUST work as an alias and additionally lets the producer interpret the value as any internally-meaningful handle.
- **Capability advertises `MAJOR.MINOR` only.** v0.2.5 was already consistent (spec at 0.2.5, capability at `"0.2"`); v0.3 makes this explicit.
- **`.exchange-id` file format pinned.** UTF-8 plaintext UUID, with or without a single trailing newline; consumers strip trailing whitespace before comparison; mode `0644`.
- **Namespace collision detection.** Servers SHOULD warn (not block) on detecting a recently-modified file from a different writer in their namespace; deployers SHOULD set `MCP_EXCHANGE_NAMESPACE` explicitly when running multiple instances of the same image.
- **Filesystem ownership.** Shared volume MUST be writable by every participating server's effective UID; PUID/PGID alignment is a deployer responsibility.
- **Consumer fetch-tool dispatch.** A consumer that advertises both `exchange` and `http` MUST handle both URI schemes in its single declared intake tool.
- **Capability-declaration mechanism on FastMCP.** New "Capability declaration via FastMCP" design-decision section documents that FastMCP 3.x has no first-class hook and implementations may patch `_mcp_server.create_initialization_options` until an upstream issue lands.
- All capability examples bumped from `"version": "0.2"` to `"version": "0.3"`.

### v0.2.5 (2026-04-04 — prior revision, retained for context)

Source frontmatter:

```yaml
---
date: '2026-04-04'
status: draft
tags:
- mcp
- spec
- interop
title: MCP File Exchange Specification
version: 0.2.5
---
```

This revision lived in a private Obsidian vault and never had any external consumers. v0.3 is the first publicly-published version.
