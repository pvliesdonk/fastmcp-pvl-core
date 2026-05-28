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


def test_supported_algorithms_match_staging_hashlib_map():
    """``_content_digest.SUPPORTED_ALGORITHMS`` and
    ``_staging._HASHLIB_BY_LABEL`` define the same algorithm set
    independently — the route looks up ``_HASHLIB_BY_LABEL[cd_algo]`` after
    ``parse_header`` has filtered against ``SUPPORTED_ALGORITHMS``, so the
    two collections must keep their key sets in sync. Drift would surface
    as a runtime ``KeyError`` in the rehash branch; this test pins the
    invariant so a future algorithm addition cannot land on only one
    side."""
    from fastmcp_pvl_core._file_exchange import _staging

    assert _content_digest.SUPPORTED_ALGORITHMS == frozenset(_staging._HASHLIB_BY_LABEL)


def test_parse_header_single_sha256_entry():
    payload = b"hello"
    raw = hashlib.sha256(payload).digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(f"sha-256=:{b64}:")
    assert parsed == ("sha-256", raw)


def test_parse_header_empty_returns_none():
    assert _content_digest.parse_header("") is None


def test_parse_header_malformed_returns_none():
    assert _content_digest.parse_header("not a structured field!!!") is None


def test_parse_header_malformed_base64_value_returns_none():
    """A structurally-valid SF dictionary key (sha-256) paired with an
    invalid byte-sequence payload should produce None — http_sf parses
    byte-sequences eagerly, so non-base64 inside the `:…:` framing raises
    at parse time. This is a different failure mode from
    ``test_parse_header_malformed_returns_none`` (which trips the
    structured-field grammar itself)."""
    assert _content_digest.parse_header("sha-256=:not!base64!:") is None


def test_parse_header_all_unsupported_returns_none():
    assert _content_digest.parse_header("md5=:YWJjZA==:, sha-3=:YWJjZA==:") is None


def test_parse_header_skips_unsupported_takes_supported():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(f"md5=:YWJjZA==:, sha-256=:{b64}:")
    assert parsed == ("sha-256", raw)


def test_parse_header_ignores_sf_parameters():
    raw = hashlib.sha256(b"hello").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(f"sha-256=:{b64}:;foo=bar;baz=42")
    assert parsed == ("sha-256", raw)


def test_parse_header_uppercase_label_rejected():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    # Per RFC 8941 dictionary keys are lcalpha; uppercase is rejected at
    # the wire layer (http_sf raises StructuredFieldError -> None). Pin
    # the rejection rather than the case-folding behaviour.
    assert _content_digest.parse_header(f"SHA-256=:{b64}:") is None


def test_parse_header_ows_around_equals_rejected():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    # http_sf rejects OWS around the `=` per strict RFC 8941 grammar
    # -> parser returns None.
    assert _content_digest.parse_header(f"sha-256= :{b64}:") is None


def test_parse_header_multi_supported_no_preferred_returns_first():
    raw256 = hashlib.sha256(b"x").digest()
    raw512 = hashlib.sha512(b"x").digest()
    b256 = base64.b64encode(raw256).decode("ascii")
    b512 = base64.b64encode(raw512).decode("ascii")
    parsed = _content_digest.parse_header(f"sha-256=:{b256}:, sha-512=:{b512}:")
    assert parsed == ("sha-256", raw256)


def test_parse_header_preferred_overrides_order():
    raw256 = hashlib.sha256(b"x").digest()
    raw512 = hashlib.sha512(b"x").digest()
    b256 = base64.b64encode(raw256).decode("ascii")
    b512 = base64.b64encode(raw512).decode("ascii")
    parsed = _content_digest.parse_header(
        f"sha-512=:{b512}:, sha-256=:{b256}:", preferred=["sha-256"]
    )
    assert parsed == ("sha-256", raw256)


def test_parse_header_preferred_absent_falls_back_to_first_supported():
    raw512 = hashlib.sha512(b"x").digest()
    b512 = base64.b64encode(raw512).decode("ascii")
    parsed = _content_digest.parse_header(f"sha-512=:{b512}:", preferred=["sha-256"])
    assert parsed == ("sha-512", raw512)


def test_parse_header_preferred_is_case_insensitive():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    parsed = _content_digest.parse_header(
        f"sha-512=:{base64.b64encode(hashlib.sha512(b'x').digest()).decode('ascii')}:, "
        f"sha-256=:{b64}:",
        preferred=["SHA-256"],
    )
    assert parsed == ("sha-256", raw)


def test_parse_header_preferred_is_unordered_tie_break_by_header():
    """When ``preferred`` lists multiple algorithms that the header all
    advertises, ``preferred`` is treated as an unordered set — the tie-break
    follows the header's dictionary order, NOT ``preferred``'s argument order.
    This matches the wire semantic of ``requireDigest`` (a set of accepted
    algorithms, no priority implied)."""
    raw256 = hashlib.sha256(b"x").digest()
    raw512 = hashlib.sha512(b"x").digest()
    b256 = base64.b64encode(raw256).decode("ascii")
    b512 = base64.b64encode(raw512).decode("ascii")
    # ``preferred`` argument lists sha-512 first, but the header lists
    # sha-256 first; tie-break wins for sha-256.
    parsed = _content_digest.parse_header(
        f"sha-256=:{b256}:, sha-512=:{b512}:",
        preferred=["sha-512", "sha-256"],
    )
    assert parsed == ("sha-256", raw256)
    # Reverse the header order to confirm the tie-break really follows it.
    parsed = _content_digest.parse_header(
        f"sha-512=:{b512}:, sha-256=:{b256}:",
        preferred=["sha-512", "sha-256"],
    )
    assert parsed == ("sha-512", raw512)


def test_satisfies_requirement_none_is_always_true():
    assert _content_digest.satisfies_requirement("sha-256", None) is True
    assert _content_digest.satisfies_requirement("anything", None) is True


def test_satisfies_requirement_empty_is_always_true():
    assert _content_digest.satisfies_requirement("sha-256", []) is True


def test_satisfies_requirement_exact_match():
    assert _content_digest.satisfies_requirement("sha-256", ["sha-256"]) is True


def test_satisfies_requirement_case_insensitive():
    assert _content_digest.satisfies_requirement("sha-256", ["SHA-256"]) is True
    assert _content_digest.satisfies_requirement("SHA-256", ["sha-256"]) is True


def test_satisfies_requirement_not_in_list():
    assert _content_digest.satisfies_requirement("sha-512", ["sha-256"]) is False


def test_satisfies_requirement_normalises_whitespace_in_required():
    assert _content_digest.satisfies_requirement("sha-256", [" sha-256 "]) is True


def test_satisfies_requirement_empty_string_entry_returns_false():
    """An ``ArtifactConstraints.requireDigest=[""]`` wire value is currently
    accepted by the Pydantic model (``min_length=1`` on the list constrains
    its length, not its elements) — see ``mcp-file-exchange-ext#16`` for the
    deferred spec evolution. Until that lands, an empty-string entry is
    syntactically valid input that no real algorithm can satisfy: the
    function returns False, and the route's response then becomes 400.
    Pinned here so a future "treat [\"\"] as no-constraint" change doesn't
    happen accidentally."""
    assert _content_digest.satisfies_requirement("sha-256", [""]) is False
    assert _content_digest.satisfies_requirement("sha-512", [""]) is False


def test_format_header_round_trips_via_parse():
    raw = hashlib.sha256(b"hello world").digest()
    header = _content_digest.format_header("sha-256", raw)
    parsed = _content_digest.parse_header(header)
    assert parsed == ("sha-256", raw)


def test_format_header_shape_matches_rfc_9530():
    raw = hashlib.sha256(b"x").digest()
    header = _content_digest.format_header("sha-256", raw)
    expected = "sha-256=:" + base64.b64encode(raw).decode("ascii") + ":"
    assert header == expected
