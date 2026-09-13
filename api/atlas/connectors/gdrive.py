"""Google Drive connector.

Drive holds two different things and they need different handling. Native Google
files (Docs, Sheets, Slides) have no bytes to download — they are *exported*,
which is how a Doc becomes text and a Sheet becomes CSV. Everything else (PDFs,
Word files, Markdown) is a real file, downloaded as bytes and handed to the same
parsers an upload would use.

Incrementality is `modifiedTime > cursor`: simple, and it needs no token
bookkeeping. The trade is that deletions are invisible — a file moved to trash
stops being updated but its chunks survive until the source is re-synced from
scratch. The Changes API would fix that at the cost of a per-drive page token
that expires; this is the honest trade for now, not an oversight.

Auth is an OAuth refresh token, exchanged for an access token on each sync. A
short-lived `access_token` in config also works and is convenient for testing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import httpx

from atlas import repo
from atlas.config import settings
from atlas.connectors import enqueue_document
from atlas.ingest import kind_from_path

log = logging.getLogger("atlas.connectors.gdrive")

API = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"

FIELDS = (
    "nextPageToken, files(id,name,mimeType,modifiedTime,webViewLink,size,"
    "trashed,owners(emailAddress))"
)

#: Drive's export endpoint refuses anything over 10 MB, and a document that
#: large is nearly always a scan or a data dump rather than prose.
MAX_EXPORT_BYTES = 10 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

MAX_PAGES = 50
PACE_SECONDS = 0.1

#: Native Google types and what each is worth converting to. Sheets export as
#: CSV because a table flattened to prose loses the row/column relationship that
#: makes it answerable.
_EXPORTS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("text/plain", "txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "txt"),
    "application/vnd.google-apps.presentation": ("text/plain", "txt"),
}

#: Binary types worth pulling down whole. Anything not listed here, and not a
#: native Google type, is skipped — images and video have no text layer.
_DOWNLOADS: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "txt",
    "text/html": "html",
    "application/json": "txt",
}


class DriveError(RuntimeError):
    pass


class DriveConnector:
    kind = "gdrive"

    async def sync(self, *, tenant_id: UUID, source: dict[str, Any]) -> int:
        config = source["config"]
        access_token = await self._access_token(config)

        since: str | None = (source["cursor"] or {}).get("modified_after")
        newest = since
        seen = 0

        async with httpx.AsyncClient(
            base_url=API,
            headers={"authorization": f"Bearer {access_token}"},
            timeout=90,
        ) as http:
            for file in await self._list(http, config, since):
                modified: str = file.get("modifiedTime") or ""
                if newest is None or modified > newest:
                    newest = modified

                try:
                    payload = await self._content(http, file)
                except (DriveError, httpx.HTTPError) as exc:
                    # One locked or oversized file should not fail the drive.
                    log.warning("skipping %s: %s", file.get("name"), exc)
                    continue
                if payload is None:
                    continue

                doc_kind, text, data, mime = payload
                await enqueue_document(
                    tenant_id=tenant_id,
                    source=source,
                    external_id=file["id"],
                    title=file.get("name") or file["id"],
                    kind=doc_kind,
                    text=text,
                    data=data,
                    mime=mime,
                    uri=file.get("webViewLink"),
                    metadata={
                        "modified_time": modified,
                        "mime_type": file.get("mimeType"),
                        "owner": ((file.get("owners") or [{}])[0]).get("emailAddress"),
                    },
                )
                seen += 1

        if newest:
            await repo.set_source_cursor(source["id"], {"modified_after": newest})
        return seen

    # ── auth ─────────────────────────────────────────────────────────────

    async def _access_token(self, config: dict[str, Any]) -> str:
        direct = config.get("access_token")
        if direct:
            # Expires in an hour. Fine for a one-off sync, useless on a
            # schedule — which is why refresh_token is the documented path.
            return str(direct)

        refresh_token = config.get("refresh_token")
        client_id = config.get("client_id") or settings().google_client_id
        client_secret = config.get("client_secret") or settings().google_client_secret
        if not (refresh_token and client_id and client_secret):
            raise ValueError(
                "Drive needs an OAuth refresh token. Put refresh_token in "
                "source.config, and either client_id/client_secret there too or "
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env.local. The "
                "scope required is drive.readonly."
            )

        async with httpx.AsyncClient(timeout=30) as http:
            res = await http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        if res.status_code >= 400:
            raise DriveError(
                f"Google rejected the refresh token: {res.text[:200]}. "
                "A revoked grant or a mismatched client is the usual cause."
            )
        token = res.json().get("access_token")
        if not token:
            raise DriveError("Google returned no access_token.")
        return str(token)

    # ── enumeration ──────────────────────────────────────────────────────

    async def _list(
        self, http: httpx.AsyncClient, config: dict[str, Any], since: str | None
    ) -> list[dict[str, Any]]:
        clauses = [
            "trashed = false",
            "mimeType != 'application/vnd.google-apps.folder'",
        ]
        if since:
            clauses.append(f"modifiedTime > '{since}'")
        folder = config.get("folder_id")
        if folder:
            clauses.append(f"'{folder}' in parents")

        params: dict[str, Any] = {
            "q": " and ".join(clauses),
            "fields": FIELDS,
            "pageSize": 200,
            "orderBy": "modifiedTime desc",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        drive_id = config.get("drive_id")
        if drive_id:
            params["corpora"] = "drive"
            params["driveId"] = drive_id

        files: list[dict[str, Any]] = []
        page: str | None = None
        for _ in range(MAX_PAGES):
            res = await http.get(
                "/files", params={**params, **({"pageToken": page} if page else {})}
            )
            if res.status_code >= 400:
                raise DriveError(f"drive files.list failed: {res.text[:200]}")
            body = res.json()
            files.extend(body.get("files") or [])
            page = body.get("nextPageToken")
            await asyncio.sleep(PACE_SECONDS)
            if not page:
                break
        return files

    # ── content ──────────────────────────────────────────────────────────

    async def _content(
        self, http: httpx.AsyncClient, file: dict[str, Any]
    ) -> tuple[str, str | None, bytes | None, str | None] | None:
        """Returns (document kind, text, bytes, mime), or None to skip."""
        mime: str = file.get("mimeType") or ""
        size = int(file.get("size") or 0)

        if mime in _EXPORTS:
            export_mime, doc_kind = _EXPORTS[mime]
            res = await http.get(
                f"/files/{file['id']}/export",
                params={"mimeType": export_mime, "supportsAllDrives": "true"},
            )
            if res.status_code == 403 and b"exportSizeLimitExceeded" in res.content:
                raise DriveError("larger than Drive's 10 MB export limit")
            if res.status_code >= 400:
                raise DriveError(f"export failed: {res.text[:160]}")
            if len(res.content) > MAX_EXPORT_BYTES:
                raise DriveError("export exceeded the size ceiling")
            await asyncio.sleep(PACE_SECONDS)
            return doc_kind, res.text, None, None

        # `code` is a real Drive case — a .py or .sql file in a shared folder —
        # and the symbol chunker handles it correctly.
        doc_kind = _DOWNLOADS.get(mime) or kind_from_path(file.get("name") or "")
        if doc_kind is None:
            return None
        if size and size > MAX_DOWNLOAD_BYTES:
            raise DriveError(f"{size // 1024 // 1024} MB is over the download ceiling")

        res = await http.get(
            f"/files/{file['id']}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        if res.status_code >= 400:
            raise DriveError(f"download failed: {res.text[:160]}")
        await asyncio.sleep(PACE_SECONDS)

        # Text kinds ride on the job payload; binaries go through the blob table
        # so the queue row stays small.
        if doc_kind in {"txt", "md", "html", "code"}:
            return doc_kind, res.text, None, None
        return doc_kind, None, res.content, mime or None
