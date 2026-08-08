# Transfer path 2 + description augmentation — design

- **Date:** 2026-08-08
- **Status:** Approved (design; implementation split across two issues)
- **Relates to:** ADR 0001 (`docs/adr/0001-transfer-lift.md`), PR #245 (tool
  metadata, released 4.7.0), PR #247 (docstring correction),
  `fastmcp-server-template`#308/#309, `markdown-vault-mcp`#979

---

## 1. Problem

ADR 0001 delivered `register_transfer_routes` — a complete, opinionated
capability-link feature. It works well for the case it was built for: a server
that wants generic upload/download links and implements two domain hooks.

It does **not** account for a second, legitimate case. A downstream may need a
transfer tool that pvl-core's generic pair cannot express: a different name, a
domain-accurate description, domain-specific parameters. Two things went wrong
around that gap:

1. **Path 2 was undocumented and unsupported.** Everything needed to build a
   custom link tool (`TransferStore`, `make_transfer_handler`) lives under
   `_transfer/` and is not exported. `_transfer/__init__.py` states the store,
   handler, and route mechanics stay internal.
2. **A false claim propagated.** PR #245's `register.py` module docstring told
   implementers to build on "the exported primitives (`TransferStore`,
   `fetch_url`, `decode_base64_capped`)". `TransferStore` was never exported.
   `fastmcp-server-template`#309 took this at face value and wrote a wiring
   example importing `fastmcp_pvl_core._transfer.routes` and `._transfer.store`
   directly.

A third, smaller gap surfaced alongside: core's generic tool descriptions carry
no domain knowledge, and for **upload** that is materially unhelpful (§4).

### The ownership question, resolved

ADR §3 assigns tool names to pvl-core. That governs **core's own tools**. A
downstream domain tool that happens to mint a capability link is *downstream's
tool* — core has no standing over its name or description, because core does
not know the domain. The two paths are intentional and complementary:

- **Path 1** — core's ready-made generic pair. Easy, predictable, identical on
  every server. Downstream implements only the two hooks.
- **Path 2** — downstream's own domain tools, built on core's link mechanics,
  for when path 1 is insufficient or its description would mislead.

pvl-core owns path 1's shape completely. Path 2 is downstream's, and core's job
is to expose enough for it to be built *without trespass*.

---

## 2. Scope

**In scope**

- **Part A** — a public link-minting surface enabling path 2.
- **Part B** — append-only description augmentation for path 1's tools.

**Out of scope (deferred, tracked)**

- **Scenario 2 of path 2** — a downstream driving the token state machine
  itself (`claim` → serve → `complete`/`release`) behind a custom route or
  streaming response. This needs `TransferToken` and the six `Token*Error`
  types public, and hands downstream the correctness burden of fencing,
  release-on-failure, and grace-settle that ADR §6 argues core should own.
  Filed as a known-but-unsupported shape so the boundary is explicit rather
  than silent.
- Any change to `TransferSink` / `TransferValidator`.
- The sink-error status-code gap (image-gen references it as #233).

---

## 3. Part A — public link-minting surface

### 3.1 What path 2 actually needs

With the route mounted once and core's handler serving it, downstream **never**
calls `claim` / `release` / `complete` — the handler does. Path 2 needs exactly
one capability: *mint a token and get a URL back*.

That rules out exporting `TransferStore` directly. `mint` knows nothing about
URLs, so downstream would hardcode `/transfer/{token}` and re-implement (or
skip) the TTL clamp — leaking two shape decisions precisely because the store
is the wrong altitude.

### 3.2 The seam

Two new public names.

```python
def build_transfer_links(
    mcp: FastMCP,
    config: ServerConfig,
    transfer_config: TransferConfig,
    *,
    sink: TransferSink,
) -> TransferLinks:
    """Mount the /transfer route and return its link minter. Registers no tools."""
```

```python
class TransferLinks:
    async def mint_download(
        self, sink_handle: str, ttl_s: float | None = None
    ) -> dict[str, Any]: ...

    async def mint_upload(
        self, sink_handle: str, ttl_s: float | None = None
    ) -> dict[str, Any]: ...
```

Each returns `{"url": ..., "expires_in_s": ...}` — the same payload path 1's
tools return today.

**`sink_handle`, not `ref`, and no `validate` hook.** In path 2 downstream's own
tool *is* the validation site: it resolves its domain ref to an opaque handle
before minting, in whatever way its domain requires. `validate` exists in path 1
only because core's generic tool must delegate that step to someone.

**What stays core's:** route path and method set, status codes, token entropy,
TTL clamp, `base_url` guard, store construction and namespace, the state
machine. `TransferLinks` exposes none of them.

### 3.3 Path 1 is re-expressed on path 2

`register_transfer_routes` becomes a thin wrapper: it calls
`build_transfer_links`, then registers its two tools, each applying `validate`
and delegating to the minter. One route-mount and one store-construction code
path, shared by both paths, so they cannot drift.

Its return type changes from `None` to `TransferLinks` — non-breaking, and it is
what makes mixed mode work.

### 3.4 The three scenarios

| Scenario | Call | Result |
|---|---|---|
| Path 1 only | `register_transfer_routes(...)` | Route + generic pair. Ignore the return. |
| Mixed | `links = register_transfer_routes(...)` | Route + generic pair; register extra domain tools on `links`. |
| Pure path 2 | `links = build_transfer_links(...)` | Route only, no tools. Register your own on `links`. |

One route, one store, one minter in every scenario. The route is mounted by
whichever entry point is called, and calling both is not a supported
combination — `register_transfer_routes` *is* `build_transfer_links` plus tools.

### 3.5 Surfacing

A public `fastmcp_pvl_core/transfer.py` module re-exports `build_transfer_links`
and `TransferLinks` from `_transfer`. So:

```python
from fastmcp_pvl_core.transfer import build_transfer_links
```

reads as an explicit "I am building my own tools". `_transfer/` stays internal
with relative imports, so a fold-in remains a directory rename (CLAUDE.md
foldability rule). Both names are also re-exported from the top-level
`__init__` alongside the existing transfer surface, so path 1 users need learn
nothing new.

`TransferStore`, `TransferToken`, `make_transfer_handler`, and the six
`Token*Error` types remain internal.

---

## 4. Part B — description augmentation

### 4.1 Why upload needs it more than download

Download refs are **referenced**; upload refs are **authored**.

A model calling `create_download_link` passes something it has already seen — a
path from a search result, an `image://` id from a listing. Core's generic
description plus the server's own instructions are usually enough, and the
validator rejects a bad ref at mint time in one round trip.

`create_upload_link` is different in kind: the model must *invent* a
destination, and nothing it has seen states the folder conventions, the
extension allowlist, or the note-vs-attachment distinction. Generic text there
is not merely terse — it leaves the model to discover the rules by being
rejected.

Both kwargs are provided anyway: download has genuine edge cases worth a
sentence, an asymmetric API invites "why only one?", and an omitted note costs
nothing.

### 4.2 The hook

```python
register_transfer_routes(
    mcp, config.server, config.transfer,
    sink=..., validate=...,
    download_note: str | None = None,
    upload_note: str | None = None,
)
```

Append-only. Core's generic description is always present and identical on every
server; the note is appended after a blank line. A `None`, empty, or
whitespace-only value is treated as absent and the description is byte-identical
to today's. There is no mechanism to replace core's text — that remains a shape
decision.

### 4.3 Why this is a hook, not a forbidden override

CLAUDE.md forbids "pvl-core has a default but downstream can override". That
rule targets **contested defaults** — where core picks a behaviour a downstream
dislikes, and the escape hatch is a kwarg. Its purpose is to stop repeated
"our server would prefer a different X" requests from eroding the shared shape.

A domain note has no contested default. There is no sentence core could pick
that downstream would want to override, because *core has no candidate sentence
at all* — it does not know the domain. Absent is not a default core chose; it is
the honest state of core's knowledge. There is nothing to disagree with and
nothing to escape.

`build_transfer_links` takes no note kwargs: path 2 downstream writes its own
tool docstrings.

### 4.4 The ADR amendment is a prerequisite, not a footnote

Resolving the CLAUDE.md rule question in §4.3 is **not** the same as changing
the decision record. ADR 0001 states, verbatim:

> **No shape-override kwargs.** Tool names, route path, status codes, and the
> scheme allowlist are pvl-core's. **The only kwargs are the two hooks
> (`sink`, `validate`)**; the only tuning is env config.
> — `docs/adr/0001-transfer-lift.md` §10 item 2

`register_transfer_routes` gaining two more kwargs contradicts that sentence as
written. §2 item 1 (`register_transfer_routes(mcp, config, *, sink, validate)`,
"Downstream implements **only** two domain hooks") and the §5 module table row
say the same thing.

A first attempt at Part B shipped the kwargs *while citing §10 item 2 as their
authority*, and the pre-flight gate returned `structural` — 11 findings at ≥80,
nine of them traceable to this single unamended premise: the missing category
label on the new Args entries, the unpinned append-only guarantee, and the tests
that therefore could not pin it either. The lesson is recorded here so the
sequencing cannot be lost again:

**The ADR amendment lands first, as its own change, before any code that
depends on it.** It ratifies a third kwarg category alongside domain hooks and
operator env config:

> **Additive-domain-text kwargs** — optional strings a downstream appends to
> text pvl-core owns. They may only *add*; the core text always survives as the
> prefix, so the shape stays pvl-core's. Not a shape override, and not operator
> config.

Amend §2 item 1, §10 item 2, and the §5 table row, using the repo's established
post-hoc form — the `> **Correction (post-implementation):** …` block already
present at `docs/adr/0001-transfer-lift.md:211` — rather than silently
rewriting shipped ADR text.

Every new kwarg's Args entry names its category (`Domain hook — …`,
`Additive domain text — …`), which CLAUDE.md's "Practical consequences"
requires and the existing `sink` / `validate` entries already do.

### 4.5 The append-only guarantee must be enforced, not just asserted

The first attempt wrote `inspect.cleandoc(fn.__doc__ or "")`. Under
`python -OO` / `PYTHONOPTIMIZE=2` docstrings are stripped, so `__doc__` is
`None`, the base collapses to `""`, and the *note becomes the entire tool
description* — the exact inversion of the guarantee. The `or ""` fallback makes
it silent.

The guarantee is therefore an invariant with a precondition, and the
precondition is checked:

- The base text is read from the tool function's docstring. A missing docstring
  is a **programming/build error**, not a runtime condition to paper over: raise
  at registration naming the tool and the `-OO` cause. Registration already
  fails loudly on a missing `base_url`, so this matches the module's existing
  posture — fail at wiring time, not at the first tool call.
- `_augment`'s docstring claims only what a pure two-line function can: it
  appends, and returns the base unchanged for an absent note. The end-to-end
  "byte-identical" claim belongs to the registration site, which is where the
  test pins it.

A blank-but-supplied note (`""` / whitespace) stays a silent no-op rather than
an error — an operator template that renders empty should not take the server
down — but registration logs at `debug` that a note was supplied and discarded,
so the operator has a signal. `register.py` has no logger today; it gains the
module-level `logging.getLogger(__name__)` that `routes.py` and `store.py`
already use.

### 4.6 Implementation

`@mcp.tool(description=...)` overrides the function docstring, and
`description=None` falls back to it (both verified against the installed
fastmcp). Core composes `inspect.cleandoc(<base>) + "\n\n" + note` and passes
the result explicitly.

The tool functions are nested closures, and a closure cannot reference its own
`__doc__` in its own decorator expression — so registration is an explicit
`mcp.tool(...)(fn)` call after the `def`, not `@` syntax. This keeps the
docstring as the single source of the base text; a duplicated module constant
would be free to drift from it.

---

## 5. Testing

**Part A**

- Pure path 2: `build_transfer_links` registers no tools; a minted link redeems
  end-to-end over ASGI (minter → route → handler → sink).
- Mixed mode: `register_transfer_routes` returns a `TransferLinks`; a
  path-1-minted link and a path-2-minted link both redeem against the same
  store and route.
- The `/transfer/{token}` route is mounted exactly once.
- `base_url` guard fires from `build_transfer_links` at call time.
- TTL clamp applies to minter calls: omitted → default; over max → clamped;
  in range → honoured; non-positive → rejected.
- Path 1's existing contract tests continue to pass unchanged (the wrapper
  must not alter observable behaviour).

**Part B**

- A note is appended to the correct tool, after core's base text.
- Notes do not cross tools: a download note never appears in the upload
  description, and vice versa (a test that would pass with the arguments
  swapped is not a test).
- **The full base body is pinned, not just its first sentence.** The first
  attempt asserted only `description.startswith("<first sentence>")`, and both
  of these mutations passed the whole suite:
  - dropping `inspect.cleandoc` (leaving every continuation line indented,
    which renders as a Markdown code block in a client);
  - truncating the base to `.split("\n\n")[0]` (silently losing the parameter
    documentation).

  So the assertions must include a verbatim fragment from the base's *second*
  paragraph, and that no line of the rendered description begins with
  indentation. Both mutations are re-run as a check on the test, not just the
  code.
- Both notes omitted → description equals the base docstring exactly (pinned
  against `inspect.cleandoc(<the docstring>)`, not against another run of the
  same new code path — comparing post-change to post-change proves nothing).
- Empty / whitespace-only note treated as absent, and logged at `debug`.
- A multi-line note is handled: `_augment` strips the note but does not dedent
  it, so a triple-quoted note keeps interior indentation. Either dedent it via
  `inspect.cleandoc` or pin the current behaviour with a test — do not leave it
  untested.
- A missing docstring (the `-OO` case) raises at registration, naming the tool.

---

## 6. Sequencing

Three issues, three PRs, one issue-cycle each (CLAUDE.md).

0. **The ADR amendment first** (§4.4) — ratifies the additive-domain-text kwarg
   category in ADR 0001 §2 item 1, §10 item 2, and the §5 table. Docs-only, no
   code. **Part B may not start until this is merged**: its code cites §10
   item 2 as authority, and shipping the two in one PR is what produced the
   `structural` gate verdict on the first attempt (a PR that both changes the
   rule and relies on it gives review nothing stable to check against).
1. **Part B second** — the description-augmentation hook. Unblocks
   `markdown-vault-mcp`'s upload-description problem.
2. **Part A third** — the larger surface change.
3. **Scenario-2 deferral issue** — filed to document the known-but-unsupported
   shape.

### 6.0 Status of the first Part B attempt

Branch `transfer-domain-notes` (4 commits, unpushed, never PR'd) implemented
Part B before this revision and was stopped by the pre-flight gate at
`structural` — 11 findings at ≥80. It is a **spike**: read it for the mechanics
it proved (the `mcp.tool(...)(fn)` call form, `-OO` docstring stripping,
`description=None` fallback), and re-implement fresh against this revised design
rather than patching it. Its two substantive gaps — the unamended ADR and the
mutation-surviving tests — are now §4.4 and §5 respectively.

### 6.1 Superseded prose already on `main`

PR #247 merged (`e4d72c6`) before this design was settled, so its `register.py`
module docstring now states three things this design supersedes. Each is
corrected by the PR that invalidates it, not separately:

| Statement on `main` | Superseded by | Correction |
|---|---|---|
| "`register_transfer_routes` is the one public entry point" | Part A | Two entry points; `register_transfer_routes` is path 1, `build_transfer_links` is path 2. |
| "there are **no override kwargs** for any shape element" | Part B | Still true as written — the note kwargs are additive, not overrides — but the sentence must name them, and may only do so **after** the ADR amendment (§4.4) ratifies the category it is citing. |
| "must NOT ... reach into `._transfer.store` / `._transfer.routes` ... full stop" | Part A | The prohibition on private imports stands; the surrounding claim that path 2 is therefore impossible does not. Rewrite to point at `build_transfer_links`. |

One further item, independent of both parts and pre-dating them:
`create_download_link` carries `destructiveHint=False` under `readOnlyHint=True`,
which is inert per the MCP annotation spec and which `claude-review` flagged
twice on PR #245. Any PR that rewrites that annotations block should drop it,
matching `_server_info.py`'s precedent.

The ADR §5 correction note from #247 (the factual record that `TransferStore`
was never a public export) stays as-is — it remains true, and Part A does not
change it: `TransferStore` is still internal.

Downstream follow-ups, out of scope here: `fastmcp-server-template`#309's
Path 2 example rewritten onto `build_transfer_links`;
`markdown-vault-mcp`#979 unblocked for both parts.
