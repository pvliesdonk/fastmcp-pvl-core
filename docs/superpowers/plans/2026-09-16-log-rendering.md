# Rendering: one format for the whole process — implementation plan (PR 3 of #327)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every record in the process render in one chosen format — Rich for a human, JSON for an aggregator — with first-party records arriving as real fields rather than a sentence.

**Architecture:** Rendering moves from the record *producer* to the root handler's *formatter*. A record's fields are recovered by parsing the log-call grammar shipped in v7.2.0 — the template the developer wrote (`record.msg`) paired with `record.args` — so one switch covers the request middleware, uvicorn, the MCP SDK and domain code alike. `{PREFIX}_LOG_FORMAT` selects the mode; unset means auto: Rich on a TTY, JSON otherwise.

**Tech Stack:** Python 3.10+, stdlib `logging`/`json`, `rich`, fastmcp 4.0.0, uvicorn 0.44.0; pytest; ruff; mypy.

**Spec:** `docs/superpowers/specs/2026-09-11-root-logging-ownership-design.md` — §3 (rendering) and §3a (the grammar) are what this implements. Read both before Task 1. §1, §2 and §4 already shipped.

**Issue:** child 4 of [#327](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/327), labelled `ships-atomically`. This is the last code child; after it, only #323's documented recipe, then one major release and one template cutover.

## Global Constraints

- **Breaking**: `{PREFIX}_LOG_FORMAT` replaces `FASTMCP_ENABLE_RICH_LOGGING`, and the request middleware's `structured=` constructor flag is removed. `feat!:` with a `BREAKING CHANGE:` footer on the commit that does it.
- **The Rich middleware line must stay byte-identical** to what `README.md` documents today — `tool_call_completed tool=read duration_ms=68.57`, with values containing whitespace or `"` quoted and escaped. That output is the family's logging standard; this PR changes how it is produced, not what it looks like.
- **A log call must never raise.** `record.msg` is not guaranteed to be a `str` (a caller may log any object), `record.args` may be a `Mapping` rather than a tuple, and a value may not be JSON-serialisable. Every one of those degrades to the formatted message.
- **Relative intra-package imports** inside `src/`; no self-name lookups; Python 3.10 floor; stderr only; public surface unchanged.
- **Local checks** before pushing: `uv sync --all-extras`, `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`, and `get_change_risk` with `introduced == 0`.

## File Structure

| File | Responsibility |
|---|---|
| `src/fastmcp_pvl_core/_log_render.py` (create) | Bind a record to the grammar; render it as Rich text or as a JSON object. The only module that knows what a rendered line looks like. |
| `src/fastmcp_pvl_core/_logging.py` (modify) | Mode selection (`{PREFIX}_LOG_FORMAT`, auto) and installing the matching handler pair at root. |
| `src/fastmcp_pvl_core/_logging_middleware.py` (modify) | Stop rendering: log through the grammar. `_render_value` moves to `_log_render`. |
| `src/fastmcp_pvl_core/_middleware.py` (modify) | Drop the `FASTMCP_ENABLE_RICH_LOGGING` read and the `structured=` argument. |
| `tests/test_log_render.py` (create) | The binding and both renderers, table-driven. |
| `tests/test_logging.py`, `tests/test_logging_middleware.py` (modify) | Mode selection; the middleware's line shape is unchanged and its existing assertions must still pass. |
| `README.md` (modify) | Document the two modes and the variable; remove `FASTMCP_ENABLE_RICH_LOGGING`. |

## Verified mechanics

Prototyped against this repo's own grammar module before the plan was written. Do not re-derive:

```
rich: cache_write ttl=3600 hit=True
json: {"ts":"…","level":"INFO","logger":"demo.mod","event":"cache_write","ttl":3600,"hit":true}

rich: tool_call_failed tool=read duration_ms=109.84 error="Section '1.3' not found"
json: {…,"event":"tool_call_failed","tool":"read","duration_ms":109.84,"error":"Section '1.3' not found"}

non-conforming → {"…","message":"Scanned 3 style(s) from /tmp"}
record.msg={'a': 1} (not a str) → message fallback, no raise
```

Typed values survive into JSON (`3600` int, `true` bool, `109.84` float) because the value comes from `record.args`, never from parsing rendered text.

## Not in scope, and tracked

- **pvl-core's own non-conforming log calls** — [#328](https://github.com/pvliesdonk/fastmcp-pvl-core/issues/328). Until that lands, most of the library's own records render as `message` in JSON mode. That is visible and expected.
- **Structured fields for domain code that doesn't follow the grammar** — template#611 and its per-repo children.
- **The #323 OTLP recipe** — child 5, docs only.

---

### Task 1: The renderer

**Files:**
- Create: `src/fastmcp_pvl_core/_log_render.py`
- Modify: `src/fastmcp_pvl_core/_logging_middleware.py` (move `_render_value`/`_render_fields` out)
- Test: `tests/test_log_render.py`

**Interfaces:**
- Consumes: `parse_log_template` from `._log_grammar`.
- Produces: `render_value(value) -> str`, `bind_record(record) -> tuple[str, tuple[_Field, ...]] | None`, `render_rich(record) -> str`, and `JsonFormatter(logging.Formatter)`. Tasks 2 and 3 import these.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_log_render.py` covering, table-driven where it fits:

- **Binding.** A conforming template with placeholder fields binds each to its arg in order; a literal field binds to its own text; a non-conforming template returns `None`; an arg-count mismatch returns `None`; a `record.msg` that is not a `str` returns `None`; `record.args` given as a `Mapping` (stdlib's single-dict form) returns `None`.
- **Rich.** `cache_write ttl=%d hit=%s` with `(3600, True)` renders `cache_write ttl=3600 hit=True`. A value containing whitespace or `"` is quoted and escaped, so `tool_call_failed tool=%s duration_ms=%s error=%s` with `("read", 109.84, "Section '1.3' not found")` renders **exactly** `tool_call_failed tool=read duration_ms=109.84 error="Section '1.3' not found"` — the README's documented line. A non-conforming record renders its formatted message unchanged.
- **JSON.** Every record is a single line of valid JSON. A conforming record carries `ts`, `level`, `logger`, `event` and one key per field, with `int`/`float`/`bool` preserved as JSON types rather than strings. A non-conforming record carries `message` instead of `event`. A record with `exc_info` carries an `exception` string. A value that is not JSON-native (e.g. a `Path`, an exception instance) is stringified rather than raising.
- **Never raises.** A record whose `msg` is an object with a `__str__` that raises still produces output — assert the formatter does not propagate. (If that proves impossible without swallowing real bugs, assert the narrower guarantee you can keep, and say so in the docstring.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_log_render.py -q`
Expected: collection error — `fastmcp_pvl_core._log_render` does not exist.

- [ ] **Step 3: Write the implementation**

Create `_log_render.py`. The binding, verified in the prototype:

```python
_JSON_NATIVE = (str, int, float, bool, type(None))


def bind_record(record: logging.LogRecord) -> tuple[str, tuple[_BoundField, ...]] | None:
    """Recover the event and fields a conforming record was logged with.

    Parses ``record.msg`` — the template written in source — and pairs each
    placeholder with its value from ``record.args``. Values are never read
    back out of a rendered string, so one containing a space, an ``=`` or a
    quote cannot confuse the result, and an ``int`` stays an ``int``.

    Returns ``None`` for anything that is not a conforming record, which is
    how every renderer falls back to the formatted message: a template that
    does not match the grammar, an argument count that disagrees with it, a
    ``msg`` that is not a ``str`` (a caller may log any object), or ``args``
    given as the stdlib's single-``Mapping`` form.
    """
```

Move `_render_value` here as `render_value` (the quoting rule is now a rendering concern, not a middleware one) and keep `_logging_middleware` importing it from here, so there is exactly one definition. `JsonFormatter.format` builds the envelope in the order §3 gives: `ts`, `level`, `logger`, then `event` + fields or `message`, then `trace_id`/`span_id` if present on the record, then `exception`.

Use `json.dumps(..., default=str)` so a stray non-serialisable value stringifies instead of raising.

- [ ] **Step 4: Run the tests** — `uv run pytest tests/test_log_render.py -q`, expect PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_log_render.py src/fastmcp_pvl_core/_logging_middleware.py tests/test_log_render.py
git commit -m "feat(logging): render records from the log-call grammar

Refs #327"
```

---

### Task 2: Mode selection

**Files:**
- Modify: `src/fastmcp_pvl_core/_logging.py`
- Test: `tests/test_logging.py`

**Interfaces:**
- Consumes: `JsonFormatter`, `render_rich` from Task 1.
- Produces: `{PREFIX}_LOG_FORMAT` handling inside `configure_logging_from_env`.

- [ ] **Step 1: Write the failing tests**

- `{PREFIX}_LOG_FORMAT=json` installs the JSON handler; every record on root is one parseable JSON object.
- `{PREFIX}_LOG_FORMAT=rich` installs the Rich pair, on a non-TTY too.
- Unset and stderr is a TTY → Rich. Unset and stderr is not a TTY → JSON. Monkeypatch the TTY check rather than the global `sys.stderr`, and say in the test why.
- An unrecognised value falls back to auto, silently — consistent with how an unknown `LOG_LEVEL` falls back to `INFO`.
- Case-insensitive (`JSON`, `Rich`).
- Idempotence still holds, including across a mode change: configure json → rich → json leaves exactly one owned chain at root. This is the case the `_OWNED_ATTR` marker exists for.
- In JSON mode an exception record carries `exception` and there is no second handler double-printing it.

- [ ] **Step 2: Run the tests to verify they fail** — the variable is not read yet.

- [ ] **Step 3: Implement**

Resolve the mode beside the level in `_resolve_level`'s neighbourhood, using the repo's `env(prefix, name)` helper. Auto-detection asks `sys.stderr.isatty()` defensively — a stream may not have `isatty`, and a closed one raises — treat any failure as "not a TTY", which is the safe default for a container.

In `_install_root_handlers`, branch on the mode: Rich installs the existing pair; JSON installs a single `StreamHandler(sys.stderr)` with `JsonFormatter`. Both carry `_OWNED_ATTR`. In JSON mode there is no separate traceback handler — the traceback is a field.

- [ ] **Step 4: Run the tests** — `uv run pytest tests/test_logging.py -q`, expect PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_logging.py tests/test_logging.py
git commit -m "feat(logging): choose the output format once, at the root handler

Refs #327"
```

---

### Task 3: The middleware logs through the grammar

**Files:**
- Modify: `src/fastmcp_pvl_core/_logging_middleware.py`, `src/fastmcp_pvl_core/_middleware.py`
- Test: `tests/test_logging_middleware.py`

**Interfaces:**
- Consumes: Tasks 1-2.
- Produces: a middleware that emits `logger.log(level, "<event> k=%s …", *values)` and no longer decides its own output shape.

- [ ] **Step 1: Study the existing tests first**

`tests/test_logging_middleware.py` asserts the emitted line's shape in both modes today. The Rich-mode assertions must keep passing **unchanged** — that is the proof the documented line is byte-identical. The structured-mode assertions describe a JSON payload the middleware built itself; those move to asserting what `JsonFormatter` produces for the same record.

- [ ] **Step 2: Write the failing tests**

- The middleware's record is conforming: `bind_record` on it returns the same event and fields the old `structured` payload carried.
- Field order is preserved — `tool` before `duration_ms`, and the trace ids last, as `README.md` documents.
- A field value containing whitespace still renders quoted in Rich mode.
- `wire_middleware_stack` no longer reads `FASTMCP_ENABLE_RICH_LOGGING`: set it to both values and assert it changes nothing.

- [ ] **Step 3: Implement**

`_emit` builds the template from the event name and the field names — `f"{event} " + " ".join(f"{name}=%s" for name in fields)` — and passes the values as args. Keep `exc_info` handling exactly as it is.

Delete the `structured` constructor parameter and the branch it selected, and in `_middleware.py` delete the `FASTMCP_ENABLE_RICH_LOGGING` read and the `structured=` argument.

Two things to get right:
- A field **name** is code-controlled, but if one ever contained a `%` or a space the template would break. Field names come from the middleware's own vocabulary, so a comment stating that is enough; do not add runtime validation for a case the code cannot produce.
- Every value goes through `%s`. That is what makes the Rich line byte-identical, because `render_value` then applies the same quoting the old `_render_fields` did.

- [ ] **Step 4: Run the tests** — `uv run pytest tests/test_logging_middleware.py tests/test_log_render.py -q`, expect PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fastmcp_pvl_core/_logging_middleware.py src/fastmcp_pvl_core/_middleware.py tests/test_logging_middleware.py
git commit -m "feat(logging)!: middleware logs through the grammar, not its own renderer

The request middleware built either key=value text or a JSON payload
itself, which is why JSON mode never covered anything but its own lines.
It now logs one conforming template and lets the root formatter render
it, so the process has one output format.

BREAKING CHANGE: FASTMCP_ENABLE_RICH_LOGGING is replaced by
{PREFIX}_LOG_FORMAT, and the middleware's structured= argument is gone.

Refs #327"
```

---

### Task 4: uvicorn access records as fields

**Files:**
- Modify: `src/fastmcp_pvl_core/_logging.py` (the access filter), `src/fastmcp_pvl_core/_log_render.py`
- Test: `tests/test_logging.py`, `tests/test_log_render.py`

**Interfaces:** consumes Tasks 1-3.

uvicorn's access record is not ours to re-template, so it cannot conform to the grammar. But the access filter already parses it — it reads the status and rewrites the path — so it is the one place that understands the record.

- [ ] **Step 1: Write the failing tests**

In JSON mode an access line carries `client`, `method`, `path` and `status` as fields rather than a formatted `message`, with `status` an `int` and `path` the **redacted** one. In Rich mode the line is unchanged from what child 2 shipped. A non-access record on that logger is untouched.

- [ ] **Step 2: Implement**

Have `_AccessLogFilter` attach the parsed values to the record under one namespaced attribute when it passes a record it understood, and have `JsonFormatter` use that attribute when present. Keep the redaction as the single source: the attribute carries the already-redacted path, so the two can never disagree.

- [ ] **Step 3: Run the tests**, then commit:

```bash
git commit -m "feat(logging): give uvicorn access lines real fields in JSON mode

Refs #327"
```

---

### Task 5: Docs

- [ ] Document the two modes, `{PREFIX}_LOG_FORMAT`, and the auto default in `README.md`'s `### Logging` section. Show one line in each mode, taken from a real run rather than written by hand.
- [ ] Remove every mention of `FASTMCP_ENABLE_RICH_LOGGING`. Grep for it across `README.md`, `src/`, and `.agents/skills/` — the previous PRs in this epic each shipped one stale doc that had to be swept afterwards, so do the sweep up front.
- [ ] State plainly that a record only becomes fields if its call follows the grammar, and point at #328 for pvl-core's own calls. Do not imply the library is already conformant.
- [ ] Update `.agents/skills/logging-standard/SKILL.md` if any claim there is now wrong.
- [ ] Commit as `docs(logging): document the two output formats`.

---

### Task 6: Full local gate

- [ ] `uv sync --all-extras`; `uv run pytest` (1248 at the start of this branch).
- [ ] `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`.
- [ ] 3.10 and 3.13 on the touched test files.
- [ ] **Show both modes end to end**, and paste the output into the report — a real server, a real request, once with `{PREFIX}_LOG_FORMAT=rich` and once with `json`, including an access line and a middleware line.
- [ ] Ask the controller to run `get_change_risk` and confirm `introduced == 0` **before** pushing.

---

## Self-review notes

- **Spec coverage.** §3's mode switch is Task 2; its envelope is Task 1; its "middleware stops rendering" is Task 3; its uvicorn-fields note is Task 4. §3a's grammar is reused, not reimplemented — this PR adds no second parser.
- **The byte-identical constraint** is asserted twice on purpose: once in Task 1 as a rendering unit test, once in Task 3 by leaving the existing middleware assertions untouched.
- **Type consistency.** `bind_record` returns `tuple[str, tuple[_BoundField, ...]] | None` in Task 1 and is consumed as such in Tasks 3 and 4. `render_value` is the single quoting rule, imported by the middleware rather than redefined.
- **Known risk.** Task 3 changes how every request line is produced. The existing middleware tests are the safety net; if they need editing to pass, that is a signal the line changed, not that the tests were wrong.
