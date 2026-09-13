"""Notion connector.

One Notion page becomes one document. Blocks are flattened back into Markdown
rather than into plain text, because the prose chunker splits on headings and a
page stripped of its heading structure chunks into meaningless slabs.

Incrementality rides on `last_edited_time`. Search returns pages newest-edited
first, so the walk stops at the first page older than the stored cursor instead
of paging through a whole workspace to find three changes.

Two limits worth knowing. Deletions and un-shares are not detected — a page
removed from the integration stops being updated, but its existing chunks remain
until the source is re-synced from scratch. And the integration only ever sees
pages explicitly shared with it, which is a feature: Notion's own sharing model
is the first permission filter, before Atlas applies its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx

from atlas import repo
from atlas.config import settings
from atlas.connectors import enqueue_document

log = logging.getLogger("atlas.connectors.notion")

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

#: Notion allows roughly three requests a second, averaged. One page costs at
#: least one block call, so pacing here is what keeps a large workspace legal.
PACE_SECONDS = 0.34

MAX_SEARCH_PAGES = 60
#: Depth at which nested toggles stop being followed. Deeper than this and the
#: content is almost always navigational rather than substantive.
MAX_DEPTH = 4
#: Ceiling on blocks pulled for one page, so a single pathological page cannot
#: consume an entire sync.
MAX_BLOCKS = 600

_HEADINGS = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### "}


class NotionError(RuntimeError):
    pass


class NotionConnector:
    kind = "notion"

    async def sync(self, *, tenant_id: UUID, source: dict[str, Any]) -> int:
        config = source["config"]
        token: str | None = config.get("token") or settings().notion_token
        if not token:
            raise ValueError(
                "No Notion token: set NOTION_TOKEN or put one in "
                "source.config.token. Create an internal integration, then share "
                "the pages or teamspaces you want indexed with it."
            )

        since: str | None = (source["cursor"] or {}).get("last_edited")
        newest = since
        seen = 0

        async with httpx.AsyncClient(
            base_url=API,
            headers={
                "authorization": f"Bearer {token}",
                "notion-version": NOTION_VERSION,
                "content-type": "application/json",
            },
            timeout=45,
        ) as http:
            async for page in self._pages(http, since):
                edited: str = page.get("last_edited_time") or ""
                if newest is None or edited > newest:
                    newest = edited

                title = _title(page)
                try:
                    body = await self._markdown(http, page["id"])
                except (NotionError, httpx.HTTPError) as exc:
                    # One unreadable page should not abandon the workspace.
                    log.warning("could not read blocks for %s: %s", title, exc)
                    continue

                if not body.strip():
                    continue

                await enqueue_document(
                    tenant_id=tenant_id,
                    source=source,
                    external_id=page["id"],
                    title=title,
                    kind="md",
                    text=f"# {title}\n\n{body}",
                    uri=page.get("url"),
                    metadata={
                        "last_edited_time": edited,
                        "created_time": page.get("created_time"),
                        "parent": (page.get("parent") or {}).get("type"),
                    },
                )
                seen += 1

        if newest:
            await repo.set_source_cursor(source["id"], {"last_edited": newest})
        return seen

    # ── enumeration ──────────────────────────────────────────────────────

    async def _request(
        self, http: httpx.AsyncClient, method: str, path: str, **kw: Any
    ) -> dict[str, Any]:
        for attempt in range(4):
            res = await http.request(method, path, **kw)
            if res.status_code == 429:
                delay = float(res.headers.get("retry-after") or 2)
                log.info("notion rate limited on %s — waiting %.0fs", path, delay)
                await asyncio.sleep(delay)
                continue
            if res.status_code >= 500:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if res.status_code >= 400:
                try:
                    detail = res.json().get("message", res.text[:200])
                except ValueError:
                    detail = res.text[:200]
                raise NotionError(f"notion {path} failed: {detail}")
            await asyncio.sleep(PACE_SECONDS)
            return res.json()
        raise NotionError(f"notion {path} did not settle after retries")

    async def _pages(
        self, http: httpx.AsyncClient, since: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yields shared pages, newest edit first, stopping at the cursor."""
        cursor: str | None = None
        for _ in range(MAX_SEARCH_PAGES):
            body: dict[str, Any] = {
                "filter": {"value": "page", "property": "object"},
                "sort": {"timestamp": "last_edited_time", "direction": "descending"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor

            result = await self._request(http, "POST", "/search", json=body)
            for page in result.get("results") or []:
                if page.get("archived") or page.get("in_trash"):
                    continue
                # Sorted descending, so the first page at or before the cursor
                # means everything after it is older too.
                if since and (page.get("last_edited_time") or "") <= since:
                    return
                yield page

            if not result.get("has_more"):
                return
            cursor = result.get("next_cursor")
            if not cursor:
                return

    # ── block → markdown ─────────────────────────────────────────────────

    async def _markdown(self, http: httpx.AsyncClient, block_id: str) -> str:
        lines: list[str] = []
        budget = [MAX_BLOCKS]
        await self._walk(http, block_id, lines, depth=0, budget=budget)
        return "\n".join(lines)

    async def _walk(
        self,
        http: httpx.AsyncClient,
        block_id: str,
        lines: list[str],
        *,
        depth: int,
        budget: list[int],
    ) -> None:
        cursor: str | None = None
        while budget[0] > 0:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            result = await self._request(
                http, "GET", f"/blocks/{block_id}/children", params=params
            )

            for block in result.get("results") or []:
                budget[0] -= 1
                if budget[0] <= 0:
                    lines.append("\n_(page truncated)_")
                    return

                rendered = _render(block, depth)
                if rendered is not None:
                    lines.append(rendered)

                # child_page and child_database become their own documents via
                # search; following them here would duplicate their content.
                nested = block.get("type") in {"child_page", "child_database"}
                if block.get("has_children") and not nested and depth < MAX_DEPTH:
                    await self._walk(
                        http, block["id"], lines, depth=depth + 1, budget=budget
                    )

            if not result.get("has_more"):
                return
            cursor = result.get("next_cursor")
            if not cursor:
                return


# ────────────────────────────────────────────────────────────────  helpers ──


def _rich(parts: list[dict[str, Any]] | None) -> str:
    """Rich text → Markdown, keeping only annotations that survive chunking."""
    out: list[str] = []
    for part in parts or []:
        text = part.get("plain_text") or ""
        if not text:
            continue
        ann = part.get("annotations") or {}
        if ann.get("code"):
            text = f"`{text}`"
        else:
            if ann.get("bold"):
                text = f"**{text}**"
            if ann.get("italic"):
                text = f"*{text}*"
        href = part.get("href")
        if href:
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def _render(block: dict[str, Any], depth: int) -> str | None:
    """One block → one Markdown line, or None when it carries no text."""
    kind = block.get("type") or ""
    data = block.get(kind) or {}
    indent = "  " * depth
    text = _rich(data.get("rich_text"))

    if kind in _HEADINGS:
        # Headings never indent: the prose chunker splits on them, and an
        # indented heading would read as body text.
        return f"\n{_HEADINGS[kind]}{text}" if text else None

    if kind == "paragraph":
        return f"{indent}{text}" if text else ""

    if kind in {"bulleted_list_item", "toggle"}:
        return f"{indent}- {text}" if text else None

    if kind == "numbered_list_item":
        return f"{indent}1. {text}" if text else None

    if kind == "to_do":
        box = "x" if data.get("checked") else " "
        return f"{indent}- [{box}] {text}" if text else None

    if kind in {"quote", "callout"}:
        icon = ((data.get("icon") or {}).get("emoji") or "").strip()
        prefix = f"{icon} " if icon else ""
        return f"{indent}> {prefix}{text}" if text else None

    if kind == "code":
        lang = data.get("language") or ""
        return f"\n```{lang}\n{text}\n```" if text else None

    if kind == "divider":
        return "\n---"

    if kind == "table_row":
        cells = [_rich(c) for c in data.get("cells") or []]
        return f"{indent}| " + " | ".join(cells) + " |" if cells else None

    if kind in {"image", "file", "video", "pdf", "embed", "bookmark"}:
        caption = _rich(data.get("caption"))
        url = data.get("url") or (data.get("external") or {}).get("url") or ""
        return f"{indent}[{caption or kind}]({url})" if url else None

    if kind == "equation":
        expr = data.get("expression") or ""
        return f"{indent}$$ {expr} $$" if expr else None

    if kind == "child_page":
        # A pointer, not the content — the child is its own document.
        return f"{indent}- {data.get('title') or 'Untitled'}"

    return f"{indent}{text}" if text else None


def _title(page: dict[str, Any]) -> str:
    """Notion has no title field — it is whichever property has type `title`."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            text = _plain(prop.get("title"))
            if text:
                return text
    return "Untitled"


def _plain(parts: list[dict[str, Any]] | None) -> str:
    return " ".join("".join(p.get("plain_text") or "" for p in parts or []).split())
