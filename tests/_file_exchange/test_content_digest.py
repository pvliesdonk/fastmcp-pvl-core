"""Contract tests for the Content-Digest parse + policy module.

Every spec edge previously surfaced as a route-level bug on PR #169
gets its own test here so the route layer can rely on the contract
without re-deriving it.
"""

from fastmcp_pvl_core._file_exchange import _content_digest


def test_supported_algorithms_set():
    assert _content_digest.SUPPORTED_ALGORITHMS == frozenset(
        {"sha-256", "sha-384", "sha-512"}
    )
