"""Retrieve, then generate, as one NDJSON stream.

One JSON object per line:

    {"type":"trace", "trace": {...}}          once, before the first token
    {"type":"delta", "text": "..."}           many
    {"type":"done",  "content", "citations"}  once
    {"type":"error", "error": "..."}          instead of done

The trace goes out before generation starts so the debugger fills in while the
answer is still being written — retrieval is the part worth watching, and making
the user wait for the last token to see why a passage was chosen defeats the
point of having a debugger.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from atlas.config import settings
from atlas.deps import CurrentPrincipal
from atlas.llm import StreamEnd, stream
from atlas.repo import Principal
from atlas.retrieval import (
    DEFAULT_SYSTEM_PROMPT,
    build_context,
    build_prompt,
    extract_citations,
    hybrid_search,
    rerank,
    rewrite_query,
    system_instruction,
)
from atlas.routers.search import SearchParams

log = logging.getLogger("atlas.chat")

router = APIRouter(tags=["chat"])

REFUSAL = (
    "Nothing in the corpus clears the current score threshold for this "
    "question.\n\nEither the answer is not in these documents, or retrieval is "
    "too tight: try lowering **min score**, raising **candidates**, or shifting "
    "**α** toward the lexical channel if the question contains identifiers or "
    "exact strings."
)

UNGROUNDED_INSTRUCTION = (
    "No passage from the user's corpus matched this question. Say that plainly "
    "in your first sentence, then answer from general knowledge if you can, "
    "making clear the answer is not grounded in their documents. Do not invent "
    "citation markers."
)


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    query: str = Field(min_length=1)
    history: list[Message] = Field(default_factory=list)
    params: SearchParams = Field(default_factory=SearchParams)
    document_ids: list[UUID] | None = None
    source_ids: list[UUID] | None = None
    model: str | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    temperature: float = Field(default=0.2, ge=0, le=1)
    max_output_tokens: int = Field(default=2048, ge=64, le=32_000)
    #: Refuse rather than answer when retrieval returns nothing.
    strict_grounding: bool = True


def _line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, default=str) + "\n").encode()


def _stop_note(reason: str | None) -> str | None:
    if reason in (None, "stop"):
        return None
    if reason == "max_tokens":
        return "Answer was cut off at the output token limit — raise it in settings."
    if reason == "safety":
        return "The model stopped this response on a safety filter."
    if reason == "refusal":
        return "The model declined this request."
    return f"Generation stopped early ({reason})."


async def _generate(body: ChatRequest, principal: Principal) -> AsyncIterator[bytes]:
    cfg = settings()
    deadline = time.monotonic() + cfg.max_query_ms / 1000

    try:
        # ── query rewrite ────────────────────────────────────────────────
        # Only with history to resolve against. On a first turn there is no
        # pronoun to expand, and a rewrite would just add a round trip.
        rewritten: str | None = None
        if body.params.rewrite_query and body.history:
            out = await rewrite_query(
                body.query, [m.model_dump() for m in body.history], model=body.model
            )
            if out and out != body.query:
                rewritten = out

        # ── retrieve ─────────────────────────────────────────────────────
        trace = await hybrid_search(
            principal=principal,
            query=body.query,
            params=body.params.to_params(),
            rewritten_query=rewritten,
            document_ids=body.document_ids,
            source_ids=body.source_ids,
        )

        if body.params.rerank and trace.fused:
            trace.fused, ms = await rerank(
                rewritten or body.query, trace.fused, model=body.model
            )
            trace.timings.rerank_ms = ms

        built = build_context(trace.fused)
        trace.context_tokens = built.tokens
        yield _line({"type": "trace", "trace": trace.to_dict()})

        if not built.used and body.strict_grounding:
            # Refuse locally: no context means no grounded answer is possible,
            # and calling the model would only produce a fluent guess.
            yield _line({"type": "delta", "text": REFUSAL})
            yield _line(
                {"type": "done", "content": REFUSAL, "citations": [], "generate_ms": 0}
            )
            return

        # ── generate ─────────────────────────────────────────────────────
        grounded = bool(built.used)
        started = time.monotonic()
        answer: list[str] = []
        end: StreamEnd | None = None

        async for item in stream(
            prompt=(
                build_prompt(rewritten or body.query, built.block)
                if grounded
                else f"Question: {body.query}"
            ),
            purpose="answer",
            system=(
                system_instruction(body.system_prompt, body.strict_grounding)
                if grounded
                else UNGROUNDED_INSTRUCTION
            ),
            model=body.model,
            max_tokens=body.max_output_tokens,
            temperature=body.temperature,
        ):
            if isinstance(item, StreamEnd):
                end = item
                break
            if time.monotonic() > deadline:
                log.warning("chat exceeded ATLAS_MAX_QUERY_MS; cutting the stream")
                break
            answer.append(item)
            yield _line({"type": "delta", "text": item})

        content = "".join(answer)
        citations = (
            [c.to_dict() for c in extract_citations(content, built.used)]
            if grounded
            else []
        )
        yield _line(
            {
                "type": "done",
                "content": content,
                "citations": citations,
                "generate_ms": int((time.monotonic() - started) * 1000),
                "stop_reason": end.stop_reason if end else "other",
                "cost_usd": round(end.usage.cost_usd, 6) if end else 0,
                "error": _stop_note(end.stop_reason if end else None),
            }
        )
    except Exception as exc:  # noqa: BLE001 — surfaced on the stream, not as a 500
        log.exception("chat failed")
        yield _line({"type": "error", "error": str(exc)})


@router.post("/chat")
async def chat(body: ChatRequest, principal: CurrentPrincipal) -> StreamingResponse:
    return StreamingResponse(
        _generate(body, principal),
        media_type="application/x-ndjson",
        headers={
            "cache-control": "no-store, no-transform",
            # Stops nginx and friends buffering the whole answer before flushing.
            "x-accel-buffering": "no",
        },
    )
