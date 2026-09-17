"""Documents: upload, list, delete, reindex.

Upload writes three rows in one transaction — the document, its blob, and the
job — and returns immediately. Nothing is parsed or embedded in the request:
that work belongs to a worker, so a 400-page PDF cannot hold a connection open
or die with the process that accepted it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from atlas import db, repo
from atlas.deps import CurrentPrincipal
from atlas.ingest import kind_from_path
from atlas.jobs import queue

router = APIRouter(prefix="/documents", tags=["documents"])

#: Anything larger is a data-transfer problem, not a retrieval problem.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _parse_groups(raw: str | None) -> list[str]:
    if not raw:
        return []
    if raw.startswith("["):
        try:
            return [str(g).strip() for g in json.loads(raw) if str(g).strip()]
        except ValueError:
            return []
    return [g.strip() for g in raw.split(",") if g.strip()]


@router.get("")
async def list_documents(
    principal: CurrentPrincipal,
    source_id: UUID | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    documents = await repo.list_documents(
        principal.tenant_id, source_id=source_id, limit=min(limit, 1000)
    )
    return {
        "documents": [
            {
                **d,
                "id": str(d["id"]),
                "source_id": str(d["source_id"]),
                "tenant_id": str(d["tenant_id"]),
            }
            for d in documents
        ],
        "counts": await repo.corpus_counts(principal.tenant_id),
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    principal: CurrentPrincipal,
    file: UploadFile = File(...),
    source_id: UUID | None = Form(default=None),
    acl_groups: str | None = Form(default=None),
    acl_public: bool = Form(default=False),
) -> dict[str, Any]:
    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"{file.filename} is {len(data) // 1024 // 1024} MB; the limit is "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB.",
        )

    filename = file.filename or "untitled"
    kind = kind_from_path(filename)
    if kind is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Atlas cannot read {filename!r}. Supported: PDF, DOCX, Markdown, "
            f"HTML, plain text, and source code.",
        )

    source = await _upload_source(principal.tenant_id, source_id)
    groups = _parse_groups(acl_groups) or list(source["default_acl_groups"])

    # One transaction: a document row with no blob and no job would sit at
    # `queued` forever with nothing to move it.
    async with db.transaction() as conn:
        document = await repo.upsert_document(
            tenant_id=principal.tenant_id,
            source_id=source["id"],
            external_id=filename,
            title=filename,
            kind=kind,
            mime=file.content_type,
            size_bytes=len(data),
            acl_groups=groups,
            acl_public=acl_public or source["default_acl_public"],
            metadata={"uploaded_by": principal.email},
        )
        await conn.execute(
            """
            insert into document_blobs (document_id, bytes, mime)
            values ($1, $2, $3)
            on conflict (document_id) do update
              set bytes = excluded.bytes, mime = excluded.mime
            """,
            document["id"],
            data,
            file.content_type,
        )
        job_id = await queue.enqueue(
            kind="ingest_document",
            tenant_id=principal.tenant_id,
            payload={
                "document_id": str(document["id"]),
                "tenant_id": str(principal.tenant_id),
                "path": filename,
            },
            # Above connector syncs: someone is watching this one.
            priority=50,
            dedupe_key=f"ingest:{document['id']}",
            conn=conn,
        )

    return {
        "document_id": str(document["id"]),
        "title": document["title"],
        "kind": kind,
        "size_bytes": len(data),
        "job_id": job_id,
        "stage": "queued",
    }


@router.get("/{document_id}")
async def get_document(document_id: UUID, principal: CurrentPrincipal) -> dict[str, Any]:
    document = await repo.get_document(principal.tenant_id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document.")
    chunks = await repo.chunks_for_document(document_id)
    return {
        "document": {**document, "id": str(document["id"])},
        "chunks": [{**c, "id": str(c["id"])} for c in chunks],
    }


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: UUID, principal: CurrentPrincipal) -> None:
    if not await repo.delete_document(principal.tenant_id, document_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document.")
    # Chunks and the blob cascade; the corpus statistics do not.
    await repo.refresh_corpus_stats(principal.tenant_id)


@router.post("/{document_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_document(
    document_id: UUID, principal: CurrentPrincipal
) -> dict[str, Any]:
    document = await repo.get_document(principal.tenant_id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document.")

    has_blob = await db.fetchval(
        "select exists(select 1 from document_blobs where document_id = $1)",
        document_id,
    )
    if not has_blob:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The uploaded file was dropped after this document was indexed, so "
            "there is nothing to re-read. Upload it again, or re-sync its source.",
        )

    job_id = await queue.enqueue(
        kind="reindex_document",
        tenant_id=principal.tenant_id,
        payload={"document_id": str(document_id), "tenant_id": str(principal.tenant_id)},
        priority=100,
        dedupe_key=f"reindex:{document_id}",
    )
    return {"queued": True, "job_id": job_id}


async def _upload_source(tenant_id: UUID, source_id: UUID | None) -> dict[str, Any]:
    """Finds or creates the tenant's default upload source.

    Uploads still belong to a source, so ACL defaults, listing and deletion work
    the same way for a dropped file as for a synced repository.
    """
    if source_id is not None:
        source = await repo.get_source(tenant_id, source_id)
        if source is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such source.")
        return source

    for existing in await repo.list_sources(tenant_id):
        if existing["kind"] == "upload":
            return existing

    return await repo.create_source(
        tenant_id=tenant_id, kind="upload", name="Uploads", acl_public=True
    )
