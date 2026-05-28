"""Matrix rows F3, F4: ``_media_range_matches`` (RFC 7231 §3.1.1.1).

Content-Digest helper tests moved to ``test_content_digest.py`` when
the parse + policy was extracted from ``_upload.py`` into
``_content_digest.py``.
"""

import pytest

from fastmcp_pvl_core._file_exchange._upload import _media_range_matches


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
        # */subtype is NOT a valid RFC 7231 media-range; reject it.
        ("application/json", ["*/json"], False),
        ("image/png", ["*/png"], False),
        ("application/json", ["bogus", "application/json"], True),
        ("", ["application/json"], False),
        ("application/json", [], False),
    ],
)
def test_media_range_matches_table(content_type, accept, expected):
    """F3."""
    assert _media_range_matches(content_type, accept) is expected
