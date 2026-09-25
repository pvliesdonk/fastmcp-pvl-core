## Closes / Refs

Closes #N  (or `Refs #N` if not closing)

> **No orphan PRs.** Create the issue first if none exists. Pure typo fixes
> and automated dependency bumps excepted.

## What & why

One or two sentences, in observation terms: what changed, and why.

## What this PR deliberately does NOT do

List each deferral with its tracking issue number. A change that says what
it deliberately did not do is easier to trust than one that appears to have
found nothing.

- (deferral): #N

## Local review

- [ ] Ran a local self-review of the cumulative diff before `gh pr create`.
- [ ] The PR title is a valid conventional commit (it becomes the squash
      subject PSR versions from), and this body has no stray
      `BREAKING CHANGE:` line.
- [ ] Any `!` breaks a library, env-var, or wire-format surface that existed
      at the **last stable release** (see the breaking-change policy in
      `AGENTS.md`). An MCP tool-surface *shape* change alone does not earn
      a `!`.

## Shape and foldability

- [ ] Every new kwarg on a `register_*` helper, `Build*` factory, or
      middleware constructor passes the classification test in `AGENTS.md`,
      and its docstring names its category (hook / config / shape). No
      override kwargs.
- [ ] Intra-package imports are relative; no runtime lookup of pvl-core's
      own name; `__all__` changes only on purpose.
- [ ] A shared-shape change links its cutover issues on
      `fastmcp-server-template` and each affected downstream (or: no shape
      change).

## Docs impact

- [ ] `README.md`
- [ ] Docstrings
- [ ] `docs/adr/`, `docs/specs/`, `docs/reference/`, feature guides under `docs/`

**Rule: code without matching docs is incomplete.** `CHANGELOG.md` is
written by PSR at release; never edit it by hand.
