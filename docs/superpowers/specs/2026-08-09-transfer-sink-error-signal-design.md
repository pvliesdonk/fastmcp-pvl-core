# `TransferSinkError` — sink-raisable HTTP status signal (issue #233)

## Problem

When a `TransferSink.read` / `write` raises any exception other than a
claim-time `TransferTokenError`, the `/transfer/{token}` handler releases the
token (correct) but returns a generic **500** to the client. A sink that knows
*why* it failed has no way to signal it:

- A download link is minted for a resource that exists at mint time; the backing
  file is then deleted before the client GETs the link. `sink.read` raises
  `FileNotFoundError` → the client sees **500** for what is semantically a
  **gone** resource, so it keeps retrying a link that will never serve
  (image-generation-mcp#307).
- A sink resolves a live backend per request (markdown-vault-mcp#981: a
  ref-counted `Vault` torn down during a server-idle window). A still-valid
  one-time link followed with no session open raises `RuntimeError` → **500**,
  where the semantically correct answer is a retryable **503 Service
  Unavailable**.

Both collapse to 500, so a client cannot tell "give up, it's gone" from "try
again shortly".

## Ownership — why this is a domain hook, not a shape override

`CLAUDE.md` reserves HTTP status codes as shape decisions pvl-core owns, using
"what status should an oversize body return?" as the counter-example. That case
is a **fixed protocol behaviour** core knows (413). The sink-failure case is
different in kind: **core cannot know** whether a domain read failed because the
resource is gone, is forbidden, or the backend is briefly down — that answer is
about the downstream's domain. So signalling the *semantics* of a sink failure
is a legitimate **domain hook** (the classification test's "pvl-core literally
cannot answer" bucket).

Core keeps the **shape invariants**; the sink supplies only the **judgment**:

- The signal must be a real error status (a 4xx or 5xx); a sink cannot fake a
  2xx/3xx or otherwise change the success/redirect shape.
- Release-on-failure is unchanged: a signalled failure still releases the token,
  exactly as an unexpected one does. Only the client-facing status changes.
- The default is unchanged: any exception that is **not** a `TransferSinkError`
  still maps to an opaque 500.

This is not the shape-override kwarg pattern reviewers reject: the sink is not
overriding a status core would otherwise pick (core has no basis to pick between
gone / unavailable / forbidden), and it cannot leave the 4xx/5xx error range.

## Public API

New in `_transfer/sink.py`, exported top-level and from `fastmcp_pvl_core.transfer`:

```python
class TransferSinkError(Exception):
    """Raised by a TransferSink.read/write to signal a specific HTTP error
    status for this transfer, instead of the opaque 500 an unexpected error
    yields. status_code must be a 4xx/5xx (400-599); the handler releases the
    token and returns that status."""
    status_code: int
    def __init__(self, status_code: int, *args: object) -> None: ...
        # raises ValueError if not 400 <= status_code <= 599
```

Seven named sugar subclasses, each fixing its status via `super().__init__(...)`
and taking `(*args)` for an optional message:

| Class | Status | Meaning |
|---|---|---|
| `TransferResourceGoneError` | 410 | the resource the link pointed at existed and is now gone |
| `TransferNotFoundError` | 404 | the resource was never there (distinct from Gone) |
| `TransferForbiddenError` | 403 | the handle resolved but access is denied |
| `TransferRateLimitedError` | 429 | a backend the sink calls is rate-limiting |
| `TransferUnavailableError` | 503 | the backend is temporarily unavailable; retry |
| `TransferBadGatewayError` | 502 | an upstream dependency returned an invalid/failed response |
| `TransferGatewayTimeoutError` | 504 | an upstream dependency timed out |

- The base accepts **any** 4xx/5xx, so a downstream can signal a status without
  a named class (e.g. `TransferSinkError(401)`) now and future ones without core
  enumerating a fixed list — "prepare for future use cases". The sugar covers
  the common cases; the base covers the rest.
- `status_code` outside 400–599 raises `ValueError` at construction, so a
  programming error (e.g. `TransferSinkError(200)`) surfaces at the raise site
  rather than silently mis-mapping.
- **No `401` sugar by design**: in this subsystem the capability-link token *is*
  the auth (checked at `claim`), so a sink signalling 401 (which implies a
  `WWW-Authenticate` challenge a bare response cannot carry) is semantically
  awkward — `TransferForbiddenError` (403) covers "authenticated-but-denied".
  The base still permits `TransferSinkError(401)` for a genuine need.

### 410 for "gone", not 404

`TransferResourceGoneError` maps to **410 Gone**, distinct from the claim-error
**404**. The sink runs only *after* a successful `claim`, so the caller already
held a valid capability link — disclosing "gone" to an already-authorised caller
leaks nothing, and 410 lets a client distinguish "my link is bad/expired" (404)
from "the thing existed and is now gone" (410). Both are non-retryable 4xx.

## Handler change (`_transfer/routes.py`)

`_download` and `_upload` gain a `except TransferSinkError` arm **before** the
existing `except BaseException`, releasing the token and returning the signalled
status:

```python
try:
    body, media_type, filename = await sink.read(cast(str, claim.sink_handle))
except TransferSinkError as exc:
    await _release_quietly(store, claim)
    logger.info("transfer download sink signalled %d: %s",
                exc.status_code, type(exc).__name__)
    return Response(status_code=exc.status_code)
except BaseException:
    await _release_quietly(store, claim)
    raise
```

- The log records the status and the exception **class name only** — never the
  message, which may embed a domain path or the token-derived key (consistent
  with `_release_quietly`'s existing redaction).
- No `Connection: close`: a download carries no request body, and on upload the
  body is fully read (`_read_capped` returned) before `sink.write` is called, so
  nothing is left undrained.
- `_upload`'s existing `_BodyTooLargeError` → 413 path is untouched.

## Surfacing

All eight names (`TransferSinkError` plus the seven sugar subclasses) are part of
the public **domain seam** (a sink implementer imports and raises them), so they
are exported from `_transfer/__init__.py`, re-exported top-level from
`fastmcp_pvl_core`, and included in `fastmcp_pvl_core/transfer.py`'s `__all__`.
The internal `Token*Error` types stay internal (unchanged; #250).

## Also in this PR — docstring reconciliation

`register.py`'s module docstring lists "the status codes" among the shape
decisions pvl-core owns. With this change a sink may signal its own read/write
failure status, so that clause is narrowed to the **protocol** status codes
(claim, method, size-cap, success) and notes the `TransferSinkError` delegation.
`routes.py` and `sink.py` docstrings gain a sentence on the signal. No behaviour
claim beyond what the code does.

## Tests

- **Exception unit tests** (new `tests/test_transfer_sink.py`): `status_code`
  stored; `400 <= status_code <= 599` enforced (a 299 / 600 raises `ValueError`);
  each of the seven sugar subclasses maps to its status (410/404/403/429/503/
  502/504) and is an instance of `TransferSinkError` (so a handler catching the
  base catches them all); an optional message arg is preserved on `str(exc)`.
- **Handler integration** (extend `tests/test_transfer_routes.py`): a download
  sink raising `TransferResourceGoneError` → **410** and the link is **released**
  (a retry after the resource returns serves 200); `TransferUnavailableError` →
  **503** + released; `TransferSinkError(403)` → **403**; an upload sink raising
  each → the same statuses + released. The existing "generic `RuntimeError` →
  500 + released" tests stay **unchanged** (the default path is preserved).
- **Public surface** (extend `tests/test_transfer_public_surface.py`): all eight
  names appear in `fastmcp_pvl_core.transfer.__all__`, alias the top-level
  re-exports, and the `Token*Error` internals remain unexported.
```
