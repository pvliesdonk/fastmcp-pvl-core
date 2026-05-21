"""FileExchangeCapability wire model + capability_declaration helper.

The helper *returns the dict* downstream places at
``capabilities.experimental["nl.liesdonk.file-exchange"]``. Wiring it
onto a FastMCP server's capability set is a later issue (the role-
registration helpers), out of scope here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from fastmcp_pvl_core._file_exchange._spec import (
    NAMESPACE,
    SPEC_VERSION,
    VERSION_PATTERN,
    check_version_skew,
)
from fastmcp_pvl_core._file_exchange._validation import validate_wire
from fastmcp_pvl_core._file_exchange._wire import _WireBase

logger = logging.getLogger(__name__)

Role = Literal["provider", "fetcher", "receiver", "sender"]

# Transports the v0.1 spec defines per role. Used only to emit a
# helpful warning when a downstream supplies a transport string
# outside this set — the spec's transport set is OPEN (§17.5), so we
# do not refuse.
_V01_KNOWN_PULL_TRANSPORTS = frozenset({"filesystem", "download"})
_V01_KNOWN_PUSH_TRANSPORTS = frozenset({"filesystem", "upload"})

_ROLE_TRANSPORTS: dict[Role, frozenset[str]] = {
    "provider": _V01_KNOWN_PULL_TRANSPORTS,
    "fetcher": _V01_KNOWN_PULL_TRANSPORTS,
    "receiver": _V01_KNOWN_PUSH_TRANSPORTS,
    "sender": _V01_KNOWN_PUSH_TRANSPORTS,
}


class FileExchangeCapability(_WireBase):
    """Peer capability under ``capabilities.experimental[NAMESPACE]`` (§5).

    ``roles`` is typed as ``dict[str, list[str]]`` rather than
    ``dict[Role, ...]``: the schema's ``additionalProperties: true``
    permits a peer to advertise a future role name. Outbound builders
    (:func:`capability_declaration`) constrain producers to the
    ``Role`` Literal — Postel's principle, applied at the type level.
    """

    version: str = Field(pattern=VERSION_PATTERN)
    roles: dict[str, list[str]]
    digests: list[str] = Field(default_factory=lambda: ["sha-256"])
    maxArtifactSize: int | None = Field(default=None, ge=0)  # noqa: N815

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> FileExchangeCapability | None:
        """Validate and construct from a raw peer capability.

        Returns ``None`` on major-version mismatch — §17.3 capability is
        a SHOULD-fail, not MUST-fail (caller treats peer as
        non-participant). Other validation failures raise
        :class:`WireFormatError` (jsonschema) or
        :class:`pydantic.ValidationError`.

        On the ``None`` path, emits a ``logging.WARNING`` naming the
        offending peer version + the implemented major + the
        namespace — operators reading the log can grep for the skip
        rather than debugging absence.
        """
        validate_wire(raw, kind="capability")
        cap = cls.model_validate(raw)
        if not check_version_skew(cap.version, kind="capability"):
            from fastmcp_pvl_core._file_exchange._spec import _IMPLEMENTED_MAJOR

            logger.warning(
                "file-exchange peer declares major-incompatible version %r "
                "(this implementation: major %d, namespace %r); treating "
                "peer as non-participant per §17.3.",
                cap.version,
                _IMPLEMENTED_MAJOR,
                NAMESPACE,
            )
            return None
        return cap


def capability_declaration(
    *,
    roles: Mapping[Role, Sequence[str]],
    digests: Sequence[str] = ("sha-256",),
    max_artifact_size: int | None = None,
) -> dict[str, Any]:
    """Build the value for ``capabilities.experimental[NAMESPACE]`` (§5).

    Args:
        roles: For each role this server plays, the transports it
            supports in that role. A role the server does not play is
            omitted. Empty role lists are allowed.
        digests: Digest algorithms the server can produce and verify.
            Defaults to ``("sha-256",)`` because the spec (§5) says
            omitting the field implies ``sha-256`` and every
            implementation MUST be able to verify ``sha-256``. This
            helper does not validate that ``sha-256`` is in the list;
            callers passing custom digests are responsible for
            ensuring their server actually supports it.
        max_artifact_size: Advisory upper bound, in bytes, on
            artifacts the server will produce or accept.

    Returns:
        A JSON-serializable dict ready for placement under
        ``capabilities.experimental["nl.liesdonk.file-exchange"]``.

    Warns:
        ``logging.WARNING`` when a role name is outside the four roles
        defined in v0.1, OR when a transport string is outside the
        v0.1 known set for its role. Both are informational only — the
        spec's role and transport sets are open per §17 — but a typo
        of ``"dowload"`` vs ``"download"`` surfaces in logs.
    """
    for role, transports in roles.items():
        known = _ROLE_TRANSPORTS.get(role)
        if known is None:
            # Unknown role — the spec's roles object has
            # additionalProperties: true, so this is legal but worth
            # surfacing. Don't run the transport check against an
            # empty set (which would produce misleading "outside set
            # []" messages for every transport).
            logger.warning(
                "file-exchange capability declares role %r outside "
                "v0.1's defined set %s; transport names not validated "
                "against this implementation's known set.",
                role,
                sorted(_ROLE_TRANSPORTS),
            )
            continue
        for t in transports:
            if t not in known:
                logger.warning(
                    "file-exchange capability declares transport %r for role "
                    "%r outside v0.1's defined set %s; ensure your "
                    "implementation really supports it.",
                    t,
                    role,
                    sorted(known),
                )

    out: dict[str, Any] = {
        "version": SPEC_VERSION,
        "roles": {role: list(transports) for role, transports in roles.items()},
        "digests": list(digests),
    }
    if max_artifact_size is not None:
        out["maxArtifactSize"] = max_artifact_size
    return out
