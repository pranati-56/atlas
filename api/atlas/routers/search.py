"""Retrieval without generation — what the debugger calls.

Separate from /chat so the retrieval half can be exercised, tuned and evaluated
without paying for a completion, which is also what makes the eval harness cheap
enough to run on every push.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from atlas.deps import CurrentPrincipal
from atlas.retrieval import RetrievalParams, find_mentions, hybrid_search, rerank

router = APIRouter(tags=["search"])


class SearchParams(BaseModel):
    alpha: float = Field(default=0.5, ge=0, le=1)
    top_k: int = Field(default=8, ge=1, le=50)
    candidates: int = Field(default=60, ge=1, le=400)
    rrf_k: int = Field(default=60, ge=1, le=1000)
    min_score: float = Field(default=0.0, ge=0, le=1)
    rerank: bool = False
    rewrite_query: bool = True

    def to_params(self) -> RetrievalParams:
        return RetrievalParams(**self.model_dump())


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    params: SearchParams = Field(default_factory=SearchParams)
    document_ids: list[UUID] | None = None
    source_ids: list[UUID] | None = None
    metadata_filter: dict[str, Any] | None = None


@router.post("/search")
async def search(body: SearchRequest, principal: CurrentPrincipal) -> dict[str, Any]:
    trace = await hybrid_search(
        principal=principal,
        query=body.query,
        params=body.params.to_params(),
        document_ids=body.document_ids,
        source_ids=body.source_ids,
        metadata_filter=body.metadata_filter,
    )

    if body.params.rerank and trace.fused:
        trace.fused, ms = await rerank(body.query, trace.fused)
        trace.timings.rerank_ms = ms

    return {"trace": trace.to_dict()}


class MentionsRequest(BaseModel):
    needle: str = Field(min_length=2, max_length=200)
    source_ids: list[UUID] | None = None
    limit: int = Field(default=200, ge=1, le=1000)


@router.post("/mentions")
async def mentions(body: MentionsRequest, principal: CurrentPrincipal) -> dict[str, Any]:
    """Every place a literal string appears, grouped by source.

    "Find all references to the deprecated /v1/auth API" wants exhaustiveness,
    not similarity — the top 8 semantic matches is the wrong answer even when
    every one of them is relevant.
    """
    grouped = await find_mentions(
        principal=principal,
        needle=body.needle,
        source_ids=body.source_ids,
        limit=body.limit,
    )
    return {
        "needle": body.needle,
        "total": sum(len(v) for v in grouped.values()),
        "by_source": {
            kind: [
                {
                    **asdict(m),
                    "chunk_id": str(m.chunk_id),
                    "document_id": str(m.document_id),
                }
                for m in items
            ]
            for kind, items in grouped.items()
        },
    }
