"""Size-capped base64 decode primitive (ADR 0001 §11 #2).

A standalone, reusable capability: decode a base64 payload while bounding the
decoded size, so an attacker-supplied ``content_base64`` cannot expand into an
oversized in-memory buffer. Consumed by the base64-inline ingest source, but
usable anywhere a server accepts inline base64.

Strict by design:

- **Reject before materialising** — the decoded size is computed from the
  encoded length + padding and checked *before* :func:`base64.b64decode` runs,
  so an over-cap payload is never fully decoded into memory.
- **Reject invalid input** — decoding uses ``validate=True``, so non-alphabet
  characters (including embedded whitespace/newlines) raise rather than being
  silently discarded. Callers that hold wrapped base64 must unwrap it first.
"""

from __future__ import annotations

import base64
import binascii


def decode_base64_capped(data: str | bytes, *, max_bytes: int) -> bytes:
    """Decode standard base64 *data*, rejecting invalid or over-cap input.

    Args:
        data: A standard (unwrapped) base64 payload, ``str`` or ``bytes``.
        max_bytes: Positive cap on the **decoded** byte size.

    Returns:
        The decoded bytes (``len(result) <= max_bytes``).

    Raises:
        ValueError: On a non-positive *max_bytes*, invalid base64, or a payload
            whose decoded size exceeds *max_bytes*.
    """
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive, got {max_bytes}")

    # Reject an oversized payload before decoding it. For valid base64 the
    # decoded size is exactly ``len(data) // 4 * 3 - padding``; invalid-length
    # input is caught by the decode below. (A loose ``len * 3 // 4`` upper bound
    # would false-reject a padded payload sitting exactly on the cap.)
    padding = (
        data.count(b"=") if isinstance(data, (bytes, bytearray)) else data.count("=")
    )
    if (len(data) // 4) * 3 - padding > max_bytes:
        raise ValueError(f"base64 input decodes to more than the {max_bytes}-byte cap")

    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64: {exc}") from exc

    # Defensive exact check — the pre-check is exact for valid base64, but this
    # guards against any padding/estimation edge the pre-check missed.
    if len(decoded) > max_bytes:
        raise ValueError(
            f"base64 payload is {len(decoded)} bytes, exceeds the {max_bytes}-byte cap"
        )
    return decoded
