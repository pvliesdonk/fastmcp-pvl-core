"""Matrix row E4: upload helpers are reachable via the public namespace
and listed in __all__."""

from fastmcp_pvl_core import file_exchange


def test_upload_helpers_importable_via_public_namespace():
    assert hasattr(file_exchange, "upload_receiver_mint")
    assert hasattr(file_exchange, "upload_sender_consume")
    assert hasattr(file_exchange, "register_file_exchange_routes")


def test_upload_helpers_in_all():
    assert "upload_receiver_mint" in file_exchange.__all__
    assert "upload_sender_consume" in file_exchange.__all__
    assert "register_file_exchange_routes" in file_exchange.__all__


def test_all_is_alphabetical():
    # The repo's existing __all__ convention is ASCII case-sensitive sort
    # (Capitalized names first, then lowercase) — same as
    # ``fastmcp_pvl_core.__all__`` and ``fastmcp_pvl_core._file_exchange.__all__``.
    # The point of pinning this is to catch "someone appended at the end"
    # bugs that drift the surface from its canonical order.
    names = list(file_exchange.__all__)
    assert names == sorted(names), (
        "file_exchange.__all__ must be ASCII-sorted (matches repo convention)"
    )
