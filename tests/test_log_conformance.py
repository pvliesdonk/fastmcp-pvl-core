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
    write(
        tmp_path,
        'logger.info(\n    "event key=%s"\n    " other=%s",\n    1,\n    2,\n)\n',
    )
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
        "import logging\n\n_audit = logging.getLogger('audit')\n\n"
        "_audit.info('Prose here')\n"
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
    assert [(v.path.name, v.line) for v in found] == [
        ("a.py", 5),
        ("b.py", 5),
        ("b.py", 6),
    ]


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
