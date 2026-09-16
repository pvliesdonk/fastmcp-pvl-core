"""Turn a log record into output — the only module that knows the shape.

The family's logging standard writes ``logger.info("event_name key=%s",
value)``. Standard library logging keeps the two halves of that call apart
on the record: ``record.msg`` is the template the developer wrote,
``record.args`` holds the values. :func:`bind_record` recovers a
conforming record's **typed** fields by parsing the template
(``._log_grammar``) and pairing each placeholder with its argument — never
by reading a value back out of rendered text. That is why a value
containing a space, an ``=`` or a quote cannot confuse the result, and why
an ``int`` stays an ``int`` in JSON.

Two renderers consume that binding: :func:`render_rich` for the
``key=value`` text mode, :class:`JsonFormatter` for the one-object-per-line
mode. Both fall back to ``record.getMessage()`` for a non-conforming
record — non-conforming is the normal case for third-party records, and is
never treated as an error.

A log call must never raise because of how it was rendered. ``record.msg``
may be any object, not a ``str``; ``record.args`` may be a ``Mapping``
(stdlib's single-dict ``%(key)s`` form) rather than a tuple; a field value
may not be JSON-serialisable. Each of those degrades to the formatted
message rather than propagating.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from ._log_grammar import parse_log_template

# JSON-native types: passed through as-is in JSON mode so an ``int``/
# ``float``/``bool``/``None`` keeps its type instead of becoming a string.
_JSON_NATIVE = (str, int, float, bool, type(None))


@dataclass(frozen=True)
class _BoundField:
    """One field of a conforming record, paired with its actual value.

    *value* is the raw argument for a placeholder field (whatever type the
    caller passed) or the literal token's text for a literal field.
    *conversion* is the field's ``%``-conversion string (e.g. ``"%d"``) for
    a placeholder field, and ``None`` for a literal field — it is only
    needed to render Rich's textual form; JSON mode uses *value* directly.
    """

    name: str
    value: object
    conversion: str | None


def bind_record(
    record: logging.LogRecord,
) -> tuple[str, tuple[_BoundField, ...]] | None:
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
    msg = record.msg
    if not isinstance(msg, str):
        return None

    template = parse_log_template(msg)
    if template is None:
        return None

    args = record.args
    if isinstance(args, Mapping):
        return None
    if args is None:
        args = ()
    if not isinstance(args, tuple):
        # Defensive: normal ``Logger.info(msg, *args)`` usage always leaves
        # ``record.args`` a tuple, a ``Mapping``, or ``None``; anything else
        # means the record was built by hand in a shape this grammar does
        # not recognise.
        return None
    if len(args) != template.placeholder_count:
        return None

    args_iter = iter(args)
    fields = tuple(
        _BoundField(name=field.name, value=next(args_iter), conversion=field.conversion)
        if field.conversion is not None
        else _BoundField(name=field.name, value=field.literal, conversion=None)
        for field in template.fields
    )
    return template.event, fields


def render_value(value: object) -> str:
    """Render a field value for the rich (text) output mode.

    Strings containing whitespace or a double quote are wrapped in double
    quotes — with embedded backslashes, double quotes, and control
    characters (newline, carriage return, tab) escaped — so the record
    stays on one unambiguous ``key=value`` line; everything else renders
    bare.
    """
    text = str(value)
    if any(char.isspace() for char in text) or '"' in text:
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return '"' + escaped + '"'
    return text


def _render_fields(fields: tuple[_BoundField, ...]) -> str:
    """Join bound fields into ``key=value`` text, in source order.

    A placeholder field's own ``%``-conversion is applied to its argument
    first (so ``%d`` / ``%.1f`` / ``%r`` format the way the developer wrote
    them), then :func:`render_value`'s quoting rule is applied to the
    result. A literal field's token is already text and only needs
    quoting.

    The argument is wrapped in a one-element tuple before the ``%`` —
    ``conversion % (value,)`` rather than ``conversion % value`` — so a
    value that is itself a tuple (``logger.info("batch items=%s", (1,
    2))``) is treated as one formatting argument instead of being unpacked
    positionally, which is what ``%`` does with a bare tuple on its right
    and would otherwise raise ``TypeError``.
    """
    parts = []
    for field in fields:
        text = (
            field.conversion % (field.value,) if field.conversion else str(field.value)
        )
        parts.append(field.name + "=" + render_value(text))
    return " ".join(parts)


def _safe_message(record: logging.LogRecord) -> str:
    """``record.getMessage()``, guarded against a ``msg``/arg that raises.

    ``getMessage()`` calls ``str(self.msg)`` and, for a template, applies
    ``%`` formatting — either can raise if a caller logged an object whose
    ``__str__`` (or a ``%r``/``%s`` conversion of one of its arguments)
    raises. A log call must never raise because of how it was phrased, so
    this is the last line of defence: a formatted message when possible, a
    fixed placeholder when not.
    """
    try:
        return record.getMessage()
    except Exception:  # noqa: BLE001 - last-resort fallback, must not raise
        return f"<unrenderable log record: {record.name}>"


def render_rich(record: logging.LogRecord) -> str:
    """Render *record* as the family's ``event key=value`` text line.

    A conforming record renders its event name followed by its fields; a
    non-conforming one renders its normal formatted message, unchanged.
    Never raises: a conforming record whose grammar-conforming shape hides
    a runtime mismatch — a ``%d`` conversion given a value ``int()`` cannot
    format, or a value whose ``__str__`` itself raises — falls back to
    :func:`_safe_message` exactly like a non-conforming one.
    """
    bound = bind_record(record)
    if bound is not None:
        event, fields = bound
        try:
            rendered = _render_fields(fields)
        except Exception:  # noqa: BLE001 - a log call must never raise
            pass
        else:
            return f"{event} {rendered}" if rendered else event
    return _safe_message(record)


class JsonFormatter(logging.Formatter):
    """One JSON object per record, for log aggregators.

    Envelope, in order: ``ts`` (ISO-8601 UTC), ``level``, ``logger``, then
    either ``event`` plus one key per field (a conforming record) or
    ``message`` (the formatted message, for everything else), then
    ``trace_id``/``span_id`` when present on the record, then
    ``exception`` when the record carries a traceback.

    Uses ``json.dumps(..., default=str)`` so a field value that is not
    JSON-native (a ``Path``, an exception instance, ...) stringifies
    instead of raising.
    """

    def format(self, record: logging.LogRecord) -> str:
        envelope: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }

        bound = bind_record(record)
        conforming: dict[str, object] | None = None
        if bound is not None:
            event, fields = bound
            try:
                conforming = {"event": event}
                for field in fields:
                    value = field.value
                    conforming[field.name] = (
                        value if isinstance(value, _JSON_NATIVE) else str(value)
                    )
            except Exception:  # noqa: BLE001 - a log call must never raise
                conforming = None
        if conforming is not None:
            envelope.update(conforming)
        else:
            envelope["message"] = _safe_message(record)

        trace_id = getattr(record, "trace_id", None)
        span_id = getattr(record, "span_id", None)
        if trace_id is not None:
            envelope["trace_id"] = trace_id
        if span_id is not None:
            envelope["span_id"] = span_id

        if record.exc_info:
            envelope["exception"] = self.formatException(record.exc_info)

        return json.dumps(envelope, default=str)
