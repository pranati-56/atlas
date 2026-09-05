"""Permission-aware hybrid retrieval.

The ranking itself lives in SQL (db/migrations/002_search.sql): dense over HNSW,
lexical over BM25, fused by weighted RRF, with the caller's group keys inside
both channel scans. This module embeds the query, calls that function, and
shapes the result into the trace the debugger renders.

The function is asked for the whole union of both candidate lists rather than
just the fused top-k. That is what lets the playground show the two channels
disagreeing — and the fused score has to be normalised against the strongest hit
in the union, since raw RRF sums land around 0.008 and the min-score cutoff is
expressed on a 0..1 scale.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

from atlas import db
from atlas.config import settings
from atlas.llm import embed_one
from atlas.repo import Principal


@dataclass(slots=True)
class RetrievalParams:
    #: 0 = pure lexical, 1 = pure vector. Weighted RRF.
    alpha: float = 0.5
    #: Rows returned to the model after fusion.
    top_k: int = 8
    #: Rows pulled from *each* channel before fusion.
    candidates: int = 60
    #: RRF smoothing constant; 60 is the value from the original paper.
    rrf_k: int = 60
    #: Drop fused rows below this. 0 disables.
    min_score: float = 0.0
    rerank: bool = False
    rewrite_query: bool = True

    @classmethod
    def defaults(cls) -> RetrievalParams:
        cfg = settings()
        return cls(
            alpha=cfg.alpha,
            top_k=cfg.top_k,
            candidates=cfg.candidates,
            rrf_k=cfg.rrf_k,
        )

    def clamped(self) -> RetrievalParams:
        """Client-supplied values, bounded. `candidates` below `top_k` would
        silently truncate the fusion input, which looks like bad retrieval
        rather than a bad parameter."""
        return RetrievalParams(
            alpha=min(1.0, max(0.0, self.alpha)),
            top_k=min(50, max(1, self.top_k)),
            candidates=min(400, max(self.top_k, self.candidates, 10)),
            rrf_k=min(1000, max(1, self.rrf_k)),
            min_score=min(1.0, max(0.0, self.min_score)),
            rerank=self.rerank,
            rewrite_query=self.rewrite_query,
        )


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: UUID
    document_id: UUID
    document_title: str
    document_uri: str | None
    source_id: UUID
    source_kind: str
    external_id: str
    ordinal: int
    page: int | None
    heading: str | None
    content: str
    metadata: dict[str, Any]

    #: Cosine similarity, 0..1. None when found only by the lexical channel.
    dense_score: float | None
    #: BM25, unbounded above. None when found only by the vector channel.
    sparse_score: float | None
    #: Reciprocal-rank-fusion score, normalised 0..1 — the ordering key.
    fused_score: float
    #: Rank within each channel before fusion, 1-based. None = absent.
    dense_rank: int | None
    sparse_rank: int | None
    #: Set only when the reranker ran.
    rerank_score: float | None = None


@dataclass(slots=True)
class Timings:
    embed_ms: int = 0
    dense_ms: int = 0
    sparse_ms: int = 0
    fuse_ms: int = 0
    rerank_ms: int | None = None
    generate_ms: int | None = None
    total_ms: int = 0


@dataclass(slots=True)
class RetrievalTrace:
    query: str
    rewritten_query: str | None
    params: RetrievalParams
    dense: list[RetrievedChunk] = field(default_factory=list)
    sparse: list[RetrievedChunk] = field(default_factory=list)
    fused: list[RetrievedChunk] = field(default_factory=list)
    timings: Timings = field(default_factory=Timings)
    context_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        def chunk_dict(c: RetrievedChunk) -> dict[str, Any]:
            d = asdict(c)
            for key in ("chunk_id", "document_id", "source_id"):
                d[key] = str(d[key])
            return d

        return {
            "query": self.query,
            "rewritten_query": self.rewritten_query,
            "params": asdict(self.params),
            "dense": [chunk_dict(c) for c in self.dense],
            "sparse": [chunk_dict(c) for c in self.sparse],
            "fused": [chunk_dict(c) for c in self.fused],
            "timings": asdict(self.timings),
            "context_tokens": self.context_tokens,
        }


def _build(row: Any, max_fused: float) -> RetrievedChunk:
    metadata = row["metadata"]
    return RetrievedChunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        document_title=row["document_title"],
        document_uri=row["document_uri"],
        source_id=row["source_id"],
        source_kind=row["source_kind"],
        external_id=row["external_id"],
        ordinal=row["ordinal"],
        page=row["page"],
        heading=row["heading"],
        content=row["content"],
        metadata=(
            metadata if isinstance(metadata, dict) else json.loads(metadata or "{}")
        ),
        dense_score=row["dense_score"],
        sparse_score=row["sparse_score"],
        # Raw RRF sums are tiny and depend on rrf_k; the UI plots 0..1.
        fused_score=row["fused_score"] / max_fused,
        dense_rank=row["dense_rank"],
        sparse_rank=row["sparse_rank"],
    )


async def hybrid_search(
    *,
    principal: Principal,
    query: str,
    params: RetrievalParams | None = None,
    rewritten_query: str | None = None,
    document_ids: list[UUID] | None = None,
    source_ids: list[UUID] | None = None,
    metadata_filter: dict[str, Any] | None = None,
) -> RetrievalTrace:
    started = time.monotonic()
    p = (params or RetrievalParams.defaults()).clamped()
    search_text = (rewritten_query or query).strip()

    trace = RetrievalTrace(query=query, rewritten_query=rewritten_query, params=p)
    if not search_text:
        return trace

    # ── embed the question ───────────────────────────────────────────────
    t0 = time.monotonic()
    vector, _usage = await embed_one(search_text, "query")
    trace.timings.embed_ms = int((time.monotonic() - t0) * 1000)

    rows = await db.fetch(
        """
        select * from hybrid_search(
          $1, $2, $3, $4, $5::vector,
          $6, $7, $8, $9, $10,
          $11, $12, $13::jsonb, $14, $15
        )
        """,
        principal.tenant_id,
        principal.groups,
        principal.is_admin,
        search_text,
        db.to_vector(vector),
        p.top_k,
        p.candidates,
        p.alpha,
        p.rrf_k,
        0.0,  # min_score applied below, after normalisation
        document_ids,
        source_ids,
        json.dumps(metadata_filter) if metadata_filter else None,
        max(100, p.candidates),
        True,  # with_candidates: return the union so both channels are visible
    )

    if not rows:
        trace.timings.total_ms = int((time.monotonic() - started) * 1000)
        return trace

    first = rows[0]
    trace.timings.dense_ms = int(first["dense_ms"] or 0)
    trace.timings.sparse_ms = int(first["sparse_ms"] or 0)
    trace.timings.fuse_ms = max(1, int(first["fuse_ms"] or 0))

    max_fused = max((r["fused_score"] for r in rows), default=0.0) or 1e-12
    all_chunks = [_build(r, max_fused) for r in rows]

    column_depth = max(p.top_k, 10)
    trace.dense = sorted(
        (c for c in all_chunks if c.dense_rank is not None),
        key=lambda c: c.dense_rank or 0,
    )[:column_depth]
    trace.sparse = sorted(
        (c for c in all_chunks if c.sparse_rank is not None),
        key=lambda c: c.sparse_rank or 0,
    )[:column_depth]
    trace.fused = sorted(
        (c for c in all_chunks if c.fused_score >= p.min_score),
        key=lambda c: c.fused_score,
        reverse=True,
    )[: p.top_k]

    trace.timings.total_ms = int((time.monotonic() - started) * 1000)
    return trace


@dataclass(slots=True)
class Mention:
    chunk_id: UUID
    document_id: UUID
    document_title: str
    document_uri: str | None
    source_kind: str
    external_id: str
    page: int | None
    heading: str | None
    match_offset: int
    snippet: str
    occurrences: int


async def find_mentions(
    *,
    principal: Principal,
    needle: str,
    source_ids: list[UUID] | None = None,
    limit: int = 200,
) -> dict[str, list[Mention]]:
    """Exhaustive lexical lookup, grouped by source.

    "Find every place we mention /v1/auth" is not a retrieval question — it wants
    *all* occurrences, not the most similar passages. Embedding similarity is the
    wrong tool and would quietly return the top 8.
    """
    rows = await db.fetch(
        "select * from find_mentions($1, $2, $3, $4, $5, $6)",
        principal.tenant_id,
        principal.groups,
        principal.is_admin,
        needle,
        source_ids,
        limit,
    )

    grouped: dict[str, list[Mention]] = {}
    for r in rows:
        grouped.setdefault(r["source_kind"], []).append(
            Mention(
                chunk_id=r["chunk_id"],
                document_id=r["document_id"],
                document_title=r["document_title"],
                document_uri=r["document_uri"],
                source_kind=r["source_kind"],
                external_id=r["external_id"],
                page=r["page"],
                heading=r["heading"],
                match_offset=r["match_offset"],
                snippet=r["snippet"],
                occurrences=r["occurrences"],
            )
        )
    return grouped
