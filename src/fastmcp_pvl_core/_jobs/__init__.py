"""Dual-mode long-running tools shared across the ``*-mcp`` family.

Implements ADR 0002 §5: a KV-backed job-record store (internal), the
:class:`Jobs` capability object with its dual-mode
``run_with_deadline`` verb, and the two registration entry points —
``register_long_running_tool`` (path 1: wrap one domain coroutine) and
``register_job_tools`` (the one generic ``get_job_result`` polling
tool). ``build_jobs`` is the path-2 seam: the same mechanics with no
tools, for a downstream composing its own domain tool on them.

The public surface is the two entry points, the seam
(``build_jobs`` / ``Jobs``), the config section (``JobsConfig``), and
the wire-shape types (``JobRecord``, ``JobStatus``, ``JobHandle``,
``JOB_POLL_TOOL_NAME``, ``JOB_RETRY_AFTER_S``) with the seam-crossing
errors (``JobNotFoundError``, ``JobLimitExceededError``). The store,
key layout, and promotion bookkeeping stay internal — pvl-core owns
their shape.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

from .config import JobsConfig
from .manager import Jobs, build_jobs
from .records import (
    JOB_POLL_TOOL_NAME,
    JOB_RETRY_AFTER_S,
    JobHandle,
    JobLimitExceededError,
    JobNotFoundError,
    JobRecord,
    JobStatus,
)
from .register import register_job_tools, register_long_running_tool

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
