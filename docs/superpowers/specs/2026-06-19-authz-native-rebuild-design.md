# Rebuild authorization on FastMCP-native auth checks

Design spec. Replaces the bespoke authorization submodule
([PR #61](https://github.com/pvliesdonk/fastmcp-pvl-core/pull/61),
`docs/specs/authorization-submodule.md`) with thin factories over
FastMCP 3.3's native authorization surface, and adds claim-based
authorization as the one gap the framework has no built-in for.

Date: 2026-06-19. A tracking issue is filed before the implementation
PR per the PR workflow.

## Status

Proposed.

## Why rebuild

The authorization submodule (designed 2026-05-06) ships its own
`AuthorizationMiddleware`, an `Authorizer` callable seam, a
`meta["required_scope"]` annotation convention, a `check_authorization`
escape hatch, and an `AuthzDenied` exception with a structured deny
envelope. FastMCP shipped **native** authorization — component-level
`auth=` checks, server-wide `AuthMiddleware`, scope/tag built-ins — in
**3.0 (2026-02-18)**, *before* the submodule was designed, yet the
submodule spec never references it. The pinned floor is now
`fastmcp>=3.3.1`.

pvl-core maintaining a parallel authorization middleware is precisely
the anti-pattern this repo's framing principle forbids: *"Downstream
reuses pvl-core; it does not reimplement the protocol… pvl-core is its
single shared implementation."* The same logic applies one layer up —
pvl-core reuses the framework rather than reimplementing a capability
the framework now provides.

Two facts make a clean rebuild (not a deprecation shim) the right move:

- **Nothing downstream depends on the authorization submodule yet.** No
  consumer reads `Authorizer`, `AuthorizationMiddleware`, the ACL, or
  `check_authorization`.
- The only live claim consumer, `markdown-vault-mcp`, reads `name` /
  `email` claims to attribute git commits — **informational identity
  via `get_claims()`, not authorization.** That path is in `_subject.py`
  and is out of scope here.

Per the repo's removal discipline, the old surface is **deleted**, not
deprecated or shimmed.

## What FastMCP 3.3 provides natively

```python
from fastmcp.server.auth import (
    AccessToken,    # .token .client_id .scopes .expires_at .claims(dict[str,Any])
    AuthContext,    # .token: AccessToken|None   .component: Tool|Resource|Prompt (.meta/.tags/.name)
    AuthCheck,      # sync-or-async Callable[[AuthContext], bool]
    require_scopes, restrict_tag, run_auth_checks,
)
from fastmcp.server.middleware import AuthMiddleware
```

- Component-level `@mcp.tool(auth=check)` controls **both** list
  visibility and access (unauthorized → not-found).
- Server-wide `AuthMiddleware(auth=check)` filters lists and raises
  `AuthorizationError` on unauthorized execution.
- A custom `AuthCheck` reads `ctx.token.claims`, `ctx.token.client_id`,
  `ctx.token.scopes`, and `ctx.component.meta` freely.
- `require_scopes` / `restrict_tag` cover OAuth-scope and tag patterns.

So scope-based and tag-based authz are *already* native; pvl-core
documents them and does not wrap them. The only authz capabilities
native lacks a built-in for are **subject→scope** (a static ACL, the
only per-token authz available in bearer modes — see below) and
**claim→scope** (group/role-driven authz for OIDC modes).

## Mode coverage (the load-bearing constraint)

A bearer token (`StaticTokenVerifier`) carries: `client_id` = the
subject, `scopes` = a hardcoded `["read","write"]` identical for every
token, and `claims` = `{client_id, scopes}` echoed back — **no real
OIDC claims.** Therefore:

| Mode | `require_scopes` | claim check | subject (ACL) check |
|---|---|---|---|
| `bearer-single` / `bearer-mapped` | useless (blanket `read,write`) | useless (no claims) | **the only option** |
| `oidc-proxy` / `remote` | works if IdP issues per-user scopes | **works** (groups/roles) | works (keyed by `sub`) |
| `multi` | — | OIDC callers only | bearer callers only |

This is why the ACL is **kept** (rebuilt as a native check), and why
`multi` mode needs an **OR** of the two checks.

**`none` / stdio mode.** FastMCP skips auth checks entirely when there
is no token (stdio, or HTTP with no `AuthProvider`), so components are
unrestricted there — the checks are never invoked. This intentionally
drops the old middleware's `"local"`-subject ACL path (where
`get_subject()` returned `"local"` and the ACL could grant it).
Authorization is now meaningful only when an `AuthProvider` is
configured; local/stdio is treated as trusted. The defensive
`ctx.token is None → deny` branch in each check therefore only guards
the unreachable-in-normal-flow case, never the stdio path.

## Design

### Public surface (5 symbols, all thin over native)

Re-exported from `fastmcp_pvl_core`, living in `_authorization.py`:

| Symbol | Kind | Purpose |
|---|---|---|
| `make_acl_check` | function | `(acl) -> AuthCheck`. Subject→scope, reads `ctx.token` (`sub` claim, else `client_id`). Covers every mode incl. bearer. |
| `make_claims_check` | function | `(claim, grants=None) -> AuthCheck`. Claim→scope, reads `ctx.token.claims`. OIDC modes only. |
| `any_check` | function | `(*checks) -> AuthCheck`. OR-combinator (native combines with AND); for `multi` mode. |
| `load_acl` | function | Unchanged TOML loader: `(path) -> dict[str, frozenset[str]]`. |
| `parse_claim_grants` | function | New inline-JSON loader: `(raw) -> dict[str, frozenset[str]]`. |

Each check honours the `meta["required_scope"]` convention: it reads
the required scope from `ctx.component.meta.get("required_scope")`; if
that yields no scope, the component is **unrestricted** (returns `True`,
regardless of caller/token) — opt-in, matching the old behaviour. The
required scope comes *solely* from the component annotation — pvl-core
does not offer a per-check `required=` override of that shape decision
(it would be the forbidden "third bucket" under `CLAUDE.md`'s
kwarg-classification rule). The convention thus survives; only its
*consumer* changes (a native check instead of bespoke middleware).

### `make_acl_check`

```python
def make_acl_check(
    acl: Mapping[str, AbstractSet[str]],
) -> AuthCheck: ...
```

Returned check `(ctx: AuthContext) -> bool`:

1. resolve `required_scope` (see meta rule above); none → `True`
   (unrestricted regardless of caller — checked before the token so an
   unannotated component is open even with no token).
2. `ctx.token is None` → `False`.
3. `subject` = `ctx.token.claims.get("sub")` if a non-empty string,
   else `ctx.token.client_id`. (Same `sub`→`client_id` resolution as
   `get_subject`, so the ACL keys identically across OIDC and bearer.)
   Non-string / empty subject → `False`.
4. `granted = acl.get(subject)`; `None` → `False`.
5. allow iff `"*" in granted or required_scope in granted`.

`acl` captured by reference (documented; same as the old bridge).

### `make_claims_check`

```python
def make_claims_check(
    claim: str,
    grants: Mapping[str, AbstractSet[str]] | None = None,
) -> AuthCheck: ...
```

`claim` stripped; empty → `ValueError` at construction. Returned check:

1. resolve `required_scope`; none → `True` (unrestricted, checked before
   the token).
2. `ctx.token is None` → `False`.
3. `values = _extract_claim_values(ctx.token.claims, claim)` (matrix below).
4. `granted = {v for v in values if v != "*"}` if `grants is None`
   (identity — `"*"` excluded so an untrusted claim value cannot trigger
   the wildcard) else the union of `grants[v]` over `v in values` present
   in `grants`.
5. allow iff `"*" in granted or required_scope in granted` (the `"*"`
   wildcard can therefore only originate from the operator's `grants`
   table, never from a raw claim).

**Claim-value extraction (runtime token data — lenient, never fail a
request on IdP shape):**

| Claim value | Returned `set[str]` |
|---|---|
| claim key absent | `∅` → deny |
| string scalar `"writers"` | `{"writers"}` — **not** whitespace-split (splitting is OAuth-scope behavior, rejected) |
| list of strings | that set |
| list with mixed types | string elements only |
| non-string/non-list scalar (int, bool, `null`) | `∅` → deny |
| empty list | `∅` → deny |

Identity mode (`grants=None`) is the zero-config common case: name IdP
groups to match scope strings and no map is needed.

### `any_check`

```python
def any_check(*checks: AuthCheck) -> AuthCheck: ...
```

Zero checks → `ValueError`. Returns an **async** check (native auth
checks may be sync or async; the combinator awaits coroutine results so
it composes either kind). Short-circuits `True` on the first passing
sub-check; otherwise `False`. Used for `multi` mode:
`any_check(make_acl_check(acl), make_claims_check("groups", grants))`.

### `parse_claim_grants`

```python
def parse_claim_grants(raw: str) -> dict[str, frozenset[str]]: ...
```

Parses the optional inline-JSON claim-value→scopes map. Strict,
fail-fast with `ConfigurationError`, schema-identical to `load_acl`'s
value rules:

| Condition | Result |
|---|---|
| not valid JSON / top-level not an object | `ConfigurationError` |
| empty object `{}` | permitted (deny-everyone, mirrors empty `[subjects]`) |
| key blank/whitespace | `ConfigurationError` |
| key `"*"` | `ConfigurationError` (collapses model, mirrors ACL) |
| value not an array / non-string entry / blank entry | `ConfigurationError` |
| `"*"` as a scope value | permitted (allow-any wildcard) |

Returns `dict[str, frozenset[str]]` — drops straight into
`make_claims_check(claim, grants=...)`. Takes the raw string (the
domain reads `MY_APP_AUTHZ_GRANTS` from its own env and passes it),
mirroring `load_acl(path)`.

### Wiring (pure FastMCP-native; no pvl-core middleware)

```python
import os
from fastmcp import FastMCP
from fastmcp.server.middleware import AuthMiddleware
from fastmcp_pvl_core import (
    make_acl_check, make_claims_check, any_check, load_acl, parse_claim_grants,
)

# OIDC mode — claim-based (identity: no map needed)
mcp = FastMCP(..., middleware=[AuthMiddleware(auth=make_claims_check("groups"))])

# bearer mode — subject ACL
mcp = FastMCP(..., middleware=[AuthMiddleware(auth=make_acl_check(load_acl(path)))])

# multi mode — OR of both
raw = os.environ.get("MY_APP_AUTHZ_GRANTS")
grants = parse_claim_grants(raw) if raw else None
mcp = FastMCP(..., middleware=[AuthMiddleware(auth=any_check(
    make_acl_check(load_acl(path)),
    make_claims_check(os.environ["MY_APP_AUTHZ_CLAIM"], grants),
))])

# tools opt in via the surviving convention
@mcp.tool(meta={"required_scope": "write"})
async def edit(...): ...
```

Config stays domain-read env vars (`MY_APP_AUTHZ_CLAIM`,
`MY_APP_AUTHZ_GRANTS`, the ACL path) — no `ServerConfig` fields, per the
composed-not-inherited pattern.

## Deletions (removal discipline)

Removed entirely from `_authorization.py`, the package `__init__`
re-exports, and the docs:

- `AuthorizationMiddleware`
- `AuthzDenied`
- `check_authorization`
- the `Authorizer` type alias
- `make_acl_authorizer` (replaced by `make_acl_check`)
- `set_current_authorizer` / `_current_authorizer` ContextVar plumbing
- the structured `{"code":"authz_denied", ...}` deny envelope and
  `expose_subject_in_error` (native denial via `AuthorizationError` /
  not-found replaces it — an intentional behaviour change, acceptable
  given no consumer)
- `docs/specs/authorization-submodule.md`

**Kept untouched:** `_subject.py` (`get_subject`, `get_claims`,
`set_current_auth_mode`) — informational identity, used by MVM.

### Removal verification (run before closing)

```bash
# No bespoke-authz symbols survive anywhere in the package or tests:
! rg -n 'AuthorizationMiddleware|AuthzDenied|check_authorization|make_acl_authorizer|set_current_authorizer|_current_authorizer|expose_subject_in_error|authz_denied' src tests
# The Authorizer alias is gone:
! rg -n '\bAuthorizer\b' src tests
# The obsolete spec is gone:
! test -e docs/specs/authorization-submodule.md
```

## Testing

mypy-strict + ruff gates. Tests patch `AuthContext` with a fake token
(no live server needed); `AuthContext` is a plain dataclass
(`.token`, `.component`). The old per-concept test files
(`test_authorization_authorizer.py`, `test_authorization_check.py`,
`test_authorization_middleware.py`) are **replaced** by new per-symbol
files (`test_authorization_acl_check.py`,
`test_authorization_claims_check.py`, `test_authorization_any_check.py`,
`test_authorization_grants_parser.py`) that assert the new state. This
satisfies the repo's removal discipline (coverage of the removed
behaviour is not silently dropped — it is rewritten to cover the
replacement symbols, and expanded); the file layout changes because the
old symbols no longer exist to test. `test_authorization_loader.py`
(for the kept `load_acl`) is retained unchanged.

| File | Coverage |
|---|---|
| `tests/test_authorization_acl_check.py` | `make_acl_check`: subject from `sub`-claim vs `client_id` fallback; allow/deny by grant membership; `"*"` wildcard; unknown subject deny; `ctx.token is None` deny; unrestricted component allowed even without a token; non-string subject deny; `meta["required_scope"]` present vs absent (unrestricted); invalid meta (non-string) treated unrestricted + warns. |
| `tests/test_authorization_claims_check.py` | `make_claims_check`: blank `claim` → `ValueError`; identity allow/deny; translation union; operator `"*"` grant passes; a `"*"` *claim value* does NOT grant universal access; full claim-extraction matrix; no-token deny; `meta`/absent (unrestricted) resolution. |
| `tests/test_authorization_any_check.py` | `any_check`: zero checks → `ValueError`; OR semantics; short-circuit; sync+async sub-checks both awaited; all-deny → deny. |
| `tests/test_authorization_loaders.py` | `load_acl` (kept cases) + `parse_claim_grants` (full strict matrix above). |
| `tests/conftest.py` | drop `_reset_authorizer` (no ambient authorizer anymore). |

### Verification list (design-time intent)

- [ ] Bearer mode: `make_acl_check` gates per-token by `client_id`; OIDC mode: keys by `sub`.
- [ ] `make_claims_check` identity vs translation; `"*"` wildcard; deny on no token / absent claim / empty values.
- [ ] String-scalar claim is one value, not whitespace-split; mixed list keeps strings only.
- [ ] `any_check` is a true OR; bearer caller passes via ACL while claims check fails, and vice-versa.
- [ ] `meta["required_scope"]` absent ⇒ component unrestricted.
- [ ] Bad `parse_claim_grants` JSON → `ConfigurationError` at startup; empty `{}` accepted.
- [ ] All deleted symbols verified gone (removal commands above).
- [ ] `get_subject` / `get_claims` unchanged and still pass their suites.

## Documentation

- API-reference docstrings on the five symbols.
- README "Authorization" section **rewritten**: the native wiring
  (`AuthMiddleware(auth=...)` / `@mcp.tool(auth=...)`), the scope-vs-claim
  distinction, the per-mode coverage table, `require_scopes`/`restrict_tag`
  pointers, and the identity-vs-translation modes.
- This spec. `docs/specs/authorization-submodule.md` deleted.

### Template-side (`pvliesdonk/fastmcp-server-template`)

Stub issues filed **when this lands**: native `AuthMiddleware` wiring
(replacing any bespoke-middleware scaffold), the `MY_APP_AUTHZ_CLAIM` /
`MY_APP_AUTHZ_GRANTS` / ACL-path env stanzas, and the operator
walkthrough (per-mode coverage, identity convention, inline-JSON map,
load-once-restart-to-update).

## Versioning

Breaking: public symbols (`AuthorizationMiddleware`, `AuthzDenied`,
`check_authorization`, `Authorizer`, `make_acl_authorizer`) are removed.
PSR-driven **major** bump from the conventional-commit footer
(`BREAKING CHANGE:`).

## Implementation phasing

Single PR: rewrite `_authorization.py`, update `__init__` re-exports,
rewrite the four test files + `conftest.py`, rewrite the README section,
delete the obsolete spec. Standard local-review-circus → draft →
CI-green + bot-LGTM → ready.

## See also

- `auth-subject-authz.md` — `get_subject` / `get_claims` (kept, untouched).
- FastMCP docs: `/servers/authorization` (native `AuthCheck` /
  `AuthMiddleware` / `AccessToken` reference).
