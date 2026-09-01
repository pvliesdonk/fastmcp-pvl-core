"""Wire-shape types and errors for the jobs subsystem (ADR 0002 §5).

Everything in this module is **public shape**: a path-2 downstream tool
(one built by hand on :class:`~._jobs.manager.Jobs`) returns the same
handle payload and reads the same record/status vocabulary as pvl-core's
own path-1 wrapper, so these types are exported rather than re-invented
per server. Tool *identity* on path 2 belongs to the downstream; the
payload and status *shape* stays pvl-core's.

The status vocabulary is the SEP-2663 ``TaskStatus`` literal set minus
``input_required`` (the fallback has no elicitation channel).
``"cancelled"`` is reserved for parity with the protocol lifecycle; the
fallback store does not emit it today.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

JobStatus = Literal["working", "completed", "failed", "cancelled"]

JOB_POLL_TOOL_NAME = "get_job_result"
"""Name of the generic polling tool (pvl-core-owned shape).

Referenced in every job-handle payload (``poll_with``) so a client knows
which tool retrieves the result — including handles minted by a path-2
downstream tool, which is why the constant is public.
"""

JOB_RETRY_AFTER_S = 5.0
"""Polling-interval hint carried in job-handle payloads (shape)."""


class JobHandle(TypedDict):
    """The payload a long-running tool returns when work is promoted.

    One shape for both paths: pvl-core's wrapper and a hand-built
    downstream tool return exactly this dict, so a client learns one
    polling contract per server.
    """

    status: JobStatus
    job_id: str
    poll_with: str
    retry_after_s: float
    message: str


class DeferredJobHandle(JobHandle):
    """A handle for work deliberately deferred by domain logic.

    ``reason`` is a runtime fact supplied by the downstream, such as an
    upstream provider's rate-limit response. It is present only on handles
    returned by :meth:`Jobs.defer`; existing ``start`` and deadline-promotion
    handles keep their established shape.
    """

    reason: str


@dataclass(frozen=True)
class JobRecord:
    """One background job as stored and as returned by lookups.

    Attributes:
        job_id: Unguessable URL-safe identifier returned to the caller.
        status: ``"working"`` until the work finishes, then
            ``"completed"`` or ``"failed"`` (``"cancelled"`` reserved).
        result: The finished payload on success; ``None`` otherwise.
            Stored as given when the tool returned a mapping; a
            non-mapping return value is wrapped as ``{"value": ...}`` so
            the record stays a JSON object.
        error: The failure message on error; ``None`` otherwise.
        created_at: Wall-clock epoch when the job record was created
            (promotion time for deadline-promoted work).
        started_at: Wall-clock epoch when execution began — for
            deadline-promoted work this precedes ``created_at`` by the
            inline window already spent.
        finished_at: Wall-clock epoch when it completed/failed; ``None``
            while working.
    """

    job_id: str
    status: JobStatus
    result: dict[str, Any] | None
    error: str | None
    created_at: float
    started_at: float
    finished_at: float | None


class JobNotFoundError(LookupError):
    """No job with this id is visible to the calling subject.

    Raised for ids that never existed, records that reached their TTL and
    expired, and ids owned by a *different* subject — the three cases are
    deliberately indistinguishable so a caller cannot probe other
    subjects' job ids.
    """


class JobLimitExceededError(RuntimeError):
    """The calling subject already has the maximum number of live jobs."""
