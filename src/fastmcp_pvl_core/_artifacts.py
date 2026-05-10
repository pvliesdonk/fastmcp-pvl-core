"""Backward-compatibility shim.

The real implementation now lives in :mod:`fastmcp_pvl_core._token_store`.
This module re-exports the public artifact-direction surface so existing
``from fastmcp_pvl_core._artifacts import ...`` imports keep working
during the deprecation window. Slated for removal one minor version
after introduction.
"""

from __future__ import annotations

import warnings

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

# stacklevel=2 makes the warning point at the caller's import line, not
# at this shim's line. Fired once per process implicitly via Python's
# default DeprecationWarning filter (Python suppresses repeated identical
# warnings from the same source).
warnings.warn(
    "fastmcp_pvl_core._artifacts is a deprecation shim; import from "
    "fastmcp_pvl_core (top-level) or fastmcp_pvl_core._token_store directly. "
    "Slated for removal one minor version after introduction.",
    DeprecationWarning,
    stacklevel=2,
)
