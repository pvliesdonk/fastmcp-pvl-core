"""RFC 9530 Content-Digest header parser + policy helpers.

Parsing delegates to the ``http_sf`` library (RFC 8941 Structured
Fields); this module adds the spec-9530 policy layered on top:
which algorithms pvl-core supports, how a multi-algorithm dictionary
is reduced to a single selected entry, and whether a selected entry
satisfies a receiver's ``requireDigest`` constraint.

Bytes verification (computing or rehashing the body's digest) is NOT
in this module — that's the route's concern because it owns the
staging temp-file lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterable

import http_sf

SUPPORTED_ALGORITHMS = frozenset({"sha-256", "sha-384", "sha-512"})


def parse_header(
    header: str, *, preferred: Iterable[str] | None = None
) -> tuple[str, bytes] | None:
    """Parse a Content-Digest header into ``(algo, raw_digest_bytes)``.

    Returns ``None`` if the header is empty, malformed at the RFC 8941
    layer, or contains no supported algorithm. Otherwise returns the
    first supported entry, preferring ``preferred`` algorithms (if any
    are present and parse) and falling back to the first supported
    entry the dictionary lists.

    Unsupported algorithms within a multi-algorithm dictionary are
    silently skipped (RFC 9530 §3 MUST-ignore). Parameter dictionaries
    on entries (``algo=:bytes:;p=v``) are accepted and ignored.
    """
    if not header:
        return None
    try:
        parsed = http_sf.parse(header.encode("ascii"), tltype="dictionary")
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    preferred_set = {p.strip().lower() for p in preferred} if preferred else None
    fallback: tuple[str, bytes] | None = None
    for raw_label, entry in parsed.items():
        if not (isinstance(entry, tuple) and len(entry) == 2):
            continue
        value, _params = entry
        label = raw_label.lower()
        if label not in SUPPORTED_ALGORITHMS:
            continue
        if not isinstance(value, bytes):
            continue
        if preferred_set is not None and label in preferred_set:
            return label, value
        if fallback is None:
            fallback = (label, value)
    return fallback
