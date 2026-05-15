# Design: e2e test for the produce-and-consume http dual-role path (issue #88)

**Status**: approved (brainstorm 2026-05-15)
**Issue**: [pvliesdonk/fastmcp-pvl-core#88](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/88)
**Umbrella**: #75 — this is the last open umbrella item.

## Problem

#86 fixed a dual-role bug in `register_file_exchange`: the helper used
`if produce / elif consume`, so a server that both produced and consumed
advertised only the producer (`source`) tool — the consumer
(`fetch_file` / `sink`) tool was hidden. #86 changed the `elif` to two
independent `if`s.

#86 guards that fix only at the **builder-unit** level
(`test_builder_http_both_roles_emits_source_and_sink` in
`tests/test_file_exchange_capability_merge.py` — the builder, given both
roles, emits both). It adds **no** test exercising the
`register_file_exchange` **public call site** with `produce and consume`,
which is where the `if/elif`→`if/if` fix actually lives.

The nearest existing test, `test_advertises_both_methods` in
`tests/test_file_exchange_facade.py`, registers a produce-and-consume
server but asserts only that the `http` and `exchange` *method* keys are
present — it never inspects `http`'s `source` / `sink` *role* sub-keys.
So the dual-role guard is untested end-to-end.

A produce-*only* e2e would not guard the fix: with `produce` only, the
old `elif consume` branch was already dead, so buggy and fixed code
behave identically. The test must register **both** roles.

Separately, #86 review noted a small coverage gap: no test confirms the
default `("*/*",)` `accepts` wildcard reaches the wire through the
`register_file_exchange_upload` public helper. The existing
`test_builder_http_upload_sink_includes_explicit_accepts` covers only an
*explicit* `accepts` value at the builder-unit level.

## The design

Two synchronous test functions added to
`tests/test_file_exchange_capability_merge.py` (the file issue #88
specifies). **Purely additive — no production code changes.**

### Test 1 — http dual-role at the public call site

Set the environment via `monkeypatch` so the producer and consumer sides
both activate:

- `{PREFIX}_TRANSPORT=http` and `{PREFIX}_BASE_URL=http://...` — makes
  `enabled` true and gives the artifact store a base URL, so
  `set_http_source` fires.
- `produce` and `consume_env` both default to `true`
  (`{PREFIX}_FILE_EXCHANGE_PRODUCE` / `_CONSUME`), so no extra env is
  needed for them; passing a non-`None` `consumer_sink` makes `consume`
  true, so `set_http_sink` fires.
- `MCP_EXCHANGE_DIR` is cleared (it is unprefixed and deployer-global) so
  no `exchange` method leaks into the assertion.

Call `register_file_exchange(mcp, namespace=..., env_prefix=...,
produces=("image/png",), consumer_sink=<stub>)`, then assert:

```python
handle.capability.to_capability_dict()["transfer_methods"]["http"] == {
    "source": {"tool": "create_download_link"},
    "sink":   {"tool": "fetch_file"},
}
```

The `consumer_sink` is a correctly-typed `ConsumerSink`
(`Callable[[bytes, FetchContext], Awaitable[FetchResult]]`) stub. It is
**never invoked** — the capability is built at registration time — so it
only needs the right type and a non-`None` identity. The facade test's
`_identity_sink` is the existing pattern to mirror.

This is the integration-level twin of #86's builder-unit guard: it would
fail against the pre-#86 `if/elif` code (which hid the `sink` role) and
pass against the fixed `if/if` code.

### Test 2 — default `accepts` wildcard reaches the wire

Register `register_file_exchange_upload` with **no** explicit `accepts`
kwarg, build the capability, and assert:

```python
transfer_methods["http_upload"]["sink"]["accepts"] == ["*/*"]
```

This confirms the `("*/*",)` default propagates through the public helper
to the wire — complementing `test_builder_http_upload_sink_includes_explicit_accepts`,
which covers only an explicit value at the builder-unit level.

### Deviation from the issue text

Issue #88 says `pytest.mark.asyncio`. Both tests are in fact plain
**synchronous** `def`s: `register_file_exchange` /
`register_file_exchange_upload` are synchronous, the capability is built
during registration, and the `consumer_sink` is never awaited. This
matches the sibling `test_advertises_both_methods`, which is also
synchronous. The `asyncio` marker is dropped unless registration is found
to need a running event loop.

## Test mechanics

- Env is set via `monkeypatch.setenv` / cleared via
  `monkeypatch.delenv(..., raising=False)`, matching
  `tests/test_file_exchange_facade.py`. An env-isolation fixture (mirroring
  that file's `_clean_env`) keeps the two new tests hermetic — in
  particular clearing `MCP_EXCHANGE_DIR` and the `{PREFIX}_*` vars.
- The new tests are the first public-call-site tests in
  `test_file_exchange_capability_merge.py` (its current tests drive
  `_FileExchangeCapabilityBuilder` directly). This is consistent with the
  file's stated scope — "capability-merge across the http / http_upload
  registrars" — and is what issue #88 directs.

## Out of scope

- Any production code change. #86 already shipped the fix; #88 only adds
  the missing regression coverage.
- Exercising the `consumer_sink` or `byte_source` runtime paths — the
  tests assert capability shape only.
- The `http_upload` `source` role and the sender `upload` tool — covered
  by #85.

## Acceptance (from #88)

- [x] A test in `tests/test_file_exchange_capability_merge.py` registers
  `register_file_exchange(produces=..., consumer_sink=...)` on one
  `FastMCP` with the http transport + base URL, and asserts
  `transfer_methods["http"]` advertises both `source`
  (`create_download_link`) and `sink` (`fetch_file`).
- [x] A test asserts the default `("*/*",)` `accepts` wildcard reaches
  the wire via `register_file_exchange_upload`.
- [x] No production code is modified; the full suite, `ruff`, and `mypy`
  stay clean.
