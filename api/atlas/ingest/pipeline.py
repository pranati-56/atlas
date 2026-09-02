"""The ingest pipeline: fetch -> extract -> chunk -> embed -> index.

Runs inside a worker, not inside the request that triggered it. Stage and
progress are written back to the documents row as it goes, so the corpus table
shows the pipeline happening rather than a spinner that either finishes or does
not.

The first thing it does is decide whether to do anything at all — see
`hashing.reindex_scope`. On a repository re-sync most documents are unchanged,
and skipping them is the difference between a sync costing cents and costing
hundreds of dollars.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from atlas import repo
from atlas.config import settings
from atlas.ingest.chunking import CHUNKER_VERSION, chunk
from atlas.ingest.extract import PARSER_VERSION, DocKind, ExtractionError, extract
from atlas.ingest.hashing import StoredState, content_hash, reindex_scope
from atlas.llm import embed

log = logging.getLogger("atlas.ingest")


@dataclass(slots=True)
class IngestRequest:
    tenant_id: UUID
    document_id: UUID
    kind: DocKind
    title: str
    acl_groups: list[str]
    acl_public: bool
    data: bytes | None = None
    text: str | None = None
    path: str | None = None
    #: Skip the unchanged-content check. Used by the reindex job after a
    #: chunker change that does not bump CHUNKER_VERSION.
    force: bool = False


@dataclass(slots=True)
class IngestResult:
    document_id: UUID
    scope: str
    chunk_count: int
    token_count: int
    skipped: bool


async def run_ingest(req: IngestRequest) -> IngestResult:
    cfg = settings()
    doc_id = req.document_id

    try:
        # ── extract ──────────────────────────────────────────────────────
        await repo.set_stage(doc_id, "extracting", progress=0.05)
        extraction = extract(kind=req.kind, data=req.data, text=req.text, path=req.path)

        digest = content_hash(extraction.pages)
        nxt = StoredState(
            content_hash=digest,
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_version=cfg.embedding_version,
        )
        stored = None if req.force else await repo.stored_state(doc_id)
        scope = "full" if req.force else reindex_scope(stored, nxt)

        if scope == "none":
            # Content and pipeline versions all match: nothing to do. The stage
            # is set to ready rather than left alone, so a previously failed
            # document that now matches recovers.
            await repo.set_stage(doc_id, "ready", progress=1.0, error=None)
            return IngestResult(doc_id, "none", 0, 0, skipped=True)

        if scope == "embed-only":
            # Same text, same chunk boundaries, new embedding model or
            # dimension. The chunk rows are still correct; only the vectors are
            # stale, so re-embed in place rather than re-chunking.
            stored_chunks = await repo.chunks_for_document(doc_id)
            await repo.set_stage(doc_id, "embedding", progress=0.0)

            async def on_progress(done: int, total: int) -> None:
                await repo.set_stage(doc_id, "embedding", progress=done / max(total, 1))

            vectors, _ = await embed(
                [c["context_text"] for c in stored_chunks],
                "document",
                on_progress=on_progress,
            )
            await repo.update_chunk_vectors(doc_id, vectors, cfg.embedding_version)
            await repo.set_stage(
                doc_id,
                "ready",
                progress=1.0,
                error=None,
                content_hash=digest,
                embedding_version=cfg.embedding_version,
            )
            await repo.refresh_corpus_stats(req.tenant_id)
            token_count = sum(int(c["token_count"]) for c in stored_chunks)
            return IngestResult(
                doc_id, "embed-only", len(stored_chunks), token_count, skipped=False
            )

        # ── chunk ────────────────────────────────────────────────────────
        await repo.set_stage(doc_id, "chunking", progress=0.15)
        chunks = chunk(
            kind=req.kind,
            title=req.title,
            pages=extraction.pages,
            lang=extraction.lang,
            path=req.path,
        )
        if not chunks:
            raise ExtractionError(
                "Extraction produced no chunks — the document appears empty."
            )

        token_count = sum(c.token_count for c in chunks)
        await repo.set_stage(
            doc_id,
            "embedding",
            progress=0.0,
            chunk_count=len(chunks),
            token_count=token_count,
        )

        # ── embed ────────────────────────────────────────────────────────
        # Batched inside `embed`; embedding a thousand chunks one at a time is
        # bounded by round-trip time, not by compute.
        async def report(done: int, total: int) -> None:
            await repo.set_stage(doc_id, "embedding", progress=done / max(total, 1))

        vectors, _usage = await embed(
            [c.context_text for c in chunks], "document", on_progress=report
        )

        # ── index ────────────────────────────────────────────────────────
        await repo.set_stage(doc_id, "indexing", progress=0.9)
        await repo.replace_chunks(
            tenant_id=req.tenant_id,
            document_id=doc_id,
            chunks=chunks,
            vectors=vectors,
            acl_groups=req.acl_groups,
            acl_public=req.acl_public,
            embedding_version=cfg.embedding_version,
        )
        await repo.refresh_corpus_stats(req.tenant_id)

        await repo.set_stage(
            doc_id,
            "ready",
            progress=1.0,
            error=None,
            chunk_count=len(chunks),
            token_count=token_count,
            content_hash=digest,
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_version=cfg.embedding_version,
        )
        return IngestResult(doc_id, "full", len(chunks), token_count, skipped=False)

    except ExtractionError as exc:
        # The document is the problem, not Atlas. Recorded and not retried —
        # a scanned PDF will still be a scanned PDF on the next attempt.
        log.info("ingest rejected %s: %s", doc_id, exc)
        await repo.set_stage(doc_id, "failed", progress=0, error=str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 — recorded, then re-raised to the queue
        log.exception("ingest failed for %s", doc_id)
        await repo.set_stage(doc_id, "failed", progress=0, error=str(exc))
        raise
