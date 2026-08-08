"""The transfer feature's public import surface (issue #249).

``fastmcp_pvl_core.transfer`` is a cohesive re-export of the transfer feature,
not a second implementation; every name is the same object as the top-level
re-export, and the internal store/handler/token types stay unexported.
"""

from __future__ import annotations

import fastmcp_pvl_core
from fastmcp_pvl_core import transfer

_EXPECTED = {
    "TransferConfig",
    "TransferKind",
    "TransferLinks",
    "TransferReadResult",
    "TransferSink",
    "TransferValidator",
    "build_transfer_links",
    "register_transfer_routes",
}


def test_all_lists_the_full_surface() -> None:
    assert set(transfer.__all__) == _EXPECTED


def test_every_name_is_importable() -> None:
    for name in _EXPECTED:
        assert hasattr(transfer, name), name


def test_names_alias_top_level_not_reimplemented() -> None:
    for name in _EXPECTED:
        assert getattr(transfer, name) is getattr(fastmcp_pvl_core, name), name


def test_path2_names_reexported_top_level() -> None:
    assert "build_transfer_links" in fastmcp_pvl_core.__all__
    assert "TransferLinks" in fastmcp_pvl_core.__all__


def test_internals_not_reexported() -> None:
    for name in ("TransferStore", "TransferToken", "make_transfer_handler"):
        assert not hasattr(transfer, name), name
        assert not hasattr(fastmcp_pvl_core, name), name
