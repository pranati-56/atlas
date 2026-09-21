"""Running a dataset through the real pipeline.

The important word is *real*. This calls `hybrid_search`, `rerank`,
`build_context` and `extract_citations` — the same functions `/chat` calls, in
the same order — rather than reimplementing retrieval in a way that could drift
from production and quietly measure something nobody ships. The one deliberate
difference is that generation uses `complete` rather than `stream`: identical
model, identical prompt, and nothing here is watching tokens arrive.

Two things change the numbers, and both are recorded on the run so a result is
never presented without them.

**Who is asking.** Retrieval applies the caller's ACLs inside the scan, so an
admin and a support agent get different passages for the same question. An eval
that silently runs as an admin reports a recall the actual users never see.

**Concurrency.** Questions run in parallel by default because it is much faster,
but that makes p50/p95 contended and not comparable to production latency. The
run records the concurrency it used; `--concurrency 1` is the setting to reach
for when latency is the thing being measured.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from atlas import repo
from atlas.config import settings
from atlas.eval import store
from atlas.eval.metrics import (
    Accumulator,
    Retrieved,
    Truth,
    abstained,
    citation_accuracy,
    excludes,
    includes,
    ndcg_at_k,
    precision_at_k,
    ranked_keys,
    ranks_of,
    recall_at_k,
    reciprocal_rank,
)
from atlas.llm import complete, complete_json
from atlas.repo import Principal
from atlas.retrieval import (
    RetrievalParams,
    build_context,
    build_prompt,
    extract_citations,
    hybrid_search,
    rerank,
    system_instruction,
)

log = logging.getLogger("atlas.eval")


@dataclass
class RunConfig:
    params: RetrievalParams = field(default_factory=RetrievalParams.defaults)
    #: Off measures retrieval alone — fast, free, and enough to tune alpha.
    generate: bool = True
    #: An LLM judge for faithfulness. Costs money and is itself noisy, so the
    #: deterministic assertions carry the load and this stays opt-in.
    judge: bool = False
    model: str | None = None
    strict_grounding: bool = True
    concurrency: int = 4
    tags: list[str] | None = None

    def snapshot(self, as_user: str) -> dict[str, Any]:
        cfg = settings()
        p = self.params
        return {
            "alpha": p.alpha,
            "top_k": p.top_k,
            "candidates": p.candidates,
            "rrf_k": p.rrf_k,
            "min_score": p.min_score,
            "rerank": p.rerank,
            "generate": self.generate,
            "judge": self.judge,
            "strict_grounding": self.strict_grounding,
            "generation_model": self.model or cfg.generation_model,
            "embedding_model": cfg.embedding_model,
            "embedding_version": cfg.embedding_version,
            "as_user": as_user,
            "concurrency": self.concurrency,
            "tags": self.tags or [],
        }


@dataclass
class RunReport:
    run_id: UUID
    label: str
    dataset: str
    question_count: int
    metrics: dict[str, Any]
    errors: list[str]


def git_sha() -> str | None:
    """Best effort. A run from a tarball is still a run."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "total_claims": {"type": "integer"},
        "supported_claims": {"type": "integer"},
        "unsupported": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["total_claims", "supported_claims"],
}

_JUDGE_SYSTEM = (
    "You check whether an answer is supported by the passages it was given. "
    "Split the answer into individual factual claims. A claim is supported only "
    "if a passage states it — not if a passage merely makes it plausible, and "
    "not if you happen to know it is true. Ignore hedges, restatements of the "
    "question, and sentences that assert nothing. Count, then reply as JSON."
)


async def _faithfulness(answer: str, context: str, model: str | None) -> float | None:
    """Fraction of the answer's claims the context actually supports.

    Returns None rather than a guess when the judge fails or finds no claims — a
    judge that errors should shrink the sample, not move the number.
    """
    try:
        data, _raw, _usage = await complete_json(
            prompt=f"PASSAGES:\n{context}\n\nANSWER:\n{answer}",
            purpose="verify",
            schema=_JUDGE_SCHEMA,
            system=_JUDGE_SYSTEM,
            model=model,
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 — a judge failure is not a run failure
        log.warning("faithfulness judge failed: %s", exc)
        return None

    if not isinstance(data, dict):
        return None
    total = int(data.get("total_claims") or 0)
    supported = int(data.get("supported_claims") or 0)
    if total <= 0:
        return None
    return max(0.0, min(1.0, supported / total))


def _truth(question: dict[str, Any]) -> Truth:
    return Truth(
        chunk_ids=frozenset(str(x) for x in question["expected_chunk_ids"]),
        document_ids=frozenset(str(x) for x in question["expected_document_ids"]),
        sources=frozenset(question["expected_sources"]),
    )


async def _score_one(
    principal: Principal, question: dict[str, Any], cfg: RunConfig
) -> dict[str, Any]:
    """One question through the pipeline, scored. Never raises."""
    started = time.monotonic()
    out: dict[str, Any] = {
        "question_id": question["id"],
        "retrieved_chunk_ids": [],
        "retrieved_document_ids": [],
        "ranks": {},
        "answer": None,
        "citations": [],
        "cost_usd": 0.0,
        "error": None,
        "metrics": {},
    }

    try:
        # No query rewrite: rewriting resolves pronouns against conversation
        # history, and an eval question has none. Running it anyway would add a
        # model call, a cost and a source of variance, for no effect.
        trace = await hybrid_search(
            principal=principal, query=question["question"], params=cfg.params
        )
        if cfg.params.rerank and trace.fused:
            trace.fused, ms = await rerank(
                question["question"], trace.fused, model=cfg.model
            )
            trace.timings.rerank_ms = ms

        built = build_context(trace.fused)
        retrieved = [
            Retrieved(
                chunk_id=str(c.chunk_id),
                document_id=str(c.document_id),
                source_kind=c.source_kind,
            )
            for c in trace.fused
        ]
        out["retrieved_chunk_ids"] = [UUID(r.chunk_id) for r in retrieved]
        out["retrieved_document_ids"] = list(
            dict.fromkeys(UUID(r.document_id) for r in retrieved)
        )

        truth = _truth(question)
        level = truth.level
        metrics: dict[str, Any] = {"labelled": bool(level)}

        if level:
            keys = ranked_keys(retrieved, level)
            relevant = truth.relevant()
            k = cfg.params.top_k
            metrics.update(
                {
                    "level": level,
                    "recall_at_k": recall_at_k(keys, relevant, k),
                    "precision_at_k": precision_at_k(keys, relevant, k),
                    "reciprocal_rank": reciprocal_rank(keys, relevant),
                    "ndcg": ndcg_at_k(keys, relevant, k),
                    "hit": any(key in relevant for key in keys[:k]),
                }
            )
            out["ranks"] = ranks_of(keys, relevant)

        # ── generate ─────────────────────────────────────────────────────
        if cfg.generate:
            grounded = bool(built.used)
            if not grounded and cfg.strict_grounding:
                # What /chat does: refuse locally rather than let the model
                # write a fluent guess with no passages behind it.
                answer = "Nothing in the corpus clears the threshold for this question."
            else:
                res = await complete(
                    prompt=build_prompt(question["question"], built.block),
                    purpose="answer",
                    system=system_instruction(None, cfg.strict_grounding),
                    model=cfg.model,
                )
                answer = res.text
                out["cost_usd"] = round(res.usage.cost_usd, 6)

            out["answer"] = answer
            citations = extract_citations(answer, built.used)
            out["citations"] = [c.to_dict() for c in citations]

            metrics["citation_accuracy"] = citation_accuracy(
                [str(c.chunk_id) for c in citations],
                [r.chunk_id for r in retrieved],
                truth.chunk_ids,
            )

            if question["must_include"]:
                ok, missing = includes(answer, question["must_include"])
                metrics["includes_ok"] = ok
                if missing:
                    metrics["missing"] = missing
            if question["must_not_include"]:
                ok, present = excludes(answer, question["must_not_include"])
                metrics["excludes_ok"] = ok
                if present:
                    metrics["forbidden_present"] = present

            # Unanswerable questions are the tail that separates a system which
            # abstains from one that improvises. Scored only where labelled.
            if question["difficulty"] == "unanswerable":
                metrics["abstention_ok"] = abstained(answer)

            if cfg.judge and built.used and answer:
                score = await _faithfulness(answer, built.block, cfg.model)
                if score is not None:
                    metrics["faithfulness"] = score

        out["metrics"] = metrics

    except Exception as exc:  # noqa: BLE001 — one bad question is not a bad run
        log.warning("question failed: %s — %s", question["question"][:60], exc)
        out["error"] = f"{type(exc).__name__}: {exc}"

    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    return out


async def _principal_for(tenant_slug: str, as_user: str | None) -> Principal:
    if as_user:
        principal = await repo.load_principal(tenant_slug, as_user)
        if principal is None:
            raise ValueError(f"No user {as_user!r} in tenant {tenant_slug!r}.")
        return principal

    # Default to an admin so an unlabelled corpus is fully visible — but this is
    # the optimistic reading of recall, and the run config records it.
    row = await repo.first_admin(tenant_slug)
    if row is None:
        raise ValueError(
            f"Tenant {tenant_slug!r} has no admin to run as. "
            f"Pass --as <email>, or run atlas-seed first."
        )
    principal = await repo.load_principal(tenant_slug, row["email"])
    assert principal is not None
    return principal


async def run_dataset(
    *,
    tenant_slug: str,
    dataset: str,
    label: str,
    cfg: RunConfig,
    as_user: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> RunReport:
    principal = await _principal_for(tenant_slug, as_user)

    ds = await store.get_dataset(principal.tenant_id, dataset)
    if ds is None:
        raise ValueError(f"No dataset named {dataset!r}. Import one first.")

    questions = await store.list_questions(ds["id"], cfg.tags)
    if not questions:
        raise ValueError(f"Dataset {dataset!r} has no questions matching those tags.")

    run_id = await store.create_run(
        tenant_id=principal.tenant_id,
        dataset_id=ds["id"],
        label=label,
        git_sha=git_sha(),
        config=cfg.snapshot(principal.email),
    )

    accumulator = Accumulator()
    errors: list[str] = []
    gate = asyncio.Semaphore(max(1, cfg.concurrency))
    done = 0

    async def one(question: dict[str, Any]) -> None:
        nonlocal done
        async with gate:
            result = await _score_one(principal, question, cfg)

        metrics = dict(result["metrics"])
        metrics["latency_ms"] = result["latency_ms"]
        metrics["cost_usd"] = result["cost_usd"]
        if result["error"]:
            metrics["error"] = result["error"]
            errors.append(f"{question['question'][:60]}: {result['error']}")

        await store.record_result(
            run_id=run_id,
            question_id=question["id"],
            retrieved_chunk_ids=result["retrieved_chunk_ids"],
            retrieved_document_ids=result["retrieved_document_ids"],
            ranks=result["ranks"],
            answer=result["answer"],
            citations=result["citations"],
            metrics=metrics,
            latency_ms=result["latency_ms"],
            cost_usd=result["cost_usd"],
            error=result["error"],
        )
        accumulator.add(metrics)

        done += 1
        if on_progress:
            on_progress(done, len(questions))

    try:
        await asyncio.gather(*(one(q) for q in questions))
    except Exception:
        # A run that died halfway is marked failed rather than left "running"
        # forever — an unfinished row that looks live is worse than one that
        # admits it broke.
        await store.finish_run(
            run_id, status="failed", metrics=accumulator.summary(), question_count=done
        )
        raise

    summary = accumulator.summary()
    await store.finish_run(
        run_id, status="complete", metrics=summary, question_count=len(questions)
    )

    return RunReport(
        run_id=run_id,
        label=label,
        dataset=dataset,
        question_count=len(questions),
        metrics=summary,
        errors=errors,
    )
