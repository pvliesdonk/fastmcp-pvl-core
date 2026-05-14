# Design: file-exchange hook-surface audit (issue #72)

**Status**: approved (brainstorm 2026-05-14)
**Issue**: [pvliesdonk/fastmcp-pvl-core#72](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/72)
**Depends on**: #73 (landed, [PR #80](https://github.com/pvliesdonk/fastmcp-pvl-core/pull/80))
**Umbrella**: #75
**Template tracker**: [pvliesdonk/fastmcp-server-template#131](https://github.com/pvliesdonk/fastmcp-server-template/issues/131)

## Problem

`fastmcp-pvl-core.file_exchange.register_file_exchange` (the download
helper) currently exposes 10 keyword arguments mixed across three
categories:

- **Domain hooks** the downstream supplies because pvl-core literally
  cannot know the value (`namespace`, `env_prefix`, `consumer_sink`,
  `produces`, `consumes`).
- **Overrides of pvl-core shape decisions** (`download_tool_name`,
  `fetch_tool_name`, `transport`, `legacy_capability_shape`).
- **An escape hatch** (`artifact_store`) that turns into a test
  injection point in practice.

Per the framing principle in [#73](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/73)
(now authoritative in `CLAUDE.md` and `README.md` post-PR #80), only
the first category belongs in a public helper. The other two are
"opt-out" surface — downstream choosing to deviate from pvl-core's
shared shape — and the user-confirmed sharpened framing is that
**there is no opt-out**.

The audit is the first concrete application of #73's principle.

## Sharpened classification test

Worth restating here because it tightens the `CLAUDE.md` / `README.md`
phrasing landed in PR #80 (see §"Documentation deliverables" below):

> Would pvl-core be wrong to make this decision itself? If pvl-core
> could pick a sensible value and downstream has no domain-specific
> basis to disagree → pvl-core picks it; no kwarg. If pvl-core
> *literally cannot* answer because the answer is about the
> downstream's domain → kwarg, and the kwarg is not optional unless
> the entire feature is opt-in. There is no third bucket of
> "pvl-core has a default but downstream can override."

Operator-side configuration (TTL ceilings, body caps, ports, base
URLs) is a separate axis — environment variables, not kwargs.

## Scope

This PR covers `register_file_exchange` (download helper) **only**.
`register_file_exchange_upload` is deliberately out of scope per
issue #72's own Notes section: #74 redoes the upload primitive
wholesale against the #71 spec release. Touching it in #72 would
be partly throwaway work.

## Per-kwarg disposition

| Kwarg | Current | Verdict | Action |
|---|---|---|---|
| `namespace: str` | required | Domain hook | Keep; tag `**Domain hook**` in docstring |
| `env_prefix: str` | required | Domain hook | Keep; tag |
| `produces: Sequence[str] = ()` | default empty | Domain hook | Keep; tag |
| `consumes: Sequence[str] = ()` | default empty | Domain hook | Keep; tag |
| `consumer_sink: ConsumerSink \| None = None` | optional | Domain hook (gates the consumer side) | Keep; tag; clarify it gates capability advertisement |
| `artifact_store: ArtifactStore \| None = None` | optional | Escape hatch / test injection | **Remove**; replace with internal test seam (see below) |
| `transport: Literal["http","stdio","auto"] = "auto"` | optional override | Override (env var `{PREFIX}_TRANSPORT` / `FASTMCP_TRANSPORT` already resolves) | **Remove**; env-var path is sole source of truth |
| `download_tool_name: str = "create_download_link"` | optional override | Override (pvl-core owns tool name) | **Remove**; pvl-core's name is the shared shape |
| `fetch_tool_name: str = "fetch_file"` | optional override | Override (pvl-core owns) | **Remove** |
| `legacy_capability_shape: bool = False` | optional shim | Compatibility shim from the v0.4-amendments window | **Remove**; no deprecation window per facet 4 |

## Final signature

```python
def register_file_exchange(
    mcp: FastMCP,
    *,
    namespace: str,
    env_prefix: str,
    produces: Sequence[str] = (),
    consumes: Sequence[str] = (),
    consumer_sink: ConsumerSink | None = None,
) -> FileExchangeHandle:
    ...
```

Five kwargs, all domain hooks, `produces`/`consumes`/`consumer_sink`
default to "this server does not produce / consume / receive."

## Internal artifact-store seam

`artifact_store` removal eliminates the public injection point but
keeps the test-injection capability behind a private API:

```python
# Module-level in file_exchange.py, NOT in __all__, NOT exported
# from __init__.py, NOT documented in README/CLAUDE.md.

_TEST_ARTIFACT_STORE: ArtifactStore | None = None

def _set_artifact_store_for_test(store: ArtifactStore | None) -> None:
    """Test-only seam. Replaces the lazy-built store with a fixture.
    NOT public API.  Reset to None between tests.
    """
    global _TEST_ARTIFACT_STORE
    _TEST_ARTIFACT_STORE = store
```

`register_file_exchange` consults `_TEST_ARTIFACT_STORE` before the
env-var build path. A pytest fixture in `tests/conftest.py` (or
alongside the file-exchange tests) resets it on teardown.

This is *not* a backdoor for downstream runtime injection. Downstream
that monkeypatches the same module-level is reaching into pvl-core's
private API — the leading underscore is the warning sign.

## Environment-variable contract

No env-var changes. Existing variables continue to do what they do
today, just without parallel kwarg overrides:

- `{PREFIX}_TRANSPORT` (fallback to `FASTMCP_TRANSPORT`) — transport
  resolution.
- `{PREFIX}_BASE_URL` — required for HTTP-served artifact URLs.
- `{PREFIX}_FILE_EXCHANGE_TTL` — TTL for issued download URLs.
- `{PREFIX}_FILE_EXCHANGE_PRODUCE` — operator opt-out of producer
  side (defaults true).
- `{PREFIX}_FILE_EXCHANGE_CONSUME` — operator opt-out of consumer
  side (defaults true).
- `MCP_EXCHANGE_DIR` (unprefixed) — deployer-controlled, already.

The docstring's "Environment" subsection enumerates these with their
roles and defaults, replacing the currently-scattered inline mentions.

## Documentation deliverables

Three doc artifacts ride in this PR.

### Tightened classification test in `README.md` and `CLAUDE.md`

Current test ("could pvl-core know this on its own?") is soft.
Replace with the sharper form above. Both files get parallel
updates; the existing structural duplication between them is
preserved. Specifically:

- `README.md` `## Design principles` → "Hooks expose domain-specific
  behaviour only" subsection: rewrite the classification test
  paragraph + bullets in the sharper form.
- `CLAUDE.md` "framing principle" section: same rewrite, directive
  voice retained.

The "operator config → env var" line stays as a separate axis
statement; the "split mixed kwargs" line stays.

### Worked-example annotation on `register_file_exchange`

Each remaining kwarg's docstring entry gets a leading category tag:

```
namespace: **Domain hook.** This server's logical name; used as both
    ...
```

Five `Domain hook` tags. This is the canonical worked example future
helpers cite.

Plus a one-paragraph design note at the top of the docstring:

> The kwarg surface is intentionally minimal — five domain hooks, no
> operator-config kwargs, no override seams. Operator config goes to
> environment variables (see "Environment" below). Implementation
> choices pvl-core makes (tool names, transport resolution,
> capability shape) are not overridable; downstream collisions
> resolve by downstream migration. See `CLAUDE.md` "framing
> principle" for the rationale.

### CHANGELOG entry

`CHANGELOG.md` gains a 3.0.0 section enumerating the removed kwargs
and pointing at the downstream migration issues.

## Downstream coordination

Per the "all-at-once + downstream issues filed pre-merge" choice.

### Pre-merge survey

Survey the four named downstream consumers from `README.md`:

1. `pvliesdonk/markdown-vault-mcp` — known to use file-exchange;
   known `create_download_link(path)` collision with pvl-core's
   `create_download_link(origin_id)`.
2. `pvliesdonk/scholar-mcp`.
3. `pvliesdonk/image-generation-mcp`.
4. `pvliesdonk/reqeng-mcp` (per local memory).

Plus the template: `pvliesdonk/fastmcp-server-template`.

Survey method: `gh search code` (or per-repo grep via `gh api`) for
`register_file_exchange(` and report which call sites pass any of
the removed kwargs. Survey result lives in the PR body.

### Issues filed per affected consumer

For each consumer that *passes any removed kwarg* OR has a known
collision, file an issue:

- Title: `migrate to fastmcp-pvl-core 3.0.0 (file-exchange kwarg removals)`.
- Body: diff sketch of what to delete + any renames.
- For `markdown-vault-mcp` specifically: the
  `create_download_link` collision section ("rename the pre-existing
  tool; pvl-core's `create_download_link(origin_id)` is the shared
  shape").
- Acceptance: migration PR landed, dep pin bumped to `>=3.0.0`.

Consumers that *don't* touch any removed kwarg get a one-paragraph
issue noting the major bump and the dep-pin update; no code
migration needed.

### Template

Folds into the existing umbrella issue
[fastmcp-server-template#131](https://github.com/pvliesdonk/fastmcp-server-template/issues/131).
A child issue specifically for the #72 fallout may not be needed —
template's `server.py.jinja` likely doesn't pass any removed kwargs
(scaffolded code is minimal by design). Will confirm during the
survey; if it *does* pass any, file a concrete child issue under
#131 mirroring the consumer-side ones.

## Release sequence

1. Survey downstreams; file all migration issues. Survey result lands
   in the design-doc commit or PR body.
2. Open pvl-core PR (closing #72) as **draft**. Body links every
   downstream issue.
3. Two-subagent local review circus on the cumulative diff. Address
   findings.
4. Flip ready; bots auto-re-review on flip per the
   `.gemini/config.yaml` policy.
5. Merge. `python-semantic-release` cuts `3.0.0` automatically from
   a `feat!:` or `BREAKING CHANGE:` commit footer.
6. Downstream consumers migrate at their own pace against the
   published `3.0.0`. They're unblocked the moment pvl-core merges.

pvl-core does **not** wait for downstream migration to release.

## Testing strategy

- Existing tests under `tests/test_file_exchange_*` need migrating:
  - Tests that pass a removed kwarg as a default-equivalent value
    (e.g. `transport="auto"`) → drop the kwarg, otherwise unchanged.
  - Tests specifically *exercising* a removed override (e.g.
    `legacy_capability_shape=True`, custom tool names) → delete the
    test along with the feature.
- Tests that injected `artifact_store=fake` migrate to
  `_set_artifact_store_for_test(fake)` via the new fixture.
- Add one new test:
  `register_file_exchange(mcp, namespace=..., env_prefix=..., artifact_store=anything)`
  raises `TypeError` (signature-level guarantee that the kwarg is
  really gone).
- Coverage target: no regression on lines that remain after the
  cleanup. Deleted lines drop their tests with them — coverage
  percentage *on the surviving surface* should not drop.

## Out of scope (explicit)

- `register_file_exchange_upload` — see #74.
- Spec-doc changes (`docs/specs/file-exchange.md`) — none needed;
  spec is already on v0.2.5 post-PR #77.
- `markdown-vault-mcp` migration PR — tracked in its own repo issue;
  out of scope for this PR.
- Template scaffold update — folds into template #131; this PR
  files the child issue only if the survey turns up a real change
  needed.

## Downstream survey result (2026-05-14)

`gh search code` for `register_file_exchange(` across each named
consumer + the template, followed by reading every hit's call-site
context. Result:

| Consumer | Affected by 3.0.0? | Removed kwargs in use | Migration issue |
|---|---|---|---|
| pvliesdonk/scholar-mcp | Yes | `transport=` (computed from CLI flag) | [pvliesdonk/scholar-mcp#196](https://github.com/pvliesdonk/scholar-mcp/issues/196) |
| pvliesdonk/image-generation-mcp | Yes (src + tests) | `transport=` | [pvliesdonk/image-generation-mcp#227](https://github.com/pvliesdonk/image-generation-mcp/issues/227) |
| pvliesdonk/reqeng-mcp | Yes (scaffold-style) | `transport="auto"` | [pvliesdonk/reqeng-mcp#17](https://github.com/pvliesdonk/reqeng-mcp/issues/17) |
| pvliesdonk/fastmcp-server-template | Yes (scaffold) | `transport="auto"` in `server.py.jinja` | [pvliesdonk/fastmcp-server-template#133](https://github.com/pvliesdonk/fastmcp-server-template/issues/133) (child of #131) |
| pvliesdonk/markdown-vault-mcp | **No** (uses `register_file_exchange_upload` only; download helper deferred per their #431) | n/a — dep-pin bump only | [pvliesdonk/markdown-vault-mcp#492](https://github.com/pvliesdonk/markdown-vault-mcp/issues/492) |

Notable findings:

- **All four `register_file_exchange` users pass `transport=`** — either an
  explicit `"auto"` (the default, no behaviour change after migration)
  or a CLI-derived `"http"`/`"stdio"` (requires the CLI to set
  `{PREFIX}_TRANSPORT` before `make_server()`). No consumer passes
  `download_tool_name=`, `fetch_tool_name=`, `legacy_capability_shape=`,
  or `artifact_store=`.
- **markdown-vault-mcp is unaffected** in the direct sense:
  `register_file_exchange` is commented out in its `server.py` pending
  the `create_download_link(path)` vs `create_download_link(origin_id)`
  tool collision (their #431). `register_file_exchange_upload` is the
  helper they actually use, and its kwarg surface is #74's job (after
  #71 lands a spec release), not this PR.
- **The template** carries the scaffold form `transport="auto"`. Once
  fastmcp-server-template#133 lands and a new template release is cut,
  fresh consumers running `copier update` pick up the no-`transport=`
  scaffold automatically.

## Acceptance (from #72)

- [x] Each kwarg classified per the three-way split (table above).
- [ ] Reclassifications applied: domain hooks documented (docstring
      tags); choices pulled into pvl-core (override kwargs removed);
      operator config moved to env vars (already there; just no
      parallel kwargs).
- [ ] The principle written up authoritatively — done in #73; this
      PR sharpens the classification-test wording per the corrected
      framing.
- [x] Template impact: addressed via fastmcp-server-template#133
      (child of #131); scaffold drops `transport="auto"`.
