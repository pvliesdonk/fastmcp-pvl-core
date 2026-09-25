# Contributing

Thanks for contributing. This guide covers how to file good issues and pull
requests, and where to send different kinds of fixes. It applies to both
human contributors and automated agents. The design rules a change must
satisfy (the shape/hook classification test, foldability, the
breaking-change policy) live in [`AGENTS.md`](AGENTS.md); this file is the
process around them.

## Filing issues

Use the issue templates in `.github/ISSUE_TEMPLATE/`:

- **Bug report**: something isn't working as expected.
- **Feature request**: a new capability or enhancement.
- **Epic**: a multi-feature effort that ships as one user-facing story.
  See [Epics](#epics) below.
- **Decay / structural debt**: refactor-later observations.
- **Question / support**: questions and support requests.

Before filing, check that the issue belongs here at all (see
[Where to send fixes](#where-to-send-fixes)), then search this repo's
existing issues, open **and** closed, for the same observation. If it is
already on file, comment there rather than opening a duplicate.

The `authoring-issues-prs` skill (`.agents/skills/authoring-issues-prs/`)
walks this guide's routing and filing procedure and performs the follow-up
steps issue forms cannot (sub-issue links, labels). It points back at this
file; this file stays the single source of the rules.

### Observation, not work order

An issue records what was **observed**. It does not diagnose, design, or
prescribe a fix. An issue that reads like a work order misleads the
implementer into treating imagination as researched fact.

- Describe what you saw: the concrete behaviour, exact error text or trace,
  where it occurred, the version/commit you checked.
- Do not assert a root cause you did not verify.
- Do not propose an architecture or list implementation steps.

### The uncertainty rule

Every cause statement must be marked:

- `[verified: how]`: you checked; here is how.
- `[unverified]`: you have not verified this.

When you have not verified the cause, this sentence is required:

> I have not verified the cause.

The implementer must inherit your doubt, not a false floor of confidence.

### One issue, one observed problem

If you notice a second suspected problem while writing, do not add it to the
body. If you genuinely suspect it shares a code path, add one line under Open
Questions: `[unverified]: <suspected problem> may share this code path`. Open
a separate issue for it.

### Remove before posting

| What you wrote | What to do instead |
|----------------|-------------------|
| "Root cause is X; fix by doing Y" | Cause: `[unverified]` + observed behaviour only |
| Any sentence starting with "Fix by", "We should", "Refactor", "Add a", "The solution is" | Delete the sentence |
| "Import is probably similarly broken" | One Open Questions line: `[unverified]: import may share this path` |
| A cause asserted without a `[verified]` or `[unverified]` marker | Add the marker; add "I have not verified the cause" if unverified |
| An "Additional context" section that introduces new problems | Open a separate issue |
| Implementation steps (a numbered list of code changes) | Remove entirely |

### Epics

An epic is not just a bigger issue. It is the unit that answers two
questions nothing else answers: **what story does this tell a user**, and
**does it ship as a whole**. In this repo the user is usually a downstream
server author. File one with the Epic form and:

- **Write "What changes for the user" at epic creation**, before any code
  exists. It records the intent that the release is later checked against,
  instead of reconstructing it from merged PRs.
- **Link children as native GitHub sub-issues, not markdown task lists.**
  Sub-issues make the grouping queryable (parent, children, and progress
  are API fields); a checklist is prose. Issue forms cannot create the link
  at filing time; use the issue sidebar ("Create sub-issue" / "Add
  existing issue") or the sub-issues API after filing. The
  `authoring-issues-prs` skill performs this mechanically.
- **If the epic ships atomically** (no release may be cut mid-epic), apply
  the **`ships-atomically` label** to the epic and its children. The form's
  yes/no field records intent; only the label is what a release decision
  can query. Milestones in this repo group work by **theme**, not by
  release: PSR derives the version number at dispatch time, so there is no
  release name to commit to in advance. A theme milestone says nothing
  about atomicity either way.

Existing epics tracked as hand-written checklists need no migration.

## Pull requests

Every PR must have at least one associated issue. If the work has no issue
yet, a bug found in the wild or an opportunistic cleanup, create the issue
first, then open the PR with `Closes #N` (or `Refs #N`) in the body. A single
PR may close multiple issues (`Closes #A, closes #B`); the rule is "no orphan
PRs", not "one PR per issue". Trivial exceptions: pure typo fixes and
automated dependency bumps may skip the issue.

A squash merge takes the PR title as the commit subject and the PR body as
the commit body, and PSR computes the next version from both. The title must
be a valid conventional commit (see "Conventions" in `AGENTS.md`), and a
line starting `BREAKING CHANGE:` anywhere in the body cuts a major just as
`!` does. Mark a change breaking only under the breaking-change policy in
`AGENTS.md`, assessed against the **last stable release**, not the previous
commit.

Every new kwarg on a `register_*` helper, `Build*` factory, or middleware
constructor passes the classification test in `AGENTS.md`, and its docstring
names its category (hook / config / shape). A PR that changes a shared shape
links the cutover issues it filed on `fastmcp-server-template` and on each
affected downstream; the change ships here regardless of how large that
migration is.

State what the PR deliberately does **not** do, with each deferral's tracking
issue. A change that says what it left out is easier to trust than one that
appears to have found nothing.

Run a local self-review of the cumulative diff before `gh pr create`.
The `self-reviewing` skill (`.agents/skills/self-reviewing/SKILL.md`) is the
procedure, and it works with any coding agent. Code without matching docs is
incomplete; see "Documentation discipline" in `AGENTS.md` for the list.

## Releases

Merging is not releasing. A release is cut by a manual `workflow_dispatch` of
`.github/workflows/release.yml`, from `main`. The `ships-atomically` label
(see [Epics](#epics)) is the input to that decision: an open atomic epic with
unclosed children means the release waits until it lands, or goes out before
its first child merges.

## Where to send fixes

This repo is the **library tier** of the `pvliesdonk/*-mcp` family. The test
is which file a fix would change:

- **Library-level** (anything in this repository: `src/fastmcp_pvl_core/`,
  a `docs/specs/` protocol, and this repo's own tests, workflows, and
  docs): here. That includes a downstream that believes
  pvl-core behaves wrongly or diverges from a spec: file it here instead of
  reimplementing the behaviour downstream. Once released, a minor or patch
  reaches downstreams through their lockfile, within the
  `fastmcp-pvl-core>=X,<Y` range the template's `pyproject.toml.jinja`
  renders. A major also needs that range raised in the template.
- **Template-level** (a downstream's template-rendered files: its
  `Dockerfile`, workflows, `server.py` skeleton, generated `AGENTS.md`
  sections, anything `copier update` re-renders): open it on
  [`pvliesdonk/fastmcp-server-template`](https://github.com/pvliesdonk/fastmcp-server-template).
- **Domain-level** (one server's tools, resources, prompts, or domain
  logic): open it on that server's repo. Domain divergence between servers
  is expected and never lands here.

When a change here needs a matching template or downstream change, file the
cutover issues there and link them from the PR.
