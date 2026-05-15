# http_upload Receiver Primitive — v0.3.0 Re-implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-implement the receiver/`sink`-side `http_upload` primitive — `register_file_exchange_upload`, the `create_upload_link` tool, the POST route, and `UploadRecord` — to be 100% conformant with the v0.3.0 file-exchange spec.

**Architecture:** The `http_upload` receiver is one tightly-coupled unit spanning three files: `_token_store.py` (`UploadRecord` / `UploadStore`), `_file_exchange_runtime.py` (the POST route), and `file_exchange.py` (the helper + tool + `UploadHandle`). The tool's parameter/return shape, the route's status codes, and the record's fields all change together — the call graph (`create_upload_link` → `UploadHandle.create_link` → `UploadStore.reserve`; route → `UploadStore.consume`) does not survive a partial migration with `mypy` green. Task 1 therefore rebuilds all three files plus rewrites the upload tests in one atomic, test-first commit. Task 2 is the full-suite sweep + quality gate.

**Tech Stack:** Python 3.10–3.13, `uv`, `pytest`, `ruff`, `mypy`. Repo `/mnt/code/fastmcp-pvl-core`, branch `impl/upload-primitive-v030-issue-74`.

**Design doc:** `docs/superpowers/specs/2026-05-15-upload-primitive-v030-design.md` (issue #74).

---

## File Structure

- `src/fastmcp_pvl_core/_token_store.py` — `UploadRecord` gains `origin_id`/`destination`/`content_type`, drops `target_id`/`extra`. `UploadStore.reserve` parameters change; `consume_or_status` is deleted (`consume` stays).
- `src/fastmcp_pvl_core/_file_exchange_runtime.py` — `_upload_handler` drops the `410` branch (uses `consume`, not `consume_or_status`).
- `src/fastmcp_pvl_core/file_exchange.py` — `PreLinkValidator` signature; `UploadHandle` / `create_link`; `register_file_exchange_upload` kwarg surface; the `create_upload_link` tool body and return; a new `_upload_transfer_failed` envelope helper.
- `tests/test_uploads.py`, `tests/test_file_exchange_upload_facade.py`, `tests/test_file_exchange_upload_route.py` — rewritten to the v0.3.0 contract.

No new files. The token-store *mechanics* (UUID4 token, atomic consume, lazy TTL purge, the `_bounded_chunks` streaming generator, sync/async receiver dispatch, `413`/`415`/`5xx` handling) are spec-conformant and preserved.

---

## The v0.3.0 target contract (reference for every task)

**`create_upload_link` tool parameters:** `origin_id: str` (required), `destination: str | None = None`, `content_type: str | None = None`, `ttl_seconds: int | None = None`, `max_bytes: int | None = None`.

**`create_upload_link` return (success):** exactly `{"url": str, "ttl_seconds": int, "max_bytes": int}` — effective post-clamp values.

**`create_upload_link` return (in-band rejection):** the `transfer_failed` envelope `{"error": "transfer_failed", "method": "http_upload", "receiver_server": <namespace>, "origin_id": <origin_id>, "message": <str>}`.

**POST route status classes:** `2xx` success; `404` for unknown/expired/consumed (indistinguishable, empty body, **no `410`**); `413` oversize; `415` content-type mismatch; other `4xx` domain rejection with `transfer_failed` body; `5xx` server error, generic body.

**`UploadRecord` fields:** `origin_id: str`, `destination: str | None`, `content_type: str | None`, `max_bytes: int`, `expires_at: float`.

**Hook signatures:** `PreLinkValidator = Callable[[str, str | None], None | Awaitable[None]]` (`origin_id`, `destination`); `BufferedReceiver` / `StreamReceiver` unchanged in shape (`(UploadRecord, bytes)` / `(UploadRecord, AsyncIterator[bytes])`).

---

## Task 1: Rebuild the http_upload receiver primitive

**Files:**
- Modify: `src/fastmcp_pvl_core/_token_store.py` (`UploadRecord` ~407-441; `UploadStore.reserve` ~469-506; `consume`/`consume_or_status` ~508-545)
- Modify: `src/fastmcp_pvl_core/_file_exchange_runtime.py` (`_upload_handler` ~706-723)
- Modify: `src/fastmcp_pvl_core/file_exchange.py` (`PreLinkValidator` ~1376-1379; `UploadHandle`/`create_link` ~1382-1488; `_disabled_upload_handle` ~1490-1512; `register_file_exchange_upload` ~1515-1843; add `_upload_transfer_failed` near `_transfer_failed` ~922)
- Test: `tests/test_uploads.py`, `tests/test_file_exchange_upload_facade.py`, `tests/test_file_exchange_upload_route.py`

Steps 3–11 leave the tree temporarily `mypy`-inconsistent; the gate runs at Step 12. Do **not** run `pytest`/`mypy` between Steps 3 and 11; do not commit before Step 12 passes.

- [ ] **Step 1: Rewrite the three upload test files to the v0.3.0 contract**

Read all three files. They test the pre-v0.3.0 contract. Rewrite each to the target contract above. Apply these mechanical transformations everywhere they appear:

- `target_id=` / `target_id` → `origin_id=` / `origin_id` (the tool param, `UploadRecord` field, `UploadStore.reserve` arg, `create_link` arg).
- Tool return `"upload_url"` → `"url"`; `"expires_in_seconds"` → `"ttl_seconds"`; the return no longer contains `target_id`; it now also contains `"max_bytes"`.
- Any `extra=` argument to the tool / `create_link` / `reserve`, and any `UploadRecord.extra` / `record.extra` access → removed.
- `UploadRecord(target_id=..., max_bytes=..., extra=..., expires_at=...)` constructions → `UploadRecord(origin_id=..., destination=..., content_type=..., max_bytes=..., expires_at=...)`.
- `store.consume_or_status(token)` calls → `store.consume(token)` (now returns `UploadRecord | None`, no status tuple).
- Any test asserting a `410` response from the upload route → assert `404` instead.
- `register_file_exchange_upload(..., upload_tool_name=X)` / `tool_tags=X` / `transport=X` / `max_bytes_default=X` / `ttl_default=X` / `ttl_max=X` → remove the kwarg; for the operator-config ones, set the corresponding env var (`{PREFIX}_UPLOAD_MAX_BYTES`, `{PREFIX}_UPLOAD_TTL`, `{PREFIX}_UPLOAD_TTL_MAX`, transport env) via `monkeypatch.setenv` if the test needs that value.

Then ensure these v0.3.0 behaviours each have an explicit test (add them where missing — full code, not a sketch):

```python
# In test_file_exchange_upload_route.py — the anti-leak guard.
@pytest.mark.asyncio
async def test_upload_route_expired_token_returns_404_not_410(...):
    """An expired token is indistinguishable from unknown/consumed: 404, never 410."""
    # reserve a token with a tiny TTL, let it expire, POST to it,
    # assert response.status_code == 404 (NOT 410).

@pytest.mark.asyncio
async def test_upload_route_unknown_and_consumed_and_expired_all_404(...):
    """All three token-unusable conditions return an identical 404."""
    # POST to a never-minted token -> 404
    # POST twice to the same token -> second POST 404
    # POST to an expired token -> 404
    # assert all three responses are byte-identical (same status, same body).
```

```python
# In test_file_exchange_upload_facade.py — the in-band transfer_failed envelope.
@pytest.mark.asyncio
async def test_create_upload_link_rejection_returns_transfer_failed_envelope(...):
    """A pre_link_validator ValueError makes the tool return the transfer_failed envelope."""
    # register with a pre_link_validator that raises ValueError("bad destination")
    # call create_upload_link(origin_id="x", destination="../etc")
    # assert the result == {"error": "transfer_failed", "method": "http_upload",
    #                       "receiver_server": <namespace>, "origin_id": "x",
    #                       "message": "bad destination"}

@pytest.mark.asyncio
async def test_create_upload_link_content_type_mismatch_returns_transfer_failed(...):
    """A content_type hint outside the accepts list is rejected in-band."""
    # register with accepts=("text/markdown",)
    # call create_upload_link(origin_id="x", content_type="image/png")
    # assert result["error"] == "transfer_failed" and result["method"] == "http_upload"
    # assert result["receiver_server"] == <namespace> and result["origin_id"] == "x"

@pytest.mark.asyncio
async def test_create_upload_link_success_returns_url_ttl_maxbytes(...):
    """Success return has exactly url / ttl_seconds / max_bytes."""
    # call create_upload_link(origin_id="x")
    # assert set(result) == {"url", "ttl_seconds", "max_bytes"}
    # assert result["max_bytes"] == <effective ceiling>

@pytest.mark.asyncio
async def test_create_upload_link_clamps_ttl_and_max_bytes_to_ceilings(...):
    """ttl_seconds and max_bytes in the return are the clamped effective values."""
    # set {PREFIX}_UPLOAD_TTL_MAX and {PREFIX}_UPLOAD_MAX_BYTES low via env
    # call create_upload_link(origin_id="x", ttl_seconds=99999, max_bytes=99999999)
    # assert result["ttl_seconds"] == ceiling and result["max_bytes"] == ceiling
```

```python
# In test_uploads.py — UploadRecord carries the WHAT/WHERE split to the receiver.
def test_upload_record_carries_origin_id_destination_content_type():
    rec = UploadRecord(origin_id="a", destination="d/x.md",
                       content_type="text/markdown", max_bytes=10, expires_at=time.time()+60)
    assert rec.origin_id == "a"
    assert rec.destination == "d/x.md"
    assert rec.content_type == "text/markdown"
    assert not hasattr(rec, "target_id")
    assert not hasattr(rec, "extra")
```

Keep all still-relevant existing coverage (token entropy, atomic one-time consume, `413` pre-declared + mid-stream, `415`, sync/async receiver dispatch, the disabled-handle paths) — only the contract-specific assertions change.

- [ ] **Step 2: Run the upload tests to verify they fail**

Run: `uv run pytest tests/test_uploads.py tests/test_file_exchange_upload_facade.py tests/test_file_exchange_upload_route.py -q`
Expected: FAIL — the tests reference the new contract (`origin_id`, `consume`, the envelope) that the source does not yet implement.

- [ ] **Step 3: Restructure `UploadRecord`**

In `src/fastmcp_pvl_core/_token_store.py`, replace the `UploadRecord` dataclass (fields and docstring) with:

```python
@dataclass(frozen=True)
class UploadRecord:
    """A reservation slot for an in-flight upload (intake direction).

    An ``UploadRecord`` does not carry bytes — bytes arrive over the wire
    when a client ``POST``s to ``/<ns>/uploads/{token}``. The record
    carries the metadata the receiver needs to commit the bytes, modelling
    the v0.3.0 spec's WHAT/WHERE split, plus the runtime guards.

    Attributes:
        origin_id: The sender's opaque stable handle for the bytes (the
            *what*). Validated against the spec's segment grammar — see
            ``docs/specs/file-exchange.md`` §"Security and Path
            Resolution".
        destination: The sender's destination instruction (the *where*),
            or ``None`` if the sender gave none. The receiver validates
            and interprets it per its own domain rules.
        content_type: The sender's hint of the ``Content-Type`` the POST
            will declare, or ``None``.
        max_bytes: Hard size cap for the POST body, enforced at the HTTP
            route before dispatch.
        expires_at: Unix timestamp after which the reservation is
            invalid; consumed reservations are removed atomically by
            ``UploadStore.consume``.
    """

    origin_id: str
    destination: str | None
    content_type: str | None
    max_bytes: int
    expires_at: float
```

- [ ] **Step 4: Update `UploadStore.reserve`**

In `_token_store.py`, replace the `reserve` method with:

```python
    def reserve(
        self,
        *,
        origin_id: str,
        max_bytes: int,
        ttl_seconds: float | None = None,
        destination: str | None = None,
        content_type: str | None = None,
    ) -> str:
        """Mint a token reserving a one-shot upload slot.

        Validation note: ``max_bytes`` / ``origin_id`` / ``destination``
        are stored verbatim; no validation runs at this layer. Higher-level
        callers (``register_file_exchange_upload`` and its
        ``pre_link_validator``) validate before reserving.
        """
        self._purge_expired()
        token = self._mint_token()
        ttl = self._ttl if ttl_seconds is None else float(ttl_seconds)
        self._records[token] = UploadRecord(
            origin_id=origin_id,
            destination=destination,
            content_type=content_type,
            max_bytes=int(max_bytes),
            expires_at=time.time() + ttl,
        )
        logger.debug(
            "upload_reserve token_prefix=%s origin_id=%s max_bytes=%d ttl=%.1fs",
            token[:8],
            origin_id,
            max_bytes,
            ttl,
        )
        return token
```

- [ ] **Step 5: Delete `consume_or_status`, keep `consume`**

In `_token_store.py`, delete the entire `consume_or_status` method. Update the `consume` method's docstring to drop the reference to `consume_or_status`:

```python
    def consume(self, token: str) -> UploadRecord | None:
        """Atomic consume; returns the record, or ``None`` for an
        unusable token — unknown, expired, or already consumed are
        indistinguishable, per the spec's anti-leak rule for the
        ``http_upload`` POST route.
        """
        return self._atomic_consume(token)
```

If `Literal` is now unused in `_token_store.py`, remove it from the imports. The `_peek_for_tests`, `has_base_url`, `build_url` methods are unchanged.

- [ ] **Step 6: Update the POST route — `consume`, drop the `410` branch**

In `src/fastmcp_pvl_core/_file_exchange_runtime.py`, in `_upload_handler`, replace the token-lookup block:

```python
        token = request.path_params.get("token", "")
        record, status = store.consume_or_status(token)
        if status == "expired":
            logger.info("upload_handler_expired token_prefix=%s", token[:8])
            return Response(content="Gone", status_code=410)
        if record is None:
            logger.debug("upload_handler_miss token_prefix=%s", (token or "")[:8])
            return Response(content="Not Found", status_code=404)
```

with:

```python
        token = request.path_params.get("token", "")
        record = store.consume(token)
        if record is None:
            # 404 covers all three token-unusable conditions — unknown,
            # expired, already-consumed — indistinguishably (spec
            # §"http_upload / POST contract": anti-leak, no 410).
            logger.debug("upload_handler_miss token_prefix=%s", (token or "")[:8])
            return Response(content="Not Found", status_code=404)
```

Then update `register_upload_route`'s docstring: remove the `410 Gone` bullet, and change the `404` bullet to "if the token is unknown, expired, or already consumed (indistinguishable, to avoid leaking token state to a probing caller)".

- [ ] **Step 7: Update the `PreLinkValidator` type**

In `src/fastmcp_pvl_core/file_exchange.py`, replace the `PreLinkValidator` alias:

```python
PreLinkValidator = Callable[
    [str, "str | None"],
    "None | Awaitable[None]",
]
```

The two positional parameters are now `origin_id: str` and `destination: str | None`.

- [ ] **Step 8: Add the `_upload_transfer_failed` envelope helper**

In `file_exchange.py`, immediately after the existing `_transfer_failed` function (~line 940), add:

```python
def _upload_transfer_failed(
    *,
    receiver_server: str,
    origin_id: str,
    message: str,
) -> dict[str, Any]:
    """Build an ``http_upload`` in-band ``transfer_failed`` envelope.

    The upload direction identifies the responding server as
    ``receiver_server`` (not ``origin_server``): no file has been
    produced, so the server is the receiver of an attempted upload.
    See spec §"Transfer Methods / http_upload".
    """
    return {
        "error": "transfer_failed",
        "method": "http_upload",
        "receiver_server": receiver_server,
        "origin_id": origin_id,
        "message": message,
    }
```

- [ ] **Step 9: Update `UploadHandle` and `create_link`**

In `file_exchange.py`, `UploadHandle` keeps its fields (`namespace`, `tool_name`, `enabled`, `upload_store`, `ttl_default`, `ttl_max`, `max_bytes_default`) and `__post_init__` unchanged — `ttl_default`/`ttl_max`/`max_bytes_default` remain handle fields (they are resolved values, populated from env/defaults). Replace only the `create_link` method:

```python
    def create_link(
        self,
        *,
        origin_id: str,
        ttl_seconds: float | None = None,
        max_bytes: int | None = None,
        destination: str | None = None,
        content_type: str | None = None,
    ) -> tuple[str, float, int]:
        """Mint an upload reservation directly. Escape valve for advanced wraps.

        Returns:
            ``(upload_url, effective_ttl_seconds, effective_max_bytes)``.

        Raises:
            RuntimeError: if upload is not enabled (transport is stdio,
                ``{PREFIX}_BASE_URL`` unset, or upload disabled by env).
        """
        if self.upload_store is None:
            raise RuntimeError(
                "upload not enabled (transport=stdio, missing BASE_URL, or disabled)"
            )
        if ttl_seconds is None or ttl_seconds <= 0:
            ttl = float(self.ttl_default)
        else:
            ttl = float(ttl_seconds)
        ttl = min(ttl, self.ttl_max)
        if max_bytes is None or max_bytes <= 0:
            cap = int(self.max_bytes_default)
        else:
            cap = min(int(max_bytes), int(self.max_bytes_default))
        token = self.upload_store.reserve(
            origin_id=origin_id,
            max_bytes=cap,
            ttl_seconds=ttl,
            destination=destination,
            content_type=content_type,
        )
        return self.upload_store.build_url(token), ttl, cap
```

Note two deliberate changes beyond the rename: `create_link` now returns the effective `max_bytes` as a third tuple element (the tool MUST return it), and a caller-supplied `max_bytes` is clamped *down* to `max_bytes_default` (the operator ceiling) rather than only floored — so the returned `max_bytes` is a true effective ceiling.

- [ ] **Step 10: Update `_disabled_upload_handle` and the `register_file_exchange_upload` signature**

In `file_exchange.py`, `_disabled_upload_handle` — drop the `upload_tool_name` parameter; the handle's `tool_name` is fixed:

```python
def _disabled_upload_handle(
    *,
    namespace: str,
    ttl_default: float,
    ttl_max: float,
    max_bytes_default: int,
) -> UploadHandle:
    """Return a no-op UploadHandle for the upload-disabled path."""
    return UploadHandle(
        namespace=namespace,
        tool_name=_DEFAULT_UPLOAD_TOOL,
        enabled=False,
        upload_store=None,
        ttl_default=ttl_default,
        ttl_max=ttl_max,
        max_bytes_default=max_bytes_default,
    )
```

Replace the `register_file_exchange_upload` signature:

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

Inside the body: `transport` is no longer a parameter — the `_resolve_transport(env_prefix, transport)` call becomes `_resolve_transport(env_prefix)` (its second parameter already defaults to `"auto"` — verify and rely on that default; if it has no default, pass `"auto"` explicitly). `max_bytes_default` / `ttl_default` / `ttl_max` are no longer parameters — initialise them as locals from the module defaults before the env-override block:

```python
    max_bytes_default = _DEFAULT_UPLOAD_MAX_BYTES
    ttl_default = _DEFAULT_UPLOAD_TTL_SECONDS
    ttl_max = _DEFAULT_UPLOAD_TTL_MAX_SECONDS
```

The existing env-override block (`{PREFIX}_UPLOAD_MAX_BYTES` / `_TTL` / `_TTL_MAX`, with `ConfigurationError` on malformed values) stays as-is and now writes those locals. The two `_disabled_upload_handle(...)` call sites drop the `upload_tool_name=` argument. The `register_upload_route(...)` call stays as-is. `UploadHandle(...)` construction: `tool_name=_DEFAULT_UPLOAD_TOOL` (was `upload_tool_name`). The `builder.set_http_upload_sink(tool_name=_DEFAULT_UPLOAD_TOOL, ...)` call uses the fixed name. Update the helper's docstring `Args:` block — remove `transport`, `upload_tool_name`, `tool_tags`, `max_bytes_default`, `ttl_default`, `ttl_max`; keep `mcp`/`namespace`/`env_prefix`/`receiver`/`stream_receiver`/`pre_link_validator`/`accepts`; rewrite `pre_link_validator`'s entry for the `(origin_id, destination)` signature. Re-ground the stale "Amendment 11" comment in the helper body (the `accepts`-verbatim comment) against spec §"Transfer Methods / http_upload" instead.

- [ ] **Step 11: Rebuild the `create_upload_link` tool**

In `file_exchange.py`, replace the `create_upload_link` tool definition (the `@mcp.tool(...)` decorator and the whole `async def create_upload_link` body) with — note the tool name and tags are now fixed, not kwargs:

```python
    @mcp.tool(name=_DEFAULT_UPLOAD_TOOL, tags={"write"})
    async def create_upload_link(
        origin_id: str,
        destination: str | None = None,
        content_type: str | None = None,
        ttl_seconds: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        r"""Mint a one-time HTTPS POST URL for an inbound upload.

        Args:
            origin_id: The sender's opaque stable handle for the bytes
                (the *what*). Validated against the spec's segment rules
                (§"Security and Path Resolution"): no ``/``, ``\``, ``.``,
                ``..``, control bytes, leading/trailing whitespace.
            destination: Optional destination instruction (the *where*).
                Only null bytes, control characters, and leading/trailing
                whitespace are rejected at the spec level; the receiver
                validates the rest per its own domain rules via
                ``pre_link_validator``.
            content_type: Optional hint of the ``Content-Type`` the POST
                will declare; pre-filtered against the receiver's
                ``accepts`` list.
            ttl_seconds: Optional requested lifetime in seconds; clamped
                to the server's TTL ceiling.
            max_bytes: Optional requested body cap; clamped to the
                server's ``max_bytes`` ceiling.

        Returns:
            On success, ``{url, ttl_seconds, max_bytes}`` — the effective
            (post-clamp) values. On in-band rejection, a ``transfer_failed``
            envelope.
        """
        # origin_id: strict spec segment grammar (the WHAT identifier).
        ExchangeURI.validate_segment(origin_id, role="json_param")
        # destination: relaxed validation (the WHERE) — reject only null
        # bytes, control chars, and leading/trailing whitespace; the
        # receiver validates the rest.
        if destination is not None:
            if destination != destination.strip():
                return _upload_transfer_failed(
                    receiver_server=namespace,
                    origin_id=origin_id,
                    message="destination must not have leading or trailing whitespace",
                )
            if any(ord(c) < 0x20 for c in destination):
                return _upload_transfer_failed(
                    receiver_server=namespace,
                    origin_id=origin_id,
                    message="destination must not contain control characters",
                )
        # content_type hint: pre-filter against the receiver's accepts
        # list so a mismatched hint is rejected in-band, before a wasted
        # POST round-trip (the route still enforces 415 on the actual
        # POST Content-Type header). _accepts_match is imported from
        # _file_exchange_runtime — add it to that module's imports in
        # file_exchange.py.
        if content_type is not None and not _accepts_match(content_type, accepts):
            return _upload_transfer_failed(
                receiver_server=namespace,
                origin_id=origin_id,
                message=f"content_type {content_type!r} is not accepted by this receiver",
            )
        if pre_link_validator is not None:
            try:
                if inspect.iscoroutinefunction(pre_link_validator):
                    await pre_link_validator(origin_id, destination)
                else:
                    validator_result = await asyncio.to_thread(
                        pre_link_validator, origin_id, destination
                    )
                    if inspect.isawaitable(validator_result):
                        await validator_result
            except ValueError as exc:
                # Caller-facing rejection — return the spec transfer_failed
                # envelope rather than letting it surface as a tool error.
                return _upload_transfer_failed(
                    receiver_server=namespace,
                    origin_id=origin_id,
                    message=str(exc),
                )
            except Exception:
                logger.exception(
                    "pre_link_validator raised non-ValueError "
                    "(origin_id=%r) — server-side bug, not a client "
                    "validation failure",
                    origin_id,
                )
                raise
        url, eff_ttl, eff_max_bytes = handle.create_link(
            origin_id=origin_id,
            ttl_seconds=ttl_seconds,
            max_bytes=max_bytes,
            destination=destination,
            content_type=content_type,
        )
        return {
            "url": url,
            "ttl_seconds": int(eff_ttl),
            "max_bytes": int(eff_max_bytes),
        }
```

- [ ] **Step 12: Run the upload tests to verify they pass**

Run: `uv run pytest tests/test_uploads.py tests/test_file_exchange_upload_facade.py tests/test_file_exchange_upload_route.py -q`
Expected: PASS. If a test fails because it still references a removed symbol, fix the test to the v0.3.0 contract (Step 1's transformation rules). If a test reveals a real implementation gap, fix the source.

- [ ] **Step 13: Commit**

```bash
git add src/fastmcp_pvl_core/_token_store.py src/fastmcp_pvl_core/_file_exchange_runtime.py src/fastmcp_pvl_core/file_exchange.py tests/test_uploads.py tests/test_file_exchange_upload_facade.py tests/test_file_exchange_upload_route.py
git commit -m "$(cat <<'EOF'
feat(file-exchange): re-implement http_upload receiver against v0.3.0 spec (refs #74)

Clean-slate rebuild of the receiver/sink-side http_upload primitive,
superseding the A11-era implementation:

- create_upload_link: origin_id (required) + optional destination /
  content_type / ttl_seconds / max_bytes; returns {url, ttl_seconds,
  max_bytes}; in-band rejection returns the transfer_failed envelope
  (with receiver_server, not origin_server).
- POST route: 410-on-expired removed; unknown / expired / consumed
  tokens all return an indistinguishable 404 (anti-leak).
- UploadRecord models the WHAT/WHERE split (origin_id / destination /
  content_type); the non-spec extra field is gone.
- UploadStore.consume_or_status removed; consume returns
  UploadRecord | None.
- register_file_exchange_upload kwarg surface cut to six domain
  hooks; upload_tool_name / tool_tags / transport / max_bytes_default
  / ttl_default / ttl_max removed (pvl-core-fixed shape or env vars).
- pre_link_validator signature is now (origin_id, destination).

Refs #74.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Full-suite sweep and quality gate

**Files:** potentially any test outside the three upload files that references the removed upload surface.

- [ ] **Step 1: Sync dependencies and run the full suite**

```bash
uv sync --all-extras
uv run pytest -q
```

Expected: green except possibly tests outside the three upload files that referenced `target_id` / `upload_url` / `expires_in_seconds` / `consume_or_status` / `UploadRecord.extra` / the removed `register_file_exchange_upload` kwargs / a `410` upload response.

- [ ] **Step 2: Fix any straggler test**

For each failure, apply Task 1 Step 1's transformation rules (the same v0.3.0-contract renames). A test that only existed to exercise removed behaviour (`extra` passthrough, the `410` branch, an `upload_tool_name` override) is rewritten to assert the new state, or deleted if the behaviour itself is gone. Re-run `uv run pytest -q` until green.

- [ ] **Step 3: Quality gate**

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

All three clean. Fix each finding minimally — e.g. an import left unused after `consume_or_status` / the removed kwargs were deleted (`Literal` in `_token_store.py`, `Literal`/`_DEFAULT_*` references in `file_exchange.py`).

- [ ] **Step 4: Commit**

If Steps 2–3 changed any file:

```bash
git add -A
git commit -m "$(cat <<'EOF'
test(file-exchange): realign remaining tests to v0.3.0 upload contract (refs #74)

<one or two lines naming the files swept>

Refs #74.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If Steps 2–3 changed nothing, skip the commit — report only.

---

## Summary

Two tasks. Task 1 is the atomic receiver-primitive rebuild across `_token_store.py`, `_file_exchange_runtime.py`, `file_exchange.py`, plus the three upload test files, test-first. Task 2 sweeps the rest of the suite and runs the quality gate.

## What changes

- `create_upload_link`: `origin_id`/`destination`/`content_type`/`ttl_seconds`/`max_bytes` params; `{url, ttl_seconds, max_bytes}` return; `transfer_failed` envelope on in-band rejection.
- POST route: `410` removed; `404` covers unknown/expired/consumed indistinguishably.
- `UploadRecord`: WHAT/WHERE split; no `extra`.
- `UploadStore`: `consume_or_status` removed.
- `register_file_exchange_upload`: six domain-hook kwargs only.
- `PreLinkValidator`: `(origin_id, destination)`.

## What does NOT change

- Token-store mechanics (UUID4 entropy, atomic one-time consume, lazy TTL purge).
- The route's `_bounded_chunks` streaming generator, sync/async receiver dispatch, and the `413`/`415`/`5xx` handling.
- The capability builder (#86) and the download `http` method (#87).
- The sender-side `upload` tool (#85).

## Local review

`preflight-circus` (five core lenses + `pr-review-toolkit:code-reviewer`) runs on the cumulative diff; clean at the ≥80 confidence bar before opening the draft PR.

## Test plan

- [ ] `uv run pytest` green on the full suite.
- [ ] `uv run ruff format --check .` / `ruff check .` / `mypy src` clean.
- [ ] `create_upload_link` accepts the five v0.3.0 params and returns `{url, ttl_seconds, max_bytes}`.
- [ ] In-band rejection returns the `transfer_failed` envelope with `receiver_server`.
- [ ] The upload POST route returns `404` (never `410`) for unknown / expired / consumed, indistinguishably.
- [ ] `grep -rn 'target_id\|upload_url\|expires_in_seconds\|consume_or_status\|\.extra\b' src/` shows no upload-surface survivals.
- [ ] CI green; bot review clean.

## Out of scope

- The sender-side `upload` tool (`http_upload.source`) — #85.
- The `http`-direction tool/route conformance audit — #87.
- The produce-and-consume e2e test — #88.

## Acceptance (from #74 / the design doc)

- [ ] `register_file_exchange_upload` has six domain-hook kwargs; every removed kwarg is env-var or pvl-core-fixed.
- [ ] `create_upload_link` matches the v0.3.0 parameter/return contract.
- [ ] In-band rejection returns the `transfer_failed` envelope.
- [ ] The POST route emits no `410`; unknown/expired/consumed are indistinguishable `404`s.
- [ ] `UploadRecord` models the WHAT/WHERE split; no `extra` field.
- [ ] The old `target_id` / `upload_url` / `expires_in_seconds` / `410` surface is grep-verified gone.
- [ ] Tests rewritten to the v0.3.0 shape; full suite + ruff + mypy clean.
