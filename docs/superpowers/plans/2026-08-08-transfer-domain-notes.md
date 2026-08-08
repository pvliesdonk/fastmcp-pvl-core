# Transfer domain notes (Part B, issue #248) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional `download_note` / `upload_note` kwargs to `register_transfer_routes` that append a downstream domain sentence to core's generic tool descriptions without replacing them.

**Architecture:** A module-level `_augment(base, note)` helper composes the final description. The two tool functions are defined first (so their docstrings remain the single source of the base text) and `mcp.tool(...)` is applied as an explicit call rather than `@` syntax, because a nested closure cannot read its own `__doc__` at decoration time. When a note is absent the helper returns the base unchanged, so the resulting description is byte-identical to today's.

**Tech Stack:** Python 3.10+, fastmcp, pytest, ruff, mypy.

## Global Constraints

- Line length 88 (`ruff` config); `select = ["E", "F", "W", "I", "B", "UP", "N", "D"]`, google docstring convention.
- Intra-package imports stay **relative** (`from .x import y`) — foldability rule in `CLAUDE.md`.
- No runtime self-name lookups (no `importlib.metadata.version(...)` / `importlib.resources`).
- All existing tests must continue to pass unchanged: `uv run pytest` → 748 passed before this work.
- Local gate before pushing: `uv sync --all-extras`, `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`.
- Branch off `main`. One PR, closing #248.

## Verified Mechanics

These were confirmed against the installed fastmcp before this plan was written — do not re-derive them:

- `mcp.tool(description=X)` **overrides** the function docstring.
- `mcp.tool(description=None)` **falls back** to the docstring.
- `inspect.cleandoc()` dedents an indented docstring and preserves blank lines between paragraphs.
- Applying the decorator as a call — `mcp.tool(name=..., description=...)(fn)` — works identically to `@` syntax and allows reading `fn.__doc__` first.

---

### Task 1: The `_augment` helper

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py`
- Test: `tests/test_transfer_register.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_augment(base: str, note: str | None) -> str` — module-private in `register.py`. Returns `base` unchanged when `note` is `None`, empty, or whitespace-only; otherwise `base + "\n\n" + note.strip()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_transfer_register.py`, after the existing imports and before `class TestBaseUrlGuard`:

```python
from fastmcp_pvl_core._transfer.register import _augment


class TestAugment:
    """The description composer: append-only, absent-note-safe."""

    def test_none_note_returns_base_unchanged(self) -> None:
        assert _augment("BASE", None) == "BASE"

    def test_empty_note_returns_base_unchanged(self) -> None:
        assert _augment("BASE", "") == "BASE"

    def test_whitespace_only_note_returns_base_unchanged(self) -> None:
        # A note of only spaces/newlines is an operator typo, not content —
        # treat it as absent rather than emitting a trailing blank paragraph.
        assert _augment("BASE", "   \n  ") == "BASE"

    def test_note_appended_after_blank_line(self) -> None:
        assert _augment("BASE", "NOTE") == "BASE\n\nNOTE"

    def test_note_is_stripped(self) -> None:
        assert _augment("BASE", "  NOTE  ") == "BASE\n\nNOTE"

    def test_base_is_never_modified(self) -> None:
        # Append-only: the base must survive verbatim as a prefix, so a
        # downstream note can never rewrite or truncate core's text.
        base = "Multi\n\nparagraph\nbase."
        assert _augment(base, "NOTE").startswith(base)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transfer_register.py::TestAugment -v`
Expected: FAIL at collection — `ImportError: cannot import name '_augment'`

- [ ] **Step 3: Write minimal implementation**

In `src/fastmcp_pvl_core/_transfer/register.py`, add after the `_icon` helper and its two `_DOWNLOAD_ICON` / `_UPLOAD_ICON` assignments, before `_ROUTE_PATH`:

```python
def _augment(base: str, note: str | None) -> str:
    """Append a downstream domain *note* to core's *base* description.

    Append-only by construction: *base* is always the prefix of the result, so
    a note can add domain specifics but never rewrite or truncate core's
    generic text. A ``None``, empty, or whitespace-only note is treated as
    absent and *base* is returned unchanged — so an omitted note leaves the
    description byte-identical to a build without this feature.
    """
    if note is None or not note.strip():
        return base
    return f"{base}\n\n{note.strip()}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transfer_register.py::TestAugment -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/register.py tests/test_transfer_register.py
git commit -m "feat(transfer): add _augment description composer

Append-only helper: base text is always the result's prefix, and an
absent (None/empty/whitespace) note returns the base unchanged so an
omitted note is byte-identical to today's description."
```

---

### Task 2: Wire the note kwargs through `register_transfer_routes`

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py`
- Test: `tests/test_transfer_register.py`

**Interfaces:**
- Consumes: `_augment(base, note)` from Task 1.
- Produces: `register_transfer_routes(mcp, config, transfer_config, *, sink, validate, download_note: str | None = None, upload_note: str | None = None) -> None`. Return type is unchanged (`None`) in this task — Part A (#249) changes it to `TransferLinks`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_transfer_register.py`. First extend the existing `_register` helper to accept the notes — replace the current `_register` function with:

```python
def _register(
    *,
    base_url: str | None = "https://x.example.com",
    transfer_config: TransferConfig | None = None,
    sink: _RecordingSink | None = None,
    validate: _RecordingValidator | None = None,
    download_note: str | None = None,
    upload_note: str | None = None,
) -> tuple[FastMCP, _RecordingSink, _RecordingValidator]:
    mcp = FastMCP("t")
    sink = sink or _RecordingSink()
    validate = validate or _RecordingValidator()
    config = ServerConfig(base_url=base_url, kv_store_url="memory://")
    register_transfer_routes(
        mcp,
        config,
        transfer_config or _tconfig(),
        sink=sink,
        validate=validate,
        download_note=download_note,
        upload_note=upload_note,
    )
    return mcp, sink, validate
```

Then add this test class after the existing `TestToolRegistration` class:

```python
class TestDomainNotes:
    """The append-only ``download_note`` / ``upload_note`` hooks (#248)."""

    async def test_download_note_appended(self) -> None:
        mcp, _, _ = _register(download_note="Vault-relative path to a note.")
        tool = await mcp.get_tool("create_download_link")
        assert tool.description.endswith("\n\nVault-relative path to a note.")

    async def test_upload_note_appended(self) -> None:
        mcp, _, _ = _register(upload_note="Destination must be an allowed type.")
        tool = await mcp.get_tool("create_upload_link")
        assert tool.description.endswith("\n\nDestination must be an allowed type.")

    async def test_notes_do_not_cross_tools(self) -> None:
        # Each note lands on its own tool — a download note must not leak into
        # the upload description, which would state a rule that does not apply.
        mcp, _, _ = _register(download_note="DOWN", upload_note="UP")
        down = await mcp.get_tool("create_download_link")
        up = await mcp.get_tool("create_upload_link")
        assert down.description.endswith("\n\nDOWN")
        assert up.description.endswith("\n\nUP")
        assert "UP" not in down.description
        assert "DOWN" not in up.description

    async def test_core_base_text_survives_the_note(self) -> None:
        # Append-only: core's generic sentence is still present and first, so
        # every server's description shares an identical prefix.
        plain, _, _ = _register()
        noted, _, _ = _register(download_note="NOTE")
        base = (await plain.get_tool("create_download_link")).description
        augmented = (await noted.get_tool("create_download_link")).description
        assert augmented.startswith(base)

    @pytest.mark.parametrize("note", [None, "", "   "])
    async def test_absent_note_leaves_description_unchanged(
        self, note: str | None
    ) -> None:
        # The no-note path must be byte-identical to a build without this
        # feature — the whole point of the default being "absent", not "empty".
        plain, _, _ = _register()
        with_note, _, _ = _register(download_note=note)
        expected = (await plain.get_tool("create_download_link")).description
        assert (
            await with_note.get_tool("create_download_link")
        ).description == expected

    async def test_download_description_starts_with_core_sentence(self) -> None:
        # Pins that the docstring — not a duplicated constant — is the base.
        mcp, _, _ = _register(download_note="NOTE")
        tool = await mcp.get_tool("create_download_link")
        assert tool.description.startswith(
            "Mint a capability link that serves the bytes for *ref* once."
        )

    async def test_upload_description_starts_with_core_sentence(self) -> None:
        mcp, _, _ = _register(upload_note="NOTE")
        tool = await mcp.get_tool("create_upload_link")
        assert tool.description.startswith(
            "Mint a capability link that accepts one upload for *ref*."
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transfer_register.py::TestDomainNotes -v`
Expected: FAIL — `TypeError: register_transfer_routes() got an unexpected keyword argument 'download_note'`

- [ ] **Step 3: Write minimal implementation**

Three edits in `src/fastmcp_pvl_core/_transfer/register.py`.

**3a — add the two kwargs to the signature.** Replace the signature:

```python
def register_transfer_routes(
    mcp: FastMCP,
    config: ServerConfig,
    transfer_config: TransferConfig,
    *,
    sink: TransferSink,
    validate: TransferValidator,
    download_note: str | None = None,
    upload_note: str | None = None,
) -> None:
```

**3b — document them.** In that function's docstring `Args:` block, after the `validate:` entry, add:

```
        download_note: Optional domain sentence appended to
            ``create_download_link``'s description. Core's generic text is
            always kept and comes first; this only adds domain specifics
            (e.g. what a ``ref`` looks like for this server). Omitted, empty,
            or whitespace-only leaves the description exactly as core writes
            it. There is no way to *replace* core's text — that is a shape
            decision pvl-core owns.
        upload_note: The same for ``create_upload_link``. This is the one that
            usually matters: a download ``ref`` is something the caller has
            already seen (a search hit, a listing entry), but an upload ``ref``
            is *authored* by the caller, and nothing it has seen states the
            destination rules — so state them here.
```

**3c — convert both tools from `@` decoration to a call, reading the docstring as the base.** Replace the whole block from `@mcp.tool(` (the download one) through the end of `create_upload_link`'s body with:

```python
    async def create_download_link(
        ref: str, ttl_s: float | None = None
    ) -> dict[str, Any]:
        """Mint a capability link that serves the bytes for *ref* once.

        *ref* is a domain reference the ``validate`` hook resolves to an opaque
        download handle (raising to reject). *ttl_s* is the requested lifetime in
        seconds — omitted uses the configured default, a value over the configured
        maximum is clamped to it, and a non-positive value is rejected. Returns
        ``{"url", "expires_in_s"}``.
        """
        return await _mint_link(ref, "download", ttl_s)

    async def create_upload_link(
        ref: str, ttl_s: float | None = None
    ) -> dict[str, Any]:
        """Mint a capability link that accepts one upload for *ref*.

        *ref* is a domain reference the ``validate`` hook resolves to an opaque
        upload handle (raising to reject). *ttl_s* is the requested lifetime in
        seconds — omitted uses the configured default, a value over the configured
        maximum is clamped to it, and a non-positive value is rejected. Returns
        ``{"url", "expires_in_s"}``.
        """
        return await _mint_link(ref, "upload", ttl_s)

    # Applied as a call rather than ``@mcp.tool(...)`` so the base description
    # can be read from each function's own docstring: a nested closure cannot
    # reference its own ``__doc__`` in its decorator expression. The docstring
    # therefore stays the single source of the generic text — no duplicated
    # module constant to drift from it.
    mcp.tool(
        name="create_download_link",
        description=_augment(
            inspect.cleandoc(create_download_link.__doc__ or ""), download_note
        ),
        annotations=ToolAnnotations(
            title="Create Download Link",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
        ),
        icons=[_DOWNLOAD_ICON],
    )(create_download_link)

    mcp.tool(
        name="create_upload_link",
        description=_augment(
            inspect.cleandoc(create_upload_link.__doc__ or ""), upload_note
        ),
        annotations=ToolAnnotations(
            title="Create Upload Link",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
        ),
        icons=[_UPLOAD_ICON],
        tags={"write"},
    )(create_upload_link)
```

**3d — add the `inspect` import.** In the import block at the top, add `import inspect` immediately after `import base64`:

```python
import base64
import inspect
from typing import Any
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transfer_register.py -v`
Expected: PASS — the 7 new `TestDomainNotes` tests plus all pre-existing tests in the file (the description change must not disturb annotations, icons, tags, minting, TTL clamp, or the end-to-end redemptions).

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/register.py tests/test_transfer_register.py
git commit -m "feat(transfer): optional append-only download_note / upload_note

Lets a downstream add domain specifics to core's generic link-tool
descriptions without replacing them. Core's text is always kept and
comes first; an omitted note leaves the description byte-identical.

Upload benefits most: a download ref is referenced from prior output,
but an upload ref is authored by the caller with nothing stating the
destination rules.

Closes #248"
```

---

### Task 3: Correct the superseded docstring sentence

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py` (module docstring, lines 9-11)

**Interfaces:**
- Consumes: the kwargs from Task 2.
- Produces: no API change — prose only.

Context: PR #247 put "there are **no override kwargs** for any shape element" on `main`. That remains true — the note kwargs are additive hooks, not overrides — but the sentence now reads as contradicted unless it names them. Design §6.1 records this.

- [ ] **Step 1: Make the edit**

In `src/fastmcp_pvl_core/_transfer/register.py`, replace this part of the module docstring:

```
``base_url``-required guard, **and the tool metadata** (annotations, icons,
tags). The only hooks are ``sink`` (where bytes land) and ``validate`` (what
bytes are acceptable); there are **no override kwargs** for any shape element
(ADR §7 / §10 item 2).
```

with:

```
``base_url``-required guard, **and the tool metadata** (annotations, icons,
tags). The hooks are ``sink`` (where bytes land), ``validate`` (what bytes are
acceptable), and the optional ``download_note`` / ``upload_note`` (domain
specifics *appended* to core's tool descriptions). There are **no override
kwargs** for any shape element (ADR §7 / §10 item 2): the notes add to core's
text, they cannot replace it.
```

- [ ] **Step 2: Verify nothing else claims the old wording**

Run: `grep -rn "no override kwargs" src/ docs/`
Expected: the ADR's own §7/§10 statements (unchanged — they describe shape overrides, which this is not) and the corrected `register.py` sentence. If any other source file repeats the pre-#248 phrasing, fix it in this commit — the class, not the instance.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest`
Expected: PASS, 755 tests (748 pre-existing + 7 from Task 2; Task 1's 6 are part of that count — confirm the number rather than assuming it, and reconcile any difference before committing).

- [ ] **Step 4: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/register.py
git commit -m "docs(transfer): name the note kwargs in the no-override-kwargs sentence

#247 landed 'there are no override kwargs for any shape element' before
#248 was designed. Still true — the notes are additive hooks — but the
sentence now names them so it does not read as contradicted."
```

---

### Task 4: Gate and PR

**Files:** none (verification only).

- [ ] **Step 1: Match CI's dependency state**

Run: `uv sync --all-extras`

- [ ] **Step 2: Run the full local gate**

```bash
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: all four clean. Fix anything that fires before proceeding — a configured check that fires is right until proven otherwise.

- [ ] **Step 3: Run the preflight circus**

Use the `preflight-circus` skill over `main..HEAD`. Resolve findings in the artifact (code, test, or docstring), not by arguing them away in a reply.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin <branch>
```

PR body must include `Closes #248` and link the design doc (`docs/superpowers/specs/2026-08-08-transfer-path2-and-notes-design.md` §4). Note that Part A (#249) follows as its own cycle and will change this function's return type to `TransferLinks`.

---

## Self-Review

**Spec coverage (design §4):**

| §4 requirement | Task |
|---|---|
| `download_note` / `upload_note` kwargs, default `None` | 2 |
| Append-only, blank-line separator | 1 (helper), 2 (wiring) |
| `None` / empty / whitespace → absent, byte-identical output | 1, 2 |
| No mechanism to replace core's text | 1 (helper is append-only by construction) |
| Implemented via `description=`, docstring stays source of truth | 2 (step 3c) |
| `build_transfer_links` takes no notes | N/A — Part A (#249) |
| Tests: appended to right tool; base intact; omitted → identical; whitespace absent | 1, 2 |
| Docstring sentence correction (§6.1) | 3 |

No gaps.

**Placeholder scan:** none — every code step carries literal content; no "add error handling" / "similar to Task N" / "write tests for the above".

**Type consistency:** `_augment(base: str, note: str | None) -> str` is defined in Task 1 and used with exactly that signature in Task 2. `download_note` / `upload_note` are spelled identically in the signature (2), docstring (2), tests (2), `_register` helper (2), and the prose fix (3). `register_transfer_routes` keeps `-> None` throughout this plan; the change to `TransferLinks` is explicitly scoped to #249.
