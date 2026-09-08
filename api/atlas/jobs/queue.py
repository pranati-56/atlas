"""Thin wrappers over the queue functions in db/migrations/003_jobs.sql.

The logic lives in SQL because that is where the concurrency control is:
`FOR UPDATE SKIP LOCKED` gives single-claim semantics across N workers with no
broker, and because the queue shares a transaction domain with the data, a job
can be enqueued atomically with the row that justifies it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from atlas import db


@dataclass(slots=True)
class Job:
    id: int
    tenant_id: UUID | None
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int

    @classmethod
    def from_row(cls, row: Any) -> Job:
        payload = row["payload"]
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            kind=row["kind"],
            payload=payload if isinstance(payload, dict) else json.loads(payload),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
        )


async def enqueue(
    *,
    kind: str,
    payload: dict[str, Any],
    tenant_id: UUID | None = None,
    priority: int = 100,
    max_attempts: int = 5,
    dedupe_key: str | None = None,
    run_after_seconds: float = 0,
    conn: Any = None,
) -> int | None:
    """Returns the job id, or None when a live job with the same dedupe key
    already exists.

    Pass `conn` to enqueue inside a caller's transaction — that is what makes
    "create the document row and schedule its ingest" atomic.
    """
    sql = """
        insert into jobs
          (tenant_id, kind, payload, priority, max_attempts, dedupe_key, run_after)
        values ($1, $2, $3::jsonb, $4, $5, $6, now() + make_interval(secs => $7))
        on conflict do nothing
        returning id
    """
    args = (
        tenant_id,
        kind,
        json.dumps(payload),
        priority,
        max_attempts,
        dedupe_key,
        run_after_seconds,
    )
    if conn is not None:
        return await conn.fetchval(sql, *args)
    return await db.fetchval(sql, *args)


async def claim(
    worker: str,
    batch: int = 1,
    lease_seconds: int = 300,
    kinds: list[str] | None = None,
) -> list[Job]:
    rows = await db.fetch(
        "select * from claim_jobs($1, $2, $3, $4)", worker, batch, lease_seconds, kinds
    )
    return [Job.from_row(r) for r in rows]


async def heartbeat(job_id: int, worker: str, lease_seconds: int = 300) -> bool:
    """Renews the lease on a long-running job.

    Without it a slow-but-healthy ingest would be reclaimed and run twice.
    """
    return bool(
        await db.fetchval("select heartbeat_job($1, $2, $3)", job_id, worker, lease_seconds)
    )


async def complete(job_id: int) -> None:
    await db.execute("select complete_job($1)", job_id)


async def fail(job_id: int, error: str) -> str:
    """Returns the resulting status: 'queued' (will retry) or 'dead'."""
    return str(await db.fetchval("select fail_job($1, $2)", job_id, error[:4000]))


async def reclaim_expired() -> int:
    """Returns jobs whose worker died mid-flight to the queue."""
    return int(await db.fetchval("select reclaim_expired_jobs()") or 0)


async def health() -> list[dict[str, Any]]:
    rows = await db.fetch("select * from job_queue_health order by kind, status")
    return [dict(r) for r in rows]
