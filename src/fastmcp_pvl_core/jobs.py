"""Public namespace for the dual-mode long-running-tools feature (ADR 0002).

Importing from ``fastmcp_pvl_core.jobs`` gathers the whole jobs surface in
one place and reads as an explicit "I am wiring long-running tools" — most
pointedly :func:`build_jobs`, the path-2 seam a downstream uses to build
its own long-running domain tool on pvl-core's mechanics. Every name here
is also available from the top-level ``fastmcp_pvl_core`` package; this
module is a cohesive re-export, not a second implementation. The feature's
mechanics stay in the internal ``_jobs`` package (relative imports,
foldable).
"""

from __future__ import annotations

from ._jobs import (
    JOB_POLL_TOOL_NAME,
    JOB_RETRY_AFTER_S,
    JobHandle,
    JobLimitExceededError,
    JobNotFoundError,
    JobRecord,
    Jobs,
    JobsConfig,
    JobStatus,
    build_jobs,
    register_job_tools,
    register_long_running_tool,
)

__all__ = [
    "JOB_POLL_TOOL_NAME",
    "JOB_RETRY_AFTER_S",
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
]
