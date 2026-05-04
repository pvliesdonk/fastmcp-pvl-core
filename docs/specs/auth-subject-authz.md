# Auth subject extraction

Design spec for issues
[#35](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/35) and
[#36](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/36).

Date: 2026-05-04 (revised post-implementation; supersedes the
2026-05-03 draft).

## Problem

Bearer auth today accepts a single shared opaque token. Authenticated callers
share an indistinct identity, sufficient for connection gatekeeping but
unhelpful for downstream consumers that want per-user attribution (audit logs,
ACLs, request metadata). OIDC already produces a real subject from the `sub`
claim; the bearer surface needs a comparable affordance for deployments that
prefer pre-shared tokens (CI bots, service-to-service, small teams, no IdP).

Once a real subject exists across all auth modes, downstream code wants a
uniform `get_subject()` extractor so it stops poking at the auth context
directly.

A follow-on optional `authorization` submodule (subject + tenant + required
scope → allow / deny middleware) is described separately in
[`authorization-submodule.md`](authorization-submodule.md) — that work was
attempted in 2026-05 and abandoned mid-implementation; issue #37 remains
open.

## Scope

This spec covers the two issues whose work shipped:

1. **Issue #35**, shipped as
   [PR #39](https://github.com/pvliesdonk/fastmcp-pvl-core/pull/39) —
   `{PREFIX}_BEARER_TOKENS_FILE` + bearer-mapped auth mode.
2. **Issue #36**, shipped as
   [PR #40](https://github.com/pvliesdonk/fastmcp-pvl-core/pull/40) —
   `get_subject()` helper with unified extraction logic.

Issue #35's implementation (PR #39) is a breaking change — it renames
the `AuthMode` literal `"bearer"` to `"bearer-single"` and changes the
single-token `client_id` from the literal `"bearer"` to a configurable
default subject. The originating issue framed itself as
backward-compatible; the breaking-change framing here was a scope
expansion accepted during implementation. See
[#45](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/45) for the
discrepancy resolution. Together with PR #40 the work shipped under
the 2.0 major bump (`2.0.0-rc.1`).

## Design

### `ServerConfig` additions

Two new fields on `ServerConfig`, populated by `from_env`:

| Field | Env var | Default |
|---|---|---|
| `bearer_tokens_file: Path \| None` | `{PREFIX}_BEARER_TOKENS_FILE` | `None` |
| `bearer_default_subject: str` | `{PREFIX}_BEARER_DEFAULT_SUBJECT` | `"bearer-anon"` |

The per-app prefix is intentional: it matches the existing
`{PREFIX}_BEARER_TOKEN` convention and lets multi-tenant hosts run several
servers with separate token files.

### Token file format

```toml
# bearer-tokens.toml
[tokens]
"ghp_alice_xxxxxxxx" = "user:alice@example.com"
"sk_ci_yyyyyyyy"     = "service:ci-bot"
```

Subject strings are opaque to the library. The `<kind>:<id>` convention
(`user:`, `service:`, `token:`) is documentation only.

### Builder behavior

`build_bearer_auth(config)` handles three cases:

1. `bearer_tokens_file` is set:
   - Parse the file at startup. Malformed TOML, missing file, blank file, or
     schema-invalid contents raise `ConfigurationError` (fail-fast — never
     silent denial).
   - Build `StaticTokenVerifier(tokens={token: {"client_id": <subject>,
     "scopes": ["read", "write"]}})`. The per-token `client_id` carries the
     subject all the way to `access_token.client_id` at request time.
   - If `bearer_token` is *also* set, log a `WARNING`
     ("`{PREFIX}_BEARER_TOKENS_FILE` takes precedence over
     `{PREFIX}_BEARER_TOKEN`") and ignore the single token.
2. `bearer_token` is set (and `bearer_tokens_file` is not): existing behavior,
   except the per-token `client_id` becomes `bearer_default_subject` (was the
   literal string `"bearer"`).
3. Neither is set: returns `None` (unchanged).

### `AuthMode` literal — breaking change

`AuthMode` becomes `none | bearer-single | bearer-mapped | remote | oidc-proxy
| multi`. `resolve_auth_mode` distinguishes the two bearer flavors by
`bearer_tokens_file` presence; `multi` continues to mean "any bearer flavor +
any OIDC". This is a breaking change for any consumer that switches on the
literal `"bearer"`; it lands together with issue #35 and drives the 2.0
major bump.

A second observable change rides along: the per-token `client_id` in
`StaticTokenVerifier` was previously the literal `"bearer"` and becomes
`bearer_default_subject` (`"bearer-anon"` by default) in single-token mode, or
the per-token mapped subject in mapped mode. Code that read
`access_token.client_id` to gate behavior on the literal `"bearer"` will
need to update; this is documented in the PR #39 release notes.

### `get_subject() -> str | None` (issue #36 / PR #40)

New module `_subject.py`, exported from the package root. The signature
takes no arguments — the function reads the auth context via FastMCP's
ambient `get_access_token()` dependency and the package-internal
auth-mode pointer set by `build_auth`.

```python
def get_subject() -> str | None:
    access_token = get_access_token()
    if access_token is None:
        # No auth context. Local stdio with auth_mode == "none" returns
        # the constant "local"; anything else returns None and lets the
        # caller decide whether to fall back or error.
        return "local" if _current_auth_mode == "none" else None
    raw_claims = getattr(access_token, "claims", None)
    claims = raw_claims if isinstance(raw_claims, dict) else {}
    sub = claims.get("sub")
    if isinstance(sub, str) and sub:
        return sub
    client_id = getattr(access_token, "client_id", None)
    if isinstance(client_id, str) and client_id:
        return client_id
    return None
```

`_current_auth_mode` captures the resolved auth mode during server
initialisation (via `set_current_auth_mode`) so that `get_subject()`
can correctly distinguish between an unauthenticated local caller
(returning `"local"`) and a missing token in an authenticated session
(returning `None`).

The current implementation stores it as a process-global; the
architectural concerns this raises — surviving concurrent `build_auth`
calls in a multi-server-in-one-process setup, the typed contract on
`set_current_auth_mode`, missing end-to-end integration tests for tool
/ middleware / resource surfaces — are tracked separately in
[#42](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/42),
[#48](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/48), and
[#44](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/44). The
contract above (functional behaviour of `get_subject()` per auth mode)
is the durable specification; the storage mechanism is expected to
change.

### README drift fix (PR #39)

The README example previously showed `auth=build_auth("MY_APP", config)`;
the real signature is `build_auth(config)`. PR #39 corrected this in
the same diff.

## Testing

mypy-strict and ruff are gates per `pyproject.toml`. Each PR shipped
its own tests.

### Issue #35 / PR #39

- `test_auth_bearer_tokens_file.py` — TOML load happy path; malformed TOML →
  `ConfigurationError`; missing file → `ConfigurationError`; blank file →
  `ConfigurationError`; both env vars set → WARNING + file wins; mapped
  token → request succeeds with the mapped subject visible on
  `access_token.client_id`. The "unmapped token → 401" assertion was
  delegated to fastmcp's `StaticTokenVerifier` and is being added as a
  direct test in [#47](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/47).
- `test_auth_mode.py` extended — `bearer-single` vs `bearer-mapped`
  resolution; `multi` mode with mapped bearer.
- `test_config.py` extended — `bearer_tokens_file` and
  `bearer_default_subject` populate from env.

### Issue #36 / PR #40

- `test_subject.py` — unit tests covering each branch of `get_subject`'s
  resolution order: `auth_mode=="none"` returns `"local"`, bearer-single
  uses the default subject from `client_id`, bearer-mapped uses the
  mapped subject, OIDC prefers `claims["sub"]` and falls back to
  `client_id` when `sub` is absent or empty, and missing-token-with-
  auth-required returns `None`. Implementation reads FastMCP's ambient
  context via `get_access_token`; tests patch that single call site
  rather than spinning up a full FastMCP server.
- The originating issue's "works equally from inside a tool body, a
  middleware, and a resource handler" verification is *not* covered by
  these unit tests; integration tests against a real FastMCP server are
  tracked in [#44](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/44).

## PR-by-PR breakdown

### Issue #35 / PR #39 — `feat(auth)!: support {PREFIX}_BEARER_TOKENS_FILE for token→subject mapping`

- Closes
  [issue #35](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/35).
- Triggered the 2.0 major bump (rename of the `AuthMode` literal +
  `client_id` default change).
- Files: `_auth.py`, `_config.py`, new `_errors.py` for
  `ConfigurationError`, new tests, `__init__.py` exports, README drift
  fix.
- Issue verification list:
  - `{PREFIX}_BEARER_TOKENS_FILE=<path>` loads and a request with a mapped
    token reports the correct subject via the auth context.
  - `{PREFIX}_BEARER_TOKEN=<token>` (single) continues to work; subject is
    `bearer_default_subject` (default `"bearer-anon"`).
  - Both env vars set → WARNING logged and file mode active.
  - Unmapped token → 401 (delegated to `StaticTokenVerifier`; direct test
    pending — see issue #47).
  - Malformed/missing file → fail-fast at startup with a clear
    `ConfigurationError`.

### Issue #36 / PR #40 — `feat(auth): get_subject helper for uniform subject extraction`

- Closes
  [issue #36](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/36).
- Branched from `main` after PR #39 landed.
- Files: new `_subject.py`; `__init__.py` exports `get_subject`; new
  unit tests; README adds a short "Identifying the caller" section
  pointing at `get_subject`.
- Issue verification list: each auth mode returns the expected subject
  shape; `auth_mode` reporting in startup logs aligns with the values
  the function returns.  The "from inside tool / middleware / resource"
  AC remains unverified at the integration level — see issue #44.

## Versioning

PSR (semantic-release) drives versions from conventional-commit subject
lines. PR #39's commit footer carries `BREAKING CHANGE:` markers for
both observable breaks (the `AuthMode` rename and the `client_id`
default change). PR #39 + PR #40 shipped together as `2.0.0-rc.1`;
subsequent fixes (the post-#40 audit cleanup cluster, issues #41–#51)
ride future PSR-determined minors. `CHANGELOG.md` is PSR-managed.

## Local review discipline

Per the global PR workflow, every PR runs the full circus before opening as
draft:

1. `pr-review-toolkit:code-reviewer` on the cumulative diff.
2. `superpowers:code-reviewer` on the same diff.
3. Targeted reviewers when the diff calls for them: `silent-failure-hunter`,
   `type-design-analyzer`, `pr-test-analyzer`, `comment-analyzer`.
4. Bar: nothing flagged at any severity from either reviewer.
5. Bot iteration after open capped at one round; escalate if a third would be
   needed.

PRs open as draft. Flip to ready only after CI green and bot LGTM bodies
(reading the body, not just the check status).

## Driving consumers

`pvliesdonk/reqeng-mcp` Phase 2 (write-substrate) is the immediate
driver: needs per-user attribution for ACLs and audit metadata. Likely
future consumers: any multi-tenant MCP server in the PVL ecosystem.

The downstream `authorization` submodule (issue #37) — the deeper
machinery for ACLs / admin tools / scope vocabulary — is described in
[`authorization-submodule.md`](authorization-submodule.md). That spec
is currently DRAFT and not implemented; treat it as forward design only.

## See also

- [`authorization-submodule.md`](authorization-submodule.md) — DRAFT
  follow-on spec for the optional authorization submodule (issue #37).
