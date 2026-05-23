"""Constants and forward-compatibility helpers for the file-exchange spec.

Pins the upstream wire-spec version + commit SHA, exposes the §17.3
version-skew rule (`check_version_skew`) and §17.4 must-understand
check (`check_requires`) with their typed exceptions.

A bump of ``SPEC_SOURCE_SHA`` is a deliberate PR through the
``scripts/sync_file_exchange_spec.py --bump <sha>`` workflow; the CI
``file-exchange-spec-sync`` job enforces it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

SPEC_VERSION = "0.1"
NAMESPACE = "nl.liesdonk.file-exchange"
# These are the NAMESPACE-prefixed type tags as Literals (a Literal can't be
# built from an f-string); keep their values consistent with NAMESPACE above.
# Pinned by tests/_file_exchange/test_spec.py and the wire models' `type` fields.
HANDLE_TYPE: Literal["nl.liesdonk.file-exchange/transfer-handle"] = (
    "nl.liesdonk.file-exchange/transfer-handle"
)
TICKET_TYPE: Literal["nl.liesdonk.file-exchange/intake-ticket"] = (
    "nl.liesdonk.file-exchange/intake-ticket"
)

# Upstream pin. Bumped only via the sync script's --bump mode (see
# scripts/sync_file_exchange_spec.py); a PR review is the gate.
SPEC_SOURCE_SHA = "5f50a4e16a33a6bbc0888c142baec7fdfe858cb6"

# Wire-format ``<major>.<minor>`` pattern from schema/file-exchange.json.
# Single source of truth — `_wire.py` and `_capability.py` import this
# so a future spec bump that loosens the format updates one place.
VERSION_PATTERN = r"^[0-9]+\.[0-9]+$"

_IMPLEMENTED_MAJOR = 0
"""Major version of the spec this implementation conforms to (§17.3)."""

_KNOWN_REQUIRES: frozenset[str] = frozenset()
"""Feature identifiers this implementation understands (§17.4).

v0.1 defines no feature identifiers, so the set is empty. A future
minor version that introduces a feature identifier extends this set
alongside the spec amendment that defines it.
"""

_VERSION_FORMAT = re.compile(VERSION_PATTERN)


class UnsupportedVersionError(ValueError):
    """Raised when a file-exchange version string can't be used.

    Two reasons share this type so callers (notably #140's error
    envelope) can dispatch on a single class and inspect ``.reason``
    for the specifics:

    - ``"malformed"`` — the version string didn't match ``<major>.<minor>``.
      Direct callers bypassing the schema layer (which validates the
      regex) get this rather than an opaque ``int()`` ``ValueError``.
      Raised on every ``kind`` of :func:`check_version_skew` call.
    - ``"unsupported_major"`` — the major component differs from this
      implementation's :data:`_IMPLEMENTED_MAJOR`. Only raised for
      ``kind="reference"`` (§17.3 MUST-fail); capability-kind callers
      get ``False`` instead.

    ``.version`` exposes the offending value; ``.reason`` carries the
    branch above. The error message is parameterised on ``.reason`` so
    a malformed-input log line doesn't claim "major version not
    supported".
    """

    def __init__(
        self,
        version: str,
        *,
        reason: Literal["malformed", "unsupported_major"],
    ) -> None:
        self.version = version
        self.reason = reason
        if reason == "malformed":
            msg = (
                f"file-exchange version string is malformed (expected "
                f"<major>.<minor>): {version!r}"
            )
        else:
            msg = (
                f"file-exchange reference major version is not supported by "
                f"this implementation: {version!r}"
            )
        super().__init__(msg)


class UnsupportedRequirementError(ValueError):
    """Raised on a reference carrying unknown ``requires`` identifiers (§17.4).

    ``.unknown_features`` carries the full set as a frozenset so #140's
    error envelope can emit ``_meta["nl.liesdonk.file-exchange/error"].detail``
    without re-parsing the message.
    """

    def __init__(self, unknown_features: Iterable[str]) -> None:
        self.unknown_features: frozenset[str] = frozenset(unknown_features)
        super().__init__(
            f"file-exchange reference requires features unknown to this "
            f"implementation: {sorted(self.unknown_features)}"
        )


def check_version_skew(
    version: str, *, kind: Literal["reference", "capability"]
) -> bool:
    """Apply the §17.3 version-skew rule.

    Args:
        version: ``<major>.<minor>`` version string from a wire payload.
        kind: ``"reference"`` (Handle / Ticket — MUST-fail) or
            ``"capability"`` (peer capability — SHOULD-fail, return False).

    Returns:
        ``True`` if the peer's major equals this implementation's major
        (proceed regardless of minor — tolerant reading per §17.2).
        ``False`` if ``kind="capability"`` and the major differs
        (caller treats peer as non-participant).

    Raises:
        UnsupportedVersionError: the input is malformed, OR
            ``kind="reference"`` and the major differs. A malformed
            version is treated like a major mismatch on a reference —
            callers bypassing the schema layer get a typed error rather
            than an opaque ``int()`` ``ValueError``.
    """
    if not _VERSION_FORMAT.fullmatch(version):
        raise UnsupportedVersionError(version, reason="malformed")
    major_s, _, _ = version.partition(".")
    if int(major_s) != _IMPLEMENTED_MAJOR:
        if kind == "reference":
            raise UnsupportedVersionError(version, reason="unsupported_major")
        return False
    return True


def check_requires(requires: Iterable[str]) -> None:
    """Apply the §17.4 must-understand check.

    Raises:
        UnsupportedRequirementError: any entry is not in
            :data:`_KNOWN_REQUIRES`.
    """
    unknown = frozenset(requires) - _KNOWN_REQUIRES
    if unknown:
        raise UnsupportedRequirementError(unknown)
