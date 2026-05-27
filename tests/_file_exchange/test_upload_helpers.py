"""Matrix rows F1, F2, F3, F4, D4, D5: pure-function helpers in _upload."""

import base64
import hashlib

import pytest

from fastmcp_pvl_core._file_exchange._upload import (
    _content_digest_format,
    _content_digest_parse,
    _media_range_matches,
)


def test_content_digest_parse_sha256_well_formed():
    payload = b"hello"
    raw = hashlib.sha256(payload).digest()
    b64 = base64.b64encode(raw).decode("ascii")
    header = f"sha-256=:{b64}:"
    algo, decoded = _content_digest_parse(header)
    assert algo == "sha-256"
    assert decoded == raw


def test_content_digest_parse_sha512_well_formed():
    raw = hashlib.sha512(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    algo, decoded = _content_digest_parse(f"sha-512=:{b64}:")
    assert algo == "sha-512"
    assert decoded == raw


def test_content_digest_parse_tolerates_whitespace():
    raw = hashlib.sha256(b"x").digest()
    b64 = base64.b64encode(raw).decode("ascii")
    algo, decoded = _content_digest_parse(f"  sha-256 = :{b64}:  ")
    assert algo == "sha-256"
    assert decoded == raw


@pytest.mark.parametrize(
    "header",
    [
        "garbage",
        "sha-256:abcd",  # missing structured-field colons
        "md5=:YWJjZA==:",  # unsupported algo label
        "sha-256=::",  # empty value
        "sha-256=:not-base64!:",
        "sha-256=:YWJjZA",  # missing trailing colon
        "",
    ],
)
def test_content_digest_parse_malformed_or_unknown_returns_none(header):
    """D4 + D5: present-but-unparseable / unsupported-algo -> None."""
    assert _content_digest_parse(header) is None


def test_content_digest_format_round_trips():
    raw = hashlib.sha256(b"hello").digest()
    header = _content_digest_format("sha-256", raw)
    parsed = _content_digest_parse(header)
    assert parsed == ("sha-256", raw)


@pytest.mark.parametrize(
    "content_type,accept,expected",
    [
        ("application/json", ["application/json"], True),
        ("application/json; charset=utf-8", ["application/json"], True),
        ("image/png", ["image/*"], True),
        ("text/plain", ["image/*"], False),
        ("application/octet-stream", ["*/*"], True),
        ("APPLICATION/JSON", ["application/json"], True),
        ("application/json", ["text/plain", "application/json"], True),
        ("application/json", ["text/plain", "text/html"], False),
        ("", ["application/json"], False),
        ("application/json", [], False),
    ],
)
def test_media_range_matches_table(content_type, accept, expected):
    """F3."""
    assert _media_range_matches(content_type, accept) is expected
