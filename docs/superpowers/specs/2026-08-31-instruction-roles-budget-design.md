# Instruction roles and client-compatibility budgets (issue #299)

**Date:** 2026-08-31
**Issue:** pvliesdonk/fastmcp-pvl-core#299
**Related:** pvliesdonk/fastmcp-pvl-core#294, #300
**Downstream evidence:** pvliesdonk/markdown-vault-mcp#1252, #1253
**Template adoption:** pvliesdonk/fastmcp-server-template#553

---

## Problem

`InstructionsBuilder` 5.x made server instructions composable and tool-aware,
but its one-dimensional priority order does not distinguish why a piece of
text exists:

```text
IDENTITY < DOCS < CAPABILITIES < WORKFLOWS < INSTANCE < OPERATOR
```

That order is coherent as a document outline. It is wrong as a survival order
when a client keeps only a prefix. Claude Code documents and enforces a limit
of 2,048 JavaScript string units per server instruction string. In
`markdown-vault-mcp`, generic workflow prose fills that prefix while the
READ-ONLY/READ-WRITE fact and `INSTRUCTIONS_EXTRA` begin after it. The client
therefore discards the information that differs between deployments and keeps
the prose shared by every deployment.

The current identity is also product identity only. A template contributes a
constant such as `Generic markdown vault MCP with hybrid search` while the
configured server name (`work-vault`, `corpus-mcp`) is carried elsewhere. A
model connected to both receives identical instruction identities.

Finally, `INSTRUCTIONS_EXTRA` is an undifferentiated escape hatch. Operators
use "deployment context" for at least two different purposes:

- routing: which instance contains the requested material;
- policy: how the model should behave after it chose that instance.

Routing and policy need different names, ownership, and ordering. Moving the
existing tail earlier without defining those semantics would repair one
observed truncation while preserving the ambiguity that caused it.

## Decision summary

1. Replace public raw priority anchors with semantic `InstructionRole` values.
2. Render roles in survival order: deployment identity, routing, enforced
   instance facts, operator policy, capabilities, workflows, documentation.
3. Shape deployment identity in pvl-core from the configured server name plus
   the product description.
4. Add `{PREFIX}_INSTANCE_DESCRIPTION` for concise operator routing context.
5. Retain `{PREFIX}_INSTRUCTIONS_EXTRA`, but define it as operator policy and
   place it before generic capabilities and workflows.
6. Keep legacy `{PREFIX}_INSTRUCTIONS` as a deprecated full replacement for
   this change; do not combine its removal with the role migration.
7. Measure instruction size in UTF-16 code units. Target at most 1,536 units
   for generated guidance and reserve 512 units for operator routing/policy.
8. Never truncate server-side and never fail startup only for exceeding a
   client-specific budget. Warn with a role-level breakdown instead.
9. Ship the public API and ordering change in pvl-core 6.

## Goals

- The first line distinguishes two deployments of the same product.
- Routing and enforced constraints survive ahead of generic usage prose.
- Operator routing and operator policy have separate contracts.
- A known client limit is visible in tests and startup logs.
- Clients without the limit still receive the complete instructions.
- Tool-dependent snippets remain prunable against an authoritative
  availability oracle.
- The design leaves room for tools that are indirectly discoverable in a
  future catalog mode without claiming that work is solved here.

## Non-goals

- Choosing or implementing FastMCP Tool Search or CodeMode (#300).
- Reducing any downstream's particular instruction wording.
- Budgeting tool descriptions, input schemas, prompt descriptions, or resource
  descriptions (markdown-vault-mcp#1253).
- Making MCP initialize instructions vary per subject or request. FastMCP
  exposes one server instruction string.
- Fixing client-side truncation or its user interface.
- Removing legacy `INSTRUCTIONS`; that remains a separate deprecation decision.

## Role model

### Public enum

The public ordering mechanism becomes a non-numeric enum, not caller-chosen
integers:

```python
class InstructionRole(Enum):
    IDENTITY = "identity"
    ROUTING = "routing"
    INSTANCE = "instance"
    POLICY = "policy"
    CAPABILITIES = "capabilities"
    WORKFLOWS = "workflows"
    DOCUMENTATION = "documentation"
```

The implementation owns a private role-to-order map. Public code passes enum
members, which provide neither numeric comparison nor arithmetic such as
`IDENTITY + 10`.

Ties keep insertion order. This retains the useful property of the 5.x
builder: contributors do not need to know one another, and helpers can add
fragments in any registration order as long as they choose the correct role.

### Why roles replace priorities

Raw priorities let a downstream put prose anywhere, but they also make role
ownership and budget diagnostics unknowable. The code cannot say "operator
policy crossed the compatibility boundary" if it only sees priority 347.

The family has seven legitimate placements. A new placement is a shared shape
decision and should require changing pvl-core, not choosing an unused integer
in one downstream. This follows pvl-core's framing principle: shape decisions
live centrally, while downstream supplies only domain text.

### Role definitions

| Role | Owner | Meaning | Tool dependencies |
|---|---|---|---|
| `IDENTITY` | pvl-core shape, template values | Which deployed server and product this is | forbidden |
| `ROUTING` | operator | Which data/domain this deployment serves | forbidden |
| `INSTANCE` | domain/core | Enforced configuration facts and limits | allowed only when the fact depends on the named tools |
| `POLICY` | operator | Deployment-specific behavioral policy | forbidden |
| `CAPABILITIES` | domain/core | What classes of work the server can perform | allowed |
| `WORKFLOWS` | domain/core | How multiple tools compose | allowed |
| `DOCUMENTATION` | template/core | Where full documentation lives | forbidden |

The table is normative. Examples:

- `work-vault: Generic markdown vault MCP with hybrid search` is identity.
- `Contains projects, meetings, and operational notes.` is routing.
- `This instance is READ-ONLY.` is an enforced instance fact.
- `New notes use PARA and go under Projects/.` is operator policy.
- `Search supports keyword and semantic retrieval.` is a capability.
- `Poll get_job_result after a promoted long-running call.` is a workflow.
- `Full documentation for this server: ...` is documentation.

## Public builder API

### Identity

Identity becomes a core-shaped two-value call:

```python
builder.identity(server_name: str, product_description: str) -> None
```

It emits exactly:

```text
<server_name>: <product_description>
```

Both values are stripped and must be non-empty. Exactly one identity remains
mandatory. Newlines in either value are rejected so the identity remains one
line. It carries no tool dependencies and cannot be pruned.

The configured name is an argument rather than a pvl-core environment read.
The template already resolves `{PREFIX}_SERVER_NAME` to construct `FastMCP`
and must pass the same value here. This preserves parameterized identity and
prevents a second environment reader from diverging.

### Semantic addition

The general contribution method becomes:

```python
builder.add(
    text: str,
    *,
    role: InstructionRole,
    requires_tools: Iterable[str] = (),
) -> None
```

`requires_tools` replaces the ambiguous `tools` name. It means the entire
fragment becomes wrong when any required tool is unavailable. Mentioning a
tool does not by itself make that tool required.

The general method accepts only `INSTANCE`, `CAPABILITIES`, and `WORKFLOWS`.
The other roles are reserved:

- `IDENTITY` is added through `identity()` so pvl-core controls its shape and
  cardinality;
- `ROUTING` and `POLICY` are added only by finalization from their operator
  environment variables;
- `DOCUMENTATION` is added through `documentation()` so pvl-core controls its
  sentence shape.

Passing a reserved role raises `ConfigurationError`. The shaped methods and
operator finalization path expose no `requires_tools` parameter, so dependency
declarations are structurally unavailable for roles whose table entry forbids
them. These constraints make the ownership table enforceable rather than
documentary.

Convenience methods remain deliberately narrow:

```python
builder.documentation(url: str) -> None
```

It adds the existing fixed sentence at `DOCUMENTATION`. No convenience method
is added for every role; `add(role=...)` is explicit enough, while identity and
documentation have core-owned text shapes.

### Compatibility aliases

The 5.x integer constants and `add(priority=..., tools=...)` are removed in
pvl-core 6 rather than retained as a second ordering system. The migration is
mechanical and all known consumers are template-managed. Keeping both would
make role-level accounting advisory and let new code recreate the ambiguity.

## Operator environment contract

### `SERVER_NAME`

`{PREFIX}_SERVER_NAME` remains template-owned. It names the live MCP server and
now also supplies the first value to `builder.identity()`.

This is deployment identity, not routing prose. A concise name such as
`work-vault` is useful even when no additional description is configured.

### New: `INSTANCE_DESCRIPTION`

`{PREFIX}_INSTANCE_DESCRIPTION` is a concise routing description answering:

> What material or responsibility distinguishes this deployment from another
> instance of the same server product?

Examples:

```text
Contains projects, meetings, and operational notes.
Curated writing-craft corpus and workshop references.
Production paper archive for the Materials programme.
```

`finalize_instructions` reads it, strips it, and adds a `ROUTING` fragment when
non-empty. It is not a `ServerConfig` field: like the two existing instruction
variables, it belongs to the final instruction composition contract and has
one reader.

The name is `INSTANCE_DESCRIPTION`, not `ROUTING_CONTEXT`. Operators configure
an instance; "routing" describes why the model consumes the value, not the
concept an operator is expected to name.

### `INSTRUCTIONS_EXTRA`

`{PREFIX}_INSTRUCTIONS_EXTRA` remains supported but its contract narrows to:

> Additional operator policy that changes how the model should use this
> deployment after selecting it.

It is added at `POLICY`, before generic capabilities and workflows. It is no
longer documented as text "appended" to the generated instructions.

Existing routing text stored in `_EXTRA` still arrives and moves earlier, so
the migration does not discard it. Operators should move such text to
`INSTANCE_DESCRIPTION` to make intent explicit. Existing behavioral guidance
can remain in `_EXTRA` unchanged.

No new `INSTRUCTIONS_POLICY` variable is introduced. Renaming the variable
would add migration cost without improving the model-facing result; the new
role and documentation provide the missing semantics.

### Legacy `INSTRUCTIONS`

`{PREFIX}_INSTRUCTIONS` remains a deprecated full replacement. It bypasses the
identity cardinality check, role composition, tool pruning, generated-target
warning, `INSTANCE_DESCRIPTION`, and `INSTRUCTIONS_EXTRA`, exactly because a
full replacement is complete operator ownership.

Two behaviors change:

1. The final 2,048-unit compatibility warning also applies to the replacement.
2. When legacy replacement is set alongside `INSTANCE_DESCRIPTION` and/or
   `INSTRUCTIONS_EXTRA`, the existing deprecation warning names every ignored
   variable.

Removal is not bundled into pvl-core 6. The role migration is already a public
breaking change, but sharing a major version is not a reason to combine an
independent operator migration.

## Rendering and finalization

On the non-legacy path, finalization performs these steps:

1. Require exactly one `IDENTITY` contribution.
2. Obtain the authoritative globally available tool set (the #294 design).
3. Read `INSTANCE_DESCRIPTION` and `INSTRUCTIONS_EXTRA` and create their
   `ROUTING` and `POLICY` fragments.
4. Drop a fragment when any `requires_tools` entry is unavailable.
5. Sort by `(role, insertion sequence)`.
6. Join fragment text with one blank line.
7. Measure generated and final UTF-16 units and emit compatibility warnings.
8. Set `mcp.instructions`, cache the text, and freeze the builder.

"Generated" means all retained fragments except the two operator fragments
read from `INSTANCE_DESCRIPTION` and `INSTRUCTIONS_EXTRA`. Identity, instance
facts, capabilities, workflows, and documentation all count as generated.

The builder still returns `str` from `finalize_instructions`. Size accounting
is observable through logs and pure public measurement helpers; it does not
widen the return contract or wrap the model-facing text in metadata.

Finalization remains synchronous and runs during synchronous server
construction, before entering an event loop. pvl-core evaluates FastMCP's
asynchronous global listing path with a private event loop. Calling finalization
from an active event loop raises `RuntimeError`; it does not move potentially
loop-affine providers to another thread and risk silently pruning valid tools.

## Compatibility budget

### Unit

Claude Code's boundary follows JavaScript `String.length`, which counts UTF-16
code units rather than Unicode code points or UTF-8 bytes. Python `len()` is
therefore insufficient for astral characters.

pvl-core exports:

```python
CLAUDE_CODE_INSTRUCTIONS_LIMIT_UTF16 = 2_048
GENERATED_INSTRUCTIONS_TARGET_UTF16 = 1_536

def utf16_code_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
```

The client name in the first constant is deliberate. The MCP protocol does not
define this limit, and pvl-core must not present a Claude Code policy as a
protocol limit.

### Two thresholds

1. **Generated target: 1,536 units.** This reserves 512 units (25% of the known
   limit) for `INSTANCE_DESCRIPTION`, `INSTRUCTIONS_EXTRA`, and the blank-line
   separators they add.
2. **Final compatibility limit: 2,048 units.** This is the known Claude Code
   per-server boundary.

The 512-unit reserve is a family-wide engineering target, not a promise that
arbitrary operator text fits. Operator text is unbounded. The reserve provides
room for the intended concise routing sentence and a small policy paragraph;
the final warning covers larger values.

### Warning, not truncation or failure

pvl-core emits a `WARNING` when either threshold is exceeded:

```text
instructions_generated_budget_exceeded units=... target=1536 crossing_role=...
instructions_client_budget_exceeded client=claude-code units=... limit=2048 crossing_role=...
```

Exact message wording is an implementation detail, but each record must carry:

- measured units;
- threshold;
- generated/final phase;
- client name for the final compatibility warning;
- the first role whose cumulative end crosses the threshold;
- UTF-16 units contributed by every retained role, excluding separators;
- total UTF-16 units contributed by blank-line separators;
- the environment prefix so an operator can locate the relevant variables.

Generated accounting renders retained non-operator fragments by themselves, so
its separator count does not include gaps around omitted operator fragments.
Final accounting follows the actual rendered order. For a legacy full
replacement, `crossing_role=legacy_override`, the role breakdown contains only
that synthetic role, and the separator count is zero.

pvl-core does not truncate. A client with no such limit should receive all
guidance, and server-side truncation would make lost text invisible in the MCP
initialize response itself.

pvl-core does not fail startup. Exceeding a client-specific context policy is
not a server correctness or security failure, and an instruction wording
change must not take down a deployment. There is no strict-mode environment
variable; CI is the strict path for generated text.

### Downstream test contract

Every downstream constructs its largest generated instruction configuration
with `INSTANCE_DESCRIPTION`, `INSTRUCTIONS_EXTRA`, and legacy `INSTRUCTIONS`
unset and asserts:

```python
assert utf16_code_units(server.instructions) <= GENERATED_INSTRUCTIONS_TARGET_UTF16
```

The matrix includes every feature that contributes snippets, not merely the
default stdio server. For markdown-vault this includes at least read-write,
OKF, conventions, summarization, jobs, and HTTP transfer guidance.

A second test sets representative routing and policy strings and asserts the
complete result remains within `CLAUDE_CODE_INSTRUCTIONS_LIMIT_UTF16` and that
the critical role order is preserved.

The checked-in target is an upper bound, not a golden exact length. Better
wording may reduce it without fixture churn. A proposed increase above the
target requires changing this shared design, not only relaxing one downstream
test.

## Tool availability and #294

`requires_tools` means "all these tools are globally reachable under the
server's current catalog and visibility configuration." In direct mode, that
is the effective FastMCP listing/call surface. pvl-core obtains it from
FastMCP's own listing path after provider, mount, namespace, and ordered
visibility transforms rather than reconstructing those rules. Listing runs in
an stdio transport context so component auth does not turn one static string
into an unauthenticated caller's view.

The role design does not solve per-subject authorization. One static
instruction string cannot reflect a catalog that differs by authenticated
subject. Guidance for such a tool must remain conditional in its wording.

The availability lookup is an internal collaborator of finalization, not a
downstream-supplied set or callback. pvl-core owns it so downstream cannot
invent visibility semantics.

### Future indirect discovery

#300 may introduce a catalog where original tools are absent from direct
`tools/list` but reachable through a search/proxy tool. That creates two
availability classes:

- directly visible;
- indirectly discoverable and callable.

This design does not add that mode pre-emptively. It requires only that #294's
availability computation live behind one pvl-core-owned internal seam rather
than being reconstructed inside the role renderer. #300 can then extend the
oracle and, if needed, add an explicit reachability requirement to snippets
without changing role ordering or operator semantics.

## Versioning

This ships in pvl-core 6 because it changes the public library interface:

- raw integer anchors are removed;
- `InstructionsBuilder.add(priority=..., tools=...)` becomes
  `add(role=..., requires_tools=...)`;
- `identity(text)` becomes `identity(server_name, product_description)`;
- rendered ordering changes;
- `INSTRUCTIONS_EXTRA` moves from a tail append to the `POLICY` role.

The new `INSTANCE_DESCRIPTION` variable alone would be additive. It does not
make the complete change non-breaking.

The MCP tool surface is not the reason for the major; pvl-core's importable
Python API and operator-visible instruction semantics are.

## Migration

### pvl-core

1. Land #294 or an equivalent effective-visibility oracle.
2. Add `InstructionRole`, the new builder signatures, measurement constants,
   UTF-16 helper, and warnings.
3. Move transfer and job contributions to `role=WORKFLOWS` with
   `requires_tools=`.
4. Update tests, README, exports, and the instruction builder specification.
5. Release as pvl-core 6.

### fastmcp-server-template

Tracked by fastmcp-server-template#553:

1. Raise the pvl-core floor to `>=6,<7`.
2. Call `identity(server_name, product_description)`.
3. Add `INSTANCE_DESCRIPTION` to configuration presentation, generated docs,
   Docker/package manifests, and smoke tests.
4. Rewrite `_EXTRA` documentation from "appended context" to operator policy.
5. Keep legacy replacement documentation and its deprecation warning.
6. Publish a copier release with explicit downstream migration notes.

### Downstream family

Every template consumer adopts through `copier update`; no downstream forks
the builder shape. Known consumers currently include:

- `markdown-vault-mcp`;
- `scholar-mcp`;
- `image-gen-mcp`;
- `paperless-mcp`;
- `logodev-mcp`;
- `openapi-mcp`.

Projects still on pvl-core 4 first adopt the composed-instructions template;
they do not need a bespoke 4-to-6 compatibility path in pvl-core.

`markdown-vault-mcp` additionally:

1. maps its prelude and search summary to `CAPABILITIES`;
2. maps read-only mode, conventions, OKF mode, and summarize limit to
   `INSTANCE` where they are enforced/configured facts;
3. maps write/index, transfer, and job sequences to `WORKFLOWS`;
4. compacts the maximal generated matrix to 1,536 UTF-16 units;
5. verifies distinct `SERVER_NAME` values produce distinct prefixes;
6. closes #1252 only after the mode and representative operator context fit.

## Testing

### Unit

- private render order and role cardinality;
- identity fixed shape and blank-value rejection;
- identity newline rejection;
- every reserved role rejected through general `add`;
- insertion order within each role;
- pruning by `requires_tools` using the #294 oracle;
- `INSTANCE_DESCRIPTION` and `_EXTRA` unset, blank, set, and both set;
- legacy replacement ignores both operator fragments and names both in its
  warning;
- UTF-16 helper covers BMP and astral characters;
- generated-target and final-limit warnings report units and crossing role;
- no warning at exactly 1,536 or 2,048 units;
- warning at one unit over each threshold;
- legacy replacement receives the final-limit warning;
- finalization remains frozen and idempotent.

### Integration

Build a real FastMCP server with:

- a configured server name distinct from its product description;
- one retained and one pruned workflow;
- routing and policy environment text;
- transfer and job contributions.

Assert through a real client initialize result that:

1. the first line is `<server_name>: <product_description>`;
2. routing, instance, and policy precede capabilities/workflows;
3. pruned workflow text is absent;
4. documentation is last;
5. the received string equals the finalized string;
6. representative text remains within 2,048 UTF-16 units.

### Template

Rendered smoke tests assert:

- `SERVER_NAME` changes both FastMCP name and instruction identity;
- `INSTANCE_DESCRIPTION` appears in routing position;
- `_EXTRA` appears in policy position rather than at the tail;
- blank operator variables are absent;
- legacy replacement still wins and logs every ignored additive variable.

## Documentation

- pvl-core README: role table, builder example, operator variable semantics,
  and compatibility warnings.
- Existing 2026-08-25 InstructionsBuilder design: mark as superseded for
  ordering/public API by this document while retaining its historical 5.x
  rationale.
- Template configuration reference and deployment guide: add
  `INSTANCE_DESCRIPTION`, revise `_EXTRA`, document the 512-unit operator
  reserve and warning behavior.
- Downstream operator docs: explain that `SERVER_NAME` identifies the deployed
  server, `INSTANCE_DESCRIPTION` routes among instances, and `_EXTRA` carries
  policy.

## Rejected alternatives

### Only shorten markdown-vault prose

This recovers the current tail but leaves deployment-specific content in the
least-survivable roles. The next workflow contribution recreates the bug.

### Move `INSTANCE` and `OPERATOR` earlier but keep raw priorities

This fixes ordering but not semantics or diagnostics. `INSTRUCTIONS_EXTRA`
would remain overloaded, and arbitrary offsets would prevent reliable
role-level budget reporting.

### Put all operator text in `SERVER_NAME`

Names are identifiers used by clients, logs, and tool namespaces. Turning the
name into prose harms those uses and still fails to distinguish routing from
policy.

### Infer routing from source paths or URLs

Paths are often identical inside containers (`/data/vault`), can be sensitive,
and describe storage rather than purpose. URLs may contain private deployment
details or credentials. Routing must be explicit operator text.

### Rename `_EXTRA` to `_INSTRUCTIONS_POLICY`

The semantic gain comes from the role and dedicated routing variable. A rename
adds operator migration without changing model-facing behavior. Keep the
existing variable and correct its documentation.

### Hard-truncate at 2,048 units

The limit is client-specific. Truncating in pvl-core destroys information for
clients that accept the full MCP field and hides the loss from protocol
inspection.

### Fail startup when over budget

Instruction size is not a server availability or security invariant. CI
strictly guards generated text; production logs operator-specific overflow.

### Add a configurable budget or strict-mode env var

No current operator needs a different server-side decision: pvl-core never
truncates, and the warning names the known client profile. More configuration
would not make an unbounded operator string fit.

### Enable Tool Search or CodeMode as the budget fix

Those transforms address tool catalogs, not initialize instructions, and have
separate approval, audit, metadata, task, and packaging questions tracked by
#300.

## Follow-ups

- #294: effective FastMCP visibility for `requires_tools` pruning.
- fastmcp-server-template#553: generated identity and environment adoption.
- markdown-vault-mcp#1252: downstream text migration and compatibility matrix.
- markdown-vault-mcp#1253 / #1010: total client-facing surface accounting and
  targeted schema-description reduction.
- #300: eager-client Tool Search/CodeMode study for the v5 family horizon.
- PrefectHQ/fastmcp#4952: parameterless docstring `Returns:` leakage.
