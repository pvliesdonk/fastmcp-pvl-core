"""Contract tests for the ``decode_base64_capped`` primitive (ADR 0001 §11 #2).

The primitive decodes standard base64 with a decoded-size cap, and is strict:
invalid base64 raises rather than silently dropping non-alphabet characters, and
an over-cap payload is rejected *before* the full output is materialised (so a
base64 memory-bomb cannot defeat the cap).
"""

from __future__ import annotations

import base64

import pytest

from fastmcp_pvl_core._transfer.base64 import decode_base64_capped


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_decodes_valid_base64_str() -> None:
    assert decode_base64_capped(_b64(b"hello"), max_bytes=100) == b"hello"


def test_decodes_valid_base64_bytes_input() -> None:
    # Accepts the encoded payload as bytes too (b64decode does).
    assert decode_base64_capped(base64.b64encode(b"hello"), max_bytes=100) == b"hello"


def test_empty_input_decodes_to_empty_bytes() -> None:
    assert decode_base64_capped("", max_bytes=100) == b""


# --------------------------------------------------------------------------- #
# invalid base64 → ValueError (strict: no silent drop of non-alphabet chars)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        "!!!!",  # non-alphabet characters
        "abc",  # wrong length / incorrect padding
        "YQ=",  # truncated padding
        "aGVs bG8=",  # embedded whitespace (strict rejects — not silently stripped)
        "aGVs\nbG8=",  # embedded newline (MIME/PEM wrapping — must be unwrapped)
        "café",  # non-ASCII str: b64decode's internal .encode("ascii") fails,
        # and base64 re-raises it as a plain ValueError ("string argument should
        # contain only ASCII characters") — a ValueError, not a binascii.Error —
        # so it is caught by the `ValueError` arm of the except clause.
    ],
)
def test_invalid_base64_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        decode_base64_capped(bad, max_bytes=100)


def test_over_cap_bytes_input_raises() -> None:
    # Exercise the `bytes` branch of the padding pre-check on the over-cap path.
    with pytest.raises(ValueError):
        decode_base64_capped(base64.b64encode(b"x" * 100), max_bytes=10)


# --------------------------------------------------------------------------- #
# size cap
# --------------------------------------------------------------------------- #


def test_decoded_over_cap_raises() -> None:
    with pytest.raises(ValueError):
        decode_base64_capped(_b64(b"x" * 100), max_bytes=10)


def test_exact_cap_boundary_succeeds() -> None:
    # Decoded size exactly == max_bytes must pass (strict `>` overflow check).
    payload = b"x" * 10
    assert decode_base64_capped(_b64(payload), max_bytes=10) == payload


def test_one_over_cap_raises() -> None:
    with pytest.raises(ValueError):
        decode_base64_capped(_b64(b"x" * 11), max_bytes=10)


def test_over_cap_rejected_before_materialising(monkeypatch) -> None:
    # An over-cap payload must be rejected on the length pre-check, *before*
    # b64decode runs — otherwise the cap would not protect against a decode
    # memory-bomb. Make b64decode explode if reached, and assert the raise is
    # the pre-check's ValueError, not the sabotaged decode.
    def _boom(*_a, **_k):
        raise AssertionError("b64decode must not run for an over-cap payload")

    monkeypatch.setattr(base64, "b64decode", _boom)
    with pytest.raises(ValueError, match="decodes to more than"):
        decode_base64_capped(_b64(b"x" * 6), max_bytes=3)


@pytest.mark.parametrize(
    "attack",
    [
        "AB" + "=" * 100_000,  # all-trailing '=' — inflates a naive count
        "A=B=" * 50_000,  # embedded '=' interspersed
    ],
)
def test_padding_inflated_bomb_rejected_before_materialising(
    monkeypatch, attack: str
) -> None:
    # A crafted payload padded with many '=' must still be rejected on the
    # pre-check, before b64decode is handed the full attacker-sized string.
    # Clamping the padding count to 2 (max valid) keeps the estimate a true
    # lower bound so the estimate cannot be driven negative.
    def _boom(*_a, **_k):
        raise AssertionError("b64decode must not run for an over-cap payload")

    monkeypatch.setattr(base64, "b64decode", _boom)
    with pytest.raises(ValueError, match="decodes to more than"):
        decode_base64_capped(attack, max_bytes=64)


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_max_bytes_raises(bad: int) -> None:
    with pytest.raises(ValueError):
        decode_base64_capped(_b64(b"hi"), max_bytes=bad)
