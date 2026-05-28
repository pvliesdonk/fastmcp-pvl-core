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

SUPPORTED_ALGORITHMS = frozenset({"sha-256", "sha-384", "sha-512"})
