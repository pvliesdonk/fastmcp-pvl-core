# Transfer domain notes (Part B, issue #248) — v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional `download_note` / `upload_note` kwargs to `register_transfer_routes` that append a downstream domain sentence to core's generic tool descriptions — with the append-only guarantee *enforced*, not merely asserted.

**Architecture:** A module-level `_augment(base, note)` composes the description; a `_base_description(fn)` helper reads and validates the base from the tool function's own docstring, raising if it is absent. The two tool functions are defined first and registered via an explicit `mcp.tool(...)(fn)` call — a nested closure cannot reference its own `__doc__` in its decorator expression — so the docstring stays the single source of the base text.

**Tech Stack:** Python 3.10+, fastmcp, pytest, ruff, mypy.

## BLOCKER — do not start until this is true

**ADR 0001's amendment must be merged to `main` first** (`docs/superpowers/plans/2026-08-08-adr-additive-text-kwargs.md`). This plan's code cites ADR §10 item 2 as the authority for the new kwargs. Until the amendment lands, that clause says *"The only kwargs are the two hooks (`sink`, `validate`)"* and the code contradicts the record it cites.

Verify before Task 1:

```bash
git fetch origin main
git show origin/main:docs/adr/0001-transfer-lift.md | grep -c "additive-domain-text kwargs"
```

Expected: `3` or more. If `0`, stop — the prerequisite has not merged.

## Why v2 exists

v1 (branch `transfer-domain-notes`, 4 commits, unpushed, never PR'd) was stopped by the pre-flight gate at `structural` — 11 findings at ≥80. Two substantive causes, both addressed here:

1. **The ADR was never amended** (nine of the eleven findings traced to this) — now a merged prerequisite, above.
2. **The tests did not pin what they claimed.** Two mutations passed v1's whole suite: dropping `inspect.cleandoc`, and truncating the base to `.split("\n\n")[0]`. Task 3 kills both and re-runs them as a check on the tests.

Treat v1 as a **spike**: it proved the mechanics listed below. Do not `git cherry-pick` from it — write fresh against this plan.

## Global Constraints

- Line length 88. Ruff: `select = ["E", "F", "W", "I", "B", "UP", "N", "D"]`, google docstring convention; `tests/**` exempt from `D`.
- Intra-package imports stay **relative** (`from .x import y`) — foldability rule in `CLAUDE.md`.
- No runtime self-name lookups (no `importlib.metadata.version(...)`, no `importlib.resources`).
- **Every new kwarg's Args entry names its category**, matching the existing `sink:` / `validate:` entries which both start `Domain hook — `. CLAUDE.md "Practical consequences" requires this and v1 was flagged for omitting it. The notes' category is `Additive domain text`.
- Run everything with `uv run`. Baseline on `main`: `748 passed`.
- Local gate before pushing: `uv sync --all-extras`, `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`.
- Branch off `main` **after** the ADR amendment has merged. One PR, closing #248.

## Verified Mechanics (from the v1 spike — do not re-derive)

- `mcp.tool(description=X)` overrides the function docstring; `description=None` falls back to it.
- `inspect.cleandoc()` dedents an indented docstring and preserves paragraph breaks.
- `mcp.tool(name=..., description=...)(fn)` works identically to `@` syntax. This is not a novel form in this repo — `src/fastmcp_pvl_core/_server_info.py:156-160` already registers `get_server_info` exactly this way.
- Under `python -OO` / `PYTHONOPTIMIZE=2`, `fn.__doc__` is `None` (confirmed: `uv run python -OO -c "def f(): '''D'''" ...`).
- A `ToolAnnotations` field left unset reads back as `None`, not `False` (confirmed) — which is why Task 3 changes the existing download-tool assertion to `is None`.
- `caplog.records[i].getMessage()` renders the `%`-args, so a `logger.debug("%s ...", name)` line is matched by `name in r.getMessage()` (confirmed).

---

### Task 1: `_augment` — the append-only composer

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py`
- Test: `tests/test_transfer_register.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_augment(base: str, note: str | None) -> str` — module-private. Returns `base` unchanged when `note` is `None`/empty/whitespace-only; otherwise `base + "\n\n" + note.strip()`.

Place `_augment` after the `_DOWNLOAD_ICON = _icon(_DOWNLOAD_SVG)` / `_UPLOAD_ICON = _icon(_UPLOAD_SVG)` assignments and before `_ROUTE_PATH = "/transfer/{token}"`.

- [ ] **Step 1: Write the failing test**

In `tests/test_transfer_register.py`, add this import beside the existing `from fastmcp_pvl_core._errors import ConfigurationError`:

```python
from fastmcp_pvl_core._transfer.register import _augment
```

And add this class immediately before `class _RecordingSink:`

```python
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

    def test_base_survives_verbatim_as_prefix(self) -> None:
        # Append-only: the base must survive as a prefix, so a downstream note
        # can never rewrite or truncate core's text.
        base = "Multi\n\nparagraph\nbase."
        assert _augment(base, "NOTE") == f"{base}\n\nNOTE"

    def test_multiline_note_keeps_interior_indentation(self) -> None:
        # Pins current behaviour: the note is stripped at its ends but NOT
        # dedented, so a triple-quoted note's interior indentation survives.
        # A downstream writing an indented multi-line note gets a Markdown
        # code block in its tool description — documented, not accidental.
        note = "First line.\n    Indented second."
        assert _augment("BASE", note) == "BASE\n\nFirst line.\n    Indented second."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transfer_register.py::TestAugment -v`
Expected: FAIL at collection — `ImportError: cannot import name '_augment'`

- [ ] **Step 3: Write minimal implementation**

```python
def _augment(base: str, note: str | None) -> str:
    """Return *base* with an optional downstream domain *note* appended.

    Append-only by construction: *base* is always the result's prefix, so a
    note can add domain specifics but never rewrite or truncate it. A ``None``,
    empty, or whitespace-only note is treated as absent and *base* is returned
    unchanged.

    The note is stripped at its ends but **not** dedented, so a multi-line note
    keeps its interior indentation (which a Markdown renderer will show as a
    code block). Callers wanting a dedented note should pass one.
    """
    if note is None or not note.strip():
        return base
    return f"{base}\n\n{note.strip()}"
```

Note the docstring claims only what this pure function does. The end-to-end "byte-identical description" property belongs to the registration site (Task 3), which is where it is tested — v1 was flagged for asserting that guarantee here, where this function cannot support it.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transfer_register.py::TestAugment -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/register.py tests/test_transfer_register.py
git commit -m "feat(transfer): add _augment append-only description composer

Base text is always the result's prefix; an absent (None/empty/
whitespace) note returns the base unchanged. The note is stripped but
not dedented — pinned by test, so a multi-line note's rendering is
documented rather than accidental."
```

---

### Task 2: `_base_description` — enforce the docstring precondition

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py`
- Test: `tests/test_transfer_register.py`

**Interfaces:**
- Consumes: nothing from Task 1 (independent helper).
- Produces: `_base_description(fn: Callable[..., Any]) -> str` — module-private. Returns `inspect.cleandoc(fn.__doc__)`. Raises `ConfigurationError` if `fn.__doc__` is `None` or blank.

Why this exists: v1 wrote `inspect.cleandoc(fn.__doc__ or "")`. Under `python -OO` docstrings are stripped, so the base collapsed to `""` and the domain note became the *entire* tool description — the exact inversion of the guarantee, silently. A missing docstring is a build/deployment error, so it fails loudly at registration. This matches the module's existing posture: `register_transfer_routes` already raises `ConfigurationError` for a missing `base_url` rather than deferring to the first tool call.

Place `_base_description` immediately after `_augment`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_transfer_register.py`, immediately after `class TestAugment`:

```python
class TestBaseDescription:
    """The docstring precondition behind the append-only guarantee."""

    def test_returns_cleandoc_of_docstring(self) -> None:
        def fn() -> None:
            """First line.

            Indented body that cleandoc must dedent.
            """

        assert _base_description(fn) == (
            "First line.\n\nIndented body that cleandoc must dedent."
        )

    def test_missing_docstring_raises(self) -> None:
        # The -OO / PYTHONOPTIMIZE=2 case: docstrings are stripped, so a
        # silent "" base would make a domain note the ENTIRE description.
        # Fail loudly at registration instead.
        def fn() -> None:
            pass

        with pytest.raises(ConfigurationError, match="docstring"):
            _base_description(fn)

    def test_blank_docstring_raises(self) -> None:
        def fn() -> None:
            """   """

        with pytest.raises(ConfigurationError, match="docstring"):
            _base_description(fn)

    def test_error_names_the_function(self) -> None:
        # The operator needs to know WHICH tool lost its description.
        def some_tool_fn() -> None:
            pass

        with pytest.raises(ConfigurationError, match="some_tool_fn"):
            _base_description(some_tool_fn)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transfer_register.py::TestBaseDescription -v`
Expected: FAIL — `NameError: name '_base_description' is not defined`

- [ ] **Step 3: Write minimal implementation**

Add `import inspect` immediately after `import base64` at the top of the file, and `from collections.abc import Callable` to the typing imports. Then:

```python
def _base_description(fn: Callable[..., Any]) -> str:
    """Return *fn*'s docstring as the base tool description, dedented.

    Raises rather than defaulting when the docstring is missing: under
    ``python -OO`` / ``PYTHONOPTIMIZE=2`` docstrings are stripped, and a silent
    empty base would let a downstream ``*_note`` become the *entire* tool
    description — inverting the append-only guarantee with no signal. Failing
    at registration matches this module's existing posture for a missing
    ``base_url``.

    Raises:
        ConfigurationError: *fn* has no docstring, or a blank one.
    """
    doc = fn.__doc__
    if doc is None or not doc.strip():
        raise ConfigurationError(
            f"{fn.__name__} has no docstring to build its tool description "
            "from; transfer tools cannot be registered under python -OO / "
            "PYTHONOPTIMIZE=2, which strips docstrings"
        )
    return inspect.cleandoc(doc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transfer_register.py::TestBaseDescription -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/register.py tests/test_transfer_register.py
git commit -m "feat(transfer): add _base_description with a docstring precondition

Reads a tool function's docstring as its base description and raises
ConfigurationError when absent. Under -OO docstrings are stripped, and
a silent empty base would make a domain note the entire description —
inverting the append-only guarantee with no signal."
```

---

### Task 3: Wire the note kwargs through `register_transfer_routes`

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py`
- Test: `tests/test_transfer_register.py`

**Interfaces:**
- Consumes: `_augment(base, note)` (Task 1), `_base_description(fn)` (Task 2).
- Produces: `register_transfer_routes(mcp, config, transfer_config, *, sink, validate, download_note: str | None = None, upload_note: str | None = None) -> None`. Return type stays `None` — a separate issue (#249) changes it to `TransferLinks`; do not anticipate that.

- [ ] **Step 1: Write the failing test**

First extend the existing `_register` helper — replace it with:

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

Then add this class after the existing `TestToolRegistration` class:

```python
# A verbatim fragment from the SECOND paragraph of both tools' base
# descriptions (the two share this sentence). Pinning a second-paragraph
# fragment — not just the opening sentence — is what kills a mutation that
# truncates the base to `.split("\n\n")[0]`; v1's tests missed it.
_BASE_SECOND_PARA = (
    "seconds — omitted uses the configured default, a value over the configured"
)


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
        # Would fail if the two kwargs were swapped at the call site.
        mcp, _, _ = _register(download_note="DOWNNOTE", upload_note="UPNOTE")
        down = await mcp.get_tool("create_download_link")
        up = await mcp.get_tool("create_upload_link")
        assert down.description.endswith("\n\nDOWNNOTE")
        assert up.description.endswith("\n\nUPNOTE")
        assert "UPNOTE" not in down.description
        assert "DOWNNOTE" not in up.description

    async def test_download_base_body_is_intact_under_a_note(self) -> None:
        # Pins the WHOLE base, not just its first sentence. The second-paragraph
        # fragment kills a `.split("\n\n")[0]` truncation mutation; the
        # no-indented-lines check kills a dropped-`cleandoc` mutation.
        mcp, _, _ = _register(download_note="NOTE")
        desc = (await mcp.get_tool("create_download_link")).description
        assert desc.startswith(
            "Mint a capability link that serves the bytes for *ref* once."
        )
        assert _BASE_SECOND_PARA in desc
        assert '``{"url", "expires_in_s"}``' in desc
        assert all(not line.startswith(" ") for line in desc.splitlines())

    async def test_upload_base_body_is_intact_under_a_note(self) -> None:
        mcp, _, _ = _register(upload_note="NOTE")
        desc = (await mcp.get_tool("create_upload_link")).description
        assert desc.startswith(
            "Mint a capability link that accepts one upload for *ref*."
        )
        assert _BASE_SECOND_PARA in desc
        assert '``{"url", "expires_in_s"}``' in desc
        assert all(not line.startswith(" ") for line in desc.splitlines())

    @pytest.mark.parametrize("note", [None, "", "   "])
    async def test_absent_note_gives_exactly_the_base(
        self, note: str | None
    ) -> None:
        # Compares against the DOCSTRING, not against another run of the same
        # new code path — v1 compared post-change to post-change, which proves
        # nothing. This is the byte-identical guarantee, pinned at the site
        # that actually owns it.
        mcp, _, _ = _register(download_note=note)
        tool = await mcp.get_tool("create_download_link")
        noted, _, _ = _register(download_note="X")
        base = (await noted.get_tool("create_download_link")).description
        assert tool.description == base.removesuffix("\n\nX")

    async def test_blank_note_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A blank note is a no-op, but the operator gets a signal that their
        # note was discarded rather than silently vanishing.
        with caplog.at_level(logging.DEBUG):
            _register(download_note="   ")
        # getMessage() applies the %-args, so this reads the rendered line.
        assert any("download_note" in r.getMessage() for r in caplog.records)
```

Add `import logging` to the test file's imports if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transfer_register.py::TestDomainNotes -v`
Expected: FAIL — `TypeError: register_transfer_routes() got an unexpected keyword argument 'download_note'`

- [ ] **Step 3: Write the implementation**

**3a — add a module logger.** After the imports, alongside the other module-level names (`register.py` has no logger today; `routes.py` and `store.py` both use this exact form):

```python
logger = logging.getLogger(__name__)
```

and `import logging` at the top of the import block.

**3b — extend the signature:**

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

**3c — document them, WITH the category label.** In the `Args:` block, after the `validate:` entry:

```
        download_note: Additive domain text — an optional sentence appended to
            ``create_download_link``'s description. Core's generic text is
            always kept and comes first; this only adds domain specifics (e.g.
            what a ``ref`` looks like for this server). Omitted, empty, or
            whitespace-only leaves the description exactly as core writes it.
            There is no way to *replace* core's text (ADR §10 item 2).
        upload_note: Additive domain text — the same for
            ``create_upload_link``. This is the one that usually matters: a
            download ``ref`` is something the caller has already seen (a search
            hit, a listing entry), but an upload ``ref`` is *authored* by the
            caller, and nothing it has seen states the destination rules.
```

**3d — log a discarded note.** Immediately after the `base_url` guard at the top of the function body:

```python
    for name, note in (("download_note", download_note), ("upload_note", upload_note)):
        if note is not None and not note.strip():
            logger.debug("%s was supplied but is blank; ignoring it", name)
```

**3e — convert both tools to explicit registration.** Replace the two `@mcp.tool(...)`-decorated definitions with plain `async def`s (keeping their docstrings byte-identical), then register them after both are defined:

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

    # Registered by an explicit call rather than ``@mcp.tool(...)`` so each
    # base description can be read from the function's own docstring: a nested
    # closure cannot reference its own ``__doc__`` in its decorator expression.
    # The docstring therefore stays the single source of the generic text — a
    # duplicated module constant would be free to drift from it.
    mcp.tool(
        name="create_download_link",
        description=_augment(_base_description(create_download_link), download_note),
        annotations=ToolAnnotations(
            title="Create Download Link",
            readOnlyHint=True,
            idempotentHint=False,
        ),
        icons=[_DOWNLOAD_ICON],
    )(create_download_link)

    mcp.tool(
        name="create_upload_link",
        description=_augment(_base_description(create_upload_link), upload_note),
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

Two details in that block are deliberate and must not be "corrected":

- `create_download_link`'s annotations **drop `destructiveHint=False`**. It is inert under `readOnlyHint=True` per the MCP annotation spec, `_server_info.py` omits it in the same situation, and `claude-review` flagged it twice on PR #245. `create_upload_link` **keeps** it — there it is meaningful, because `readOnlyHint=False`.
- `tags={"write"}` stays on the upload tool **only**. It is what hides that tool under a downstream's read-only `mcp.disable(tags={"write"})` pass.

Also update the existing `test_download_link_has_annotations` test, which asserts `destructiveHint is False` on the download tool — change that line to:

```python
        assert tool.annotations.destructiveHint is None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transfer_register.py -v`
Expected: PASS — the new `TestDomainNotes` plus every pre-existing test in the file.

- [ ] **Step 5: Mutation-test the tests**

This is the step v1 lacked. Each mutation must make the suite **fail**; a mutation that passes means the test is decorative.

```bash
# Mutation A — drop cleandoc (would leave every continuation line indented)
sed -i 's/return inspect\.cleandoc(doc)/return doc/' src/fastmcp_pvl_core/_transfer/register.py
uv run pytest tests/test_transfer_register.py -q   # MUST FAIL
git checkout src/fastmcp_pvl_core/_transfer/register.py

# Mutation B — truncate the base to its first paragraph
sed -i 's/return inspect\.cleandoc(doc)/return inspect.cleandoc(doc).split("\\n\\n")[0]/' src/fastmcp_pvl_core/_transfer/register.py
uv run pytest tests/test_transfer_register.py -q   # MUST FAIL
git checkout src/fastmcp_pvl_core/_transfer/register.py

# Mutation C — swap the two notes at the call site
# (edit by hand: pass `upload_note` to the download tool and vice versa)
uv run pytest tests/test_transfer_register.py -q   # MUST FAIL
git checkout src/fastmcp_pvl_core/_transfer/register.py
```

If any mutation passes, strengthen the corresponding test before continuing, then re-run all three. Record the three outcomes in your report.

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/register.py tests/test_transfer_register.py
git commit -m "feat(transfer): optional append-only download_note / upload_note

Lets a downstream add domain specifics to core's generic link-tool
descriptions without replacing them (ADR §10 item 2's additive-domain-
text category). Core's text is always kept and comes first; an omitted
note leaves the description exactly as core writes it, and a blank one
is logged at debug rather than vanishing silently.

Upload benefits most: a download ref is referenced from prior output,
but an upload ref is authored by the caller with nothing stating the
destination rules.

Also drops the inert destructiveHint=False from create_download_link's
annotations — it is a no-op under readOnlyHint=True and claude-review
flagged it twice on #245.

Closes #248"
```

---

### Task 4: Update the module docstring

**Files:**
- Modify: `src/fastmcp_pvl_core/_transfer/register.py` (module docstring)

**Interfaces:**
- Consumes: the kwargs from Task 3.
- Produces: prose only.

The module docstring on `main` says *"there are **no override kwargs** for any shape element (ADR §7 / §10 item 2)"*. That is still true — the notes are additive, not overrides — but it must now name them, and it may do so because the ADR amendment (the prerequisite) ratified the category.

- [ ] **Step 1: Make the edit**

Replace:

```
tags). The only hooks are ``sink`` (where bytes land) and ``validate`` (what
bytes are acceptable); there are **no override kwargs** for any shape element
(ADR §7 / §10 item 2).
```

with:

```
tags). Downstream supplies two domain hooks — ``sink`` (where bytes land) and
``validate`` (what bytes are acceptable) — plus the optional ``download_note``
/ ``upload_note`` additive domain text. There are still **no override kwargs**
for any shape element (ADR §7 / §10 item 2, as amended): the notes *append* to
core's descriptions and cannot replace them.
```

Then replace:

```
The two tools carry generic, universal metadata so every downstream server
presents them identically.
```

with:

```
The two tools carry generic, universal metadata, and their descriptions share
an identical core prefix on every downstream server — a domain note only ever
appends to it.
```

- [ ] **Step 2: Sweep for the same claim elsewhere**

Run: `grep -rn "override kwarg" src/ docs/ README.md`

Expected hits: `docs/adr/0001-transfer-lift.md` (the amended §10 item 2 and §7 — both about *shape* overrides, still correct), `README.md:40,65` (the design-principles voice, about shape overrides — unaffected), and the sentence you just edited. Fix any other source file that repeats the pre-amendment phrasing in a now-misleading way; report what you found either way.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest`
Expected: PASS. Report the count; it should be `748` plus the tests added in Tasks 1-3.

- [ ] **Step 4: Commit**

```bash
git add src/fastmcp_pvl_core/_transfer/register.py
git commit -m "docs(transfer): name the additive note kwargs in the module docstring

The no-override-kwargs sentence stays true — the notes append rather
than replace — but now names them, which the merged ADR amendment
ratifies. Also corrects the identical-presentation claim: descriptions
share an identical core prefix, not identical text."
```

---

### Task 5: Gate and PR

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

All four must be clean. A configured check that fires is right until proven otherwise — fix rather than suppress.

- [ ] **Step 3: Test the CI floor interpreter**

CI runs Python 3.10 through 3.13; the local default is 3.13. Version-dependent behaviour has bitten this repo before.

Run: `uv run --python 3.10 pytest tests/test_transfer_register.py -q`
Expected: PASS.

- [ ] **Step 4: Verify the ADR prerequisite is actually in the merge base**

```bash
git fetch origin main
git merge-base --is-ancestor $(git rev-list -1 origin/main -- docs/adr/0001-transfer-lift.md) HEAD && echo "ADR amendment is in history"
```

Expected: the echo prints. If not, this branch is not based on the merged amendment — rebase before opening the PR, or the PR will again cite a clause that does not exist on its base.

- [ ] **Step 5: Run the preflight circus**

Use the `preflight-circus` skill over `main..HEAD`.

Before invoking, do the self-review the skill expects, in writing: re-read both diffs and check the decorator arguments, the category labels on the new Args entries, the mutation results from Task 3 step 5, and whether any claim in a docstring exceeds what the code enforces. v1 failed this gate because findings that were visible in self-review were logged as deferred minors instead of fixed. Do not repeat that: fix what the self-review surfaces, before invoking.

- [ ] **Step 6: Push and open the PR**

```bash
git push -u origin <branch>
```

PR body: `Closes #248`; link the design doc §4.4-§4.6; state that the ADR amendment merged first and this branch is based on it; note the three mutation results from Task 3 step 5 as evidence the tests bite.

---

## Self-Review

**Spec coverage (design §4):**

| §4 requirement | Task |
|---|---|
| `download_note` / `upload_note` kwargs, default `None` | 3 |
| Append-only, blank-line separator | 1, 3 |
| `None`/empty/whitespace → absent, description unchanged | 1, 3 |
| No mechanism to replace core's text | 1 (append-only by construction) |
| ADR amendment lands first (§4.4) | BLOCKER section + Task 5 step 4 |
| Category label on every new kwarg (§4.4) | 3 step 3c |
| Guarantee enforced, not asserted; `-OO` raises (§4.5) | 2 |
| Blank note logged at debug (§4.5) | 3 steps 3a, 3d |
| `_augment` docstring claims only what it can (§4.5) | 1 step 3 |
| Docstring stays the base source; explicit `mcp.tool(...)(fn)` (§4.6) | 3 step 3e |
| Full base body pinned, both mutations killed (§5) | 3 steps 1, 5 |
| Notes do not cross tools (§5) | 3 step 1 |
| Multi-line note behaviour pinned (§5) | 1 step 1 |
| Missing docstring raises, naming the tool (§5) | 2 step 1 |
| Inert `destructiveHint` dropped (§6.1) | 3 step 3e |
| Module docstring corrected (§6.1) | 4 |

No gaps.

**Placeholder scan:** none — every code step carries literal content; no "add error handling", no "similar to Task N".

**Type consistency:** `_augment(base: str, note: str | None) -> str` (Task 1) and `_base_description(fn: Callable[..., Any]) -> str` (Task 2) are used with exactly those signatures in Task 3. `download_note` / `upload_note` are spelled identically in the signature, Args block, `_register` helper, tests, and prose. `register_transfer_routes` keeps `-> None` throughout; the `TransferLinks` return is #249's and is explicitly excluded.
