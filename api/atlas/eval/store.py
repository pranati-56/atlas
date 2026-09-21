"""Every query that touches an eval table.

Deliberately not in `repo.py`. That module is the request path — the SQL a user
waits on — and its rule is that the queries for a table live in one place. This
keeps the rule while keeping the two apart: nothing here runs during a request,
and nothing in `repo.py` runs during an eval.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from atlas import db

# ───────────────────────────────────────────────────────────────── datasets ──


async def ensure_dataset(
    tenant_id: UUID, name: str, description: str | None = None
) -> UUID:
    return await db.fetchval(
        """
        insert into eval_datasets (tenant_id, name, description)
        values ($1, $2, $3)
        on conflict (tenant_id, name) do update
          set description = coalesce(excluded.description, eval_datasets.description)
        returning id
        """,
        tenant_id,
        name,
        description,
    )


async def get_dataset(tenant_id: UUID, name: str) -> dict[str, Any] | None:
    row = await db.fetchrow(
        "select * from eval_datasets where tenant_id = $1 and name = $2",
        tenant_id,
        name,
    )
    return dict(row) if row else None


async def list_datasets(tenant_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select d.*,
               (select count(*) from eval_questions q where q.dataset_id = d.id)
                 as question_count
          from eval_datasets d
         where d.tenant_id = $1
         order by d.name
        """,
        tenant_id,
    )
    return [dict(r) for r in rows]


async def upsert_question(dataset_id: UUID, q: dict[str, Any]) -> UUID:
    """Keyed on the question text, so re-importing a file updates in place.

    Delete-and-reinsert would cascade to eval_results and erase the per-question
    history of every run recorded so far — the rows that make a regression
    diagnosable rather than merely visible.
    """
    return await db.fetchval(
        """
        insert into eval_questions
          (dataset_id, question, expected_answer, expected_chunk_ids,
           expected_document_ids, expected_sources, must_include,
           must_not_include, tags, difficulty, metadata)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
        on conflict (dataset_id, md5(question)) do update
          set expected_answer       = excluded.expected_answer,
              expected_chunk_ids    = excluded.expected_chunk_ids,
              expected_document_ids = excluded.expected_document_ids,
              expected_sources      = excluded.expected_sources,
              must_include          = excluded.must_include,
              must_not_include      = excluded.must_not_include,
              tags                  = excluded.tags,
              difficulty            = excluded.difficulty,
              metadata              = excluded.metadata
        returning id
        """,
        dataset_id,
        q["question"],
        q.get("expected_answer"),
        q.get("expected_chunk_ids") or [],
        q.get("expected_document_ids") or [],
        q.get("expected_sources") or [],
        q.get("must_include") or [],
        q.get("must_not_include") or [],
        q.get("tags") or [],
        q.get("difficulty"),
        json.dumps(q.get("metadata") or {}),
    )


async def list_questions(
    dataset_id: UUID, tags: list[str] | None = None
) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select * from eval_questions
         where dataset_id = $1
           and ($2::text[] is null or tags && $2)
         order by created_at
        """,
        dataset_id,
        tags,
    )
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────── title resolution ──


async def documents_by_title(tenant_id: UUID, titles: list[str]) -> dict[str, UUID]:
    """Title -> id, so a dataset file can name documents a person recognises.

    Titles are not unique. Where one matches several documents this returns the
    most recent, and `title_collisions` is what the importer warns from. Picking
    one silently would produce ground truth that looks labelled and is wrong,
    which is worse than no label at all.
    """
    rows = await db.fetch(
        """
        select distinct on (title) title, id
          from documents
         where tenant_id = $1 and title = any($2)
         order by title, created_at desc
        """,
        tenant_id,
        titles,
    )
    return {r["title"]: r["id"] for r in rows}


async def title_collisions(tenant_id: UUID, titles: list[str]) -> dict[str, int]:
    rows = await db.fetch(
        """
        select title, count(*) as n
          from documents
         where tenant_id = $1 and title = any($2)
         group by title
        having count(*) > 1
        """,
        tenant_id,
        titles,
    )
    return {r["title"]: int(r["n"]) for r in rows}


# ─────────────────────────────────────────────────────────────────────  runs ──


async def create_run(
    *,
    tenant_id: UUID,
    dataset_id: UUID,
    label: str,
    git_sha: str | None,
    config: dict[str, Any],
) -> UUID:
    return await db.fetchval(
        """
        insert into eval_runs (tenant_id, dataset_id, label, git_sha, config)
        values ($1, $2, $3, $4, $5::jsonb)
        returning id
        """,
        tenant_id,
        dataset_id,
        label,
        git_sha,
        json.dumps(config),
    )


async def record_result(
    *,
    run_id: UUID,
    question_id: UUID,
    retrieved_chunk_ids: list[UUID],
    retrieved_document_ids: list[UUID],
    ranks: dict[str, Any],
    answer: str | None,
    citations: list[dict[str, Any]],
    metrics: dict[str, Any],
    latency_ms: int | None,
    cost_usd: float | None,
    error: str | None,
) -> None:
    await db.execute(
        """
        insert into eval_results
          (run_id, question_id, retrieved_chunk_ids, retrieved_document_ids,
           ranks, answer, citations, metrics, latency_ms, cost_usd, error)
        values ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8::jsonb, $9, $10, $11)
        on conflict (run_id, question_id) do update
          set retrieved_chunk_ids    = excluded.retrieved_chunk_ids,
              retrieved_document_ids = excluded.retrieved_document_ids,
              ranks                  = excluded.ranks,
              answer                 = excluded.answer,
              citations              = excluded.citations,
              metrics                = excluded.metrics,
              latency_ms             = excluded.latency_ms,
              cost_usd               = excluded.cost_usd,
              error                  = excluded.error
        """,
        run_id,
        question_id,
        retrieved_chunk_ids,
        retrieved_document_ids,
        json.dumps(ranks),
        answer,
        json.dumps(citations),
        json.dumps(metrics),
        latency_ms,
        cost_usd,
        error,
    )


async def finish_run(
    run_id: UUID, *, status: str, metrics: dict[str, Any], question_count: int
) -> None:
    await db.execute(
        """
        update eval_runs
           set status         = $2::eval_run_status,
               metrics        = $3::jsonb,
               question_count = $4,
               finished_at    = now()
         where id = $1
        """,
        run_id,
        status,
        json.dumps(metrics),
        question_count,
    )


async def list_runs(
    tenant_id: UUID, dataset: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select s.*
          from eval_run_summary s
          join eval_runs r on r.id = s.id
         where r.tenant_id = $1
           and ($2::text is null or s.dataset = $2)
         limit $3
        """,
        tenant_id,
        dataset,
        limit,
    )
    return [dict(r) for r in rows]


async def find_run(tenant_id: UUID, label: str) -> dict[str, Any] | None:
    """By label, most recent first — a label is a name, not a key."""
    row = await db.fetchrow(
        """
        select * from eval_runs
         where tenant_id = $1 and label = $2
         order by started_at desc
         limit 1
        """,
        tenant_id,
        label,
    )
    return dict(row) if row else None


async def load_results(run_id: UUID) -> dict[str, dict[str, Any]]:
    """Per-question metrics for a run, keyed by question id, ready to compare."""
    rows = await db.fetch(
        """
        select r.question_id, r.metrics, r.latency_ms, r.cost_usd, q.question
          from eval_results r
          join eval_questions q on q.id = r.question_id
         where r.run_id = $1
        """,
        run_id,
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        raw = r["metrics"]
        metrics = dict(json.loads(raw) if isinstance(raw, str) else (raw or {}))
        metrics["question"] = r["question"]
        metrics["latency_ms"] = r["latency_ms"]
        out[str(r["question_id"])] = metrics
    return out


async def failures(run_id: UUID, limit: int = 20) -> list[dict[str, Any]]:
    """The questions worth looking at first: errors, then worst retrieval."""
    rows = await db.fetch(
        """
        select q.question, r.error, r.ranks, r.answer,
               (r.metrics->>'recall_at_k')::float as recall
          from eval_results r
          join eval_questions q on q.id = r.question_id
         where r.run_id = $1
           and (r.error is not null
                or coalesce((r.metrics->>'recall_at_k')::float, 1) < 1)
         order by (r.error is null),
                  (r.metrics->>'recall_at_k')::float nulls first
         limit $2
        """,
        run_id,
        limit,
    )
    return [dict(r) for r in rows]
