"""Job handlers, keyed by job kind.

A handler is a coroutine taking the job payload. Raising means the job failed:
the worker records the error and `fail_job` decides whether it retries with
backoff or goes to the dead letter. Returning means it succeeded.

Handlers must be idempotent. `SKIP LOCKED` prevents two workers claiming the
same job, but a worker that dies after finishing the work and before marking it
complete will have the job reclaimed and run again — at-least-once, not
exactly-once. The ingest pipeline's content-hash check is what makes the common
case cheap on a repeat.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from atlas import db, repo
from atlas.ingest import IngestRequest, run_ingest

log = logging.getLogger("atlas.jobs")

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class NonRetryable(Exception):
    """Raised by a handler when retrying cannot help.

    A scanned PDF will still be a scanned PDF on the next attempt, and burning
    five attempts on it delays real work in the queue.
    """


# ────────────────────────────────────────────────────────────────────  ingest ──


async def ingest_document(payload: dict[str, Any]) -> None:
    document_id = UUID(payload["document_id"])
    tenant_id = UUID(payload["tenant_id"])

    doc = await repo.get_document(tenant_id, document_id)
    if doc is None:
        # Deleted between enqueue and claim. Not an error — the work is moot.
        log.info("ingest skipped: document %s no longer exists", document_id)
        return

    blob = await db.fetchrow(
        "select bytes, mime from document_blobs where document_id = $1", document_id
    )

    text: str | None = payload.get("text")
    data: bytes | None = bytes(blob["bytes"]) if blob is not None else None
    if data is None and text is None:
        raise NonRetryable(
            f"No bytes and no text for document {document_id}. The upload blob "
            f"was removed before the worker claimed the job."
        )

    result = await run_ingest(
        IngestRequest(
            tenant_id=tenant_id,
            document_id=document_id,
            kind=doc["kind"],
            title=doc["title"],
            acl_groups=list(doc["acl_groups"]),
            acl_public=doc["acl_public"],
            data=data,
            text=text,
            path=payload.get("path") or doc["external_id"],
            force=bool(payload.get("force")),
        )
    )

    # The chunks are the durable artefact; the original upload is not. Dropping
    # it here is what stops the blob table growing without bound.
    if not result.skipped:
        await db.execute("delete from document_blobs where document_id = $1", document_id)
    log.info(
        "ingested %s: scope=%s chunks=%d", document_id, result.scope, result.chunk_count
    )


async def reindex_document(payload: dict[str, Any]) -> None:
    """Re-runs ingest for a document whose content can still be supplied.

    Uploads cannot be reindexed once their blob has been dropped; connector
    documents re-fetch from the upstream, which is why a source-wide reindex
    goes through `sync_source` instead.
    """
    await ingest_document({**payload, "force": True})


async def reindex_tenant(payload: dict[str, Any]) -> None:
    """Fans out one reindex job per document whose embedding_version is stale.

    Enqueued after a model or dimension change. One job per document rather than
    one long job so progress is visible, failures are isolated, and the work
    spreads across every worker.
    """
    from atlas.config import settings
    from atlas.jobs import queue

    tenant_id = UUID(payload["tenant_id"])
    target = int(payload.get("embedding_version") or settings().embedding_version)

    rows = await db.fetch(
        """
        select distinct d.id
          from documents d
          join chunks c on c.document_id = d.id
         where d.tenant_id = $1 and c.embedding_version <> $2
        """,
        tenant_id,
        target,
    )

    for row in rows:
        await queue.enqueue(
            kind="reindex_document",
            tenant_id=tenant_id,
            payload={"document_id": str(row["id"]), "tenant_id": str(tenant_id)},
            # Below interactive ingests: a background migration must not starve
            # a user waiting on an upload.
            priority=500,
            dedupe_key=f"reindex:{row['id']}",
        )
    log.info("reindex_tenant %s: enqueued %d document(s)", tenant_id, len(rows))


# ────────────────────────────────────────────────────────────────────  source ──


async def sync_source(payload: dict[str, Any]) -> None:
    """Walks a connector and enqueues an ingest per changed document."""
    from atlas.connectors import get_connector

    tenant_id = UUID(payload["tenant_id"])
    source_id = UUID(payload["source_id"])

    source = await repo.get_source(tenant_id, source_id)
    if source is None:
        log.info("sync skipped: source %s no longer exists", source_id)
        return

    connector = get_connector(source["kind"])
    if connector is None:
        raise NonRetryable(f"No connector registered for source kind {source['kind']!r}")

    await repo.set_source_status(source_id, "syncing")
    try:
        count = await connector.sync(tenant_id=tenant_id, source=source)
        await repo.set_source_status(source_id, "idle")
        await db.execute(
            "update sources set document_count = $2 where id = $1", source_id, count
        )
        log.info("synced source %s: %d document(s)", source_id, count)
    except Exception as exc:  # noqa: BLE001 — recorded on the source, then re-raised
        await repo.set_source_status(source_id, "error", str(exc))
        raise


# ──────────────────────────────────────────────────────────────  maintenance ──


async def refresh_stats(payload: dict[str, Any]) -> None:
    """BM25's N and avgdl. Cheap; scheduled rather than done inline so a burst of
    ingests does not recompute it once per document."""
    await repo.refresh_corpus_stats(UUID(payload["tenant_id"]))


HANDLERS: dict[str, Handler] = {
    "ingest_document": ingest_document,
    "reindex_document": reindex_document,
    "reindex_tenant": reindex_tenant,
    "sync_source": sync_source,
    "refresh_stats": refresh_stats,
}
