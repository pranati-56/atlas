"""Connectors: one interface per upstream, one registry.

A connector's whole job is to enumerate what exists upstream, decide what has
changed, and enqueue an ingest for each changed document. It never embeds and
never writes chunks — that is the pipeline's job, reached through the queue, so
that a slow upstream and a slow embedder cannot block each other.

`sync` is expected to be re-runnable at any time. Every connector keys its
documents on (source_id, external_id) and lets `reindex_scope` decide whether a
document is actually reprocessed, so a full re-sync of an unchanged repository
costs a listing and nothing else.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol
from uuid import UUID

import httpx

from atlas import repo
from atlas.ingest import kind_from_path
from atlas.jobs import queue

log = logging.getLogger("atlas.connectors")


class Connector(Protocol):
    kind: str

    async def sync(self, *, tenant_id: UUID, source: dict[str, Any]) -> int:
        """Enumerates the upstream and enqueues ingests. Returns documents seen."""
        ...


async def enqueue_document(
    *,
    tenant_id: UUID,
    source: dict[str, Any],
    external_id: str,
    title: str,
    kind: str,
    text: str | None = None,
    data: bytes | None = None,
    mime: str | None = None,
    uri: str | None = None,
    metadata: dict[str, Any] | None = None,
    priority: int = 200,
) -> None:
    """Upserts the document row and schedules its ingest.

    Text rides on the job payload; bytes do not — a 20 MB PDF inline in a job
    row would be read back on every queue poll. Binary documents go to
    `document_blobs` instead, which the ingest handler already reads and then
    deletes once the chunks exist.

    The dedupe key collapses duplicate work: re-syncing while an ingest for the
    same document is still queued must not enqueue a second one.
    """
    document = await repo.upsert_document(
        tenant_id=tenant_id,
        source_id=source["id"],
        external_id=external_id,
        title=title,
        kind=kind,
        uri=uri,
        acl_groups=list(source["default_acl_groups"]),
        acl_public=source["default_acl_public"],
        metadata=metadata or {},
    )
    if data is not None:
        await repo.put_blob(document["id"], data, mime or "application/octet-stream")
    await queue.enqueue(
        kind="ingest_document",
        tenant_id=tenant_id,
        payload={
            "document_id": str(document["id"]),
            "tenant_id": str(tenant_id),
            "path": external_id,
            **({"text": text} if text is not None else {}),
        },
        priority=priority,
        dedupe_key=f"ingest:{document['id']}",
    )


# ────────────────────────────────────────────────────────────────────  upload ──


class UploadConnector:
    """Uploads are pushed, not pulled.

    The route writes the document row and the blob and enqueues the job
    directly, so there is nothing to enumerate. It exists as a registry entry so
    that "sync this source" is a uniform operation rather than a special case
    that has to be hidden for one kind.
    """

    kind = "upload"

    async def sync(self, *, tenant_id: UUID, source: dict[str, Any]) -> int:
        rows = await repo.list_documents(tenant_id, source_id=source["id"], limit=10_000)
        return len(rows)


# ───────────────────────────────────────────────────────────────────────  web ──

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class WebConnector:
    """Fetches a fixed list of URLs.

    Deliberately not a crawler: an unbounded crawl is a different problem —
    politeness, robots.txt, loop detection, budget — and mixing it in here would
    make every sync unpredictable. `config.urls` is the contract.
    """

    kind = "web"

    async def sync(self, *, tenant_id: UUID, source: dict[str, Any]) -> int:
        urls: list[str] = list(source["config"].get("urls") or [])
        if not urls:
            log.info("web source %s has no urls configured", source["id"])
            return 0

        seen = 0
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"user-agent": "Atlas/0.1"}
        ) as client:
            for url in urls:
                try:
                    res = await client.get(url)
                    res.raise_for_status()
                except httpx.HTTPError as exc:
                    # One bad URL should not fail the whole source.
                    log.warning("web fetch failed for %s: %s", url, exc)
                    continue

                content_type = res.headers.get("content-type", "")
                doc_kind = "html" if "html" in content_type else "txt"
                match = _TITLE.search(res.text)
                title = " ".join(match.group(1).split()) if match else url

                await enqueue_document(
                    tenant_id=tenant_id,
                    source=source,
                    external_id=url,
                    title=title,
                    kind=doc_kind,
                    text=res.text,
                    uri=url,
                    metadata={"content_type": content_type},
                )
                seen += 1
        return seen


# ──────────────────────────────────────────────────────────────────  registry ──


def _registry() -> dict[str, Connector]:
    # Imported lazily so a connector with a heavy or optional dependency cannot
    # break the worker's startup for sources nobody is using.
    from atlas.connectors.gdrive import DriveConnector
    from atlas.connectors.github import GitHubConnector
    from atlas.connectors.jira import JiraConnector
    from atlas.connectors.notion import NotionConnector
    from atlas.connectors.slack import SlackConnector

    return {
        "upload": UploadConnector(),
        "web": WebConnector(),
        "github": GitHubConnector(),
        "slack": SlackConnector(),
        "notion": NotionConnector(),
        "gdrive": DriveConnector(),
        "jira": JiraConnector(),
    }


def get_connector(kind: str) -> Connector | None:
    return _registry().get(kind)


def available_kinds() -> list[str]:
    return sorted(_registry())


__all__ = [
    "Connector",
    "UploadConnector",
    "WebConnector",
    "available_kinds",
    "enqueue_document",
    "get_connector",
    "kind_from_path",
]
