"""Matrix row G1: extracted staging primitives match the download originals."""

import hashlib

from fastmcp_pvl_core._file_exchange import _staging


def test_chunk_constant_is_one_megabyte():
    assert _staging._CHUNK == 1024 * 1024


def test_hashlib_label_map_covers_sha_256_384_512():
    assert _staging._HASHLIB_BY_LABEL == {
        "sha-256": "sha256",
        "sha-384": "sha384",
        "sha-512": "sha512",
    }


def test_digest_verifier_unknown_label_unverifiable():
    hasher, expected_hex, unverifiable = _staging._digest_verifier("md5:abcd")
    assert hasher is None
    assert expected_hex == "abcd"
    assert unverifiable is True


def test_digest_verifier_known_label_returns_hasher():
    hasher, expected_hex, unverifiable = _staging._digest_verifier(
        "sha-256:" + "0" * 64
    )
    assert isinstance(hasher, type(hashlib.sha256()))
    assert expected_hex == "0" * 64
    assert unverifiable is False


def test_digest_verifier_none_declared_no_op():
    assert _staging._digest_verifier(None) == (None, None, False)


def test_digest_verifier_no_colon_is_unverifiable():
    """Pre-existing edge: "sha-256" without colon -> empty hex; treat as
    unverifiable so the caller raises DIGEST_MISMATCH instead of comparing
    a real hexdigest against the empty string."""
    assert _staging._digest_verifier("sha-256") == (None, None, True)


def test_digest_verifier_empty_hex_is_unverifiable():
    """Pre-existing edge: "sha-256:" (trailing colon, empty hex) -> nothing
    to verify against; unverifiable."""
    assert _staging._digest_verifier("sha-256:") == (None, None, True)


def test_digest_verifier_label_whitespace_is_stripped():
    """Pre-existing edge: a leading-space label (" sha-256:...") must not
    cause a valid declaration to be reported as an unsupported algorithm."""
    declared = " sha-256:" + "0" * 64
    hasher, expected_hex, unverifiable = _staging._digest_verifier(declared)
    assert isinstance(hasher, type(hashlib.sha256()))
    assert expected_hex == "0" * 64
    assert unverifiable is False


def test_write_chunk_writes_and_hashes(tmp_path):
    target = tmp_path / "buf.bin"
    with target.open("wb") as fh:
        h = hashlib.new("sha256")
        _staging._write_chunk(fh, h, b"abc")
        _staging._write_chunk(fh, h, b"def")
    assert target.read_bytes() == b"abcdef"
    assert h.hexdigest() == hashlib.sha256(b"abcdef").hexdigest()


def test_write_chunk_no_hasher_writes_only(tmp_path):
    target = tmp_path / "buf.bin"
    with target.open("wb") as fh:
        _staging._write_chunk(fh, None, b"abc")
    assert target.read_bytes() == b"abc"
