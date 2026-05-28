"""Contract tests for the Content-Digest parse + policy module.

Every spec edge previously surfaced as a route-level bug on PR #169
gets its own test here so the route layer can rely on the contract
without re-deriving it.
"""

import base64
import hashlib

from fastmcp_pvl_core._file_exchange import _content_digest


def test_supported_algorithms_set():
    assert _content_digest.SUPPORTED_ALGORITHMS == frozenset(
        {"sha-256", "sha-384", "sha-512"}
    )


def test_parse_header_single_sha256_entry():
    payload = b"hello"
    raw = hashlib.sha256(payload).digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(f"sha-256=:{b64}:")
    assert parsed == ("sha-256", raw)
