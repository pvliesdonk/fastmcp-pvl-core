# Authorization submodule — DRAFT

> **Status: DRAFT — not implemented.** The work described here was
> attempted in 2026-05 and abandoned mid-implementation. The
> originating issue [#37](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/37)
> is still **open**; pick up here when work resumes. Nothing in this
> document corresponds to shipped code: there is no
> `fastmcp_pvl_core.authorization` package on `main`, and the symbols
> referenced below (`AuthorizationMiddleware`, `register_acl_admin_tools`,
> `TenantResolver`, etc.) do not exist.

## Problem

A growing set of MCP servers in this ecosystem (`pvliesdonk/reqeng-mcp`
first, future multi-tenant servers next) need fine-grained per-tenant
per-subject access control distinct from connection-level auth. The
pattern is uniform: subject (from auth) + tenant (from request) +
required scope (per-tool annotation) → allow / deny.

Doing this per-server reinvents the same middleware, ACL file format,
admin tools, and tenant resolver protocol every time. This spec moves
the machinery upstream as an optional submodule.

Depends on issues #35 (bearer subject mapping — shipped as PR #39) and
#36 (`get_subject` helper — shipped as PR #40); see
[`auth-subject-authz.md`](auth-subject-authz.md) for those.

## Module layout

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

## `AuthorizationMiddleware`

Installed via the existing `wire_middleware_stack(mcp, extra=[...])`
extension point. Hooks:

- **Tool call:** `subject = get_subject()`;
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

## `TenantResolver`

```python
class TenantResolver(Protocol):
    def __call__(self, request: object) -> str | None: ...
```

Domain code provides the callable. For tool calls, typically extracts
`tenant_id` (or domain-specific name like `project_id`) from tool args; may
fall back to a configured default. Returns `None` for tenant-less operations
— the middleware then checks the wildcard tenant `*` only.

## Resource `requires_tenant` annotation

Annotation value is one of:

- `str` — static tenant ID, applies to the whole resource.
- `Callable[[str, dict], str | None]` — given the resolved URI and any
  URI-template match groups, return the tenant. Domain owns the logic; the
  helper for the common templated case (`vault://{tenant_id}/...`) is just
  `lambda uri, params: params.get("tenant_id")`.

## Scope vocabulary

Three flat scopes, ordered: `read < write < admin`. Tool annotation
`requires_scope`, default `"read"` for unannotated tools. An internal helper
`_satisfies(granted: set[str], required: str) -> bool` encodes the ordering.

## ACL TOML store

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

## Admin tools

`register_acl_admin_tools(mcp)` registers four tools, all annotated
`requires_scope: "admin"`:

| Tool | Behavior |
|---|---|
| `acl_list_subjects()` | Returns ACL filtered to tenants the caller admins. Caller with admin on `*` sees the full ACL. |
| `acl_grant(subject, tenant, scopes, intent)` | Adds/extends grants. `intent` is a free-form audit string, **required**, written to the git commit message when `commit_acl_to_git=True`. |
| `acl_revoke(subject, tenant, scopes, intent)` | Removes grants. |
| `acl_set_default(scopes, intent)` | Replaces `[default].tenants["*"]`. |

## Mutation flow

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

## Configuration

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

## Out of scope (v1)

Verbatim from issue #37:

- Per-document/per-node ACLs (tenant-grain only).
- Role hierarchy beyond the three flat scopes.
- OIDC group-mapping (subject-string-only ACLs); group-based extension is
  additive — middleware lookup function changes, surface doesn't.
- Structured audit log of failed authz beyond standard middleware logging.

## Testing (planned)

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

## Driving consumers

`pvliesdonk/reqeng-mcp` Phase 2 (write-substrate) is the immediate driver:
needs per-user attribution for ACLs and audit metadata, and the authz
submodule directly. Likely future consumers: any multi-tenant MCP server in
the PVL ecosystem.

When this submodule lands, `pvliesdonk/fastmcp-server-template` needs three
matching scaffold-side stanzas. Stub issues already filed against the
template repo, each with a `Depends-on:` pointer back to upstream:

- [`pvliesdonk/fastmcp-server-template#94`](https://github.com/pvliesdonk/fastmcp-server-template/issues/94)
  — commented `acl_enabled` field in the `CONFIG-FIELDS-START`/`-END` block
  of `config.py.jinja`, plus the matching `CONFIG-FROM-ENV-*` env reader.
- [`pvliesdonk/fastmcp-server-template#95`](https://github.com/pvliesdonk/fastmcp-server-template/issues/95)
  — commented `AuthorizationMiddleware` + `register_acl_admin_tools` wiring
  in `server.py.jinja` after `wire_middleware_stack(mcp)`.
- [`pvliesdonk/fastmcp-server-template#96`](https://github.com/pvliesdonk/fastmcp-server-template/issues/96)
  — multi-tenant deployment section in `README.md.jinja` (ACL TOML format,
  admin tool walkthrough, deployment matrix, pointer to upstream reference
  docs). Also depends on issue #35 because the deployment matrix includes
  the bearer-mapped flavor.

## When work resumes

The previous attempt (2026-05) ran into severe-enough issues mid-flight to
warrant abandonment. Picking this up should start by re-validating the
design against the now-shipped `get_subject()` and bearer-mapped surface,
not from this document alone.
