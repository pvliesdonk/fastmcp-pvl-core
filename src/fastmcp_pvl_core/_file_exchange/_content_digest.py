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

# http_sf re-exports ``ser_dictionary`` into its package namespace but does
# not list it in ``__all__``, so mypy refuses ``http_sf.ser_dictionary(...)``.
# The submodule path is the cleanest mypy-clean alternative; revisit on any
# http-sf upgrade in case a public serialiser surfaces upstream.
from http_sf.dictionary import ser_dictionary as _ser_dictionary

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
    for label, entry in parsed.items():
        # http_sf enforces RFC 8941 lcalpha at parse time, so ``label`` is
        # already lowercase; SUPPORTED_ALGORITHMS membership is the only check
        # we need. The runtime narrowing on ``entry`` guards against
        # pathological return shapes from a future library version, not
        # against well-formed RFC 8941 input.
        if not (isinstance(entry, tuple) and len(entry) == 2):
            continue
        value, _params = entry
        if label not in SUPPORTED_ALGORITHMS:
            continue
        if not isinstance(value, bytes):
            continue
        if preferred_set is not None and label in preferred_set:
            return label, value
        if fallback is None:
            fallback = (label, value)
    return fallback


def satisfies_requirement(algo: str, required: Iterable[str] | None) -> bool:
    """Case-insensitive: does ``algo`` appear in ``required``?

    ``required=None`` or empty means no constraint — always True.
    ``algo`` and the ``required`` entries are compared after
    ``str.strip().lower()`` normalisation, so callers can pass an
    unvalidated wire-derived list without pre-normalising.
    """
    if not required:
        return True
    needle = algo.strip().lower()
    return needle in {r.strip().lower() for r in required}


def format_header(algo: str, raw: bytes) -> str:
    """Serialise ``(algo, raw)`` as an RFC 9530 Content-Digest value.

    Delegates to http_sf.ser_dictionary for the canonical RFC 8941
    form. ``algo`` is the lowercase algorithm label
    (``sha-256``/``sha-384``/``sha-512``); ``raw`` is the digest bytes.
    """
    return _ser_dictionary({algo: (raw, {})})
