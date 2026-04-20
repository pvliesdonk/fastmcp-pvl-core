"""Smoke test that the package imports and exposes a version."""

from __future__ import annotations

import fastmcp_pvl_core


def test_package_has_version() -> None:
    """Package must expose a ``__version__`` attribute."""
    assert isinstance(fastmcp_pvl_core.__version__, str)
    assert fastmcp_pvl_core.__version__
