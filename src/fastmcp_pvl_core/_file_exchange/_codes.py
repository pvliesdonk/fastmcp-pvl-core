"""§13 error codes for the file-exchange extension.

Ships the spec-defined codes as a single ``(str, Enum)`` mixin so each
member is both an enum (for autocompletion and grep-ability at call
sites) and a real ``str`` (for use anywhere a plain string is expected).

The code set is OPEN per §13: callers MAY pass arbitrary strings to
:func:`fastmcp_pvl_core.file_exchange.build_file_exchange_error`. This
module names the spec-defined values only.
"""

from __future__ import annotations

from enum import Enum


class TransferErrorCode(str, Enum):
    """The 9 error codes defined in §13 of the wire spec.

    Members are usable anywhere a ``str`` is expected (``str, Enum``
    mixin); ``TransferErrorCode.DIGEST_MISMATCH == "digest-mismatch"``.

    Stdlib :class:`enum.StrEnum` would be the natural choice on Python
    3.11+, but ``requires-python = ">=3.10"`` rules it out — the mixin
    form is the back-compat-safe equivalent.
    """

    NO_SUPPORTED_TRANSPORT = "no-supported-transport"
    DESCRIPTOR_EXPIRED = "descriptor-expired"
    NOT_ACCESSIBLE = "not-accessible"
    DIGEST_MISMATCH = "digest-mismatch"
    SIZE_MISMATCH = "size-mismatch"
    TOO_LARGE = "too-large"
    MIME_TYPE_REJECTED = "mime-type-rejected"
    UNSUPPORTED_REQUIREMENT = "unsupported-requirement"
    TRANSFER_FAILED = "transfer-failed"


KNOWN_CODES: frozenset[str] = frozenset(c.value for c in TransferErrorCode)
"""The 9 spec-defined codes as a frozenset for membership testing.

Derived from :class:`TransferErrorCode` at module load — single source
of truth. Callers SHOULD treat any code NOT in ``KNOWN_CODES`` as a
generic failure per §13's open-code-set rule.
"""
