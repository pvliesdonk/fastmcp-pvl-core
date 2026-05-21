"""jsonschema validation against the vendored file-exchange wire schema.

Spec-conformant first-pass validation (§5, §7, §13, §17.2 of the wire
spec). Pydantic models in :mod:`._wire` provide typed access on top;
their ``from_wire`` classmethods call into here first so a wire-format
failure surfaces as a :class:`WireFormatError` with an RFC 6901 JSON
Pointer locating the offending field — exactly the shape #140's error
envelope needs in ``_meta["nl.liesdonk.file-exchange/error"].detail``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import (  # type: ignore[import-untyped]
    ValidationError,
)

Kind = Literal["handle", "ticket", "capability", "error"]

_KIND_TO_DEF: dict[Kind, str] = {
    "handle": "TransferHandle",
    "ticket": "IntakeTicket",
    "capability": "FileExchangeCapability",
    "error": "TransferError",
}

_SCHEMA_PATH = Path(__file__).parent / "_schema" / "file-exchange.json"


class WireFormatError(ValueError):
    """Raised when a wire payload fails jsonschema validation.

    Carries the JSON Pointer path of the offending field so callers
    (notably #140's error envelope) can quote ``json_pointer`` in
    ``_meta["nl.liesdonk.file-exchange/error"].detail``.
    """

    def __init__(self, message: str, *, json_pointer: str) -> None:
        super().__init__(message)
        self.json_pointer = json_pointer

    @classmethod
    def from_jsonschema(cls, exc: ValidationError) -> WireFormatError:
        """Wrap a :class:`jsonschema.ValidationError` with an RFC 6901 pointer.

        The escape order is load-bearing: ``~`` becomes ``~0`` *first*,
        so a literal ``/`` (then escaped to ``~1``) doesn't round-trip
        into a literal ``~``. Future maintainers reordering these will
        silently break round-tripping; the dedicated tests exist to
        catch that regression.
        """
        parts = [
            str(p).replace("~", "~0").replace("/", "~1") for p in exc.absolute_path
        ]
        pointer = "/" + "/".join(parts)
        return cls(exc.message, json_pointer=pointer)


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    """Load and parse the vendored file-exchange wire schema."""
    return json.loads(_SCHEMA_PATH.read_text())  # type: ignore[no-any-return]


@lru_cache(maxsize=4)
def _validator_for(kind: Kind) -> Draft202012Validator:
    """Return a cached Draft 2020-12 validator scoped to ``kind``'s ``$defs`` entry."""
    schema = _load_schema()
    sub_schema = {
        "$ref": f"#/$defs/{_KIND_TO_DEF[kind]}",
        "$defs": schema["$defs"],
    }
    return Draft202012Validator(sub_schema)


def validate_wire(raw: Mapping[str, Any], *, kind: Kind) -> None:
    """Validate ``raw`` against the vendored wire schema for ``kind``.

    Args:
        raw: Raw decoded JSON payload — the dict received from the
            peer (handle, ticket, capability, or error envelope).
        kind: Which wire type to validate as. Determines which
            ``$defs/...`` entry the schema is scoped to.

    Raises:
        ValueError: ``kind`` is not one of the four wire types. The
            type system already constrains the literal at callsites;
            this catches the dynamic-callsite case (e.g. tests with a
            ``type: ignore``).
        WireFormatError: ``raw`` fails schema validation. The message
            and ``json_pointer`` mirror the underlying
            :class:`jsonschema.ValidationError`.
    """
    if kind not in _KIND_TO_DEF:
        raise ValueError(
            f"unknown wire kind {kind!r}; expected one of {sorted(_KIND_TO_DEF)}"
        )
    validator = _validator_for(kind)
    try:
        validator.validate(raw)
    except ValidationError as exc:
        raise WireFormatError.from_jsonschema(exc) from exc
