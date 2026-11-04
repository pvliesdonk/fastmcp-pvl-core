# fastmcp-pvl-core roadmap

This is agent-authored synthesis, not a record of decisions. Each item is
tagged `stated` (the account holder's words, with the date), `derived`
(synthesis, revisable at any pass) or `evidenced` (with a locator). The
model of epics, packages and refinement is the family's, written down in
the template's
[`roadmapping` skill](https://github.com/pvliesdonk/fastmcp-server-template/blob/main/.agents/skills/roadmapping/SKILL.md);
read it before charting, refining or revisiting work here.

Every dependency between this repository, the template and the downstream
servers is kept once, in the template index's
[family dependency map](https://github.com/pvliesdonk/fastmcp-server-template/blob/main/docs/design/roadmap.md#family-dependency-map).
Check it before ordering work here. Most pvl-core changes only matter once
the template or a downstream adopts them.

GitHub holds membership, dependencies and progress. This index holds the
argument for the direction and the order.

## Direction

### Client, authentication and a non-MCP API

`stated` (3 November 2026, on #543): A possible split "at pvl-core level
with a premade client+authentication+(non-mcp) api". `stated` (4 November
2026): The FastAPI composition beside the tool bridge "sounds good"; "We
will not need it all in one bang, which especially holds for this last
part."

`derived`: Five epics carry it, in this order:

- #547 light import;
- #548 client package (two lockstep distributions);
- #549 HTTP composition, with research #555 in front;
- #550 tool bridge;
- #551 client auth.

Their bodies were trimmed to outcomes on 4 November 2026; the
implementation notes stay on the 3 November design comment on #543.

### One record per tool-call outcome: [#559](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/559)

`derived`: A refusal is logged twice, a caller's mistake is logged as a
server fault, and under masking the log keeps only the masked text.
Members today: #373, #380, #355.

"Done when", as frozen on the epic: every tool-call outcome produces one log record, at
the level its cause deserves, naming the real exception type even when the
client sees a masked message.

Unknown: none that changes the order. #355 must land before the template
masks error details by default (template #646).

### pvl-core's own tools have one shape: [#560](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/560)

`derived`: The tools pvl-core registers on a downstream can be overridden
silently, still carry override kwargs, and report one upstream only.
Members today: #356, #348, #321.

"Done when", as frozen on the epic: the tools pvl-core registers keep one fixed shape, a
downstream tool that reuses one of their names fails loudly, and the
server-info payload can report every upstream.

Unknowns:

- Whether the duplicate-name refusal lives here or in the template's
  constructor call. Resolved by template #645.
- Whether the multi-upstream need survives. scholar-mcp#384, the trigger
  for #321, has closed. Resolved by refinement.

This epic carries the first foreseen major (#356 removes public kwargs).

### Operators on mainstream identity providers: [#561](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/561)

`derived`: Machine callers under `oidc-proxy` have only static tokens,
claim checks cannot read nested or map claims, scope-gated components
cannot step up, and OAuth discovery under a subpath depends on upstream.
Members today: #354, #352, #353, #196.

"Done when", as frozen on the epic: an operator on a mainstream identity provider admits
machine callers with expiring tokens, authorises on nested or map claims,
and serves OAuth discovery under a subpath.

Unknowns:

- The claim-path syntax for #352, which also decides whether it is a
  major. Resolved by #352's design.
- `evidenced` (4 November 2026, probe recorded on #196): subpath OAuth
  discovery works since fastmcp 4.0.0, pvl-core's floor, so #196 closed.
- #353 waits on upstream; nothing here resolves it.

### Interaction primitives across protocol eras: [#562](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/562)

`derived`: Each server builds its own elicitation, request state lives
per process, and completion ignores component visibility. Members today:
#349, #350, #351.

"Done when", as frozen on the epic: a family tool asks the user for input the same way
on either protocol era, its state survives restarts and replicas, and
completion respects the visibility listing already applies.

Unknowns:

- Fail-open or fail-closed when a client cannot elicit (#349). Resolved by
  #349's design.
- #351 waits on PrefectHQ/fastmcp#5236; nothing here resolves it.

### Without an epic

`derived`: Each of these ships when a package picks it up:

- #527, a downstream Vale failure with a one-line fix;
- #340, a prose lint over the config help text;
- #362, the search tool catalog, blocked upstream. Its study, #300, has an
  ADR stranded on a branch.
- structural housekeeping: #230, #231.

## Packages

`stated` (4 November 2026): Ordinal packages in this repository: "it
cannot hurt". Releases still come from python-semantic-release, which reads
no milestone, so a package here is a planning commitment and not a release
gate.

| Package | Kind | Argument |
|---|---|---|
| [`005 downstream-fixes`](https://github.com/pvliesdonk/fastmcp-pvl-core/milestone/6) | patch | First: #527 fails a downstream's CI today, and #355 must precede the template's `030` (template #646). It also carries #373 and #380. |
| [`010 light-import`](https://github.com/pvliesdonk/fastmcp-pvl-core/milestone/3) | minor | Independent of every other decision, and it removes an import cost downstreams pay today. It also carries #354, the service-case prerequisite of #551. |
| [`020 client-package`](https://github.com/pvliesdonk/fastmcp-pvl-core/milestone/4) | minor | After the template's `050 workspace-roles` proves lockstep in a real release. Its first PR settles this repository's lockstep tooling, which the HTTP epics inherit. |
| [`030 http-composition`](https://github.com/pvliesdonk/fastmcp-pvl-core/milestone/5) | minor | Research #555 first: it is the largest technical unknown here and decides the bridge's mechanism. |

`derived`: Client auth (#551) follows as its own minor. The tool-shape
epic's major gets a package when it is refined, so that it collects any
other breaking change with it.

## Known unknowns

- Python-semantic-release or knope for lockstep versions. Resolved by
  #548's first PR.
- How a non-MCP route reuses the configured auth provider and request
  context. Resolved by #555 (half a day).
- `stated` (4 November 2026): The template/pvl-core one-repository
  document is undecided. Resolved by the account holder. It changes
  nothing before `020`, and #548 notes the effect on its own refinement.

## Revisions

### 4 November 2026: index created

Charted from a triage of every open issue here and in the template. Three
corrections and proposals:

- **Corrected provenance.** On #543, "the HTTP composition comes last"
  was tagged `stated`; it is `derived` from "this last part".
- **Proposed trims.** The bodies of #549, #550 and #551 name routes,
  variable names and class names, and #551's "Done when" reads as a
  configuration spec. Proposed: trim them to outcomes, and move the
  detail to the 3 November design comment on #543 that already holds it.
- **Proposals awaiting the account holder.** Four epics above are
  proposed, not filed, and so is package `005`. The milestone wording in
  `CONTRIBUTING.md` changes with this index.

### 4 November 2026: epics filed, triage applied

`stated` (4 November 2026): "file the epics and create 005, apply the
rest".

- **Epics filed.** The four proposed epics are #559 to #562, with
  refinement issues #563 to #566. Their existing issues are linked as
  sub-issues.
- **Package `005`.** Approved, carrying #527, #355, #373 and #380.
- **Trims.** The bodies of #549, #550 and #551 were trimmed to outcomes;
  their implementation notes stay on the 3 November design comment on
  #543.
- **Triage applied.** #196 closed after a probe, #232 and #60 closed,
  #230 and #231 retyped to `decay` and #231 retargeted, #300 retyped to
  `research` (its ADR still needs landing), and #321's trigger flagged
  for refinement.
