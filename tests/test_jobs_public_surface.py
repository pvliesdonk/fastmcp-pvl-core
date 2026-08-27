"""The jobs feature's public import surface (issue #268).

``fastmcp_pvl_core.jobs`` is a cohesive re-export of the jobs feature, not
a second implementation; every name is the same object as the top-level
re-export, and the internal store/manager mechanics stay unexported.
"""

from __future__ import annotations

import fastmcp_pvl_core
from fastmcp_pvl_core import jobs

_EXPECTED = {
    "JOB_POLL_TOOL_NAME",
    "JOB_RETRY_AFTER_S",
    "DeferredJobHandle",
    "JobHandle",
    "JobLimitExceededError",
    "JobNotFoundError",
    "JobRecord",
    "JobStatus",
    "Jobs",
    "JobsConfig",
    "build_jobs",
    "register_job_tools",
    "register_long_running_tool",
}


def test_all_lists_the_full_surface() -> None:
    assert set(jobs.__all__) == _EXPECTED


def test_every_name_is_importable() -> None:
    for name in _EXPECTED:
        assert hasattr(jobs, name), name


def test_same_objects_as_top_level() -> None:
    for name in _EXPECTED:
        assert getattr(jobs, name) is getattr(fastmcp_pvl_core, name), name


def test_store_stays_internal() -> None:
    assert not hasattr(jobs, "JobStore")
    assert "JobStore" not in fastmcp_pvl_core.__all__
