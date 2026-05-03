# Auth subject extraction & optional authorization submodule

Design spec for issues
[#35](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/35),
[#36](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/36),
[#37](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/37).

Date: 2026-05-03.

## Problem

Bearer auth today accepts a single shared opaque token. Authenticated callers
share an indistinct identity, sufficient for connection gatekeeping but
unhelpful for downstream consumers that want per-user attribution (audit logs,
ACLs, request metadata). OIDC already produces a real subject from the `sub`
claim; the bearer surface needs a comparable affordance for deployments that
prefer pre-shared tokens (CI bots, service-to-service, small teams, no IdP).

Once a real subject exists across all auth modes, the next two layers can land:
a uniform `get_subject(request)` extractor so downstream code stops poking at
the auth context directly, and an optional `authorization` submodule providing
the `subject + tenant + required_scope → allow/deny` middleware that several
consuming servers will otherwise reinvent.

## Scope

This spec covers three sequential pull requests, one per issue:

1. **PR #35** — `{PREFIX}_BEARER_TOKENS_FILE` + bearer-mapped auth mode.
2. **PR #36** — `get_subject(request)` helper with unified extraction logic.
3. **PR #37** — optional `fastmcp_pvl_core.authorization` submodule (middleware,
   ACL store, scope vocabulary, admin tools, optional git-commit integration).

PR #35 is a breaking change (renames the `AuthMode` literal `"bearer"` to
`"bearer-single"`) and triggers a major version bump (1.x → 2.0). PR #36 and
#37 are additive and ship as `2.1.0` / `2.2.0` respectively under the existing
PSR + conventional-commits flow.

## Design

### Subject extraction (PR #35 + #36)

#### `ServerConfig` additions

Two new fields on `ServerConfig`, populated by `from_env`:

| Field | Env var | Default |
|---|---|---|
| `bearer_tokens_file: Path \| None` | `{PREFIX}_BEARER_TOKENS_FILE` | `None` |
| `bearer_default_subject: str` | `{PREFIX}_BEARER_DEFAULT_SUBJECT` | `"bearer-anon"` |

The per-app prefix is intentional: it matches the existing
`{PREFIX}_BEARER_TOKEN` convention and lets multi-tenant hosts run several
servers with separate token files.

#### Token file format

```toml
# bearer-tokens.toml
[tokens]
"ghp_alice_xxxxxxxx" = "user:alice@example.com"
"sk_ci_yyyyyyyy"     = "service:ci-bot"
```

Subject strings are opaque to the library. The `<kind>:<id>` convention
(`user:`, `service:`, `token:`) is documentation only.

#### Builder behavior

`build_bearer_auth(config)` is rewritten to handle three cases:

1. `bearer_tokens_file` is set:
   - Parse the file at startup. Malformed TOML, missing file, blank file, or
     schema-invalid contents raise a new `ConfigurationError` exception
     (fail-fast — never silent denial).
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

#### `AuthMode` literal — breaking change

`AuthMode` becomes `none | bearer-single | bearer-mapped | remote | oidc-proxy
| multi`. `resolve_auth_mode` distinguishes the two bearer flavors by
`bearer_tokens_file` presence; `multi` continues to mean "any bearer flavor +
any OIDC". This is a breaking change for any consumer that switches on the
literal `"bearer"`; it lands together with PR #35 and drives the 2.0 major
bump.

A second observable change rides along: the per-token `client_id` in
`StaticTokenVerifier` was previously the literal `"bearer"` and becomes
`bearer_default_subject` (`"bearer-anon"` by default) in single-token mode, or
the per-token mapped subject in mapped mode. Code that read `access_token.
client_id` to gate behavior on the literal `"bearer"` will need to update;
this is documented in the PR #35 release notes.

#### `get_subject(ctx_or_request) -> str | None` (PR #36)

New module `_subject.py`, exported from the package root. Implementation is
mode-agnostic at runtime — the per-mode logic was already pushed into the
builders:

```python
def get_subject(ctx_or_request: object | None = None) -> str | None:
    access_token = get_access_token()
    if access_token is None:
        # No auth context. Local stdio with auth_mode == "none" returns the
        # constant "local"; anything else returns None and lets the caller
        # decide whether to fall back or error.
        return "local" if _current_auth_mode() == "none" else None
    sub = (access_token.claims or {}).get("sub")
    if sub:
        return sub
    return access_token.client_id or None
```

`_current_auth_mode()` returns the `AuthMode` value resolved at server
startup; PR #36 wires this through whatever the simplest indirection is at
implementation time (likely a module-level `set_current_auth_mode(mode)` that
`build_auth` calls before returning).

The optional positional argument exists so callers can pass an explicit
request/context object in the future without breaking the signature; v1 ignores
it and reads from FastMCP's existing context plumbing.

#### README drift fix (PR #35)

The README example currently shows `auth=build_auth("MY_APP", config)`; the
real signature is `build_auth(config)`. PR #35 corrects this in the same diff.

### Authorization submodule (PR #37)

#### Module layout

New package directory `src/fastmcp_pvl_core/authorization/`:

| File | Purpose |
|---|---|
| `__init__.py` | Public exports |
| `_middleware.py` | `AuthorizationMiddleware`, `AuthzDenied` |
| `_store.py` | TOML loader, schema validation, in-memory `ACL` model |
| `_admin.py` | `register_acl_admin_tools` + the four admin tool implementations |
| `_git.py` | Internal helper for `commit_acl_to_git` |

Re-exported from `fastmcp_pvl_core`: `AuthorizationMiddleware`,
`register_acl_admin_tools`, `TenantResolver` (Protocol), `AuthzDenied`,
`build_authorization_middleware`.

#### `AuthorizationMiddleware`

Installed via the existing `wire_middleware_stack(mcp, extra=[...])`
extension point. Hooks:

- **Tool call:** `subject = get_subject(request)`;
  `tenant = tenant_resolver(request)`;
  `required = annotation.get("requires_scope", "read")`;
  ACL lookup → continue or raise `AuthzDenied`.
- **Resource read:** same flow, but tenant comes from the resource's
  `requires_tenant` annotation. **Permissive default: resources without the
  annotation pass through unfiltered**, so existing downstream resources keep
  working when authz is enabled until they opt in.
- **`resources/list`:** filter the listing using the same per-resource logic;
  resources without `requires_tenant` always included.

`AuthzDenied` returns the structured error
`{code: "authz_denied", subject, tenant, missing_scope}`.

#### `TenantResolver`

```python
class TenantResolver(Protocol):
    def __call__(self, request: object) -> str | None: ...
```

Domain code provides the callable. For tool calls, typically extracts
`tenant_id` (or domain-specific name like `project_id`) from tool args; may
fall back to a configured default. Returns `None` for tenant-less operations
— the middleware then checks the wildcard tenant `*` only.

#### Resource `requires_tenant` annotation

Annotation value is one of:

- `str` — static tenant ID, applies to the whole resource.
- `Callable[[str, dict], str | None]` — given the resolved URI and any
  URI-template match groups, return the tenant. Domain owns the logic; the
  helper for the common templated case (`vault://{tenant_id}/...`) is just
  `lambda uri, params: params.get("tenant_id")`.

#### Scope vocabulary

Three flat scopes, ordered: `read < write < admin`. Tool annotation
`requires_scope`, default `"read"` for unannotated tools. An internal helper
`_satisfies(granted: set[str], required: str) -> bool` encodes the ordering.

#### ACL TOML store

```toml
[subjects."user:alice@example.com".tenants]
"*" = ["read", "write", "admin"]

[subjects."service:ci-bot".tenants]
"*" = ["read"]

[default]
tenants = {}
```

- Wildcard `*` allowed on the tenant side only; subject-side wildcard rejected
  at load time with a clear `ConfigurationError`.
- Reload-on-each-request: file mtime checked, re-parsed on change. Cheap;
  filesystem watch is out of scope for v1.
- ACL absent: log `WARNING` once per process, default-deny.
- Schema-invalid: load fails the request with `authz_denied` carrying a clear
  reason — never silent allow.

#### Admin tools

`register_acl_admin_tools(mcp)` registers four tools, all annotated
`requires_scope: "admin"`:

| Tool | Behavior |
|---|---|
| `acl_list_subjects()` | Returns ACL filtered to tenants the caller admins. Caller with admin on `*` sees the full ACL. |
| `acl_grant(subject, tenant, scopes, intent)` | Adds/extends grants. `intent` is a free-form audit string, **required**, written to the git commit message when `commit_acl_to_git=True`. |
| `acl_revoke(subject, tenant, scopes, intent)` | Removes grants. |
| `acl_set_default(scopes, intent)` | Replaces `[default].tenants["*"]`. |

#### Mutation flow

Single function path used by all three mutating tools:

1. Load current ACL from disk.
2. Apply mutation in memory.
3. Validate the result against the schema (defense-in-depth).
4. Write back atomically (temp-file + `os.replace`).
5. If `commit_acl_to_git`: invoke `_git.commit_acl(path, intent, subject)` —
   runs `git -C <repo> add <path>` then `git commit -m "acl: <intent>" --author
   "<subject>"`. Surface git failures as a tool error *after* the file write
   succeeded (i.e. the ACL is updated; the operator is told the commit didn't
   happen).

#### Configuration

The library does not add fields to `ServerConfig` for authz; the submodule is
opt-in per server. Domain code declares its own config fields:

| Field | Default | Meaning |
|---|---|---|
| `enabled: bool` | `False` | Master switch. |
| `acl_path: Path` | (required when enabled) | Path to the ACL TOML. |
| `tenant_resolver: TenantResolver` | (required when enabled) | Callable to extract tenant from request. |
| `commit_acl_to_git: bool` | `False` | Commit ACL mutations to git. |

Helper `build_authorization_middleware(config) -> AuthorizationMiddleware |
None` returns `None` when `enabled=False`; downstream code uses
`wire_middleware_stack(mcp, extra=[m] if (m := build_authorization_middleware(
config)) else [])`.

#### Out of scope (v1)

Verbatim from issue #37:

- Per-document/per-node ACLs (tenant-grain only).
- Role hierarchy beyond the three flat scopes.
- OIDC group-mapping (subject-string-only ACLs); group-based extension is
  additive — middleware lookup function changes, surface doesn't.
- Structured audit log of failed authz beyond standard middleware logging.

## Testing

Each PR ships its own tests. mypy-strict and ruff are gates per pyproject.toml.

### PR #35

- `test_auth_bearer_tokens_file.py` — TOML load happy path; malformed TOML →
  `ConfigurationError`; missing file → `ConfigurationError`; blank file →
  `ConfigurationError`; both env vars set → WARNING + file wins; mapped token
  → request succeeds with the mapped subject visible on
  `access_token.client_id`; unmapped token → 401.
- `test_auth_mode.py` extended — `bearer-single` vs `bearer-mapped`
  resolution; `multi` mode with mapped bearer.
- `test_config.py` extended — `bearer_tokens_file` and
  `bearer_default_subject` populate from env.

### PR #36

- `test_subject.py` — all five auth modes return expected subjects via a fake
  auth context (using FastMCP's testing primitives); `get_subject` works from
  inside a tool body, a middleware, and a resource handler (three integration
  tests).

### PR #37

- `test_authz_store.py` — schema validation; wildcard expansion; default-deny;
  reload-on-mtime-change; subject-wildcard rejected.
- `test_authz_middleware.py` — tool denial; scope ordering; tenant-less
  operation (global wildcard); permissive default for resources without
  `requires_tenant`; structured `authz_denied` shape.
- `test_authz_admin.py` — non-admin caller refused; grant/revoke/set_default
  flows; `intent` required; atomic write under simulated crash;
  `commit_acl_to_git` happy path + git failure surfaces tool error post-write.
- `test_authz_resource_filtering.py` — `resources/list` filtered correctly with
  mixed annotated and unannotated resources.

## PR-by-PR breakdown

### PR #35 — `feat(auth)!: support {PREFIX}_BEARER_TOKENS_FILE for token→subject mapping`

- `closes #35`
- Triggers the 2.0 major bump (rename of the `AuthMode` literal).
- Files: `_auth.py`, `_config.py`, new `_errors.py` for `ConfigurationError`,
  new tests, `__init__.py` exports, README drift fix.
- Verification list (from issue #35):
  - `{PREFIX}_BEARER_TOKENS_FILE=<path>` loads and a request with a mapped
    token reports the correct subject via the auth context.
  - `{PREFIX}_BEARER_TOKEN=<token>` (single) continues to work; subject is
    `bearer_default_subject` (default `"bearer-anon"`).
  - Both env vars set → WARNING logged and file mode active.
  - Unmapped token → 401.
  - Malformed/missing file → fail-fast at startup with a clear
    `ConfigurationError`.

### PR #36 — `feat(auth): get_subject helper for uniform subject extraction`

- `closes #36`
- Branched from `main` after #35 lands.
- Files: new `_subject.py`; `__init__.py` exports `get_subject`; new tests;
  README adds a short "Identifying the caller" section pointing at
  `get_subject`.
- Verification list (from issue #36): each auth mode returns the expected
  subject shape; `get_subject` works equally from inside tool bodies,
  middleware, and resource handlers; `auth_mode` reporting in startup logs
  aligns with the values the function returns.

### PR #37 — `feat(authorization): optional fastmcp_pvl_core.authorization submodule`

- `closes #37`
- Branched from `main` after #36 lands.
- Files: new `authorization/` package (5 files); top-level `__init__.py`
  exports; new tests; README adds an "Authorization (optional)" section with
  the wiring example; `pyproject.toml` adds an `authorization` extra
  (currently empty — reserved so a future `pygit2` switch doesn't bloat the
  default install).
- Verification list (from issue #37): default deny; wildcard expansion; scope
  ordering; admin tools refuse non-admin callers; ACL disabled → middleware
  is a no-op; `resources/list` filtering correct.

## Versioning

PSR (semantic-release) drives versions from conventional-commit subject lines.
PR #35's commit footer carries `BREAKING CHANGE: bearer auth-mode literal
renamed from "bearer" to "bearer-single"` to trigger 2.0. PR #36 and #37 ship
as `feat:` minors (2.1, 2.2). `CHANGELOG.md` is PSR-managed.

## Local review discipline

Per the global PR workflow, every PR runs the full circus before opening as
draft:

1. `pr-review-toolkit:code-reviewer` on the cumulative diff.
2. `superpowers:code-reviewer` on the same diff.
3. Targeted reviewers when the diff calls for them: `silent-failure-hunter`
   (PR #37 — error/fallback logic in mutation flow), `type-design-analyzer`
   (PR #37 — new `TenantResolver` protocol, `ACL` model), `pr-test-analyzer`
   (all three PRs).
4. Bar: nothing flagged at any severity from either reviewer.
5. Bot iteration after open capped at one round; escalate if a third would be
   needed.

PRs open as draft. Flip to ready only after CI green and bot LGTM bodies
(reading the body, not just the check status).

## Driving consumers

`pvliesdonk/reqeng-mcp` Phase 2 (write-substrate) is the immediate driver:
needs per-user attribution for ACLs and audit metadata, and the authz
submodule directly. Likely future consumers: any multi-tenant MCP server in
the PVL ecosystem.

Once PR #37 lands, `pvliesdonk/fastmcp-server-template` needs three matching
scaffold-side stanzas. Stub issues already filed against the template repo,
each with a `Depends-on:` pointer back to upstream:

- [`pvliesdonk/fastmcp-server-template#94`](https://github.com/pvliesdonk/fastmcp-server-template/issues/94)
  — commented `acl_enabled` field in the `CONFIG-FIELDS-START`/`-END` block
  of `config.py.jinja`, plus the matching `CONFIG-FROM-ENV-*` env reader.
- [`pvliesdonk/fastmcp-server-template#95`](https://github.com/pvliesdonk/fastmcp-server-template/issues/95)
  — commented `AuthorizationMiddleware` + `register_acl_admin_tools` wiring
  in `server.py.jinja` after `wire_middleware_stack(mcp)`.
- [`pvliesdonk/fastmcp-server-template#96`](https://github.com/pvliesdonk/fastmcp-server-template/issues/96)
  — multi-tenant deployment section in `README.md.jinja` (ACL TOML format,
  admin tool walkthrough, deployment matrix, pointer to upstream reference
  docs). Also depends on PR #35 because the deployment matrix includes the
  bearer-mapped flavor.

The template-side surface stays in sync with this upstream spec. When the
submodule's API names or env var conventions settle here, the three template
PRs land with exactly the matching strings — no drift between
`fastmcp_pvl_core` and the template scaffold. As implementation lands in this
repo, the corresponding template PR is the natural follow-up; tracking via
the issues above. Not in scope for this spec, but called out so the
cross-repo coupling is explicit.
