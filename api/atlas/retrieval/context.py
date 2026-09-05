"""Context assembly and citation extraction.

The marker a passage is given here is the marker the model must cite, and the
same number is what the UI turns into a clickable chip. Nothing renumbers
downstream — if the model writes [3], the third passage in this block is what it
is claiming to have used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from atlas.retrieval.hybrid import RetrievedChunk

#: Ceiling on context handed to the model, independent of top_k. A model given
#: 200k characters of context does not answer better; it answers slower and
#: buries the passage that mattered.
MAX_CONTEXT_CHARS = 60_000

PROMPT_VERSION = "grounded-v1"

DEFAULT_SYSTEM_PROMPT = (
    "You answer strictly from the provided context. Cite every claim with the "
    "bracketed marker it came from. If the context does not contain the answer, "
    "say so plainly rather than guessing."
)


@dataclass(slots=True)
class BuiltContext:
    block: str
    #: Passages that actually fit, in marker order. Marker is index + 1.
    used: list[RetrievedChunk]
    tokens: int


def build_context(chunks: list[RetrievedChunk]) -> BuiltContext:
    used: list[RetrievedChunk] = []
    parts: list[str] = []
    budget = MAX_CONTEXT_CHARS

    for c in chunks:
        source = " · ".join(
            p
            for p in (c.heading or c.document_title, f"p.{c.page}" if c.page else None)
            if p
        )
        entry = f"[{len(used) + 1}] {source}\n{c.content}"
        if len(entry) > budget and used:
            break
        budget -= len(entry)
        used.append(c)
        parts.append(entry)

    block = "\n\n---\n\n".join(parts)
    return BuiltContext(block=block, used=used, tokens=max(1, len(block) // 4))


def system_instruction(user_prompt: str, strict: bool) -> str:
    rules = [
        "Answer only from the passages provided in the user message.",
        "Cite with the bracketed marker of the passage you used, like [2], placed "
        "at the end of the sentence it supports. Cite every factual claim.",
        "Never cite a marker that does not appear in the context.",
        (
            "If the passages do not contain the answer, say so plainly and name "
            "what is missing. Do not fall back on general knowledge."
            if strict
            else "If the passages are incomplete, answer what they support, then "
            "state clearly which part is not covered by the context."
        ),
        "Do not repeat the passages verbatim at length — synthesise.",
    ]
    body = "\n".join(f"- {r}" for r in rules)
    return f"{user_prompt.strip()}\n\nRules:\n{body}"


def build_prompt(query: str, context: str) -> str:
    return f"Context passages:\n\n{context}\n\n---\n\nQuestion: {query}"


@dataclass(slots=True)
class Citation:
    #: 1-based marker rendered inline as [1], [2] …
    marker: int
    chunk_id: UUID
    document_id: UUID
    document_title: str
    document_uri: str | None
    source_kind: str
    page: int | None
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "document_title": self.document_title,
            "document_uri": self.document_uri,
            "source_kind": self.source_kind,
            "page": self.page,
            "snippet": self.snippet,
        }


_MARKER = re.compile(r"\[(\d{1,2})\]")


def _lead(content: str, limit: int = 240) -> str:
    """First sentence or two, trimmed to something quotable."""
    flat = re.sub(r"\s+", " ", content).strip()
    parts = re.split(r"(?<=[.!?])\s+", flat)
    out = parts[0] if parts else flat
    if len(out) < 90 and len(parts) > 1:
        out = f"{out} {parts[1]}"
    if len(out) > limit:
        out = re.sub(r"\s+\S*$", "", out[:limit]) + "…"
    return out


def extract_citations(answer: str, used: list[RetrievedChunk]) -> list[Citation]:
    """Citations for the markers the model actually wrote.

    Listing every retrieved passage would overstate the grounding — the sources
    panel should show what the answer leans on, not what retrieval happened to
    return. Markers pointing outside the context are dropped here; the renderer
    leaves the bare `[n]` visible in the text so the invented reference is still
    apparent to the reader.
    """
    seen: set[int] = set()
    out: list[Citation] = []

    for match in _MARKER.finditer(answer):
        marker = int(match.group(1))
        if marker in seen or not (1 <= marker <= len(used)):
            continue
        chunk = used[marker - 1]
        seen.add(marker)
        out.append(
            Citation(
                marker=marker,
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_title=chunk.document_title,
                document_uri=chunk.document_uri,
                source_kind=chunk.source_kind,
                page=chunk.page,
                snippet=_lead(chunk.content),
            )
        )

    return sorted(out, key=lambda c: c.marker)
