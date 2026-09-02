"""Content identity for incremental indexing.

A re-sync compares this hash and the three pipeline versions against what is
stored. All four equal means the document is byte-identical and was processed by
the same code — so it is skipped entirely rather than re-embedded, which is the
difference between a repository sync costing cents and costing hundreds of
dollars.

Hashed over the *normalised extracted text*, not the source bytes: a PDF
re-exported with a new creation timestamp has different bytes and identical
content, and re-embedding it would be pure waste.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


class HasText(Protocol):
    text: str


def content_hash(pages: Sequence[HasText]) -> str:
    h = hashlib.sha256()
    for p in pages:
        h.update(p.text.encode("utf-8"))
        h.update(b"\x1f")  # page separator, so ["ab"] and ["a","b"] differ
    return h.hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredState:
    content_hash: str | None
    parser_version: int
    chunker_version: int
    embedding_version: int


#: Cheapest sufficient action when something has changed.
ReindexScope = Literal["none", "embed-only", "full"]


def reindex_scope(stored: StoredState | None, nxt: StoredState) -> ReindexScope:
    if stored is None or not stored.content_hash:
        return "full"

    content_same = stored.content_hash == nxt.content_hash
    shape_same = (
        stored.parser_version == nxt.parser_version
        and stored.chunker_version == nxt.chunker_version
    )

    if not content_same or not shape_same:
        return "full"
    # Same text, same chunk boundaries, different embedding model or dimension:
    # the stored chunk rows are still correct, only their vectors are stale.
    if stored.embedding_version != nxt.embedding_version:
        return "embed-only"
    return "none"


def needs_reindex(stored: StoredState | None, nxt: StoredState) -> bool:
    return reindex_scope(stored, nxt) != "none"
