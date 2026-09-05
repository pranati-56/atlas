"""Query rewriting and reranking — the two optional LLM passes around retrieval.

Both are written so that failing is never fatal. An optional pass that can take
the whole query down with it is worse than no optional pass.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from atlas.llm import complete, complete_json
from atlas.retrieval.hybrid import RetrievedChunk

log = logging.getLogger("atlas.retrieval")


async def rewrite_query(
    query: str, history: list[dict[str, str]], *, model: str | None = None
) -> str:
    """Turns a follow-up into a standalone query.

    Retrieval has no conversation history — "what about the other one?" embeds to
    nothing useful. The rewrite is deliberately conservative: on anything
    unexpected it returns the original rather than a worse query.
    """
    if not history:
        return query

    recent = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')}"
        for m in history[-6:]
    )

    prompt = (
        "Rewrite the user's latest message as a single standalone search query "
        "for a document retrieval system. Resolve pronouns and implicit "
        "references using the conversation. Keep every identifier, error code, "
        "function name and version string exactly as written — they are what the "
        "lexical channel matches on. Do not answer the question. Reply with the "
        "query and nothing else.\n\n"
        f"Conversation:\n{recent}\n\n"
        f"Latest message: {query}\n\nStandalone query:"
    )

    try:
        res = await complete(
            prompt=prompt,
            purpose="rewrite",
            model=model,
            max_tokens=128,
            temperature=0,
        )
        cleaned = res.text.split("\n")[0].strip().strip("\"'")
        # A rewrite that collapsed to nothing, or ballooned, is a failed rewrite.
        if not cleaned or len(cleaned) > len(query) * 6 + 120:
            return query
        return cleaned
    except Exception:  # noqa: BLE001 — an optional pass must never fail the query
        log.warning("query rewrite failed; using the original", exc_info=True)
        return query


_RERANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {"type": "number", "minimum": 0, "maximum": 10},
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}


async def rerank(
    query: str, chunks: list[RetrievedChunk], *, model: str | None = None
) -> tuple[list[RetrievedChunk], int]:
    """Reorders the fused shortlist. Returns (chunks, elapsed_ms).

    A real cross-encoder scores the query and passage jointly in one forward
    pass; this asks the generation model for the same judgement in one call over
    the whole shortlist. Same shape of win — it sees the pair, not two
    independent vectors — at one round trip instead of N.

    Reorder only: the fused score keeps its meaning in the debugger, and
    overwriting it would make the bar chart disagree with the RRF arithmetic
    drawn next to it.
    """
    if len(chunks) < 2:
        return chunks, 0

    started = time.monotonic()
    listed = "\n\n".join(
        f"[{i}] {' '.join((c.heading or '').split())} {' '.join(c.content.split())}"[:1200]
        for i, c in enumerate(chunks)
    )
    prompt = (
        "Score how well each passage answers the query, from 0 (irrelevant) to "
        "10 (directly answers it). Judge each passage on its own content, not on "
        f"its position in the list.\n\nQuery: {query}\n\nPassages:\n{listed}\n\n"
        f"Return exactly {len(chunks)} scores, in the same order as the passages."
    )

    try:
        value, _raw, _usage = await complete_json(
            prompt=prompt,
            purpose="rerank",
            schema=_RERANK_SCHEMA,
            model=model,
            max_tokens=512,
            temperature=0,
        )
        scores = value.get("scores") if isinstance(value, dict) else None
        if not isinstance(scores, list) or len(scores) != len(chunks):
            # A malformed reranker response is not worth failing the query over;
            # fusion order is already a good answer.
            log.info(
                "rerank returned %s scores for %d passages; keeping fusion order",
                len(scores) if isinstance(scores, list) else "no",
                len(chunks),
            )
            return chunks, int((time.monotonic() - started) * 1000)

        for c, s in zip(chunks, scores, strict=True):
            c.rerank_score = min(1.0, max(0.0, float(s) / 10))

        ordered = sorted(
            chunks, key=lambda c: (c.rerank_score or 0, c.fused_score), reverse=True
        )
        return ordered, int((time.monotonic() - started) * 1000)
    except Exception:  # noqa: BLE001 — an optional pass must never fail the query
        log.warning("rerank failed; keeping fusion order", exc_info=True)
        return chunks, int((time.monotonic() - started) * 1000)
