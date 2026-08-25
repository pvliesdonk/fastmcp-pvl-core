# `InstructionsBuilder` — composable server instructions (issue #283)

## Problem

`build_instructions(env_prefix, domain_line)` returns one finished string, and
every consumer (template `server.py.jinja`, markdown-vault-mcp, scholar-mcp,
image-generation-mcp) wraps it as:

```python
instructions = env(_ENV_PREFIX, "INSTRUCTIONS") or build_instructions(...)
```

So `{PREFIX}_INSTRUCTIONS` is **full replacement**: an operator who wants to
add one sentence of deployment context loses every generated line and must
hand-maintain a copy that silently stales. markdown-vault-mcp generates ~600
words of config-gated guidance (write-tool workflow, conventions, OKF,
summarize limits) and had to grow its own `config.instructions or
build_default_instructions(...)` re-implementation to cope — the "invented
semantics" #283 describes.

Two further defects in the current shape:

- `build_instructions` appends *"Operators: set `{PREFIX}_INSTRUCTIONS` …"* to
  the **model-facing** text. The model cannot act on it; it is dead tokens on
  every session.
- pvl-core features that register tools (`register_transfer_routes`,
  `register_long_running_tool`) have no way to tell the model how their tools
  compose. Downstream hand-writes prose about pvl-core's own behaviour.

## What belongs in instructions

MCP `instructions` is a session-level hint to the model, spliced into the
system prompt by most clients and sent **once**, in the `initialize` result.
Tool descriptions already carry per-tool semantics. The governing principle:

> **Instructions carry what no single tool description can carry.**

| Category | Belongs | Example |
|---|---|---|
| Identity | yes — exactly one line | "A searchable markdown document vault." |
| Documentation | yes | "Full documentation for this server: `<llms.txt URL>`." |
| Capability map | yes | "Reach for X when …" — matters because clients may defer or hide the tool list, so this is the only reliable overview |
| Cross-tool workflows | yes | "`write` updates the index; never call `reindex` after it." |
| Instance facts | yes, **only when enforced** (#222) | read-only, OKF mode, conventions filename, summarize batch limit |
| Operator context | yes | "This vault uses PARA; new notes go under `Projects/`." |
| Single-tool caveats | **no** — tool description | "`browse_vault` opens a UI, do not use it to fetch content" |
| Per-tool parameters | **no** — tool description | |
| Operator-facing hints | **no** — docs | "set `X_INSTRUCTIONS` to …" |
| Unenforced capability claims | **no** | a read-only announcement the server does not enforce |

Consequence: a core feature contributes a snippet **only if it has a
cross-tool workflow**. `register_server_info_tool` contributes nothing.

## Design

### Public API

New module `_instructions.py`; re-exported from the package root. The old
`build_instructions` is **removed** (major bump).

```python
IDENTITY, DOCS, CAPABILITIES, WORKFLOWS, INSTANCE, OPERATOR = 0, 100, 200, 300, 400, 500

def instructions_for(mcp: FastMCP) -> InstructionsBuilder
def finalize_instructions(mcp: FastMCP, env_prefix: str) -> str

class InstructionsBuilder:
    def add(self, text: str, *, priority: int, tools: Iterable[str] = ()) -> None
    def identity(self, text: str) -> None          # add(text, priority=IDENTITY)
    def documentation(self, url: str) -> None      # core-shaped sentence at DOCS
```

- **Priority is the mechanism; the constants are named anchors.** Snippets
  sort by `(priority, insertion order)`. A contributor that wants "just after
  the capability map" writes `CAPABILITIES + 10`.
- `tools` declares the tool names the snippet references. At finalize, a
  snippet is **dropped if any referenced tool is not exposed** (a workflow
  missing one step is worse than absent).
- `identity` and `documentation` exist so pvl-core owns the *shape* of those
  two lines while downstream supplies the domain value (identity text, docs
  URL). Both are thin wrappers over `add`.

### One builder per server

`instructions_for(mcp)` returns the builder for that `FastMCP` instance,
creating it on first use, held in a `WeakKeyDictionary` in `_instructions.py`.
`register_*` helpers already receive `mcp`, so they reach the builder with
**no new kwargs** — by `CLAUDE.md`'s classification test the builder is
plumbing, not a domain hook, and must not appear on helper signatures.

### Lifecycle in `make_server` (template-owned)

1. `mcp = FastMCP(name=..., lifespan=..., auth=...)` — no `instructions=`.
2. Domain calls `instructions_for(mcp).identity(...)` and its config-gated
   `add(...)` calls (read-only line, conventions, OKF, …) from its own module.
3. Core `register_*` helpers add their workflow snippets.
4. `apply_tool_visibility(mcp, config)`.
5. `finalize_instructions(mcp, _ENV_PREFIX)`.

Steps 2–3 may run in any order. Only step 5's position matters: it must
follow visibility.

### `finalize_instructions` — in order

1. **Exposed tools** = names of `Tool` components in
   `mcp.local_provider._components` with `enabled` true (the enumeration
   `_icons.py` already uses; same `RuntimeError` on API drift).
2. **Prune**: drop every snippet whose `tools` are not all exposed. `DEBUG`
   log per drop, naming the missing tool.
3. **Identity**: exactly one snippet at priority `IDENTITY` must remain, else
   `ConfigurationError`. (Zero: the server has no identity. Two or more: two
   contributors both claimed it.)
4. **Serialize**: sort by `(priority, insertion)`; join snippet texts with a
   blank line. Plain prose, **no markdown headings** — most clients splice the
   text into a system prompt, headings cost tokens and render inconsistently.
5. **Env contract** (see below).
6. Set `mcp.instructions`, cache the result, freeze the builder.

Idempotence: a second `finalize` returns the cached string without
re-reading env or re-logging. `add` after finalize raises `RuntimeError`.

### Env contract (core-owned shape; documented in the template)

| Var | Semantics |
|---|---|
| `{P}_INSTRUCTIONS_EXTRA` | Appended as one snippet at `OPERATOR`. Whitespace-only counts as unset. |
| `{P}_INSTRUCTIONS` | **Legacy.** When set (non-whitespace), its value **replaces** the entire built text verbatim. `finalize` logs one `WARNING`: "`{P}_INSTRUCTIONS` replaces all generated guidance and is deprecated; use `{P}_INSTRUCTIONS_EXTRA` to add context." If `_EXTRA` is also set the warning adds that it was ignored. Removal in a later major. |

Keeping `INSTRUCTIONS` as full-replace is deliberate: changing its meaning
would be a breaking change for **every deployment**, whereas the pvl-core API
change is a breaking change only for downstream *code*, absorbed by a
`copier update`.

`env_prefix` is normalised as elsewhere (`rstrip("_")`).

### Errors

| Condition | Raised by | Type |
|---|---|---|
| `add` with empty / whitespace text | `add` | `ConfigurationError` |
| `add` after finalize | `add` | `RuntimeError` |
| no identity, or more than one | `finalize` | `ConfigurationError` |
| tool enumeration API missing | `finalize` | `RuntimeError` (as `_icons.py`) |

### Known limit: per-subject auth visibility

FastMCP filters `list_tools` per caller through component `AuthCheck`s
(`server.py`, `run_auth_checks(tool.auth, ctx)`), so a tool gated by
`make_acl_check` / `make_claims_check` is hidden from some subjects and
visible to others — within one process, from one instructions string. No
single-string design can follow that: `initialize` is built from the static
field, not per subject.

Rule: **pruning covers the process-lifetime exposed set** (registered ∧ not
operator-hidden), which is fully correct because nothing in the family
enables/disables tools after `apply_tool_visibility`. Per-subject visibility
is out of scope; instructions describe the server's full surface, and a
snippet about an auth-gated tool should read naturally when the tool is absent
("if `delete` is available, …").

## Core contributions

| Helper | Snippet (gist) | priority | `tools` |
|---|---|---|---|
| `register_transfer_routes` | Upload: call `create_upload_link`, then PUT the bytes to the returned URL; the link is single-use and expires. Download: call `create_download_link`, then GET. Links are capability URLs — do not reuse or share them. | `WORKFLOWS` | `{create_upload_link, create_download_link}` |
| first `register_long_running_tool` / `register_job_tools` | A long-running tool returns a job id when the client cannot run it as a task; poll `get_job_result` with that id until it completes rather than invoking the tool again. Added once per server. | `WORKFLOWS` | `{get_job_result}` |
| `builder.documentation(url)` | "Full documentation for this server: `<url>`." | `DOCS` | — |
| `register_server_info_tool`, `register_tool_icons`, `apply_tool_visibility` | nothing | | |

The exact wording is fixed in the implementation and covered by tests; the
table fixes intent and gating.

## Worked downstream example (markdown-vault-mcp, informative)

```python
b = instructions_for(mcp)
b.identity("A searchable markdown document vault. Paths are always relative.")
b.documentation("https://pvliesdonk.github.io/markdown-vault-mcp/llms.txt")
if not read_only:
    b.add("Write tools update the search index immediately; never call "
          "'reindex' after write, edit, append, delete, or rename.",
          priority=WORKFLOWS, tools={"write", "reindex"})
if conventions_file:
    b.add(f"Folders may carry conventions in '{conventions_file}'; call "
          "'get_conventions(path)' before creating or restructuring notes.",
          priority=INSTANCE, tools={"get_conventions"})
b.add("This instance is READ-ONLY." if read_only else "This instance is READ-WRITE.",
      priority=INSTANCE)
```

Its `config.instructions` field and post-construction
`mcp.instructions = ...` assignment go away; core owns the env contract.

## Testing

**Unit (`tests/test_instructions.py`)**

- ordering: priority then insertion; constants sort as documented
- pruning: absent tool → dropped; operator-hidden tool → dropped; snippet with
  no `tools` → kept; mixed exposed/hidden → dropped; `DEBUG` record names the
  missing tool
- identity: none → `ConfigurationError`; two → `ConfigurationError`; identity
  snippet itself never pruned (no tools)
- freeze: `add` after finalize → `RuntimeError`; second finalize returns the
  identical string and logs nothing new
- env matrix: unset / `_EXTRA` / legacy / both / whitespace-only for each;
  legacy warning emitted exactly once and names `_EXTRA` when it was ignored
- `env_prefix` with and without trailing underscore identical
- `documentation` produces the fixed sentence; `add` rejects empty text

**Integration (`tests/test_instructions_integration.py`)**

Build a real `FastMCP`, register a transfer route (with a stub sink) and one
long-running tool, set `tools_deny=create_upload_link`, apply visibility,
finalize. Assert: the job snippet is present, the transfer snippet is absent,
and the string read back through an in-memory `Client`'s `initialize` result
equals `mcp.instructions`.

## Out of scope

- Per-session or per-subject instructions (FastMCP has no hook).
- Re-sending instructions after startup.
- Migrating the downstreams (tracked per repo; template change under
  fastmcp-server-template#131's umbrella).
- Any change to tool descriptions.

## Downstream cutover

1. pvl-core ships this as a major (removes `build_instructions`).
2. Template: `FastMCP(...)` drops `instructions=`; the `env(...) or
   build_instructions(...)` line becomes `finalize_instructions(mcp,
   _ENV_PREFIX)` after `apply_tool_visibility`; `docs/configuration.md.jinja`
   documents `_EXTRA` and marks `INSTRUCTIONS` legacy.
3. Each downstream moves its identity/domain snippets into
   `instructions_for(mcp)` calls; mvm deletes `config.instructions`.
