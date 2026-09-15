# Log-call grammar and conformance check — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the log-call grammar pvl-core will render from, plus the checker that finds calls violating it, with no change to runtime logging.

**Architecture:** Two private modules and one public pair of exports. `_log_grammar.py` decides whether a logging format string conforms to the family standard's `event_name key=%s` shape and, when it does, what its event and fields are — parsing the *template* a developer wrote, never rendered output. `_log_conformance.py` walks a source tree with `ast` and reports every logging call whose first argument is not a conforming literal. Later PRs in epic #327 make the formatters consume `_log_grammar`; nothing in this PR touches a handler, a level, or a record.

**Tech Stack:** Python 3.10+, stdlib `re`, `ast`, `dataclasses`, `functools.lru_cache`; pytest; ruff; mypy.

**Spec:** `docs/superpowers/specs/2026-09-11-root-logging-ownership-design.md` — §3a is the section this plan implements. Read it before Task 1; the grammar's decided cases come from there verbatim.

**Issue:** child of [#327](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/327); unblocks [template#611](https://github.com/pvliesdonk/fastmcp-server-template/issues/611).

## Global Constraints

- **Non-breaking.** Additive only. No existing signature, env var, log level, handler or record changes in this PR. Conventional-commit type is `feat:` — never `feat!:`.
- **Relative intra-package imports** (`from ._log_grammar import …`). Never `from fastmcp_pvl_core...` inside `src/`. (CLAUDE.md, foldability.)
- **No self-name lookups.** No `importlib.metadata.version("fastmcp-pvl-core")`, no `importlib.resources.files("fastmcp_pvl_core")`.
- **Narrow public surface.** Exactly two new names in `__all__`: `LogCallViolation`, `find_nonconforming_log_calls`. Everything else stays `_`-prefixed.
- **Python 3.10 floor.** CI runs 3.10–3.13. Use `from __future__ import annotations` in every new module so `X | None` annotations are legal on 3.10.
- **The checker reads source only.** It never imports, executes, or writes to the tree it scans.
- **Local checks before pushing** (CLAUDE.md): `uv sync --all-extras`, `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`.

## File Structure

| File | Responsibility |
|---|---|
| `src/fastmcp_pvl_core/_log_grammar.py` (create) | The grammar: is this template conforming, and what are its event and fields? No AST, no I/O. |
| `src/fastmcp_pvl_core/_log_conformance.py` (create) | The AST walk: which calls in this tree violate the grammar? Imports `_log_grammar`. |
| `src/fastmcp_pvl_core/__init__.py` (modify) | Re-export `LogCallViolation` and `find_nonconforming_log_calls`. |
| `tests/test_log_grammar.py` (create) | Table-driven over every decided case in §3a. |
| `tests/test_log_conformance.py` (create) | Checker behaviour against files written to `tmp_path`. |
| `README.md` (modify) | Operator/consumer-facing description of the grammar and the checker. |

Two modules rather than one because the grammar is imported on every record in PR 4 (hot path, no `ast`), while the checker is imported only by a test.

## Not in scope, and tracked

- **Rendering.** Binding `record.args` to the parsed fields, and the JSON/Rich formatters, land in PR 4 of #327. This PR only *parses*.
- **pvl-core's own log calls.** 8 of 55 conform (14%) under this grammar, measured at `5bd1dff`. Migrating them is a separate child of #327, filed before this PR merges — the library cannot enforce on downstream what it does not follow. Deliberately not bundled here: it is ~47 unrelated call-site edits that would triple this diff.
- **Wiring the checker into pvl-core's own CI.** It would fail on the call sites above. It goes in with that migration.

---

### Task 1: The grammar

**Files:**
- Create: `src/fastmcp_pvl_core/_log_grammar.py`
- Test: `tests/test_log_grammar.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `LogField(name: str, conversion: str | None, literal: str | None)`, `LogTemplate(event: str, fields: tuple[LogField, ...])` with property `placeholder_count: int`, and `parse_log_template(template: str) -> LogTemplate | None` (returns `None` for a non-conforming template). Task 2 and PR 4 both consume `parse_log_template`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_log_grammar.py`:

```python
"""The log-call grammar — §3a of the root-logging-ownership spec."""

from __future__ import annotations

import pytest

from fastmcp_pvl_core._log_grammar import parse_log_template

CONFORMING = [
    # (template, event, [(name, conversion, literal), ...], placeholder_count)
    ("server_started", "server_started", [], 0),
    (
        "standard_cache_hit identifier=%s",
        "standard_cache_hit",
        [("identifier", "%s", None)],
        1,
    ),
    (
        "cache_write ttl=%d hit=%s",
        "cache_write",
        [("ttl", "%d", None), ("hit", "%s", None)],
        2,
    ),
    ("epo_ops status=configured", "epo_ops", [("status", None, "configured")], 0),
    (
        "s2_keepalive_not_started reason=no_api_key",
        "s2_keepalive_not_started",
        [("reason", None, "no_api_key")],
        0,
    ),
    ("probe waiting_s=%.1f", "probe", [("waiting_s", "%.1f", None)], 1),
    ("probe payload=%r", "probe", [("payload", "%r", None)], 1),
    (
        "mixed a=%s b=literal c=%d",
        "mixed",
        [("a", "%s", None), ("b", None, "literal"), ("c", "%d", None)],
        2,
    ),
]

NON_CONFORMING = [
    "OllamaProvider initialised: host=%s model=%s",  # prose prefix
    "Service started",                               # prose, capitalised
    "s2_rate_limited attempt=%d/%d",                 # compound placeholder
    "probe waiting=%.1fs",                           # unit suffix on placeholder
    "ratio pct=%s%%",                                # %% escape
    "Scanned %d style(s) from %s",                   # positional, no names
    "event stray",                                   # bare token, no "="
    "event =%s",                                     # empty field name
    "event Name=%s",                                 # non-snake_case field name
    "Event_Name key=%s",                             # non-snake_case event
    "event key=",                                    # empty value
    "event key=with space",                          # space inside literal
    "",                                              # empty template
    " event key=%s",                                 # leading space
    "event key=%s ",                                 # trailing space
    "event  key=%s",                                 # doubled separator
]


@pytest.mark.parametrize(
    ("template", "event", "fields", "placeholders"),
    CONFORMING,
    ids=[case[0] or "<empty>" for case in CONFORMING],
)
def test_conforming_template_parses(template, event, fields, placeholders):
    parsed = parse_log_template(template)
    assert parsed is not None
    assert parsed.event == event
    assert [(f.name, f.conversion, f.literal) for f in parsed.fields] == fields
    assert parsed.placeholder_count == placeholders


@pytest.mark.parametrize("template", NON_CONFORMING, ids=range(len(NON_CONFORMING)))
def test_non_conforming_template_returns_none(template):
    assert parse_log_template(template) is None


def test_field_order_is_source_order():
    parsed = parse_log_template("event z=%s a=%s m=%s")
    assert parsed is not None
    assert [f.name for f in parsed.fields] == ["z", "a", "m"]


def test_repeated_parse_is_cached():
    first = parse_log_template("event key=%s")
    second = parse_log_template("event key=%s")
    assert first is second
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_log_grammar.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'fastmcp_pvl_core._log_grammar'`.

- [ ] **Step 3: Write the implementation**

Create `src/fastmcp_pvl_core/_log_grammar.py`:

```python
"""The family log-call grammar: ``event_name key=value`` templates.

The logging standard every first-party module follows writes
``logger.info("event_name key=%s", value)``. Standard library logging keeps
the two halves of that call apart on the record — ``record.msg`` is the
template the developer wrote, ``record.args`` holds the values — so a
renderer can recover typed fields by parsing the *template* and pairing each
placeholder with its argument. This module owns that parse.

It deliberately never looks at a rendered message. A value is whatever the
caller passed; it is never re-read out of formatted text, so a value
containing a space, an ``=`` or a quote cannot confuse the result.

Non-conforming templates are not an error here. ``parse_log_template``
returns ``None`` and callers fall back to the formatted message — a log call
must never raise because of how its message was phrased.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# ``event`` and field names are snake_case; JSON keys come straight from the
# latter, so no dots or dashes that an aggregator would read as structure.
_EVENT = r"[a-z][a-z0-9_]*"
_NAME = r"[a-z][a-z0-9_]*"
# One %-conversion, e.g. ``%s`` ``%d`` ``%.1f`` ``%r``. ``%%`` is excluded by
# construction: its second character is not a type character.
_CONVERSION = r"%[-#0+ ]*\d*(?:\.\d+)?[sdifeEgGxXor]"
# A fixed value, e.g. ``status=configured``. No space (it would start a new
# field), no ``%`` (it would be a malformed placeholder), no ``=``.
_LITERAL = r"[^\s%=]+"

_VALUE = rf"(?:{_CONVERSION}|{_LITERAL})"
_TEMPLATE_RE = re.compile(rf"\A{_EVENT}(?: {_NAME}={_VALUE})*\Z")
_FIELD_RE = re.compile(rf"\A(?P<name>{_NAME})=(?:(?P<conv>{_CONVERSION})|(?P<lit>{_LITERAL}))\Z")


@dataclass(frozen=True)
class LogField:
    """One ``name=value`` field of a conforming template.

    Exactly one of *conversion* and *literal* is set: a placeholder field
    consumes the next positional argument, a literal field carries its own
    value and consumes none.
    """

    name: str
    conversion: str | None
    literal: str | None


@dataclass(frozen=True)
class LogTemplate:
    """A parsed conforming template: its event name and its fields, in order."""

    event: str
    fields: tuple[LogField, ...]

    @property
    def placeholder_count(self) -> int:
        """How many positional arguments this template consumes."""
        return sum(1 for field in self.fields if field.conversion is not None)


@lru_cache(maxsize=1024)
def parse_log_template(template: str) -> LogTemplate | None:
    """Parse *template* into its event and fields, or ``None`` if it does not conform.

    Conforming means: a snake_case event name, then zero or more
    space-separated ``name=value`` fields, where a value is either a single
    %-conversion or a fixed token containing no space, ``%`` or ``=``.

    Cached, because the same template string is re-parsed on every record
    that logging call emits; the cache is keyed on the template, so the
    per-record cost after the first is a dictionary lookup.
    """
    if not _TEMPLATE_RE.match(template):
        return None

    event, *raw_fields = template.split(" ")
    fields: list[LogField] = []
    for raw in raw_fields:
        match = _FIELD_RE.match(raw)
        if match is None:  # pragma: no cover - _TEMPLATE_RE already rejected it
            return None
        fields.append(
            LogField(
                name=match.group("name"),
                conversion=match.group("conv"),
                literal=match.group("lit"),
            )
        )
    return LogTemplate(event=event, fields=tuple(fields))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_log_grammar.py -q`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_log_grammar.py tests/test_log_grammar.py
git commit -m "feat(logging): parse the family log-call grammar

Refs #327"
```

---

### Task 2: The conformance checker

**Files:**
- Create: `src/fastmcp_pvl_core/_log_conformance.py`
- Test: `tests/test_log_conformance.py`

**Interfaces:**
- Consumes: `parse_log_template` from Task 1.
- Produces: `LogCallViolation(path: Path, line: int, reason: str, template: str | None)` and `find_nonconforming_log_calls(root: Path) -> list[LogCallViolation]`. Task 3 re-exports both. Reason values are exactly: `"f-string"`, `"non-literal-message"`, `"no-arguments"`, `"starred-arguments"`, `"non-conforming-template"`, `"argument-count-mismatch"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_log_conformance.py`:

```python
"""The AST conformance check over a source tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from fastmcp_pvl_core._log_conformance import find_nonconforming_log_calls

PREAMBLE = "import logging\n\nlogger = logging.getLogger(__name__)\n\n"


def write(tmp_path: Path, body: str, name: str = "mod.py") -> Path:
    path = tmp_path / name
    path.write_text(PREAMBLE + body)
    return path


def reasons(tmp_path: Path) -> list[str]:
    return [v.reason for v in find_nonconforming_log_calls(tmp_path)]


def test_conforming_calls_produce_no_violations(tmp_path):
    write(
        tmp_path,
        'logger.info("server_started port=%d", 8000)\n'
        'logger.debug("cache_hit key=%s ttl=%d", "k", 60)\n'
        'logger.warning("epo_ops status=configured")\n',
    )
    assert find_nonconforming_log_calls(tmp_path) == []


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('logger.info(f"started on {8000}")', "f-string"),
        ("logger.info(MESSAGE)", "non-literal-message"),
        ('logger.info("event " + "tail")', "non-literal-message"),
        ("logger.info()", "no-arguments"),
        ('logger.info("event key=%s", *args)', "starred-arguments"),
        ('logger.info("Service started")', "non-conforming-template"),
        ('logger.info("event key=%s")', "argument-count-mismatch"),
        ('logger.info("event key=%s", 1, 2)', "argument-count-mismatch"),
    ],
)
def test_each_violation_kind_is_reported(tmp_path, body, reason):
    write(tmp_path, body + "\n")
    assert reasons(tmp_path) == [reason]


def test_keyword_arguments_are_not_counted_as_values(tmp_path):
    write(tmp_path, 'logger.error("event key=%s", "v", exc_info=True)\n')
    assert find_nonconforming_log_calls(tmp_path) == []


def test_every_level_method_is_checked(tmp_path):
    write(
        tmp_path,
        "\n".join(
            f'logger.{level}("Prose here")'
            for level in ("debug", "info", "warning", "error", "exception", "critical")
        )
        + "\n",
    )
    assert reasons(tmp_path) == ["non-conforming-template"] * 6


def test_implicit_string_concatenation_is_one_literal(tmp_path):
    write(tmp_path, 'logger.info(\n    "event key=%s"\n    " other=%s",\n    1,\n    2,\n)\n')
    assert find_nonconforming_log_calls(tmp_path) == []


def test_calls_on_other_receivers_are_ignored(tmp_path):
    write(
        tmp_path,
        "import click\n"
        'click.echo("Anything at all")\n'
        'typer.secho("Prose is fine here")\n'
        'self.console.info("Prose here too")\n',
    )
    assert find_nonconforming_log_calls(tmp_path) == []


def test_any_name_bound_to_getlogger_is_a_receiver(tmp_path):
    (tmp_path / "mod.py").write_text(
        "import logging\n\n_audit = logging.getLogger('audit')\n\n_audit.info('Prose here')\n"
    )
    assert reasons(tmp_path) == ["non-conforming-template"]


def test_violation_carries_path_line_and_template(tmp_path):
    path = write(tmp_path, '\nlogger.info("Service started")\n')
    (violation,) = find_nonconforming_log_calls(tmp_path)
    assert violation.path == path
    assert violation.line == 6
    assert violation.template == "Service started"


def test_results_are_sorted_by_path_then_line(tmp_path):
    write(tmp_path, 'logger.info("B one")\nlogger.info("B two")\n', name="b.py")
    write(tmp_path, 'logger.info("A one")\n', name="a.py")
    found = find_nonconforming_log_calls(tmp_path)
    assert [(v.path.name, v.line) for v in found] == [("a.py", 5), ("b.py", 5), ("b.py", 6)]


def test_scans_subdirectories(tmp_path):
    (tmp_path / "pkg").mkdir()
    write(tmp_path / "pkg", 'logger.info("Prose here")\n', name="deep.py")
    assert reasons(tmp_path) == ["non-conforming-template"]


def test_non_python_files_are_ignored(tmp_path):
    (tmp_path / "notes.txt").write_text('logger.info("Prose here")\n')
    assert find_nonconforming_log_calls(tmp_path) == []


def test_syntax_error_propagates(tmp_path):
    (tmp_path / "broken.py").write_text("def (:\n")
    with pytest.raises(SyntaxError):
        find_nonconforming_log_calls(tmp_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_log_conformance.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'fastmcp_pvl_core._log_conformance'`.

- [ ] **Step 3: Write the implementation**

Create `src/fastmcp_pvl_core/_log_conformance.py`:

```python
"""Find logging calls that do not follow the family log-call grammar.

The grammar in :mod:`._log_grammar` is what turns a log record into fields
rather than a sentence. A call that does not follow it still logs — it just
arrives at an aggregator as an opaque ``message``. This module finds those
calls so a project can fail its build on them instead of discovering them in
production.

It reads source with :mod:`ast` and never imports the tree it scans, so it is
safe to point at a package whose dependencies are not installed.

Kept separate from :mod:`._log_grammar` because that module is imported on
every log record once the formatters consume it, and this one pulls in
``ast`` for the benefit of a single test.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from ._log_grammar import parse_log_template

_LEVEL_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical"}
)
"""The level methods the standard uses. ``log`` is excluded: its first
argument is the level, not the message, so it has a different shape."""

REASON_F_STRING = "f-string"
REASON_NON_LITERAL = "non-literal-message"
REASON_NO_ARGUMENTS = "no-arguments"
REASON_STARRED = "starred-arguments"
REASON_NON_CONFORMING = "non-conforming-template"
REASON_ARG_COUNT = "argument-count-mismatch"


@dataclass(frozen=True)
class LogCallViolation:
    """One logging call that does not follow the grammar.

    Attributes:
        path: File the call is in.
        line: 1-based line of the call.
        reason: Why it does not conform — one of the ``REASON_*`` constants.
        template: The literal template, when the call had one; ``None`` when
            the message was an f-string or another non-literal expression.
    """

    path: Path
    line: int
    reason: str
    template: str | None


def _logger_names(tree: ast.Module) -> set[str]:
    """Names bound to a ``logging.getLogger(...)`` call anywhere in *tree*."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        is_get_logger = (
            isinstance(func, ast.Attribute) and func.attr == "getLogger"
        ) or (isinstance(func, ast.Name) and func.id == "getLogger")
        if not is_get_logger:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _violation(path: Path, call: ast.Call) -> LogCallViolation | None:
    """Judge one logging call, or return ``None`` when it conforms."""
    if not call.args:
        return LogCallViolation(path, call.lineno, REASON_NO_ARGUMENTS, None)

    message, *values = call.args
    if isinstance(message, ast.JoinedStr):
        return LogCallViolation(path, call.lineno, REASON_F_STRING, None)
    if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
        return LogCallViolation(path, call.lineno, REASON_NON_LITERAL, None)

    template = message.value
    parsed = parse_log_template(template)
    if parsed is None:
        return LogCallViolation(path, call.lineno, REASON_NON_CONFORMING, template)

    # ``*args`` makes the count unknowable from source; report rather than
    # guess, so a reader is never told a call was checked when it was not.
    if any(isinstance(value, ast.Starred) for value in values):
        return LogCallViolation(path, call.lineno, REASON_STARRED, template)
    if len(values) != parsed.placeholder_count:
        return LogCallViolation(path, call.lineno, REASON_ARG_COUNT, template)
    return None


def find_nonconforming_log_calls(root: Path) -> list[LogCallViolation]:
    """Report every logging call under *root* that breaks the grammar.

    Walks ``*.py`` beneath *root*, finds calls to a level method on a name
    bound to ``logging.getLogger(...)`` in the same file, and judges each one
    against :func:`._log_grammar.parse_log_template`. Results are sorted by
    path, then line, so output is stable across runs and platforms.

    Reads source only — nothing under *root* is imported or executed.

    Args:
        root: Directory to scan, typically a project's ``src/``.

    Returns:
        Violations, sorted; empty when everything conforms.

    Raises:
        SyntaxError: If a file under *root* cannot be parsed. The scanned
            tree is the caller's own source, so unparseable input is a
            defect in it rather than something to report as a violation.
    """
    violations: list[LogCallViolation] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        receivers = _logger_names(tree)
        if not receivers:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LEVEL_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in receivers
            ):
                found = _violation(path, node)
                if found is not None:
                    violations.append(found)
    return sorted(violations, key=lambda v: (str(v.path), v.line))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_log_conformance.py -q`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_log_conformance.py tests/test_log_conformance.py
git commit -m "feat(logging): report log calls that break the grammar

Refs #327"
```

---

### Task 3: Export and document

**Files:**
- Modify: `src/fastmcp_pvl_core/__init__.py`
- Modify: `README.md` (the `## Logging` section, near the `key=value` description at ~line 320)
- Test: `tests/test_log_conformance.py` (append one test)

**Interfaces:**
- Consumes: `LogCallViolation`, `find_nonconforming_log_calls` from Task 2.
- Produces: both names importable as `from fastmcp_pvl_core import …`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_log_conformance.py`:

```python
def test_public_export():
    import fastmcp_pvl_core

    assert "find_nonconforming_log_calls" in fastmcp_pvl_core.__all__
    assert "LogCallViolation" in fastmcp_pvl_core.__all__
    assert (
        fastmcp_pvl_core.find_nonconforming_log_calls
        is find_nonconforming_log_calls
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_log_conformance.py::test_public_export -q`
Expected: FAIL — `AssertionError` on the `__all__` membership check.

- [ ] **Step 3: Add the exports**

In `src/fastmcp_pvl_core/__init__.py`, add the import beside the other `from ._x import y` lines:

```python
from ._log_conformance import LogCallViolation, find_nonconforming_log_calls
```

and add both names to `__all__`, keeping its existing alphabetical order — `"LogCallViolation"` goes between `"JobsConfig"` and `"SecretMaskFilter"`; `"find_nonconforming_log_calls"` goes between `"finalize_instructions"` and `"get_claims"` (`fina` sorts before `find`).

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_log_conformance.py -q`
Expected: PASS.

- [ ] **Step 5: Document it in the README**

In `README.md`, directly after the paragraph describing `key=value` request-log lines, add:

````markdown
### The log-call grammar

Every first-party log call follows one shape, so a log consumer can read it
as fields rather than a sentence:

```python
logger.info("cache_write key=%s ttl=%d", key, ttl)
```

An event name in snake_case, then `name=value` fields — each value either a
single `%`-conversion or a fixed token with no space, `%` or `=`. Prose, a
compound placeholder (`attempt=%d/%d`), a unit suffix (`waiting=%.1fs`) and
`%%` are all outside it.

`find_nonconforming_log_calls` reports calls that break it, so a project can
fail its build rather than find out from an aggregator:

```python
from pathlib import Path

from fastmcp_pvl_core import find_nonconforming_log_calls


def test_log_calls_conform():
    assert find_nonconforming_log_calls(Path("src")) == []
```

It parses source with `ast` and imports nothing from the tree it scans. Each
violation carries `path`, `line`, `reason` and the offending `template`.
````

- [ ] **Step 6: Commit**

```bash
git add src/fastmcp_pvl_core/__init__.py README.md tests/test_log_conformance.py
git commit -m "feat(logging): export the log-call conformance check

Refs #327"
```

---

### Task 4: Full local gate

**Files:** none modified — this task only runs checks and fixes what they report.

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: a branch that CI will pass.

- [ ] **Step 1: Match CI's dependency state**

Run: `uv sync --all-extras`

- [ ] **Step 2: Run the whole suite, not just the new files**

Run: `uv run pytest`
Expected: PASS, with no change to the existing count other than the new tests. If an existing test fails, this PR broke something it should not have touched — it is additive by definition.

- [ ] **Step 3: Run format, lint and types**

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

Expected: all clean. If `ruff format --check` fails, run `uv run ruff format .` and re-run.

- [ ] **Step 4: Run the suite on the floor and the ceiling of the matrix**

```bash
uv run --python 3.10 pytest tests/test_log_grammar.py tests/test_log_conformance.py -q
uv run --python 3.13 pytest tests/test_log_grammar.py tests/test_log_conformance.py -q
```

Expected: PASS on both. `ast` node attributes and `re` behaviour differ across versions often enough that one interpreter is not evidence.

- [ ] **Step 5: Sanity-check the checker against real trees**

```bash
uv run python -c "
from pathlib import Path
from fastmcp_pvl_core import find_nonconforming_log_calls
for repo in ('scholar-mcp', 'markdown-vault-mcp'):
    found = find_nonconforming_log_calls(Path(f'/mnt/code/mcp-servers/{repo}/src'))
    print(repo, len(found), found[0].reason if found else '')
"
```

Expected: both report a non-zero count without raising. This is a smoke test of the walk against code nobody wrote for it — not an assertion, since the counts move as those repos change.

- [ ] **Step 6: Commit any fixes from this task**

```bash
git add -u
git commit -m "chore: satisfy format and type checks

Refs #327"
```

(Skip if the working tree is clean.)

---

## Self-review notes

- **Spec coverage.** §3a's grammar is Task 1; its "conformance check" paragraph is Task 2; its export name and signature match the spec verbatim. §3a's *rendering* rules (JSON field types, Rich quoting) are explicitly PR 4 and appear in no task here, as the spec's §8 sequence requires.
- **Decided cases.** Every case §3a names — literal value, compound placeholder, unit suffix, prose prefix, `%%`, arg-count mismatch — appears as a test row in Task 1 or Task 2.
- **Type consistency.** `parse_log_template` returns `LogTemplate | None` in Task 1 and is consumed as such in Task 2's `_violation`. `LogField.conversion`/`literal` are the names used in both the Task 1 test table and the Task 2 `placeholder_count` logic. `LogCallViolation`'s four fields are identical in Task 2's definition, its tests, and Task 3's README text.
