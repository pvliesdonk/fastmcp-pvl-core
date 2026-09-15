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
