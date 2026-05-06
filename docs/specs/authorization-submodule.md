# Authorization submodule

Design spec for [issue #37](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/37).

Date: 2026-05-06.

## Status

Design accepted; implementation pending. Replaces the abandoned 2026-05
draft (`docs/specs/authorization-submodule.md` at commit
[9f79cfa](https://github.com/pvliesdonk/fastmcp-pvl-core/commit/9f79cfa)),
which was deleted before this redesign started.

## Problem

The package today gives downstream MCP servers a working
*identification* story: `get_subject()` returns "who is calling?" across
every supported auth mode, and `bearer-mapped` plus the OIDC modes
provide multi-user identification. What is still missing is
*authorization*: turning "who is this?" into "are they allowed to do
X?". Each consumer that needs per-user gating today reinvents the same
small pieces — a middleware that reads tool metadata and short-circuits,
a per-subject permission lookup, the structured deny error shape, list
filtering for `tools/list` and `resources/list`. Moving those pieces
upstream once is the goal of this spec.

## Spirit

The original issue body specified a heavyweight surface (middleware,
tenant resolver protocol, ACL TOML loader, four admin tools that mutate
the ACL via MCP, optional commit-to-git, fixed `read | write | admin`
scope vocabulary). That shape was attempted in 2026-05 and abandoned
mid-implementation. This spec is the redesign authored after the user
explicitly asked for the *spirit* — "provide (multi-)user auth to
downstream" — without inheriting the original shape.

The redesign's scope is intentionally narrower than the issue body:

| Original surface | Status in this spec |
|---|---|
| Enforcement middleware | **In** — `AuthorizationMiddleware`. |
| Annotation convention for required scope | **In** — `meta["required_scope"]`. |
| ACL TOML loader (read-only) | **In** — `load_acl`, plus `make_acl_authorizer` bridge. |
| Tenant resolver protocol / tenant axis | **Out** — collapsed into open-ended scope strings (e.g. `read:project-foo`). Domain decides whether to namespace scopes by project / vault / workspace. |
| Fixed `read \| write \| admin` scope vocabulary | **Out** — scopes are arbitrary domain-defined strings. The library only treats `*` specially (in `make_acl_authorizer`). |
| Default `read` requirement when no annotation | **Out** — fully opt-in: tools without `meta["required_scope"]` are unrestricted. |
| Admin tool helpers (`acl_grant`, `acl_revoke`, …) | **Out** — domain decides whether to expose ACL mutation; library does not ship admin tools. |
| Commit-ACL-to-git integration | **Out**. |
| ACL reload-on-change semantics | **Deferred** — load-once-at-startup in the initial implementation; restart to update. A future `make_reloading_acl_authorizer(path)` is purely additive. |

## Design

### Module layout and public API

A single new private module `src/fastmcp_pvl_core/_authorization.py`
following the package's leading-underscore convention. No subpackage;
no per-component file split — the surface is small enough that one
module is clearer than four.

Re-exported from `fastmcp_pvl_core`:

| Symbol | Kind | Purpose |
|---|---|---|
| `AuthorizationMiddleware` | class | The middleware. Subclasses `fastmcp.server.middleware.Middleware`. |
| `AuthzDenied` | exception | Raised by `check_authorization`; middleware translates to per-operation MCP error. |
| `check_authorization` | function | Imperative escape-hatch helper for fine-grained checks inside tool/resource bodies. |
| `load_acl` | function | TOML loader. `load_acl(path) -> dict[str, frozenset[str]]`. |
| `make_acl_authorizer` | function | Bridge: `Mapping[str, AbstractSet[str]] -> Authorizer`. Encodes the `*` wildcard scope semantics. |
| `Authorizer` | type alias | `Callable[[str \| None, str], bool]`. Public name for the seam. |

Six public symbols. `Authorizer` is a `TypeAlias`, not a `Protocol` —
the Protocol upgrade across this and other Callable-typed seams in the
package is tracked in
[#60](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/60).

The package uses the word *scope* throughout, despite the OAuth/OIDC
collision. Documentation calls out that "authorization scopes" in this
context are application-level identifiers chosen by the domain server
operator, distinct from the OAuth scopes carried in tokens. The
`Authorizer` callable's parameter is named `required_scope` to make
the namespace explicit.

### `AuthorizationMiddleware`

```python
class AuthorizationMiddleware(Middleware):
    def __init__(
        self,
        *,
        authorizer: Authorizer,
        expose_subject_in_error: bool = False,
    ) -> None: ...
```

One required keyword: the authorizer callable. One optional flag:
whether the wire-side deny payload includes the subject string
(`False` by default — see the error-shape section below).

The middleware overrides hooks for the three component types,
symmetrically:

| Hook | Behavior |
|---|---|
| `on_call_tool` | Coarse static check based on `tool.meta.get("required_scope")`. Then `await call_next(context)` wrapped in `try/except AuthzDenied` so a `check_authorization` raised in the tool body becomes a `ToolError`. |
| `on_read_resource` | Same pattern with `get_resource(uri)`; deny via `ResourceError`. |
| `on_get_prompt` | Same pattern with `get_prompt(name)`; deny via `PromptError`. |
| `on_list_tools` | Iterate tools returned from `call_next`, drop any whose `meta["required_scope"]` is set and `authorizer(subject, required) is False`. Tools without the meta key are always kept. |
| `on_list_resources` | Same as `on_list_tools` for resources. |
| `on_list_resource_templates` | Same for resource templates (kept symmetric even though it's niche). |
| `on_list_prompts` | Same for prompts. |

Subject lookup is internal: the middleware calls `get_subject()` at the
top of each hook. The authorizer signature is therefore
`(subject, required_scope) → bool`, not `(context, required_scope)` —
which keeps the authorizer testable without spinning up a fastmcp
context, and matches the existing ambient-`get_subject` pattern.

The list-filtering hooks evaluate per-tool by calling
`authorizer(subject, tool.meta["required_scope"])` once per tool that
has the meta key. The middleware never asks "what scopes does this
subject have?" — the authorizer's only obligation is yes/no per scope.
This works for any backing implementation (dict-backed, DB-backed,
IdP-group-backed) without forcing a "list scopes" capability into the
seam.

When the inner-component lookup
(`fastmcp_context.fastmcp.get_tool/get_resource/get_prompt`) raises —
mounted-server edge case, race during reload, transient internal state
— the middleware logs a `WARNING` and falls through to `call_next`
rather than denying. Deny-on-internal-error felt punitive when the
operator's authz config is fine and the underlying issue is in fastmcp
internals. Operators see the warning in logs.

`call_next` is *not* called when the static-meta check denies. Per the
fastmcp middleware docs (`servers/middleware`): denials raise per-
operation exceptions before `call_next`; never return error values.

The middleware also publishes the authorizer to a package-internal
`_current_authorizer` `ContextVar` at `__init__` time, so
`check_authorization` reads it ambient (see below). Same pattern as
`_current_auth_mode` in `_subject.py`; same composition caveat (last
writer wins across multiple `AuthorizationMiddleware` constructions in
the same context — operators wishing to compose multiple authorizers
must wrap each install in `contextvars.copy_context().run(...)`).

### Annotation convention

Tools, resources, and prompts opt in by setting
`meta={"required_scope": "<scope>"}` at registration:

```python
@mcp.tool(meta={"required_scope": "write"})
async def edit_document(...): ...
```

A single string per component. No list, no callable, no nested
per-arg structure — the static annotation is intentionally coarse.
For per-argument granularity (e.g. "user can write project-foo but
not project-bar"), the tool body uses `check_authorization` (next
section).

Why `meta` and not `annotations`: fastmcp's `ToolAnnotations` is the
MCP-standard set (`title`, `readOnlyHint`, `destructiveHint`,
`idempotentHint`, `openWorldHint`). Free-form keys belong in `meta`
(`dict[str, Any]`). The abandoned spec's
`annotations={"requires_scope": "write"}` example was simply wrong
against current fastmcp.

No `requires_scope` decorator is shipped in the initial
implementation: `meta={"required_scope": "..."}` is six characters
more than `@requires_scope("...")` and orders-of-magnitude less
likely to break across a fastmcp upgrade. Adding the decorator later
is purely additive.

### `check_authorization` and `AuthzDenied`

```python
def check_authorization(
    required_scope: str,
    *,
    authorizer: Authorizer | None = None,
    subject: str | None = None,
) -> None: ...
```

Imperative form for use inside a tool/resource/prompt body. Raises
`AuthzDenied` on deny; returns `None` on allow.

When `authorizer` is omitted, the helper reads the
`_current_authorizer` `ContextVar` set by `AuthorizationMiddleware.__init__`.
If neither path supplies one, raises `RuntimeError` with an actionable
message ("install AuthorizationMiddleware or pass authorizer=
explicitly"). This makes the helper usable both inside and outside
the middleware-installed context, while keeping the no-middleware path
explicit rather than silent.

When `subject` is omitted, the helper calls `get_subject()` — same
extraction as the middleware, single source of truth for "who is
calling?".

```python
class AuthzDenied(Exception):
    subject: str | None
    required_scope: str

    def __init__(self, *, subject: str | None, required_scope: str) -> None: ...
```

Plain `Exception` subclass with two attributes. Inherits from
`Exception`, *not* from `ToolError` / `ResourceError` / `PromptError`:
the middleware catches `AuthzDenied` raised from any component body
and re-raises it as the per-operation type appropriate to the active
hook. The same `AuthzDenied` raised from a tool body becomes
`ToolError`; raised from a resource handler becomes `ResourceError`;
raised from a prompt handler becomes `PromptError`. This keeps a
single domain-side exception type while the wire-shape varies per
component.

If a domain uses `check_authorization` *without* installing the
middleware, `AuthzDenied` propagates as a plain `Exception` and
surfaces as a generic MCP error. Documented as the cost of skipping
the middleware.

### Error shape

The wire payload (the string passed to `ToolError(...)` /
`ResourceError(...)` / `PromptError(...)`, JSON-encoded) defaults to:

```json
{"code": "authz_denied", "required_scope": "write"}
```

When `AuthorizationMiddleware(..., expose_subject_in_error=True)`:

```json
{"code": "authz_denied", "required_scope": "write", "subject": "user:alice@example.com"}
```

Subject identifiers are sensitive in multi-user deployments: leaking
"user:bob@example.com was denied" across a connection can confirm
account existence. Off by default; opt-in for deployments where the
information is appropriate (e.g. internal tools, debugging).

The subject is *always* logged at `WARNING` level on every deny,
regardless of the wire-payload flag. Operators see the full picture
in logs without exposing it to the client.

### TOML loader and authorizer bridge

```toml
# acl.toml
[subjects]
"user:alice@example.com" = ["read", "write"]
"user:admin@example.com" = ["*"]
"service:ci-bot"         = ["read"]
"local"                  = ["*"]
```

One flat `[subjects]` table. Keys are subject strings (opaque to the
library; the `<kind>:<id>` convention from the bearer-tokens TOML is
documentation only). Values are arrays of scope strings.

The `*` scope is interpreted by `make_acl_authorizer` as "any required
scope passes" — useful for admin grants. No subject-side wildcard
(`*` as a key); rejected at load time because a global subject
wildcard collapses the model.

`load_acl(path: Path) -> dict[str, frozenset[str]]` is fail-fast.
`ConfigurationError` (the existing package exception, already used by
`_load_bearer_tokens`) on every malformed condition:

- Path missing or not a regular file.
- Unreadable or non-UTF-8.
- Unparseable TOML.
- Top-level `[subjects]` table missing or not a table.
- Subject key blank or whitespace-only.
- Subject key equals `"*"` (the rejected-wildcard case).
- Subject value not an array.
- Array contains a non-string entry, or an entry that is blank /
  whitespace-only.

Empty `[subjects]` (`[subjects] = {}`) is *permitted* — it represents
an explicit "deny everyone" ACL, a valid state. The path is normalized
with `Path.expanduser()` once inside the loader, mirroring the single-
expansion-site pattern from `_load_bearer_tokens` (PR #55).

`make_acl_authorizer(acl: Mapping[str, AbstractSet[str]]) -> Authorizer`
returns:

```python
def authorize(subject: str | None, required_scope: str) -> bool:
    if subject is None:
        return False
    granted = acl.get(subject)
    if granted is None:
        return False
    return "*" in granted or required_scope in granted
```

`subject is None` denies. The `OIDC-required server with missing
token` case is mostly handled by fastmcp 401-ing before the middleware
runs, but the deny-by-default branch covers callers that invoke
`check_authorization` from a code path without auth context.

The ACL is captured by reference, not copied — a downstream that
mutates the dict in place sees the change reflected by the closure.
Documented; not a recommended pattern, but explicit so consumers know
the semantics.

No combined `load_and_make` helper. Consumers write
`make_acl_authorizer(load_acl(path))` — one short line, no helper
needed.

### Wiring from a domain server

```python
import os
from pathlib import Path
from fastmcp import FastMCP
from fastmcp_pvl_core import (
    ServerConfig, build_auth, build_instructions,
    wire_middleware_stack,
    AuthorizationMiddleware, load_acl, make_acl_authorizer, check_authorization,
)

config = ServerConfig.from_env("MY_APP")
mcp = FastMCP(
    name="my-app",
    instructions=build_instructions(
        read_only=False, env_prefix="MY_APP", domain_line="…"
    ),
    auth=build_auth(config),
)
wire_middleware_stack(mcp)

# Authz is opt-in. ACL path is a domain config concern, not on ServerConfig.
acl_path_raw = os.environ.get("MY_APP_ACL_PATH")
if acl_path_raw:
    authorizer = make_acl_authorizer(load_acl(Path(acl_path_raw)))
    mcp.add_middleware(AuthorizationMiddleware(authorizer=authorizer))

@mcp.tool(meta={"required_scope": "write"})
async def edit_document(project_id: str, doc_id: str, body: str) -> None:
    # Coarse "write" gate already passed at middleware. Per-project gate here:
    check_authorization(f"write:{project_id}")  # ambient authorizer
    ...

@mcp.tool                                       # no meta = unrestricted
async def list_documents() -> list[str]: ...
```

Three points worth calling out:

- The library does **not** add ACL fields to `ServerConfig`. Per the
  composed-not-inherited pattern documented in `_config.py`, ACL
  configuration belongs in the domain config (`MY_APP_ACL_PATH` in
  the example).
- `wire_middleware_stack(mcp)` is called *before*
  `mcp.add_middleware(AuthorizationMiddleware(...))`, so middleware
  order is: ErrorHandling → Timing → Logging → Authorization. Denied
  requests *are* timed and logged — operationally desirable.
- `check_authorization(f"write:{project_id}")` omits the `authorizer`
  kwarg; the middleware install above has populated the
  `_current_authorizer` `ContextVar`.

## What this submodule does NOT do

- No tenant axis or `TenantResolver` protocol. Multi-tenant deployments
  encode the tenant into the scope string (`read:project-foo`).
- No fixed scope vocabulary. `read`, `write`, `admin` are the
  conventional choices but the library treats every scope as an
  opaque string except `*`.
- No default required scope. Tools without `meta["required_scope"]`
  are unrestricted regardless of subject.
- No subject-side wildcard. `*` as a subject key in the ACL TOML is
  rejected at load time.
- No admin tools (`acl_grant`, `acl_revoke`, …). Domain decides
  whether and how to expose ACL mutation.
- No commit-ACL-to-git integration.
- No ACL reload-on-change semantics in the initial implementation.
  Restart the server to pick up changes. Future
  `make_reloading_acl_authorizer(path)` is purely additive.
- No structured audit log of failed authz beyond standard middleware
  logging (the per-deny `WARNING` already covers the basics).
- No OIDC group-mapping. The authorizer is a callable, so a domain
  that wants group-based authorization writes its own — no library
  change needed.
- No `requires_scope` decorator. `meta={"required_scope": "..."}` is
  the documented form; sugar can be added later.

## Testing

mypy-strict and ruff are gates per `pyproject.toml`. Test files follow
the existing one-concern-per-file convention from `test_auth_*.py` and
`test_subject.py`. Integration tests against a real fastmcp server stay
deferred for the same reason as
[#44](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/44) and
[#47](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/47):
fastmcp's in-memory `FastMCPTransport` runs the low-level MCP server
directly and bypasses the HTTP-layer auth middleware, so there is no
in-process surface that exercises the full chain end-to-end.

### Test files

| File | Coverage |
|---|---|
| `tests/test_authorization_loader.py` | `load_acl`: happy path, missing file, non-file path, unreadable bytes, malformed TOML, missing or non-table `[subjects]`, blank/whitespace subject key, subject key literal `"*"` rejected, non-list value, list with non-string entry, blank scope string. Each error case asserts on `ConfigurationError` and a clear message. Empty `[subjects]` permitted. |
| `tests/test_authorization_authorizer.py` | `make_acl_authorizer`: subject-in-ACL allow, subject-in-ACL deny (scope absent), unknown subject deny, `subject is None` deny, `"*"` scope wildcard. ACL captured by reference (mutating the dict reflects in the closure — explicit test). |
| `tests/test_authorization_check.py` | `check_authorization`: ambient authorizer (set via test helper) found and used, explicit `authorizer=` overrides ambient, no authorizer in either path raises `RuntimeError` with actionable message, `subject=` kwarg overrides `get_subject()`, `AuthzDenied` raised on deny carries `subject` and `required_scope` attrs. |
| `tests/test_authorization_middleware.py` | Middleware behaviors — see scenarios below. Patches `get_access_token` (per `test_subject.py`'s pattern) and a fake `MiddlewareContext` rather than spinning up a real fastmcp server. |
| `tests/conftest.py` | Adds `_reset_authorizer` autouse fixture mirroring `_reset_auth_mode`. |

### Middleware test scenarios

- Tool call, no `required_scope` meta → passes through, `call_next` called once.
- Tool call, `required_scope` set, authorizer returns `True` → passes through.
- Tool call, `required_scope` set, authorizer returns `False` → `ToolError` raised, `call_next` *not* called.
- Resource read with `required_scope`, denied → `ResourceError`.
- Prompt get with `required_scope`, denied → `PromptError`.
- Tool body raises `AuthzDenied` (via `check_authorization`) → middleware catches around `call_next`, converts to `ToolError`. Same for resource body → `ResourceError`, prompt body → `PromptError`.
- `on_list_tools` filters out tools whose `required_scope` is denied; tools without the meta key are kept.
- Same for `on_list_resources`, `on_list_resource_templates`, `on_list_prompts`.
- `tool` / `resource` / `prompt` lookup raises (mounted-server edge case) → log warning, fall through to `call_next`.
- Default error payload: `{"code": "authz_denied", "required_scope": "<scope>"}`. No `subject` key.
- With `expose_subject_in_error=True`: payload includes `"subject": "<sub>"`.
- Subject always logged at WARNING on deny regardless of `expose_subject_in_error`. Asserted via `caplog`.

### Verification list

Mapping the original issue's spirit to this design:

- [ ] Tool with `required_scope` denies unmapped subject; allows mapped subject.
- [ ] Tool without `required_scope` is unrestricted regardless of subject.
- [ ] `*` scope wildcard lets a subject pass any required scope.
- [ ] `subject is None` denies (default-deny in `make_acl_authorizer`).
- [ ] `subject == "local"` works exactly like any other subject (no special casing in either middleware or bridge).
- [ ] `tools/list`, `resources/list`, `resource_templates/list`, `prompts/list` are filtered to what the subject can use.
- [ ] `AuthzDenied` from a tool/resource/prompt body becomes the matching `*Error` on the wire.
- [ ] `check_authorization` works without explicit `authorizer=` when the middleware is installed; raises `RuntimeError` when neither ambient nor explicit is provided.
- [ ] Subject not exposed in deny wire payload by default; opt-in flag turns it on.
- [ ] Subject logged at WARNING on every deny regardless.
- [ ] Bad ACL → `ConfigurationError` at startup; never silent denial.

## Documentation

The audience splits in two — handled in two repos.

### Library-side (this repo)

- API reference in `_authorization.py` docstrings (developer audience reading the library directly).
- Terse "Authorization" section in `README.md`, after the existing "Identifying the caller" section: surface signatures + the minimal wiring example, link to this spec for the full picture.
- This spec, plus updating `docs/specs/auth-subject-authz.md` to replace its broken "See also" pointer to the deleted authorization-submodule draft.

### Template-side (`pvliesdonk/fastmcp-server-template`)

Operator-facing walkthrough belongs in the template, not the library.
Three stub issues to be filed against the template repo when
implementation lands here, each with `Depends-on:` pointing at this
issue:

1. Commented `acl_path` config field in `config.py.jinja` (between the
   `CONFIG-FIELDS-START`/`-END` block) plus the matching
   `CONFIG-FROM-ENV-*` env reader stanza. Default commented-out so the
   scaffold stays opt-in.
2. Commented `AuthorizationMiddleware` wiring in `server.py.jinja`
   immediately after the `wire_middleware_stack(mcp)` line, with the
   load + bridge dance shown.
3. Operator walkthrough section in `README.md.jinja`: ACL TOML schema,
   the `<kind>:<id>` subject convention, the `*` scope wildcard,
   opt-in posture, the load-once-at-startup-restart-to-update story,
   and the bearer-mapped subject ↔ ACL key alignment (the same
   string that's the value in the bearer-tokens TOML is the key in
   the ACL TOML).

Filed when implementation lands here, not before — the abandoned spec
filed analogous template-repo issues prematurely (#94, #95, #96 in
that repo) which then blocked on this work for months.

## Implementation phasing

Single PR is feasible — the surface is six symbols across one new
module plus a `conftest.py` fixture and four new test files, with
small README and existing-spec edits riding along. Splitting into
loader + middleware sub-PRs would not provide useful intermediate
states (the middleware can't be tested end-to-end without something
producing an authorizer; the loader has nothing to be wired into
without the middleware).

The implementation PR opens against `main` after this design lands as
its own commit/PR. Local review circus on each, per the standard PR
workflow.

Versioning is PSR-driven from conventional-commit subjects: this is a
non-breaking feature add (no existing symbol changes shape, no
existing behavior changes), so the implementation PR ships under a
minor bump (`feat(authorization): …`).

## Driving consumers

`pvliesdonk/reqeng-mcp` Phase 2 (write-substrate) is the immediate
driver: needs per-user attribution for ACLs and audit metadata.
Likely future consumers: `markdown-vault-mcp` (per-vault scopes),
`scholar-mcp` (read/write distinction), other multi-user MCP servers
in the PVL ecosystem.

## Local review discipline

Per the global PR workflow, every PR runs the full circus before
opening as draft:

1. `pr-review-toolkit:code-reviewer` on the cumulative diff.
2. `superpowers:code-reviewer` on the same diff.
3. Targeted reviewers when the diff calls for them: `silent-failure-hunter`, `type-design-analyzer`, `pr-test-analyzer`, `comment-analyzer`.
4. Bar: nothing flagged at any severity from either reviewer.
5. Bot iteration after open capped at one round; escalate if a third would be needed.

PRs open as draft. Flip to ready only after CI green and bot LGTM
bodies (reading the body, not just the check status).

## Deviations from issue #37

The issue body specified a heavyweight surface that was attempted in
2026-05 and abandoned. This redesign deviates as follows:

| Issue body | This spec | Reason |
|---|---|---|
| Subject + tenant + scope as the access-control unit | Subject + scope only; tenants encoded into scope strings | User decision in design discussion: per-tenant grain felt wrong; namespaced scopes (`read:project-foo`) are the simpler primitive. |
| Fixed `read \| write \| admin` vocabulary | Open-ended scope strings | Domain-specific scopes (`read:vault/personal`) need open-ended; the abandoned spec's `read < write < admin` ordering is a special case domains can encode in their own authorizer if they want. |
| Default `read` scope when no annotation | No default; tools without `meta["required_scope"]` unrestricted | User decision: makes the system fully opt-in. Avoids deny-by-default-on-omission, which surprises consumers. |
| `wire_middleware_stack(mcp, extra=[...])` install pattern | `mcp.add_middleware(AuthorizationMiddleware(...))` after `wire_middleware_stack(mcp)` | The `extra=` parameter does not exist in the shipped `wire_middleware_stack`. The previous session's implementation agent flagged this and was overridden — this design uses the actual fastmcp install API. |
| `annotations={"requires_scope": "..."}` | `meta={"required_scope": "..."}` | `ToolAnnotations` is the MCP-standard set; free-form keys go in `meta`. The previous design's choice was wrong against current fastmcp. |
| Admin tool helpers in the library | Out of scope | User decision: domain owns ACL mutation. Library does not ship tools that mutate library-loaded files. |
| Commit-ACL-to-git | Out of scope | Same. |
| Reload-on-each-request ACL semantics | Load-once-at-startup; future `make_reloading_acl_authorizer` is additive | Reload semantics are tricky (mtime-only? checksum? inode?) and were not justified by a concrete consumer need; deferring keeps the initial implementation predictable. |
| `fastmcp_pvl_core.authorization/` subpackage with `_middleware.py`, `_store.py`, `_admin.py`, `_git.py` | Single `_authorization.py` module | Smaller scope means a single module is clearer; the four-file split was sized for the heavyweight design. |

These are intentional deviations made during the redesign and accepted
by the user. They are listed here so a future reader of the issue body
sees exactly where this implementation parts ways with it.

## See also

- [`auth-subject-authz.md`](auth-subject-authz.md) — the design spec
  for the shipped `get_subject` + bearer-mapped work this builds on.
- [Issue #60](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/60)
  — tracks the broader question of whether `Authorizer` and other
  Callable-typed seams in the package should be Protocols. Low
  priority; not blocking.
