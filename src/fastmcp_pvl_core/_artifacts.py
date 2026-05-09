"""Backward-compatibility shim.

The real implementation now lives in :mod:`fastmcp_pvl_core._token_store`.
This module re-exports the public artifact-direction surface so existing
``from fastmcp_pvl_core._artifacts import ...`` imports keep working
during the deprecation window. Slated for removal one minor version
after introduction.
"""

from __future__ import annotations

from fastmcp_pvl_core._token_store import (
    ArtifactStore,
    TokenRecord,
    get_artifact_store,
    set_artifact_store,
)

__all__ = [
    "ArtifactStore",
    "TokenRecord",
    "get_artifact_store",
    "set_artifact_store",
]
