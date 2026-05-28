# File-Exchange #146 — upload data plane failure-mode matrix

> **Status:** Process artifact for the fourth (TDD-first) attempt at #146.
> The behavioural contract is unchanged: see
> `2026-05-24-file-exchange-146-upload-data-plane-design.md` for the
> spec. This document enumerates the failure modes the implementation
> must address **before** any test or implementation code is written,
> per the `CLAUDE.md` "Test-driven discipline" section. Three prior
> attempts (PR #163, #164, #165) failed at integration time precisely
> because these modes were discovered post-hoc as bot findings; this
> matrix flips the order.

## How this is used

Test order = matrix order. Each row below becomes one (or a small
group of closely-related) failing tests, then the implementation code
that makes those tests pass. **No row is implemented before its test
exists and fails.** A row that cannot be tested mechanically is a
design hole, not an acceptable carve-out.

The matrix's row count is the size of the contract's edge surface.
If a row is missing on first read, **add it here** rather than
discover it in review.

## Source attribution

Rows are sourced from:

- **Spec** — the §10.3 / §12 / §15 / §13 obligations recorded in
  `2026-05-24-file-exchange-146-upload-data-plane-design.md`.
- **Mirror** — failure modes already handled by the merged #145
  download data plane (`_download.py` on `main`) that the upload side
  must mirror coherently.
- **Bot finding history** — modes flagged in PR #163, #164, #165 bot
  rounds. These are the cheapest signal we have about which modes
  this kind of code consistently misses; treating them as gold is
  the central lesson of the three-abandon spiral.

Each row tags its primary source in square brackets.

## A. Ordering modes

| # | Trigger | Required behaviour | Test |
|---|---|---|---|
| A1 | Receiver mints a token, the route's first PUT succeeds, a second PUT against the same URL arrives. [spec §10.3, bot-165 finding 2 inverse] | The second PUT returns `404`. `token_store.consume` was called exactly once (after the first store). The sink was called exactly once. | Route test: mint → PUT (204) → PUT (404); sink call count == 1; consume call count == 1. |
| A2 | The sink raises mid-`store_artifact`. [spec §10.3 single-success-per-URL, bot-163 round] | The token is **not** consumed. A subsequent PUT against the same URL succeeds (the slot is not burned). | Route test: PUT with a sink that raises → 500, then PUT with a sink that succeeds → 204; consume called once total, at the end. |
| A3 | `acceptMimeTypes` mismatch on a PUT. [spec §10.3 RFC 7231, bot-163] | `415`, sink **not** called, token **not** consumed. | Route test: mint with `expected.acceptMimeTypes=["application/json"]`, PUT `text/plain` → 415; sink call count == 0; consume call count == 0. |
| A4 | Body exceeds `expected.maxSize` or `config.file_exchange_max_artifact_size` (whichever is smaller). [spec §15] | `413`, sink **not** called, token **not** consumed, body read stopped within one chunk past the cap. | Route test: cap=N, send N+1 bytes → 413; sink call count == 0; assert the staging helper read at most N + one-chunk bytes. |
| A5 | `Content-Digest` mismatch. [spec §10.3 verify-before-use, bot-165] | `400`, sink **not** called, token **not** consumed. The verification happens **after** the full body is staged but **before** the sink is called. | Route test: send a body with a deliberately wrong `Content-Digest` → 400; sink not called; consume not called. |
| A6 | `expected.requireDigest` lists `sha-256` and the request has **no** `Content-Digest` header. [spec §10.3, bot-163] | `400`, sink **not** called, token **not** consumed. | Route test: mint with `requireDigest=["sha-256"]`, PUT without `Content-Digest` → 400. |
| A7 | Atomicity of `register_file_exchange_routes`. [bot-165 finding 2] | If `sink` is given without `config`, raise `ValueError` **before** any route is mounted. If both sides are given and the download registrar raises after the upload check passes, the upload route is also not mounted (best-effort: validate everything first, mount second). | Unit test on the registrar: a precondition violation does not mount either route; validate this by injecting a mock `mcp.custom_route` and asserting call count == 0 in the failure case. |
| A8 | Mounting order: `register_file_exchange_routes` accepts source-only, sink-only, or both, in any caller order. [spec] | Each combination mounts exactly the routes that are asked for, never the other. | Registrar test: three combinations, each asserts the mounted-path set. |

## B. Lifecycle modes

| # | Trigger | Required behaviour | Test |
|---|---|---|---|
| B1 | The route opens a `mkstemp` and `fdopen`s it; any exception before the staging `try/finally` would leak the fd. [bot-165 finding 1, mirror `_download.py` lines 53–61] | The fd is closed on **every** error path between `mkstemp` and the staging `try/finally`. The temp file is unlinked on every exit path. | Route test: monkey-patch `hashlib.new` to raise after `fdopen` but before staging; assert the OS-level fd is closed (e.g. via a sentinel `fd` wrapper that tracks close) and the temp path no longer exists. |
| B2 | Staging-loop close failure: `tmp.close()` raises after the body is staged. [mirror `_download.py` line 256] | The close failure is suppressed — it must not replace an in-flight `FileExchangeTransferError` or HTTP error response with a raw `OSError`. | Sender test: force `tmp.close` to raise on a happy-path send; assert the function returns normally and the temp is unlinked. |
| B3 | Unlink failure: `os.unlink(tmp_path)` raises during cleanup. [mirror `_download.py` line 302] | Suppressed — cleanup failure must not mask the in-flight outcome. | Route + sender test: monkey-patch `os.unlink` to raise; happy path still returns 204 / completes normally. |
| B4 | The sink reads but does not close the stream (hook contract). [spec / `_hooks.py`] | The route closes the fd handed to the sink itself, in a `finally`, with `OSError` suppression. | Route test: a sink that just reads → bytes deposit correctly, fd is closed afterwards. |
| B5 | Sender's source stream close: `source.open_artifact` returns a stream that the sender owns; the sender must close it on every path. [mirror download fetcher] | The sender closes the source stream on success and failure paths alike (suppressed). | Sender test: a source whose stream tracks close; assert close called exactly once on happy + on guard-refusal + on non-2xx + on sink raise. |
| B6 | Temp file open after staging for the sink: the temp is opened sync (`open(tmp_path, "rb")`); the open itself can fail (`OSError`). [mirror `_download.py` lines 281–289] | An open failure maps to `500` (route) / `transfer-failed` (sender), with the temp still unlinked. | Route test: monkey-patch the open to raise; 500 returned; temp gone. |

## C. Concurrency modes

| # | Trigger | Required behaviour | Test |
|---|---|---|---|
| C1 | Two PUTs against the same URL arrive concurrently, the first one already past the `lookup` but not past `consume`. [spec §10.3 at-most-once "successful upload" semantics] | At most one of them consumes the token; both may successfully invoke `store_artifact` (the duplicate-sink-call tolerance is on the sink contract — observation 4539). The route does not need an in-memory lock; the atomic `consume` provides at-most-one. | Route test: two concurrent PUTs (both with valid bodies) → one returns 204, the other returns either 204 or 404; the **`consume`** boolean ledger shows exactly one `True`. The sink may be called 1 or 2 times. |
| C2 | A PUT arrives concurrently with a `revoke`. [spec §15] | Revoke that lands before the route's `lookup` makes the PUT a `404`. Revoke that lands after `lookup` but before `consume` does not retroactively undo a store that has already been written to the sink — from the wire, the upload succeeded; revoke racing the consume just means the consume returns `False`, the response is `204`. The bytes are in the sink either way; this is the spec's at-most-once semantics, not at-most-once-and-rollback. | Route test: two-step sequence: (a) revoke → PUT → 404; (b) PUT held mid-body via a controllable sink, revoke fires while the sink awaits → sink completes, consume returns False, response is 204. |
| C3 | A guard refusal arises from `guarded_stream` mid-send. [spec; mirror `_outbound.py`] | The sender propagates `FileExchangeTransferError(not-accessible, transport="upload")`; the temp is unlinked. | Sender test: `guarded_stream` raises `FileExchangeTransferError(NOT_ACCESSIBLE)` synchronously; assert it propagates verbatim (same code, transport label correct) and the temp is gone. |

## D. Failure-path modes (non-2xx, exception classification)

| # | Trigger | Required behaviour | Test |
|---|---|---|---|
| D1 | The sender's send returns a 4xx/5xx response. [spec; observation 4480] | `FileExchangeTransferError(transfer-failed, transport="upload")`. | Sender test: mock guarded_stream yielding a 500 response; assert TRANSFER_FAILED is raised. |
| D2 | The sender's `source.open_artifact` raises. [spec error-handling] | The exception propagates (the offering-side hook contract permits any exception); no temp file is created. | Sender test: source that raises → propagates; assert no leftover `fx-upload-*` temp files. |
| D3 | The sender's `tempfile.mkstemp` raises (disk full). [mirror `_download.py` lines 47–52] | `FileExchangeTransferError(transfer-failed)`. | Sender test: monkey-patch `mkstemp` to raise `OSError` → TRANSFER_FAILED. |
| D4 | The route's `Content-Digest` header is **present but unparseable** (e.g. malformed RFC 9530 structured-field). [spec §10.3, bot-165 false-alarm clarification] | `400` (`digest-mismatch`) — never a silent skip; matches `_digest_verifier`'s `unverifiable` semantics. | Route test: PUT with `Content-Digest: garbage` → 400; sink not called. |
| D5 | The route's `Content-Digest` declares an **unsupported algo label** (e.g. `md5`). [spec §10.3] | `400` (`digest-mismatch`). | Route test: PUT with `Content-Digest: md5=:...:` → 400. |
| D6 | The sink raises a `FileExchangeTransferError`. [parity with download fetcher] | Route maps to `500`; the original exception is **not** echoed in the response body (it may carry server-internal detail). Logged locally. | Route test: sink raises `FileExchangeTransferError(transfer-failed)` → 500 with no body; log captured. |
| D7 | The sink raises a generic `Exception`. [parity with download fetcher] | Route maps to `500`; logged; no body. | Route test: sink raises `RuntimeError("oops")` → 500. |
| D8 | The route's body read raises `OSError` mid-stream (disk full on the temp file). [mirror `_download.py` lines 213–222] | `500` body-free, temp unlinked, sink not called, token not consumed. | Route test: monkey-patch `_write_chunk` to raise on the 2nd chunk → 500; temp gone. |
| D9 | The sender's body iteration raises an `OSError` reading the staged temp. [parity] | `FileExchangeTransferError(transfer-failed)`, temp unlinked. | Sender test: force the body iterator to raise OSError mid-send → TRANSFER_FAILED. |

## E. Reentrancy & boundary modes

| # | Trigger | Required behaviour | Test |
|---|---|---|---|
| E1 | `upload_receiver_mint` called twice for the same `artifact_id`. [spec] | Two distinct tokens minted, both valid until consumed/expired (the token store is the source of truth; mint is stateless beyond it). | Mint test: two mints → two different URLs; both look up to records with the same `artifact_id`. |
| E2 | `register_file_exchange_routes` called with `source=None, sink=None`. [spec — the no-op shape] | Either: (a) raises `ValueError` (nothing to mount is a misuse), or (b) is a no-op. Pick **(a)** so misconfigurations are visible at registration time; document the choice. | Registrar test: `register_file_exchange_routes(mcp, token_store=..., source=None, sink=None)` raises `ValueError`. |
| E3 | A registrar precondition violation must raise `ValueError`, not `TypeError`. [bot-165 finding 3] | The spec calls for `ValueError` on `sink-without-config`; this is a deliberate narrowing from "Python's native `TypeError: missing kw"`. Document the rationale; the only callers in this repo are the umbrella in #148 + tests. | Registrar test: `sink-without-config` raises `ValueError` (not `TypeError`). |
| E4 | Public re-export surface. [spec] | `fastmcp_pvl_core.file_exchange.upload_receiver_mint`, `.upload_sender_consume`, `.register_file_exchange_routes` are importable; `__all__` updated alphabetically. | Test: importable, in `__all__`. |

## F. Wire / protocol modes

| # | Trigger | Required behaviour | Test |
|---|---|---|---|
| F1 | RFC 9530 `Content-Digest` parsing — well-formed `sha-256=:<b64>:`. [spec] | Parse succeeds, returns `("sha-256", <raw bytes>)`. Whitespace tolerated where the structured-fields grammar tolerates it. | Parser unit test, table-driven; at minimum the RFC 9530 §3 example `sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:` plus malformed variants (missing colons, unknown algo, double-`=`, empty value) each yielding the `(unverifiable=True)` branch. |
| F2 | RFC 9530 `Content-Digest` format — sender produces `sha-256=:<b64>:` from a sha-256 digest of staged bytes. [spec] | Header is well-formed and re-parseable. | Sender test: round-trip parse of the produced header. |
| F3 | RFC 7231 media-range matching for `acceptMimeTypes`. [spec] | `image/*` matches `image/png`; `*/*` matches anything; `application/json` does not match `application/json; charset=utf-8` parameters-only-ignored; case is media-type-insensitive. | Matcher unit test, table-driven. |
| F4 | `acceptMimeTypes=None` (no constraint). [spec] | All content types accepted. | Route test: PUT with any Content-Type when `expected` is `None` or `acceptMimeTypes` is unset → 204. |
| F5 | Ambient `Authorization`/`Cookie` headers on a PUT. [spec — token is the only authorization] | Ignored; outcome is the same as without them. | Route test: PUT with `Authorization: Bearer foo` succeeds normally; PUT with an invalid token URL + `Authorization` still 404. |
| F6 | `upload_sender_consume` always sends `Content-Digest`. [spec] | The PUT request carries `Content-Digest: sha-256=:<b64>:`, `Content-Length: <size>`, and `Content-Type: <metadata.mimeType>` when known. | Sender test: assert headers on the captured request to `guarded_stream`. |

## G. Spec parity modes (#145 ↔ #146 contract symmetry)

| # | Required behaviour | Test |
|---|---|---|
| G1 | The `_staging.py` extraction does not change observable download behaviour — every existing download test still passes after the extraction. | Run `tests/_file_exchange/test_download*.py` against the post-refactor tree as part of the matrix's first commit. |
| G2 | The error-code envelope (`FileExchangeTransferError(code, transport="upload", ...)`) uses the same code constants as the download side. No "upload-only" code is introduced. | Static check: grep the `TransferErrorCode` enum usage; assert the upload code paths use only the codes the download side already uses. |

## Implementation work order

The matrix's row count drives the work order:

1. `_staging.py` extraction (G1 + B1 + B2 + B3 mirror parity). Tests
   first: write the staging-helper contract tests, **then** move the
   code; download tests must stay green.
2. `upload_receiver_mint` (E1 + E4). Smallest, no I/O.
3. RFC 9530 parser/formatter (F1, F2, D4, D5). Pure functions.
4. RFC 7231 media-range matcher (F3, F4). Pure function.
5. The upload route (A1–A6, B1, B4, B6, C1–C2, D4–D8, F4, F5). The
   bulk of the work.
6. `register_file_exchange_routes` shape change (A7, A8, E2, E3).
7. `upload_sender_consume` (D1–D3, D9, B2, B3, B5, C3, F6).
8. End-to-end test (push two-server flow).

After each step, decide: is this an independently mergeable PR with a
small review surface, or does the next step belong with it? The
abandon-rule is the guide — **do not pre-commit to "one PR" or
"five PRs"**; let the work shape its packaging.

## Out of scope for this matrix

- The umbrella `register_file_exchange_*` helpers, shared token-store
  construction, and Tasks integration — those are #148.
- Any change to the wire spec or to `docs/specs/`.
- Any change to the merged download data plane's observable
  behaviour (only its module layout moves).

## References

- `2026-05-24-file-exchange-146-upload-data-plane-design.md` — the
  behavioural contract this matrix maps onto.
- `2026-05-24-file-exchange-145-download-data-plane-design.md` and
  `src/fastmcp_pvl_core/_file_exchange/_download.py` (on `main`) —
  the mirror.
- PR #163, #164, #165 bot findings — the empirical source for the
  rows in B1, A1, A2, A7, D4, E3.
- `CLAUDE.md` "Test-driven discipline" — the discipline this matrix
  operationalises.
