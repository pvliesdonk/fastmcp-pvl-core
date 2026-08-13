"""KV-backed background-job record store (ADR 0002 §5).

A ``JobStore`` persists :class:`~.records.JobRecord` entries over an
:class:`~key_value.aio.protocols.key_value.AsyncKeyValue` backend
(``build_kv_store(config, namespace="jobs")``) with a
``working → completed | failed`` lifecycle:

- :meth:`JobStore.create` registers a ``working`` record whose lifetime
  is the KV entry TTL — there is no sweep loop; an expired record (and a
  job orphaned by a process crash) simply vanishes from the backend, and
  a later poll reports the id as unknown or expired.
- :meth:`JobStore.finish` / :meth:`JobStore.fail` settle the record with
  the result payload or the error message, keeping the **remaining** TTL
  so the retention window is pinned at creation and never slides.
- :meth:`JobStore.get` looks a record up *within one subject scope*; an
  id owned by another scope is indistinguishable from an unknown id.

**Per-subject cap.** Each scope keeps an index record of its live job
ids. The cap is enforced lazily: only when the index reaches the cap is
it pruned against the backend (dropping ids whose records have expired)
before the create is rejected — so the common under-cap path costs one
index read and two writes, not O(cap) lookups.

**Correctness boundary.** The KV facade exposes no compare-and-set, so
one in-process :class:`asyncio.Lock` serialises every read-modify-write —
the same boundary as ``TransferStore`` (ADR 0001 §6.3): exact semantics
within the single serving process these servers run, plus restart
survival of the records themselves when the backend persists.

This module is **internal** — it is fronted by the
:class:`~._jobs.manager.Jobs` capability object; the record/error types
it traffics in are public via :mod:`._jobs.records`.

Intra-package imports stay relative so a fold-in is a directory rename.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .records import JobLimitExceededError, JobNotFoundError, JobRecord

if TYPE_CHECKING:
    from collections.abc import Callable

    from key_value.aio.protocols.key_value import AsyncKeyValue

_RECORD_COLLECTION = "records"
"""KV collection holding job records, under the ``jobs`` namespace."""

_INDEX_COLLECTION = "index"
"""KV collection holding one live-id index record per subject scope."""


def _scope_key(scope: str, job_id: str = "") -> str:
    """Compose the KV key for *scope* (+ optional job id).

    The scope is URL-quoted so a subject containing the delimiter cannot
    collide with (or forge) another scope's keys.
    """
    encoded = quote(scope, safe="")
    return f"{encoded}:{job_id}" if job_id else encoded


def _record_to_mapping(record: JobRecord) -> dict[str, Any]:
    return {
        "job_id": record.job_id,
        "status": record.status,
        "result": record.result,
        "error": record.error,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
    }


def _record_from_mapping(raw: dict[str, Any]) -> JobRecord:
    return JobRecord(
        job_id=str(raw["job_id"]),
        status=raw["status"],
        result=raw["result"],
        error=raw["error"],
        created_at=float(raw["created_at"]),
        started_at=float(raw["started_at"]),
        finished_at=(
            float(raw["finished_at"]) if raw["finished_at"] is not None else None
        ),
    )


class JobStore:
    """Subject-scoped, TTL-bounded store of background-job records."""

    def __init__(
        self,
        kv: AsyncKeyValue,
        *,
        result_ttl_s: float,
        max_per_subject: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire the store over a namespaced KV backend.

        Args:
            kv: Namespaced ``AsyncKeyValue`` (from
                ``build_kv_store(config, namespace="jobs")``).
            result_ttl_s: Record lifetime in seconds, from creation.
            max_per_subject: Live-record cap per subject scope.
            clock: Wall-clock source returning epoch seconds; injected
                only for deterministic tests. Wall clock (not monotonic)
                so timestamps persisted in KV stay meaningful across a
                restart.
        """
        self._kv = kv
        self._result_ttl_s = float(result_ttl_s)
        self._max_per_subject = int(max_per_subject)
        self._clock = clock
        self._lock = asyncio.Lock()

    async def create(self, scope: str, *, started_at: float | None = None) -> JobRecord:
        """Register a new ``working`` record under *scope*.

        Args:
            scope: The owning subject scope.
            started_at: Epoch when execution actually began; defaults to
                now. Deadline-promoted work passes the earlier call start
                so a poll's ``running_for_s`` covers the inline window.

        Returns:
            The stored record (its ``job_id`` is the caller's handle).

        Raises:
            JobLimitExceededError: If *scope* already holds
                ``max_per_subject`` live records after pruning expired ids.
        """
        now = self._clock()
        record = JobRecord(
            job_id=secrets.token_urlsafe(16),
            status="working",
            result=None,
            error=None,
            created_at=now,
            started_at=started_at if started_at is not None else now,
            finished_at=None,
        )
        async with self._lock:
            index_key = _scope_key(scope)
            raw_index = await self._kv.get(index_key, collection=_INDEX_COLLECTION)
            ids: list[str] = list(raw_index["ids"]) if raw_index else []
            if len(ids) >= self._max_per_subject:
                ids = await self._prune(scope, ids)
                if len(ids) >= self._max_per_subject:
                    raise JobLimitExceededError(
                        f"subject already has {len(ids)} live background "
                        f"jobs (cap {self._max_per_subject}); retry after "
                        "existing jobs expire."
                    )
            ids.append(record.job_id)
            await self._kv.put(
                _scope_key(scope, record.job_id),
                _record_to_mapping(record),
                collection=_RECORD_COLLECTION,
                ttl=self._result_ttl_s,
            )
            await self._kv.put(
                index_key,
                {"ids": ids},
                collection=_INDEX_COLLECTION,
                ttl=self._result_ttl_s,
            )
        return record

    async def _prune(self, scope: str, ids: list[str]) -> list[str]:
        """Drop ids whose records have expired from the backend.

        Called only on the over-cap path, under the store lock.
        """
        live: list[str] = []
        for job_id in ids:
            raw = await self._kv.get(
                _scope_key(scope, job_id), collection=_RECORD_COLLECTION
            )
            if raw is not None:
                live.append(job_id)
        return live

    async def finish(self, scope: str, job_id: str, result: dict[str, Any]) -> None:
        """Settle *job_id* as ``completed`` with its result payload.

        A record that expired mid-run is a no-op — there is nothing left
        to settle, and the poller already reports the id as unknown.
        """
        await self._settle(scope, job_id, status="completed", result=result, error=None)

    async def fail(self, scope: str, job_id: str, error: str) -> None:
        """Settle *job_id* as ``failed`` with the failure message."""
        await self._settle(scope, job_id, status="failed", result=None, error=error)

    async def cancel(self, scope: str, job_id: str) -> None:
        """Settle *job_id* as ``cancelled`` (its task was cancelled)."""
        await self._settle(scope, job_id, status="cancelled", result=None, error=None)

    async def _settle(
        self,
        scope: str,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        key = _scope_key(scope, job_id)
        async with self._lock:
            raw, remaining = await self._kv.ttl(key, collection=_RECORD_COLLECTION)
            # Fail closed on the anomalous live-record-without-TTL state as
            # well: create() always writes with a TTL, so a record missing
            # one was not minted here, and settling it would re-mint a full
            # retention window — the exact "never slides" violation the
            # remaining-TTL rule exists to prevent. Same handling as
            # _transfer/store.py's _read.
            if raw is None or remaining is None:
                return  # expired mid-run (or foreign record) — nothing to settle
            raw = dict(raw)
            raw["status"] = status
            raw["result"] = result
            raw["error"] = error
            raw["finished_at"] = self._clock()
            # Keep the REMAINING TTL: retention is pinned at creation and
            # never slides, so a subject's records cannot live forever by
            # being repeatedly settled.
            await self._kv.put(
                key, raw, collection=_RECORD_COLLECTION, ttl=float(remaining)
            )

    async def get(self, scope: str, job_id: str) -> JobRecord:
        """Return the record for *job_id* as visible to *scope*.

        Raises:
            JobNotFoundError: Unknown id, expired record, or an id owned
                by another scope — deliberately indistinguishable.
        """
        raw = await self._kv.get(
            _scope_key(scope, job_id), collection=_RECORD_COLLECTION
        )
        if raw is None:
            raise JobNotFoundError(
                f"no job {job_id!r} for this subject — the id is unknown, "
                "expired, or belongs to another caller."
            )
        return _record_from_mapping(dict(raw))
