---
name: authoring-issues-prs
description: >-
  Use when filing a bug, opening or creating a GitHub issue, drafting an
  epic or ticket, writing up a finding or review observation worth
  tracking, or opening a pull request for fastmcp-pvl-core. Routes the
  change to the right repo first (library / template / downstream),
  picks the right issue form, links epic children as native sub-issues,
  and applies CONTRIBUTING.md before anything is posted.
---

# Authoring issues and pull requests

`CONTRIBUTING.md` at the repository root is the single source for the rules:
issue voice, uncertainty markers, one-issue-one-problem, PR discipline, and
the routing. This skill adds only what a document cannot: the trigger, the
order of operations, and the API mechanics for the steps issue forms cannot
perform. It deliberately does not restate the rules; a second copy would
drift from the file silently.

## Procedure

### 1. Read CONTRIBUTING.md now, not from memory

Read `CONTRIBUTING.md` (repository root) before drafting a single sentence,
even if you believe you remember it. You are about to apply these sections,
and their exact wording matters:

- "Observation, not work order" and "The uncertainty rule": issue voice
  and the `[verified: how]` / `[unverified]` markers.
- "One issue, one observed problem" and the "Remove before posting" table:
  run your draft through the table before submitting.
- "Pull requests": no orphan PRs, the conventional-commit title, the
  classification test, cutover issues, the deliberately-does-not section.
- "Where to send fixes": the routing walked in step 2.

If anything in this skill appears to conflict with `CONTRIBUTING.md`, the
file wins.

### 2. Route before writing

Decide the repo first, then write for that repo. The test: **which file
would a fix change?**

1. Anything in this repository (`src/fastmcp_pvl_core/`, a `docs/specs/`
   protocol, this repo's own tests, workflows, and docs) → **library**:
   `pvliesdonk/fastmcp-pvl-core`. A downstream observation that pvl-core
   behaves wrongly belongs here too.
2. A downstream's template-rendered file (its workflows, `Dockerfile`,
   `server.py` skeleton, anything `copier update` re-renders) →
   **template**: `pvliesdonk/fastmcp-server-template`.
3. One server's tools, resources, prompts, or domain logic →
   **downstream**: that server's repo.

A pvl-core shape change usually needs all three: the change here, plus a
cutover issue on the template and on each affected downstream. File each on
its own repo and link them from the PR. When the tier is genuinely unclear,
say so in the issue with an `[unverified]` marker instead of guessing
silently.

### 3. Search for duplicates

Search the target repo's issues, open **and** closed, before filing:

```bash
gh issue list --repo OWNER/REPO --state all --search "<key terms>"
```

Match on the observation, not the wording. If the problem is already on
file, comment on the existing issue rather than opening a twin; if a closed
issue shows it regressed, say that in the new issue and link it.

### 4. Pick the form

Forms live in `.github/ISSUE_TEMPLATE/` of the target repo. For this repo:

| You have | Form | Label |
|----------|------|-------|
| Something not working as expected | `bug-report.yml` | `bug` |
| A capability that's missing | `feature-request.yml` | `enhancement` |
| A multi-feature effort telling one user-facing story | `epic.yml` | `epic` |
| Structural decay worth refactoring later | `decay.yml` | `decay` |
| A question or support request | `question.yml` | `question` |

When filing via API/CLI rather than the web form, mirror the chosen form's
section headings and apply its label so the issue is indistinguishable from
a form-filed one. Template and downstream repos use `feature` where this
repo uses `enhancement`; check the target repo's form.

### 5. For epics: finish what the form cannot do

Issue forms cannot create sub-issue links or apply labels conditionally.
After the epic is filed, perform these steps; this is the mechanical half of
the epic form's "After filing" checklist:

```bash
repo=pvliesdonk/fastmcp-pvl-core
epic=EPIC_NUMBER

# Per child. A cutover child lives on the template or a downstream, so
# look it up in ITS repo:
child_repo=pvliesdonk/fastmcp-pvl-core   # or the template / downstream repo
child=CHILD_NUMBER

# 1. Link it as a NATIVE sub-issue (the endpoint takes the child's
#    database id, not its issue number):
child_id=$(gh api "repos/$child_repo/issues/$child" --jq '.id')
gh api -X POST "repos/$repo/issues/$epic/sub_issues" -F "sub_issue_id=$child_id"

# 2. Ships atomically: label the epic once, and each child in its repo
#    (the label exists on the template and the downstreams too).
gh issue edit "$epic" --repo "$repo" --add-label ships-atomically
gh issue edit "$child" --repo "$child_repo" --add-label ships-atomically
```

Do not assign a release milestone: milestones in this repo are themes, not
releases, and neither carry nor replace the atomicity signal. Assigning a
theme milestone is a separate triage call.

Working through the GitHub MCP server instead: `sub_issue_write` (method
`add`) performs the same link, and `issue_read` returns `has_parent` /
`has_children` / `sub_issues_summary` to verify it took.

Never track children as a markdown task list in the epic body; the native
link is what can be queried.

### 6. Pull requests

Route first (step 2): the issue and the PR that closes it belong in the
same repo. Then follow `CONTRIBUTING.md`'s "Pull requests" section and the
repo's PR template (`.github/PULL_REQUEST_TEMPLATE.md`), every section,
including "What this PR deliberately does NOT do", the shape-and-foldability
checklist, and the docs-impact checklist.

### 7. Attribution footer

When you (an agent) author an issue or PR body, end it with an attribution
footer naming the agent that wrote it, in the form your agent's own
conventions prescribe; for example:

```markdown
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```
