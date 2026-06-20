# fastmcp-pvl-core — contributor guidance

Repo-specific rules for contributors (human and AI) working on
`fastmcp-pvl-core` itself. Personal/global agent rules (PR workflow,
bot reviewers, memory) live elsewhere; this file is for cross-cutting
rules that apply to *this* repo regardless of who is contributing.

## What this repo is

`fastmcp-pvl-core` is the **opinionated shared implementation** for
the `pvliesdonk/*-mcp` server family (`markdown-vault-mcp`,
`scholar-mcp`, `image-generation-mcp`, and future siblings). It is
consumed transitively by downstream servers via the
[`fastmcp-server-template`](https://github.com/pvliesdonk/fastmcp-server-template)
copier template (`copier update` propagation).

It is **not a buffet of helpers** downstream picks from à la carte.
It is the load-bearing layer that fixes the shape of cross-cutting
concerns so the server family stays coherent as it grows.

## The framing principle

Shape decisions live in pvl-core. Downstream contributes
domain-specific logic only. Five facets in detail; a sixth keeps the
exit clean for forks that leave the family.

### Shape decisions live in pvl-core

Tool names, parameter shapes, route structures, capability
declarations, error envelopes, environment-variable contracts —
pvl-core picks one shape and downstream conforms. If two downstream
servers would each prefer a different shape, the resolution is for
pvl-core to pick one and migrate the others to it, **not** for
pvl-core to grow an override kwarg.

### Hooks expose domain-specific behaviour only

A hook like *"where in my storage model do these bytes go?"* is
appropriate — pvl-core cannot know the answer for a particular
downstream. A hook like *"what should this tool be called?"* or
*"what HTTP status code should an oversize body return?"* is not —
those are shape decisions pvl-core owns and downstream accepts them.

**The classification test for a proposed new keyword argument** on a
`register_*` helper, `Build*` factory, or middleware constructor:
**would pvl-core be wrong to make this decision itself?**

- pvl-core *could* pick a sensible value and downstream has no
  domain-specific basis to disagree → **pvl-core picks it; no kwarg.**
  If downstream genuinely needs different behaviour, pvl-core changes
  shape and *all* downstreams follow.
- pvl-core *literally cannot* answer because the answer is about the
  downstream's domain → **domain hook**, accept the kwarg. The kwarg
  is not optional unless the entire feature is opt-in. There is no
  third bucket of "pvl-core has a default but downstream can override."

Operator-side configuration (TTL ceilings, max body sizes, listening
ports, debug flags) is a separate axis — environment variables, not
kwargs at all. The kwarg surface stays purely domain hooks.

If a proposed kwarg mixes the two — a legitimate hook bundled with an
override of shape — split it: keep the hook component, drop the
override component. Reviewers reject PRs that grow override kwargs
disguised as hooks.

### Spec docs are protocol extensions, not design docs

Files under `docs/specs/` describe **wire format and behaviour
requirements between independently developed servers** — what bytes
move between systems and under what rules. They are read by anyone
implementing the same protocol, not just pvl-core.

Implementation choices that pvl-core happens to make (lazy
materialisation, route mechanics, framework-specific helpers,
downstream tool naming and registration mechanics, default values
that pvl-core picks for its own use) belong in pvl-core's own
implementor docs or code comments, **not** in a spec doc.

Real spec gaps are resolved through a proper spec evolution: a new
release with the version field bumped per the spec's own
versioning-and-compatibility section. Inline amendments to a
published version are not a valid spec-evolution mechanism.

### Pre-existing downstream conflicts resolve by migration

If a downstream server has already shipped a different *shape* (a
differently named tool, a divergent parameter, a custom error
envelope), the resolution is for the **downstream** to migrate.
pvl-core does not grow a compatibility shim to spare downstream the
migration cost, even when the migration is large. If the migration
cannot land immediately, file a tracked downstream issue and ship
the breaking change in pvl-core anyway — the umbrella tracker
coordinates the cutover.

This applies to *shape* divergence (the things owned by pvl-core).
Domain-specific divergence between downstreams is expected and does
not require any migration — downstreams are supposed to differ in
domain logic.

### Downstream reuses pvl-core; it does not reimplement the protocol

Downstream servers reuse pvl-core's implementation of the shared
cross-cutting protocols (auth, logging, ...). They do
not reimplement a wire protocol independently. The `docs/specs/` spec
is the wire authority; pvl-core is its single shared implementation.
**No implementation is "the reference" — not even pvl-core's; the spec
is.** A downstream is never a reference implementation entitled to
declare its own behaviour authoritative.

If pvl-core's implementation is wrong, or diverges from a `docs/specs/`
spec, the resolution is to fix pvl-core centrally — one change, every
downstream follows — or to evolve the spec. A downstream that believes
pvl-core is wrong files against pvl-core; it does not fork the
behaviour and reimplement. "pvl-core is wrong, so I'll do it myself" is
the failure this principle exists to prevent.

### Keep pvl-core cleanly foldable

A fork is not a downstream. MIT lets anyone vendor pvl-core into their own tree
— to take over a single server when the fleet is no longer maintained, or to run
their own opinionated variant. We keep that exit ramp cheap: credible
foldability lowers the cost of depending on pvl-core in the first place, and the
seams that make the package vendorable are the same seams that keep it a clean
load-bearing layer. Foldability is a modularity property, not a coherence
compromise.

Contributors preserve:

- **Relative intra-package imports** (`from ._x import …`) so a fold-in is a
  directory rename, not a find-replace.
- **No self-name lookups** — never resolve pvl-core's own distribution name or
  package resources at runtime (`importlib.metadata.version(...)`,
  `importlib.resources.files("fastmcp_pvl_core")`). Naming the package in
  human-facing text (install hints, log/error messages, docstring
  cross-references) is fine; the prohibition is on *runtime* resolution of the
  name, not on mentioning it in prose.
- **Parameterized identity** — env prefixes, CLI `prog`, and similar
  caller-facing identity stay arguments, never hard-coded to pvl-core's name.
- **A narrow public surface** — the `__init__` re-export with `__all__` is the
  contract; internals stay `_`-prefixed.

This does **not** authorize pre-flattening abstractions "in case someone forks."
The factory/`Build*` layer, the `env(prefix, name)` indirection, and the
optional extras exist because pvl-core serves the whole family; collapsing them
is fork-side work documented in `docs/forking.md`, never done in pvl-core.

## Practical consequences

- **Adding a new `register_*` helper or `Build*` factory**: every
  kwarg passes the classification test above. Document each kwarg's
  category (hook / config / shape) in the docstring.
- **Adding a new spec under `docs/specs/`**: it documents wire format
  for interop, not pvl-core's implementation. Implementation
  decisions are out of scope of the spec file.
- **Adopting a downstream-suggested rename or shape change**: if it
  changes a shared shape, the change ships in pvl-core and downstream
  follows; if it changes only domain-specific behaviour, it does not
  belong in pvl-core at all.

## Local checks before pushing

Before declaring local checks clean:

```bash
uv sync --all-extras   # match CI's dependency state
uv run pytest          # unit + integration tests
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

CI runs the same checks on Python 3.10 through 3.13.

## Related

- [`fastmcp-server-template`](https://github.com/pvliesdonk/fastmcp-server-template)
  — copier template downstream consumers `copier update` from.
  Template-side equivalents of these principles are tracked in
  [fastmcp-server-template#131](https://github.com/pvliesdonk/fastmcp-server-template/issues/131).
- `docs/specs/` — wire-format specs (what goes between systems).
  Read these to understand the protocol; do not put pvl-core's
  implementation choices in here.
- [README.md](README.md#design-principles) — `## Design principles`
  section carries the same framing in user-facing voice; this file
  is the contributor-facing voice. Keep them aligned when one
  changes.
