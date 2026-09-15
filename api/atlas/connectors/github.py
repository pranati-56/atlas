"""GitHub connector.

Walks a repository's tree at a ref and enqueues an ingest per indexable file.
Two things make this cheap enough to run often:

  - The tree is fetched recursively in **one** call, not one call per directory.
  - The commit sha is stored on the source. On the next sync, if the sha has not
    moved, the whole repository is skipped without listing anything.

When the sha *has* moved, the compare endpoint gives the changed paths directly,
so a one-file commit costs one comparison rather than a full tree walk. The
content hash in the ingest pipeline is the backstop for anything that slips
through either check.
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from uuid import UUID

import httpx

from atlas import repo
from atlas.config import settings
from atlas.connectors import enqueue_document
from atlas.ingest import kind_from_path

log = logging.getLogger("atlas.connectors.github")

API = "https://api.github.com"

#: Files above this are almost never worth indexing and are usually generated —
#: lockfiles, bundled vendor code, checked-in minified assets.
MAX_FILE_BYTES = 400_000

_SKIP_DIRS = {
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    ".git",
    ".next",
    "__pycache__",
    ".venv",
    "site-packages",
}

_SKIP_SUFFIXES = (".min.js", ".min.css", ".map", ".lock", "-lock.json", ".snap")


def _indexable(path: str, size: int) -> bool:
    if size > MAX_FILE_BYTES:
        return False
    if any(part in _SKIP_DIRS for part in path.split("/")):
        return False
    if path.endswith(_SKIP_SUFFIXES):
        return False
    # Binary formats have no text layer worth chunking; the parser would only
    # produce mojibake.
    return kind_from_path(path) is not None


class GitHubConnector:
    kind = "github"

    async def sync(self, *, tenant_id: UUID, source: dict[str, Any]) -> int:
        config = source["config"]
        repository: str = config.get("repository", "")
        if "/" not in repository:
            raise ValueError(
                f'source.config.repository must be "owner/name", got {repository!r}'
            )

        branch: str = config.get("branch") or "HEAD"
        token: str | None = config.get("token") or settings().github_token
        if not token:
            raise ValueError(
                "No GitHub token: set GITHUB_TOKEN or put one in source.config.token. "
                "A fine-grained PAT with Contents:read is enough."
            )

        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
            "user-agent": "Atlas/0.1",
        }
        previous_sha: str | None = (source["cursor"] or {}).get("commit_sha")
        targets: list[dict[str, Any]] = []

        async with httpx.AsyncClient(base_url=API, headers=headers, timeout=60) as http:
            head_sha = await self._head_sha(http, repository, branch)

            if previous_sha == head_sha:
                log.info(
                    "%s is unchanged at %s — nothing to sync", repository, head_sha[:8]
                )
                return 0

            changed: set[str] | None = None
            if previous_sha:
                changed = await self._changed_paths(
                    http, repository, previous_sha, head_sha
                )

            tree = await self._tree(http, repository, head_sha)
            targets = [
                node
                for node in tree
                if node["type"] == "blob"
                and _indexable(node["path"], int(node.get("size") or 0))
                and (changed is None or node["path"] in changed)
            ]

            log.info(
                "%s @ %s: %d indexable file(s)%s",
                repository,
                head_sha[:8],
                len(targets),
                " (changed only)" if changed is not None else "",
            )

            for node in targets:
                path = node["path"]
                try:
                    text = await self._blob_text(http, repository, node["sha"])
                except httpx.HTTPError as exc:
                    # One unreadable file should not fail the whole repository.
                    log.warning("could not read %s:%s — %s", repository, path, exc)
                    continue
                if text is None:
                    continue  # binary despite the extension

                await enqueue_document(
                    tenant_id=tenant_id,
                    source=source,
                    external_id=path,
                    title=path,
                    kind=kind_from_path(path) or "txt",
                    text=text,
                    uri=f"https://github.com/{repository}/blob/{head_sha}/{path}",
                    metadata={
                        "repository": repository,
                        "path": path,
                        "branch": branch,
                        "commit_sha": head_sha,
                    },
                )

        # Written only after a clean pass: a sync that dies half way must repeat
        # the same range rather than skipping what it never enqueued.
        await repo.set_source_cursor(source["id"], {"commit_sha": head_sha})
        return len(targets)

    async def _head_sha(self, http: httpx.AsyncClient, repository: str, ref: str) -> str:
        res = await http.get(f"/repos/{repository}/commits/{ref}")
        res.raise_for_status()
        return str(res.json()["sha"])

    async def _changed_paths(
        self, http: httpx.AsyncClient, repository: str, base: str, head: str
    ) -> set[str] | None:
        """Paths touched between two commits, or None if the range is unusable.

        A force-push can leave the stored sha unreachable; GitHub answers 404 and
        the correct response is a full tree walk, not a failed sync.
        """
        res = await http.get(f"/repos/{repository}/compare/{base}...{head}")
        if res.status_code == 404:
            log.info(
                "%s: %s is unreachable, falling back to a full walk",
                repository,
                base[:8],
            )
            return None
        res.raise_for_status()
        body = res.json()
        # The compare endpoint truncates at 300 files; past that a full walk is
        # both simpler and cheaper than paginating.
        if body.get("files") is None or len(body["files"]) >= 300:
            return None
        return {f["filename"] for f in body["files"]}

    async def _tree(
        self, http: httpx.AsyncClient, repository: str, sha: str
    ) -> list[dict[str, Any]]:
        res = await http.get(
            f"/repos/{repository}/git/trees/{sha}", params={"recursive": "1"}
        )
        res.raise_for_status()
        body = res.json()
        if body.get("truncated"):
            log.warning(
                "%s: tree listing was truncated by GitHub; some files will be missed",
                repository,
            )
        return list(body.get("tree") or [])

    async def _blob_text(
        self, http: httpx.AsyncClient, repository: str, sha: str
    ) -> str | None:
        res = await http.get(f"/repos/{repository}/git/blobs/{sha}")
        res.raise_for_status()
        body = res.json()
        if body.get("encoding") != "base64":
            return None
        raw = base64.b64decode(body["content"])
        if b"\x00" in raw[:8000]:
            return None  # binary despite the extension
        return raw.decode("utf-8", errors="replace")
