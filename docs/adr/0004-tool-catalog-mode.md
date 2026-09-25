# ADR 0004 — Tool catalog mode: an operator-selected, read-only-fenced search catalog

- **Status:** Proposed (study for [#300]; recommendation with no-go
  conditions; implementation tracked in the follow-ups below)
- **Date:** 2026-09-24; amended 2026-09-25 after FastMCP answered the two
  upstream issues (§2.5)
- **Deciders:** pvl-core maintainers
- **Relates to:** [#300] (the study request), [#294] and [#299] (the
  instruction-visibility and instruction-role work this must not undo),
  ADR 0002 (dual-mode tasks, whose registration this touches), ADR 0003
  (the same classification test applied to a different surface),
  [`docs/reference/fastmcp-search-transform.md`] and
  [`docs/reference/mcp-client-tool-discovery.md`] (the evidence)

> This is an **implementor design**, not a wire specification. The
> synthetic tools it describes are ordinary MCP tools; nothing here
> changes bytes between family servers. Per `AGENTS.md`, that is why this
> lives in `docs/adr/` and not `docs/specs/`.

---

## 1. Context

Some MCP clients send every tool description and input schema to the
model at the start of a session. A read-write markdown-vault-mcp instance
advertises 48 tools and about 97 kB of tool JSON (roughly 24,000 tokens);
an operator who connects three vaults pays that three times before any
work starts. Claude Code avoids the cost with a client-local tool search
and loads only tool names up front; OpenCode does not, and Codex CLI does
not.

FastMCP offers two server-side transforms that shrink the listing: the
search transforms (`search_tools` plus a `call_tool` proxy) and the
experimental CodeMode (discovery tools plus a sandboxed `execute`). [#300]
asks whether pvl-core should offer an operator-selected catalog mode built
on one of them, under these constraints: direct listing stays the default;
pvl-core owns the shape; write, destructive, elicitation, task-aware and
identity/status tools stay direct where required; no generic call may
become an unobservable path to a destructive operation; annotations,
visibility, approval and audit semantics survive; tasks, elicitation,
structured results and response `_meta` survive; the instruction snippets
still make sense when a tool is discoverable rather than listed.

The maintainer's framing when the study started was: a tool-search mode,
optional, because Claude Code does not need it; and an open question
whether the server can detect that a client needs it or whether the choice
must be operator configuration.

## 2. Research findings

Every finding below is recorded with its evidence in the two reference
pages; this section summarises what decides the design. The transform
probes ran on fastmcp 4.0.0 (this repository's lock) under CPython 3.10
and again on fastmcp 4.0.5 under CPython 3.11, with identical behaviour;
the client observations are single runs, recorded as such.

### 2.1 Need is undetectable; identity is detectable but not a legitimate switch

Claude Code with tool search active sends the same `initialize`,
`tools/list`, `prompts/list` and `resources/list` as the eager clients.
No client capability in either protocol era says "I defer tool
definitions". A server therefore cannot know whether the listing it
returns will be pasted into a model's context or held back.

What a server *can* read is `clientInfo`: Claude Code reports
`claude-code`, OpenCode `opencode`, Codex `codex-mcp-client`, all today
through a legacy `initialize`. FastMCP exposes it inside a transform's
`transform_tools()` during a client's `tools/list`, on both eras, so a
per-client catalog is mechanically possible; the probe proved it. Three
things rule it out as the shape:

- The 2026-07-28 specification makes `clientInfo` optional per request
  and says implementations "SHOULD NOT use them to change the behavior of
  the client or server".
- The name does not carry the fact that matters: a Claude Code with
  `ENABLE_TOOL_SEARCH=false` is eager and still says `claude-code`.
- Claude Code caches a server's tool list across sessions under the
  configured server name; a catalog whose shape depends on who connected
  last is a catalog the cache gets wrong.

So the answer to the maintainer's question is: it is operator
configuration, an environment variable, on pvl-core's operator axis.

### 2.2 FastMCP's search transform: what it preserves and what it breaks

- Through v4.0.9, `always_visible` fences **discovery, not access**. The
  built-in `call_tool` proxy resolves any name in the full catalog, pinned
  tools included, and carries no annotations at all. A pinned `delete`
  tool is reachable through a proxy the client reads as an unannotated
  generic call. This is exactly the path [#300] forbids. PR 5263 (open)
  closes both gaps; see §2.5.
- Hidden tools remain directly callable by name, as documented.
- On a legacy connection the proxy passes structured content, `_meta`, an
  elicitation round-trip and pvl-core's job handle through unchanged.
- Through v4.0.9, `FastMCP.get_tasks()` applies server-level transforms.
  With a search transform installed, every **hidden** task-capable tool
  disappears from Docket registration. On a modern connection such a tool
  then fails even when called directly ("Background tasks require a
  running tasks extension"); on a legacy connection it silently loses the
  native task path and falls back to pvl-core's job store. Pinning the
  tool restores both. This contradicted FastMCP's "remain fully
  functional"; PR 5262 fixed it on `main` (§2.5).
- On a modern connection with a tasks-negotiating client, proxying a
  task-capable tool returns an empty result where the direct call returns
  the value. This persists on `main` after PR 5262 and is filed as
  fastmcp#5267.
- `finalize_instructions` sees the transformed listing: with the transform
  installed before finalisation, every snippet naming a hidden tool is
  pruned. Installed after, the snippets survive, and the Claude Code trace
  shows why that is the right order: given instructions naming a hidden
  tool, the model called the proxy with that name directly and never
  called `search_tools`.
- Claude Code against a search catalog needs one MCP call; its
  `ToolSearch` is client-local. The cost to a deferring client is the lost
  per-tool annotations, not round trips.

### 2.3 Measured on markdown-vault-mcp

| Listing | Tools | Bytes | ≈ tokens | Note |
|---|---|---|---|---|
| Direct (today) | 48 | 96,925 | 24,200 | 34 read-only, 3 task-capable |
| Search, nothing pinned | 2 | 1,199 | 300 | every tool behind the proxy (1,151 on CPython 3.10) |
| Search, policy pin set (§3) | 19 | 38,255 | 9,600 | 61 % smaller than direct |

One `search_tools` call returns five results at 8–13 kB as JSON or about
3 kB as markdown. BM25 relevance is uneven on this vocabulary: "search
notes by keyword" ranks `search` first; "find notes about a topic" does
not return `search` at all.

### 2.4 CodeMode

CodeMode is experimental, needs the `code-mode` extra (`pydantic-monty`,
absent from the family's lock), runs model-written Python under a sandbox
whose defaults are 30 s, 100 MB and 50 tool calls per `execute`, and has
no supported way to keep a tool direct (PrefectHQ/fastmcp#4925, open; the
proposal on it is a contributor's). Everything §2.2 says about annotations, tasks and the
proxy applies with a sandbox on top. It is not evaluated further here.

### 2.5 Upstream response, 2026-09-25

Both issues this study filed were answered within a day, by the same
maintainer, in two PRs re-run against the study's probes:

- **PR 5262, merged** (closes fastmcp#5261; after v4.0.9, unreleased as
  of 2026-09-25): `get_tasks()` keeps the components a catalog transform
  hides. On `main` a hidden task-capable tool is registered, runs on a
  plain direct call and as a native task on a modern connection, unpinned.
- **PR 5263, open** (closes fastmcp#5260): the proxy refuses pinned
  names and carries the least-permissive hints over the tools it can
  reach. With only read-only tools hidden it lists `readOnlyHint: true`;
  one unannotated hidden tool makes it destructive. The hints are computed
  before the enabled filter, so a write tool hidden by `TOOLS_DENY` still
  counts unless it is also pinned by name; a pin set that wants a
  read-only proxy is computed over registered tools. `search_tools` is
  still unannotated.
- **Still open**: the empty proxy result for a task-capable tool on a
  modern connection reproduces on `main` without pvl-core; filed as
  fastmcp#5267.

The decision below was rewritten for this: pvl-core no longer needs its
own proxy, and instead depends on the FastMCP release that carries both
PRs.

## 3. Decision

pvl-core **may** offer a second catalog mode, opt-in, with this shape.
Nothing changes for a deployment that sets nothing.

**Operator contract.** One environment variable, `{PREFIX}_TOOL_CATALOG`,
values `direct` (default) and `search`. Under `AGENTS.md`'s classification
test ("would pvl-core be wrong to make this decision itself?") this is
operator configuration, so it is an env var and not a kwarg; the values
and everything below are pvl-core shape, so there is no override. A downstream server wires it in
with one call at the end of server assembly (a `register_*`-style helper
with no kwargs beyond the server and its config), after
`finalize_instructions` and after `apply_tool_visibility`.

**The invariant.** In `search` mode:

> reachable-through-proxy set = search-index set = hidden set
> = { tools with `readOnlyHint: true` } − { task-capable tools }
> − { `get_server_info`, `get_job_result` }

Everything outside that set stays in `tools/list` exactly as in `direct`
mode, with its own name, schema, annotations, icons and `execution` field.
Consequences that follow, each answering one of [#300]'s requirements:

- **Destructive and write tools are never behind the proxy**, so the
  proxy cannot become a path to them. A downstream tool with no
  annotations defaults to `readOnlyHint: false` and stays direct, which is
  the safe default.
- **The proxy is legitimately annotated** `readOnlyHint: true` and
  `destructiveHint: false`, and its description says it runs read-only
  tools found with `search_tools`. `openWorldHint` is left unset: a
  read-only tool may still reach an external service (scholar-mcp's
  lookups do), the hidden set is chosen by `readOnlyHint` alone, and the
  protocol default for an unset hint is the conservative one. A client that
  auto-approves reads and gates writes keeps that policy intact. Approval
  boundaries and audit names for writes are unchanged because writes are
  direct.
- **Task-capable tools are pinned unconditionally.** Through v4.0.9 that
  is what keeps them registered with Docket; on every version it is what
  keeps the modern-era empty result (fastmcp#5267) unreachable, since a
  pinned tool is refused by the proxy and a proxied call can never run as
  a native task. This is not a preference; §2.2 and §2.5 make it a
  correctness requirement.
- **The transfer link tools follow their annotations**: `create_upload_link`
  is not read-only and stays direct; `create_download_link` is read-only,
  is hidden, and the transfer workflow snippet still names it.
- **The identity and polling tools stay direct.** `get_server_info` is
  how a client learns what it is talking to; `get_job_result` is what a
  job handle tells the model to call next, and a handle that names a tool
  the model must first search for is a broken workflow.
- **Elicitation** needs no special case: it passes through the proxy on
  the legacy era every observed client speaks, and on the modern era the
  only elicitation the study could not verify is one it could not verify
  for direct calls either.
- **Structured results and `_meta`** pass through unchanged; the study
  observed both.

**The proxy is FastMCP's; the pin set is pvl-core's.** The mode ships
only against a FastMCP release that contains PR 5262 and PR 5263 (both
after v4.0.9); pvl-core raises its lower bound rather than subclassing
the transform. With that release, `BM25SearchTransform(always_visible=
<pin set>)` is used as shipped: its proxy refuses pinned names and, with
only read-only tools left hidden, lists as `readOnlyHint: true`. pvl-core
computes the pin set over every **registered** tool, not the effective
listing, because the proxy's hints count disabled tools (§2.5): a write
tool hidden by `TOOLS_DENY` is pinned by name too, which changes nothing
for the listing and keeps the proxy read-only. The search tool keeps
FastMCP's name `search_tools`; the proxy keeps `call_tool`. Search
results use the markdown serialiser (about a quarter of the JSON size);
the schema a model needs to construct a call is in it. Two accepted
costs: `search_tools` carries no annotations upstream, and on CPython
3.10 the proxy's `arguments` parameter lists without a description (the
`Annotated` loss recorded in `mcp-model-facing-text.md`); both are
FastMCP's to change, and neither touches the invariant.

**Ordering.** The transform is applied *after* `finalize_instructions`
and after `apply_tool_visibility`. Instructions therefore keep naming
hidden read tools, which the Claude Code trace shows lets a model skip the
search round trip entirely. Operator allow/deny lists are applied first,
so a denied tool is neither listed, indexed, nor reachable through the
proxy; the study observed it for the deny path and the implementation
pins it with a test.

**One added instruction snippet.** In `search` mode pvl-core appends a
CAPABILITIES-role snippet, written under the `writing-model-facing-text`
skill, stating that further read-only tools are found with `search_tools`
and run with `call_tool`, and that write tools are listed directly.

**No automatic mode.** There is no `auto` value. §2.1 is the reason; §5
records how it would be built if the family ever decides otherwise.

## 4. No-go conditions

Any of these blocks shipping or keeping the mode:

- A proxy that can reach a tool outside the hidden read-only set, by any
  route (name, version, namespace, hashed app address).
- A task-capable tool left unpinned.
- The transform applied before instruction finalisation.
- A deployment that changes `{PREFIX}_TOOL_CATALOG` between sessions
  under one configured server name; the mode is a deployment setting.
  The README will say so.
- CodeMode in any form until FastMCP ships supported direct passthrough
  (PrefectHQ/fastmcp#4925), annotation-preserving discovery, and the
  family accepts `pydantic-monty` in its dependency set; then a separate
  study.
- A FastMCP lower bound that does not contain PR 5262 and PR 5263: on
  v4.0.9 and earlier the built-in proxy reaches pinned tools and hidden
  task tools lose Docket registration.
- A change in FastMCP that makes the proxy's fence or its derived
  annotations behave differently from §2.5 without the implementation's
  tests catching it: the tests are the contract, and a FastMCP major bump
  re-opens this ADR.

## 5. Alternatives rejected

- **Switch on `clientInfo.name` (`auto`).** Feasible: the session is
  readable inside `transform_tools()` on both eras, and a pvl-core-owned
  set of names known to defer (`claude-code`) could select `direct` per
  session while everyone else gets `search`. Rejected because the spec
  says not to, because the name does not encode the fact (the
  `ENABLE_TOOL_SEARCH=false` case), because Claude Code's discovery cache
  assumes a stable shape per server name, and because it would make the
  catalog per-session state on a server the modern spec wants stateless.
  If a mixed-client HTTP deployment ever needs it, this is the mechanism.
- **Use FastMCP's transform as shipped at v4.0.9.** Rejected: unannotated
  proxy reaching pinned destructive tools; hidden task tools unregistered.
  Superseded on 2026-09-25: with PR 5262 and PR 5263 the transform is used
  as shipped (§3), and the subclass this ADR first proposed is not built.
- **A pvl-core-owned proxy subclass.** The first version of this ADR
  decided it. Dropped once upstream fenced and annotated the built-in
  proxy: a subclass would duplicate that and drift from it.
- **Pin nothing and hide everything** (the 1.2 kB listing). Rejected:
  every write becomes a generic call, and the task defect bites every
  dual-mode tool.
- **Annotation-split proxies** (`call_read_tool`, `call_write_tool`,
  `call_delete_tool`, as ha-mcp does). Considered and rejected for this
  family: a write proxy is still a generic path to writes, and the write
  set is small enough (14 of 48 on markdown-vault-mcp) to list directly.
- **Do nothing in pvl-core; rely on client-side deferral.** Claude Code
  already defers; OpenCode has an experimental client-side CodeMode behind
  a flag. Rejected as the *only* answer because the eager default costs
  OpenCode operators about 24,000 tokens per vault today, but it is why the
  default stays `direct` and the mode stays opt-in.
- **CodeMode.** See §2.4 and §4.

## 6. Tradeoffs

| | `direct` (default) | `search` |
|---|---|---|
| Eager client, markdown-vault-mcp | 97 kB listing | 38 kB listing; +3 kB per search result |
| Deferring client (Claude Code) | tool names only; per-tool annotations on load | one local `ToolSearch` hop; hidden read tools lose per-tool annotations behind the proxy |
| Write / destructive tools | direct | direct, unchanged |
| Task-capable tools | direct | direct, unchanged (pinned) |
| Read-only tools | direct | one `search_tools` round trip unless the instructions name the tool |
| Client approval policy on reads | per tool | per proxy (`readOnlyHint: true`) |
| Audit name for a read | tool name | `call_tool` with the tool name in its arguments |
| Search relevance | n/a | BM25 over names and descriptions; uneven on this vocabulary |

The last row is the honest cost of `search` mode for the model: a
read-only tool it cannot name from the instructions costs it a search,
and the search may miss. The instructions work under [#299] is what keeps
that cost low.

## 7. Consequences

- pvl-core gains one env var, one helper, one instruction snippet and a
  FastMCP lower-bound bump to the release carrying PR 5262 and PR 5263.
  Nothing changes for existing deployments.
- The `writing-model-facing-text` skill gains a paragraph on descriptions
  that must survive being read through a search result (the first
  sentence carries the choice; BM25 sees names, descriptions and
  parameter names).
- Downstream servers do nothing but wire the helper in through the
  template's `copier update`; the README documents the variable in the
  operator voice.
- The template's operator docs gain the variable and the one-line "do not
  flip it per session" note.

## 8. Follow-ups

Children of [#300], filed with this ADR:

- **pvl-core implementation** ([#362]): `{PREFIX}_TOOL_CATALOG`, the pin
  set computed over registered tools (not read-only, task-capable, the
  two core tools), ordering after finalisation, the CAPABILITIES snippet,
  the FastMCP lower-bound bump, README and template docs. Tests pin: the
  invariant on every tool of a fixture server, including that the proxy
  lists `readOnlyHint: true`; `TOOLS_DENY` unreachable through search and
  proxy and not loosening the proxy's hints; a task-capable tool runs as a
  native task in `search` mode and is refused by the proxy; instructions
  unchanged between modes apart from the added snippet; `_meta` and
  structured content through the proxy. Blocked on the FastMCP release.
- **Upstream, FastMCP** ([fastmcp#5261], closed by [fastmcp-pr-5262],
  merged 2026-09-24): `get_tasks()` applied server-level transforms, so a
  search transform dropped hidden task-capable tools from Docket.
- **Upstream, FastMCP** ([fastmcp#5260], closed by [fastmcp-pr-5263],
  open as of 2026-09-25): the built-in proxy carried no annotations and
  resolved pinned tools.
- **Upstream, FastMCP** ([fastmcp#5267], open): the proxy of a
  task-capable tool returns `{}` on a modern connection with a
  tasks-declaring client; the unconditional pin keeps pvl-core clear of it
  either way.
- **Downstream evaluation** ([markdown-vault-mcp#1602]): markdown-vault-mcp under OpenCode with
  `search` on, measuring task success on a fixed script; the numbers in
  §2.3 are listing sizes, not task outcomes.

## 9. Limitations of this study

- Client observations are one connection each from Claude Code 2.1.280,
  OpenCode 1.18.18 and Codex CLI 0.154.0 on one machine; OpenCode's and
  Codex's model turns failed on account state after the listing, so only
  their connect behaviour is observed. Claude Desktop and claude.ai were
  not run.
- FastMCP probes ran on fastmcp 4.0.0 under CPython 3.10.20 and 3.14.5
  and on fastmcp 4.0.5 under CPython 3.11.14; the 2026-09-25 re-runs on
  `main` at edc991e and the PR 5263 branch at cf970fb were under CPython
  3.11.14 only. Behaviour agreed on all; the proxy's listing size differs
  on 3.10 (recorded). The client
  identity probe (`idserver.py`) and the in-transform identity probe
  (`probe_a.py`) ran on 4.0.0 only.
- Elicitation through the proxy on a modern connection is unverified
  because the in-memory client rejected it for direct calls too.
- Relevance was measured with four queries on one server. A family-wide
  vocabulary test belongs to the downstream evaluation.
- No token counts are exact; bytes divided by four is the estimate used
  throughout.

[#300]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/300
[#294]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/294
[#299]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/299
[#362]: https://github.com/pvliesdonk/fastmcp-pvl-core/issues/362
[fastmcp#5260]: https://github.com/PrefectHQ/fastmcp/issues/5260
[fastmcp#5261]: https://github.com/PrefectHQ/fastmcp/issues/5261
[fastmcp#5267]: https://github.com/PrefectHQ/fastmcp/issues/5267
[fastmcp-pr-5262]: https://github.com/PrefectHQ/fastmcp/pull/5262
[fastmcp-pr-5263]: https://github.com/PrefectHQ/fastmcp/pull/5263
[markdown-vault-mcp#1602]: https://github.com/pvliesdonk/markdown-vault-mcp/issues/1602
[`docs/reference/fastmcp-search-transform.md`]: ../reference/fastmcp-search-transform.md
[`docs/reference/mcp-client-tool-discovery.md`]: ../reference/mcp-client-tool-discovery.md
